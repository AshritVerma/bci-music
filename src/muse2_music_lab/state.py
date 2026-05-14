"""Shared mutable state for the asyncio orchestrator.

All `perform` async tasks read and write this single `AppState` instance.
Reads/writes don't take a lock: asyncio guarantees only one coroutine runs
at any moment, so plain field assignment is atomic from a coroutine's
perspective. Anything that needs strict consistency across multiple reads
should snapshot at the top of the consumer.

Layout follows PROJECT_PLAN.md section 3.5:

    Session config
        prompt          - the active prompt driving Lyria. Initially set
                          by the browser Start panel; can change mid-
                          session via the change_prompt action (the
                          Lyria crossfade engine atomically replaces it
                          when a transition completes).

    EEG features (normalized to [0, 1], written by brainflow_loop)
        alpha           - eyes-closed relaxation
        beta            - active focus / arousal
        theta           - drowsy / meditative
        asymmetry       - AF8-AF7 alpha asymmetry, idle ~0.5

    Audio features (normalized to [0, 1], written by audio_analysis)
        rms             - perceived loudness
        centroid        - spectral brightness
        onset           - kick/percussive event strength

    EEG triggers (discrete, written by brainflow_loop)
        blink_triggered     - True for one tick after a blink fires.
        jaw_triggered       - True for one tick after a jaw clench fires.
        blink_ptp_uv        - Diagnostic: most recent peak-to-peak μV
                              on frontal channels (drives the TUI meter).
        jaw_rms_uv          - Diagnostic: most recent peak |HP| in μV
                              averaged across all 4 channels.

    Lyria mirror (written by lyria/session.py)
        lyria_bpm           - Last value pushed to Lyria's bpm.
        lyria_density       - Last density (0..1).
        lyria_brightness    - Last brightness (0..1).
        lyria_temperature   - Last temperature (~0.6..1.8).
        lyria_chunks        - Cumulative count of audio chunks received.

    Synchronization
        eeg_tick            - set by brainflow_loop after each fresh feature
                              window. Lyria control task awaits and clears
                              it so we get exactly one Lyria push per fresh
                              EEG sample (coalescing falls out for free).
        recalibrate_request - set by the keyboard listener (or any future
                              UI surface) to ask the EEG loop to re-run
                              its baseline calibration without restarting
                              the process. The EEG loop clears it once
                              the new normalizer has been swapped in.
                              Ignored entirely on the simulated-EEG path
                              (no calibration there).
        eeg_ready           - set once the EEG path is producing meaningful
                              normalized values (post-calibration on the
                              real path, immediately on the simulated path).
                              The TUI waits on this before activating.
        lyria_ready         - set once Lyria has emitted its first audio
                              chunk. The TUI waits on this too (when Lyria
                              is enabled) so the panel doesn't show garbage
                              "0.00" Lyria values before generation starts.
        tui_active          - set by perform_tui while a rich.Live panel
                              owns the terminal. Other tasks consult this
                              to suppress their own periodic-summary prints
                              that would otherwise stomp on the panel.

        audio_ready         - set once the audio analysis loop has produced
                              its first non-zero frame of features. The
                              perform TUI uses it to swap the audio
                              section's "warming" placeholder for the
                              live bars.

    Audio plumbing
        audio_queue           - Lyria PCM bytes -> sounddevice playback.
                                Bounded; producer blocks if full so we
                                don't outpace the speaker.
        audio_analysis_queue  - Lyria PCM bytes -> FFT analysis. Bounded
                                AND lossy: the lyria producer drops the
                                oldest chunk on overflow rather than
                                blocking. Analysis is allowed to skip
                                frames; playback never is.

    Session metadata
        session_start_ts    - monotonic time at orchestrator startup; used
                              by the TUI footer to show elapsed wall time.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from muse2_music_lab import config


def _bounded_audio_queue() -> "asyncio.Queue[bytes]":
    """Bounded queue so the producer (Lyria receive loop) blocks instead
    of growing memory unbounded if the consumer (audio playback) stalls.
    Sized in `config.LYRIA_AUDIO_QUEUE_MAX` (~3s of 48kHz stereo s16)."""
    return asyncio.Queue(maxsize=config.LYRIA_AUDIO_QUEUE_MAX)


def _bounded_analysis_queue() -> "asyncio.Queue[bytes]":
    """Small bounded queue for the FFT analysis tee. Separate from the
    playback queue: it's drained by the analysis task and is allowed to
    drop oldest on overflow (analysis is lossy by design; playback is
    not). Sized in `config.AUDIO_ANALYSIS_QUEUE_MAX`."""
    return asyncio.Queue(maxsize=config.AUDIO_ANALYSIS_QUEUE_MAX)


def _bounded_broadcast_queue() -> "asyncio.Queue[bytes]":
    """Bounded queue for the WebSocket-to-browser audio fan-out (cloud mode).

    Lossy-on-overflow: if the broadcaster falls behind (a client on a slow
    connection, the loop having to encode many frames in a row), drop
    OLDEST and push NEW so latency stays bounded. Going gap-less here is
    impossible without buffering on the browser side anyway -- that's the
    visitor-side jitter buffer's job.

    Same sizing as the analysis queue (a few seconds of audio is plenty)."""
    return asyncio.Queue(maxsize=config.AUDIO_ANALYSIS_QUEUE_MAX)


@dataclass
class AppState:
    prompt: str = ""

    # ------- EEG features (0..1) -------
    alpha: float = 0.0
    beta: float = 0.0
    theta: float = 0.0
    asymmetry: float = 0.5

    # ------- EEG triggers (discrete) -------
    blink_triggered: bool = False
    jaw_triggered: bool = False
    blink_ptp_uv: float = 0.0
    jaw_rms_uv: float = 0.0

    # ------- Audio features (0..1) -------
    rms: float = 0.0
    centroid: float = 0.0
    onset: float = 0.0

    # ------- Lyria mirror (last value pushed) -------
    lyria_bpm: int = 0
    lyria_density: float = 0.0
    lyria_brightness: float = 0.0
    lyria_temperature: float = 0.0
    lyria_chunks: int = 0

    # ------- Seed evolver -------
    # `seed_version` increments every time the evolver writes a new
    # seed.png. The browser watches it in incoming WS messages and
    # triggers visualizer.refreshSeed() on a bump (cross-fade to the
    # new texture). `seed_prompt` is the most recent EVOLVED prompt
    # (starts equal to the session prompt, drifts each cycle).
    seed_version: int = 0
    seed_prompt: str = ""

    # ------- EEG mode (browser-toggleable) -------
    # Which EEG source the supervisor is currently driving:
    #   "real"      -- BLE-connected Muse 2 via BrainFlow
    #   "simulated" -- synthetic generator (no headset needed)
    # Initial value is set by main.py from --simulate-eeg. The supervisor
    # is the sole writer; the WS handler writes `eeg_mode_target` and
    # sets `eeg_mode_change_request` instead, then the supervisor reads
    # the target, swaps inner tasks, and updates this field.
    eeg_mode: str = "real"
    # Mode the user (browser) wants to switch TO. Empty when no swap is
    # pending. The supervisor consumes this on each event-fire.
    eeg_mode_target: str = ""
    # Wakes the supervisor when the user clicks the EEG-mode toggle.
    eeg_mode_change_request: asyncio.Event = field(default_factory=asyncio.Event)

    # ------- Browser-driven control surface (Phase 10) -------
    # eeg_connection_state surfaces the EEG link's current state to the
    # browser so it can render a Muse status pill that mirrors the WS
    # one. Values:
    #   "idle"          -- before the EEG task has started
    #   "searching"     -- BLE scan in progress
    #   "found"         -- BLE handshake done, no telemetry yet
    #   "connected"     -- streaming features (this is the green state)
    #   "lost"          -- transient disconnect, awaiting reconnect
    #   "reconnecting"  -- reconnect attempt in flight
    #   "simulated"     -- --simulate-eeg path; never connects to real BLE
    #   "failed"        -- terminal failure (max reconnects exceeded)
    eeg_connection_state: str = "idle"
    # Mirrors whether Lyria has been started this session. The browser
    # uses it to hide the Start panel after the user has clicked Start.
    # NOT the same as lyria_ready (which fires on first audio chunk):
    # this flips True the moment the user requests playback, lyria_ready
    # follows once Lyria has actually produced sound.
    lyria_started: bool = False

    # ------- Mid-session prompt change -------
    # Browser sends an action {action:"change_prompt", prompt:"...", chunks:N}
    # The server validates and writes prompt_change_target +
    # prompt_change_chunks then fires prompt_change_request. The Lyria
    # receive loop snoops the event, replaces its current prompt, and
    # ramps a weighted set_weighted_prompts() crossfade over `chunks`
    # audio chunks (each chunk ~= 2 s of music).
    #
    # `prompt_transition_progress` is the live 0..1 ramp value, exposed
    # in snapshot() so the browser can render a "changing... 37%" badge
    # next to the prompt. Resets to 0.0 at boot AND on transition
    # completion (so the badge disappears after the crossfade ends).
    prompt_change_target: str = ""
    prompt_change_chunks: int = 0
    prompt_transition_progress: float = 0.0
    prompt_change_request: asyncio.Event = field(default_factory=asyncio.Event)

    # ------- Calibration progress (browser banner) -------
    # `calibrating` flips True while the EEG loop is collecting baseline
    # samples (initial connect AND every user-triggered recalibrate). The
    # browser shows a "sit still, eyes open" banner with a live countdown
    # while it's True. Cleared in a finally-block at the end of
    # _calibrate() so a cancelled/exceptional calibration still releases
    # the banner instead of leaving the user stuck on screen.
    #
    # `calibration_started_ts` is monotonic time when the current window
    # started; `calibration_total_s` is the (constant per run) duration
    # the window was scheduled for. snapshot() computes the live
    # `calibration_remaining_s` from these two so the browser doesn't
    # have to share a clock with the server -- it just gets a fresh
    # remaining value 20x/s and ticks it down between snapshots itself.
    calibrating: bool = False
    calibration_started_ts: float = 0.0
    calibration_total_s: float = 0.0

    # ------- Sync + plumbing -------
    eeg_tick: asyncio.Event = field(default_factory=asyncio.Event)
    recalibrate_request: asyncio.Event = field(default_factory=asyncio.Event)
    eeg_ready: asyncio.Event = field(default_factory=asyncio.Event)
    lyria_ready: asyncio.Event = field(default_factory=asyncio.Event)
    audio_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Phase 10: gates Lyria, audio playback, FFT analysis, seed image,
    # and seed evolver. Set by the browser's Start button (or by
    # main.py at boot if --prompt was passed on the CLI). EEG, server,
    # TUI, keys never wait on this -- they're the always-on surface
    # the user sees BEFORE clicking Start.
    start_requested: asyncio.Event = field(default_factory=asyncio.Event)
    tui_active: bool = False
    audio_queue: asyncio.Queue[bytes] = field(default_factory=_bounded_audio_queue)
    audio_analysis_queue: asyncio.Queue[bytes] = field(
        default_factory=_bounded_analysis_queue
    )
    # Cloud-deploy fan-out queue. Populated by the Lyria session as a
    # third tee (alongside audio_queue and audio_analysis_queue). Drained
    # by server.audio_broadcast.run_audio_broadcast_loop, which encodes
    # each chunk and sends it as a binary WS frame to every connected
    # browser. Empty in local-dev runs; only populated when --cloud is on.
    audio_broadcast_queue: asyncio.Queue[bytes] = field(
        default_factory=_bounded_broadcast_queue
    )

    # ------- Cloud / multi-tenant flag -------
    # True when the orchestrator is running in `--cloud` mode (Railway,
    # any other PaaS). Forces simulated EEG, no local audio output, no
    # browser auto-launch, no TUI. The frontend reads this from the WS
    # snapshot to hide single-operator controls (Quit, EEG-mode toggle)
    # that don't make sense for a shared public demo.
    cloud_mode: bool = False

    # ------- Session metadata -------
    session_start_ts: float = field(default_factory=time.monotonic)

    def snapshot(self) -> dict[str, Any]:
        """JSON-friendly view used by the WebSocket broadcaster (Phase 7)."""
        return {
            "prompt": self.prompt,
            "alpha": self.alpha,
            "beta": self.beta,
            "theta": self.theta,
            "asymmetry": self.asymmetry,
            "blink": self.blink_triggered,
            "jaw": self.jaw_triggered,
            "rms": self.rms,
            "centroid": self.centroid,
            "onset": self.onset,
            "lyria": {
                "bpm": self.lyria_bpm,
                "density": self.lyria_density,
                "brightness": self.lyria_brightness,
                "temperature": self.lyria_temperature,
                "chunks": self.lyria_chunks,
            },
            # Browser watches seed_version for changes and re-fetches
            # /static/seed.png + cross-fades when it bumps.
            "seed_version": self.seed_version,
            "seed_prompt": self.seed_prompt,
            # Phase 10 control surface.
            "eeg_connection_state": self.eeg_connection_state,
            "lyria_started": self.lyria_started,
            # True once Lyria has emitted at least one audio chunk this
            # process. Lets the browser distinguish "user clicked Start
            # but no music yet (warming up)" from "music is live". The
            # frontend uses this to show a small Warming-up banner
            # between Start-clicked and first-audio.
            "lyria_ready": self.lyria_ready.is_set(),
            # Live 0..1 progress of an in-flight mid-session prompt
            # change. 0.0 when no transition is happening. Browser
            # renders a "changing prompt... NN%" badge while > 0.0.
            "prompt_transition_progress": self.prompt_transition_progress,
            # The destination prompt while a transition is in flight.
            # Empty when no transition; the browser shows it next to
            # the progress badge so the user can see what's coming.
            "prompt_change_target": self.prompt_change_target,
            # Calibration banner: True while EEG baseline window is
            # collecting samples. `calibration_remaining_s` is computed
            # here (not stored as a field) so every snapshot ships an
            # up-to-date value without _calibrate() needing to write
            # the field on every loop iteration.
            "calibrating": self.calibrating,
            "calibration_remaining_s": (
                max(
                    0.0,
                    self.calibration_total_s
                    - (time.monotonic() - self.calibration_started_ts),
                )
                if self.calibrating and self.calibration_total_s > 0.0
                else 0.0
            ),
            "calibration_total_s": self.calibration_total_s,
            # EEG mode toggle (browser shows current; lets the user swap
            # between real and simulated mid-session).
            "eeg_mode": self.eeg_mode,
            # Cloud-mode flag: the browser uses this to hide controls
            # (Quit, EEG-mode toggle) that don't make sense for a shared
            # public deployment.
            "cloud_mode": self.cloud_mode,
        }
