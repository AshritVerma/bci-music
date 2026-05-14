"""All tunable constants live here. Edit freely during performance tuning.

Nothing in this file does any work on import except looking up BrainFlow's
board-id enum, so it is cheap to import from anywhere.
"""

from __future__ import annotations

from brainflow.board_shim import BoardIds


# ---------------------------------------------------------------------------
# Board / acquisition
# ---------------------------------------------------------------------------

BOARD_ID: int = BoardIds.MUSE_2_BOARD.value

WINDOW_SIZE: int = 256              # samples per feature computation (~1 s at 256 Hz)
SEND_RATE_HZ: float = 30.0          # TUI refresh rate
CALIBRATION_DURATION: float = 8.0   # seconds of baseline on startup

# `perform` pipeline cadence: BrainFlow loop pumps features into AppState
# every PERFORM_TICK_S seconds and sets state.eeg_tick. Each tick is one
# Lyria control push (Phase 5) and one AppState refresh for the WS
# broadcaster (Phase 7). Decoupled from the TUI's SEND_RATE_HZ.
PERFORM_TICK_S: float = 0.25        # 4 Hz
PERFORM_LOG_PERIOD_S: float = 2.0   # how often _state_logger prints in main.py

BOARD_PREPARE_TIMEOUT_S: float = 15.0


# ---------------------------------------------------------------------------
# BLE teardown / reconnect hygiene (Phase 4 hardening)
# ---------------------------------------------------------------------------
#
# Background: the Muse 2 + macOS CoreBluetooth combo leaves stale peripheral
# state behind after a teardown if we don't pace it carefully. The next
# `prepare_session` then has to fight a half-disconnected handle, which
# manifests as the slow-connect-then-drop pattern (Found device -> 5s
# silence -> Peripheral Connection failed -> Connected late -> link rots).
# Power-cycling the headset works around it but is hostile to iteration.
#
# These two sleeps in Board.stop() give the headset and CoreBluetooth time
# to fully settle before the process exits, so the *next* connect starts
# from a clean slate.

# Pause between stop_stream() and release_session(). Lets the headset
# actually process the streaming-halt command before we yank the BLE link.
BOARD_TEARDOWN_HALT_PAUSE_S: float = 0.3

# Pause after release_session() returns, before stop() yields. Lets
# CoreBluetooth's async disconnect notification flush so the next
# `prepare_session` (this process or a freshly spawned one) doesn't get
# handed a stale peripheral handle.
BOARD_TEARDOWN_FLUSH_S: float = 2.0


# Reconnect supervisor in run_real_eeg_loop: when the BLE link drops
# mid-session (ConnectionError from get_window or BrainFlowError on
# re-prepare), tear down the Board and try to bring it back without
# losing the user's calibration baseline. After this many CONSECUTIVE
# failures (a successful tick resets the counter), give up and let the
# orchestrator shut everything down cleanly.
EEG_RECONNECT_MAX_ATTEMPTS: int = 3

# Base backoff between reconnect attempts. The supervisor scales this
# linearly with the failure count so flapping links don't hammer BLE.
EEG_RECONNECT_BACKOFF_S: float = 3.0


# ---------------------------------------------------------------------------
# Lyria RealTime (Phase 5)
# ---------------------------------------------------------------------------
#
# These were validated end-to-end by scripts/lyria_smoke.py before the
# orchestrator wiring landed. Treat them as the canonical numbers; the
# smoke script and the perform task should always agree.

LYRIA_MODEL_ID: str = "models/lyria-realtime-exp"
LYRIA_SAMPLE_RATE: int = 48_000
LYRIA_CHANNELS: int = 2
LYRIA_DTYPE: str = "int16"             # little-endian signed 16-bit PCM
LYRIA_MIME_PREFIX: str = "audio/l16"   # whatever the server sends, must start with this
LYRIA_INITIAL_BUFFER_S: float = 1.0    # pre-roll before audio_play starts draining

# How long the audio queue can grow before the producer blocks. At 48kHz
# stereo s16 = 192 KB/s, so 64 chunks * ~10KB/chunk = ~3s of audio. Plenty
# of headroom over network jitter without letting the queue swallow GBs
# of memory if the consumer hangs.
LYRIA_AUDIO_QUEUE_MAX: int = 64

# Default Lyria config sent at session start. EEG-driven updates ride on
# top of this baseline -- so e.g. brightness=0.5 here means "the default
# is mid-brightness; alpha will modulate it up or down from there".
LYRIA_DEFAULT_BPM: int = 90
LYRIA_DEFAULT_DENSITY: float = 0.5
LYRIA_DEFAULT_BRIGHTNESS: float = 0.5
LYRIA_DEFAULT_TEMPERATURE: float = 1.1

# EEG -> Lyria mapping ranges. Each AppState feature is in [0, 1], and we
# map it (after the SENSITIVITY_GAIN expansion below) linearly into the
# corresponding Lyria parameter range.
#
# Tradeoff: ranges that are too narrow waste Lyria's musical envelope
# (the brain has to push hard to reach an audibly different state);
# ranges that are too wide let the model degenerate into noise (very
# high temperature) or unmusical metronome ticks (BPM extremes).
# Widened from the original 60-140 / 0.6-1.8 alongside the gain bump
# so saturating the curve actually reaches a noticeably-different
# musical state instead of just clipping at a "still kind of normal"
# value.
LYRIA_BPM_MIN: int = 55                # asymmetry=0 (left-frontal lean)
LYRIA_BPM_MAX: int = 160               # asymmetry=1 (right-frontal lean)
LYRIA_TEMPERATURE_MIN: float = 0.5     # theta=0 (alert / drowsy off)
LYRIA_TEMPERATURE_MAX: float = 1.9     # theta=1 (drowsy / dreamy on)

# How aggressively EEG deviations from the neutral 0.5 midpoint amplify
# into Lyria control changes. The mapping in lyria/mapping.py rewrites
# each feature x as `0.5 + (x - 0.5) * GAIN` (then clips to [0, 1]) BEFORE
# linear-interpolating into the BPM/temperature ranges. Effect is a
# "contrast knob" on the brain -> music coupling:
#
#   GAIN = 1.0 (off):         x=0.65 -> y=0.65  ("standard mapping")
#   GAIN = 2.0 (default):     x=0.65 -> y=0.80  ("twice as responsive")
#   GAIN = 3.0 (aggressive):  x=0.65 -> y=0.95  ("nearly saturated at 0.65")
#
# Why a gain instead of just widening the parameter ranges: the EEG
# normalizer's tanh squashes z-scores into ~[0.05, 0.95] even at strong
# brain shifts (z=±2 -> y=0.04 / 0.96). A linear 1:1 mapping wastes
# that headroom -- the music never reaches the bright/dense ends of
# the Lyria envelope unless you basically stop existing. The gain
# pulls those mid-range deviations into the audibly-extreme zone where
# they belong.
#
# Defaults to 2.0 because that's the lowest setting where the change is
# unmistakably audible to the wearer in the first 5-10 seconds of focus
# vs. eyes-closed transitions. Push to 2.5-3.0 if even faster response
# is wanted; pull below 1.5 if the music feels jittery.
LYRIA_SENSITIVITY_GAIN: float = 2.0

# How long to wait between reconnect attempts on Lyria session failure.
# Lyria's WebSocket can drop on transient network blips; we treat a drop
# the same as the EEG reconnect supervisor -- log, sleep, retry.
LYRIA_RECONNECT_BACKOFF_S: float = 3.0
LYRIA_RECONNECT_MAX_ATTEMPTS: int = 3

# Minimum seconds between consecutive set_music_generation_config()
# pushes to Lyria. Sim EEG fires eeg_tick every 0.25s (4 Hz); pushing
# config at that rate empirically destabilizes the WebSocket -- the
# server seems to fall behind processing the rapid update stream and
# either delays audio production or drops the connection with a
# keepalive timeout. Throttling to 1 push/second matches the smoke
# script's tempo and the timescale on which musical parameters are
# perceptually distinguishable anyway. Set to 0 to push every tick
# (fastest response, least stable).
LYRIA_CTRL_PUSH_INTERVAL_S: float = 1.0

# How long to wait after session.play() for the first audio chunk
# before declaring the session a dud and forcing a reconnect.
# Background: the lyria-realtime-exp model occasionally accepts a
# session and then never produces audio (no error, no chunks, just
# silence until the WebSocket eventually times out at ~60s). The
# reconnect supervisor would catch this on its own, but only after
# the WebSocket-level keepalive expires -- a full minute the audience
# spends in awkward silence. With this watchdog we kill stalled
# sessions and let the supervisor retry; second attempts usually
# succeed in 3-6s.
#
# 15s is a deliberate compromise: a healthy first-session typically
# produces audio in 3-6s, so 15s is 3-5x that and won't false-positive
# on a slow but real warmup. Worst case: stall detected at 15s +
# reconnect overhead ~5s = ~20s in silence before the audience hears
# anything. Acceptable for a demo; the "warming up Lyria..." banner
# in the browser bridges the wait.
LYRIA_FIRST_CHUNK_TIMEOUT_S: float = 15.0

# How many Claude rewrites to try if Lyria filters the prompt at startup.
# The prompt-guard module knows how to translate filtered prompts into
# pure-sonic descriptors. Default 1 = "rewrite once, then surrender".
LYRIA_MAX_PROMPT_REWRITES: int = 1

# Approximate wall-clock duration of one Lyria audio chunk. Lyria's
# lyria-realtime-exp model emits ~96000-frame stereo chunks at 48 kHz,
# which is exactly 2 s of music. The mid-session prompt crossfade
# uses this as the per-step pacing (one weighted_prompts push every
# ~LYRIA_CHUNK_DURATION_S so the audible blend matches the rate the
# music actually unfolds). Wall-clock-driven rather than receive-loop-
# driven so a full audio queue (Lyria sometimes bursts ahead of the
# player at session start) doesn't delay the user-visible crossfade.
LYRIA_CHUNK_DURATION_S: float = 2.0

# Default crossfade duration when the user changes the prompt mid-session
# (browser clicks the header prompt, types a new one, hits Enter).
# Expressed in chunks for an intuitive units. At LYRIA_CHUNK_DURATION_S
# per step, 8 chunks = ~16 s of musical handoff -- long enough to feel
# smooth, short enough to not seem broken. The browser may override
# per-request but defaults to this if it doesn't pass a `chunks` field.
LYRIA_PROMPT_CHANGE_DEFAULT_CHUNKS: int = 8


# ---------------------------------------------------------------------------
# Audio analysis (Phase 6)
# ---------------------------------------------------------------------------
#
# We tee each Lyria chunk into a second small bounded queue and run a
# numpy FFT pipeline over it to extract three perceptual features
# (rms / spectral_centroid / onset_strength). These drive the visualizer
# in Phase 7+ and animate the "Audio (P6)" section of the perform TUI.

# Hop size in samples (per channel) for one analysis frame. 2400 samples
# at 48 kHz = 50 ms = ~20 Hz update rate. Smaller = jitterier features
# but more reactive; larger = smoother but laggy. 50 ms is a sweet spot
# for visual reactivity without oversampling small noise fluctuations.
AUDIO_ANALYSIS_HOP_SAMPLES: int = 2400

# Bounded analysis queue. Lossy on overflow: if the FFT can't drain fast
# enough we drop the OLDEST chunk silently rather than backpressure
# Lyria. Sized for ~8 chunks (each Lyria chunk is ~96k samples = 2s of
# audio at 48 kHz stereo s16, so 8 chunks ~= 16s buffer). In practice
# the FFT runs orders of magnitude faster than real-time, so this only
# absorbs occasional GC stalls.
AUDIO_ANALYSIS_QUEUE_MAX: int = 8

# EMA smoothing weight for audio features. Heavier smoothing than EEG
# (EEG already has its own EMA upstream and runs at 4 Hz; audio runs
# at ~20 Hz so we need a lower alpha to get a comparable visual response
# rate). 0.15 = 5-frame effective time constant ~250 ms.
AUDIO_FEATURE_SMOOTHING: float = 0.15

# Adaptive percentile-baseline normalization. Each feature is normalized
# against a slow-moving high-water mark so the [0, 1] output adapts to
# track-level loudness/brightness rather than absolute units. The
# baseline tracks toward the rolling high quickly when current >
# baseline, slowly when current < baseline -- matching how a perceptual
# AGC behaves.
AUDIO_BASELINE_ATTACK: float = 0.30   # baseline rises fast on louder peaks
AUDIO_BASELINE_RELEASE: float = 0.005 # baseline falls slow on quiet sections

# Spectral centroid is normalized against the Nyquist frequency. Real
# music rarely lands above ~6 kHz centroid even at peak brightness, so
# this cap keeps the [0, 1] output from compressing into the bottom
# fifth of its range. Values above the cap saturate to 1.0.
AUDIO_CENTROID_NORM_HZ: float = 6000.0


# ---------------------------------------------------------------------------
# Server / browser visualizer (Phase 7)
# ---------------------------------------------------------------------------

# Broadcast cadence to all connected WebSocket clients. EEG updates at
# 4 Hz, audio at ~20 Hz, so 20 Hz is the natural ceiling -- faster
# would just send duplicate frames. Lower if a future demo runs over
# Wi-Fi to a remote browser and bandwidth becomes an issue.
SERVER_BROADCAST_HZ: float = 20.0

# WebSocket path. The browser opens "ws://host:port{SERVER_WS_PATH}".
# Keep in sync with static/app.js if you change it.
SERVER_WS_PATH: str = "/ws"

# Where the browser visualizer files live. Directory is served at /static/*
# and contains index.html + app.js + style.css today; Phase 8 drops
# seed.png in here too. Path is resolved relative to the repo root.
SERVER_STATIC_DIR: str = "static"

# How long to wait between server bind and auto-launching Chrome. Small
# enough to be invisible to the operator, big enough that the listener
# is definitely accepting before we hand a URL to the browser.
SERVER_BROWSER_OPEN_DELAY_S: float = 0.25


# ---------------------------------------------------------------------------
# Seed image / Imagen (Phase 8)
# ---------------------------------------------------------------------------
#
# A one-shot text-to-image call at session start. The generated image
# becomes the visual identity of the session: Phase 9's Three.js shader
# loads it as the initial texture, and EEG/audio uniforms warp / blur /
# colorize it from there.

# Imagen model (same google-genai SDK we use for Lyria).
# imagen-4.0-fast-generate-001 is the lowest-latency Imagen 4 variant
# available on this API key (verified via client.models.list()): ~3-5s
# wall time, lower per-image cost than the standard or ultra tiers,
# zero local model weights to download. Swap to imagen-4.0-generate-001
# for the standard quality tier or imagen-4.0-ultra-generate-001 for
# best quality at higher latency / cost.
SEED_MODEL_ID: str = "imagen-4.0-fast-generate-001"

# Aspect ratio passed to the Imagen API. The shader uniforms work with
# any ratio, but 16:9 matches a typical full-screen browser window and
# avoids letterboxing in the visualizer.
SEED_ASPECT_RATIO: str = "16:9"

# Where the canonical seed image lands. Served at /static/seed.png by
# the Phase 7 server; gitignored so different prompts don't pollute git.
SEED_OUTPUT_PATH: str = "static/seed.png"

# Per-prompt cache. Filenames are sha256(prompt + model + ratio)[:16].png.
# Lets you swap prompts back and forth in dev without paying the API cost
# every time. Cleared by deleting the directory (or `--no-seed-cache`).
SEED_CACHE_DIR: str = "static/cache"


# ---------------------------------------------------------------------------
# Seed evolver (Phase 10)
# ---------------------------------------------------------------------------
#
# A background task that periodically regenerates the seed image so the
# visual evolves with how the brain has been moving. Hybrid mutation:
# template-based modifiers from EEG/audio drift, polished by Claude into
# a single coherent prompt, fed to Imagen.

# How often a new image is generated -- expressed in Lyria audio chunks
# rather than wall-clock seconds. Each chunk is ~2s of music (96k stereo
# samples at 48 kHz, see audio/fft.py), so the default 12 chunks ≈ 24 s
# of music between regenerations. Locking the cadence to chunks instead
# of seconds keeps the visual transitions feeling tied to the music
# rather than drifting against it. Set to 0 via --evolve-chunks 0 to
# disable entirely.
EVOLVE_INTERVAL_CHUNKS: int = 12

# Cost reminder: at 12 chunks (~24s) cadence, ~150 Imagen calls/hour.
# Roughly $3/hr for imagen-4.0-fast (rates may change). If running long
# sessions, bump to 24+ chunks.

# Rolling window of recent EEG/audio samples used to compute the drift
# descriptor. Long enough to smooth out one-second blips, short enough
# that "trends" reflect what just happened, not the whole session.
EVOLVE_WINDOW_S: float = 45.0

# How frequently the evolver samples AppState into its rolling window.
# 2 Hz is plenty -- the drift summary only cares about averages and
# slopes, not high-frequency detail.
EVOLVE_SAMPLE_HZ: float = 2.0

# Maximum number of template-generated modifiers to feed into Claude per
# cycle. Picks the strongest ones by absolute drift magnitude. Too many
# and Claude can't weave them naturally; too few and the visual barely
# evolves.
EVOLVE_MAX_MODIFIERS: int = 4

# Threshold below which a feature trend / mean is considered "neutral"
# and contributes no modifier. Keeps the prompt from being padded with
# noise when the brain is just sitting still.
EVOLVE_NEUTRAL_THRESHOLD: float = 0.10

# Claude model used for the polish step (after template modifiers are
# generated). Same anthropic SDK as the Lyria prompt-guard, same
# ANTHROPIC_API_KEY required.
EVOLVE_CLAUDE_MODEL: str = "claude-opus-4-7"   # match music/prompt_guard.py
EVOLVE_CLAUDE_MAX_TOKENS: int = 200

# Browser-side crossfade duration when a new seed lands. Long enough to
# feel smooth, short enough that the new image still feels like a real
# event. At the default 12-chunk (~24s) cadence, 6s of fade leaves ~18s
# of "settled" view per cycle. Mirrored in the visualizer.js render
# loop's `state.crossfadeDur` default.
EVOLVE_CROSSFADE_S: float = 6.0


# ---------------------------------------------------------------------------
# Smoothing / normalization
# ---------------------------------------------------------------------------

# EMA weight for the EEG features (alpha/beta/theta/asymmetry) inside
# the brainflow loop. Lower = smoother + laggier. Bumped 0.20 -> 0.30
# alongside the LYRIA_SENSITIVITY_GAIN work: the gain amplifies whatever
# arrives at the mapping, so a heavily-smoothed feature stream produces
# an expressive-but-laggy music response. With alpha=0.30 the effective
# time constant drops from ~5 ticks (~1.25 s @ 4 Hz) to ~3 ticks
# (~0.75 s), which is short enough that voluntary focus / eye-close
# transitions are heard within the first second, but long enough that
# 1-tick noise spikes don't clip Lyria's BPM up and down.
SMOOTHING_ALPHA: float = 0.3


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

# Order matches BrainFlow's Muse 2 EEG channel ordering.
EEG_CHANNEL_NAMES: tuple[str, ...] = ("TP9", "AF7", "AF8", "TP10")

# Channels used for band power (focus/calm/alpha/beta/theta).
FRONTAL_CHANNELS: tuple[str, ...] = ("AF7", "AF8")

# Channels used for whole-head triggers (jaw clench).
ALL_CHANNELS: tuple[str, ...] = EEG_CHANNEL_NAMES


# ---------------------------------------------------------------------------
# Band definitions (Hz)
# ---------------------------------------------------------------------------

BAND_THETA: tuple[float, float] = (4.0, 8.0)
BAND_ALPHA: tuple[float, float] = (8.0, 12.0)
BAND_BETA:  tuple[float, float] = (13.0, 30.0)


# ---------------------------------------------------------------------------
# Discrete triggers (tune these to your headset fit and signal quality)
# ---------------------------------------------------------------------------

# Blink: peak-to-peak on AF7/AF8.
#   2026-05-13: bumped from ~120 -> 225 (idle PTP was ~60μV).
#   2026-05-13 evening: still oversaturated; captured blink read 1015μV,
#     bumped 225 -> 500.
#   2026-05-13 night: still firing on casual eye movement (1400μV at rest);
#     bumped 500 -> 1500.
#   2026-05-13 late: 1500 too insensitive -- ordinary blinks weren't
#     firing in the browser visualizer, captured at-rest bar was ~7%.
#     Pulled back to 1000: above the casual eye-movement floor (~700μV)
#     but below the typical full blink (~1000-1400μV) so a normal blink
#     fires reliably without ambient eye motion triggering it.
BLINK_THRESHOLD_UV: float = 1000.0
BLINK_REFRACTORY_S: float = 0.3

JAW_HP_CUTOFF_HZ: float = 20.0
# Threshold is peak |HP| averaged across channels.
#   2026-05-13: bumped from 80 -> 160 (idle was ~86μV).
#   2026-05-13 evening: still oversaturated; captured casual clench was
#     221μV. Bumped 160 -> 320.
#   2026-05-13 late: 320 too insensitive -- firm clenches weren't firing
#     in the browser visualizer (captured at-rest bar 21%, captured fire
#     was 221μV which sat *below* the 320 threshold). Pulled back to 220:
#     above the at-rest noise floor (~70-90μV) but at the level a
#     genuine clench produces, so deliberate jaws fire on demand.
JAW_THRESHOLD_UV: float = 220.0
JAW_REFRACTORY_S: float = 0.3
