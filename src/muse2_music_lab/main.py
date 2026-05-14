"""Orchestrator entry point for `muse2 perform`.

Phase status (each phase replaces one heartbeat stub with a real task):

    Phase 4   eeg-board / eeg-sim   REAL  writes alpha/beta/theta/asymmetry
                                          into AppState, sets eeg_tick.
                                          Reconnects on transient BLE drop.
    Phase 5   lyria                 REAL  WebSocket to lyria-realtime-exp;
                                          pushes config on every eeg_tick;
                                          PCM bytes -> state.audio_queue
                                          AND state.audio_analysis_queue.
              audio-play            REAL  drains audio_queue to sounddevice.
    Phase 6   audio-fft             REAL  drains audio_analysis_queue,
                                          writes rms / centroid / onset
                                          into AppState at ~20 Hz.
    Phase 7   server                REAL  aiohttp HTTP + WebSocket broadcast
                                          of state.snapshot() to browser at
                                          SERVER_BROADCAST_HZ. Optionally
                                          auto-launches Chrome.
    Phase 8   seed_image            REAL  one-shot Imagen call at startup,
                                          writes static/seed.png (and a
                                          per-prompt cache copy). Runs
                                          synchronously BEFORE the asyncio
                                          orchestrator; --skip-seed bypasses.

Lifecycle pattern (used here, kept for every later phase):

    1. Build AppState from the prompt.
    2. Spawn each task as `asyncio.create_task(...)` -- name the task so
       cancellation logs are readable.
    3. Install a SIGINT handler via `loop.add_signal_handler` that sets a
       `stop_evt`. Cleaner than `signal.signal()` because it integrates
       with the loop's wake-up.
    4. `await stop_evt.wait()`.
    5. In `finally`: cancel every task, gather with return_exceptions, log.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass

from muse2_music_lab import config
from muse2_music_lab.audio import run_audio_analysis_loop
from muse2_music_lab.eeg.brainflow_loop import run_eeg_supervisor
from muse2_music_lab.lyria import (
    run_audio_playback_loop,
    run_lyria_loop,
)
from muse2_music_lab.perform_tui import PerformTuiOptions, run_perform_tui
from muse2_music_lab.server import ServerOptions, run_server_loop
from muse2_music_lab.state import AppState
from muse2_music_lab.visuals import run_initial_seed_loop
from muse2_music_lab.visuals.seed_evolver import run_seed_evolver_loop


@dataclass
class PerformOptions:
    """Runtime options for the `muse2 perform` pipeline."""

    prompt: str = ""
    http_port: int = 8000
    no_browser: bool = False
    simulate_eeg: bool = False
    no_lyria: bool = False
    no_server: bool = False
    # When True, fall back to the plain `[state] alpha=...` line printer
    # instead of the rich.Live panel. Useful for piping logs to a file
    # or for environments where rich's terminal manipulation misbehaves.
    no_tui: bool = False
    # Phase 8: seed image controls.
    skip_seed: bool = False         # bypass the Imagen call entirely
    no_seed_cache: bool = False     # always regenerate, even on cache hit
    # Phase 10: seed evolver. Regenerate the seed every N Lyria chunks
    # of music (each chunk ≈ 2s, so 12 chunks ≈ 24s). 0 disables.
    # Cost ~$3/hr at the default 12-chunk cadence.
    evolve_chunks: int = config.EVOLVE_INTERVAL_CHUNKS
    # Cloud / Railway mode: forces simulated EEG, no local sounddevice
    # output (audio fans out to browsers as binary WS frames instead),
    # no auto-browser, no TUI, binds to 0.0.0.0 so the PaaS can route
    # external traffic, locks per-visitor controls (Quit / EEG mode
    # toggle) so a single visitor can't break the experience for
    # everyone else.
    cloud: bool = False


# ---------------------------------------------------------------------------
# Stub tasks (Phase 3 -- each replaced as later phases land)
# ---------------------------------------------------------------------------


async def _heartbeat(name: str, state: AppState, period_s: float = 1.0) -> None:
    """Generic heartbeat. Each phase replaces this with a real task body.

    Suppresses the per-tick print while the rich.Live TUI owns the screen,
    so the placeholder doesn't smear the panel with `[audio-fft] tick N`
    lines. The task itself stays alive so we have something to cancel on
    shutdown (and so `len(tasks)` stays honest).
    """
    n = 0
    try:
        while True:
            n += 1
            if not state.tui_active:
                print(f"[{name}] tick {n}", flush=True)
            await asyncio.sleep(period_s)
    except asyncio.CancelledError:
        print(f"[{name}] cancelled (after {n} ticks)", flush=True)
        raise


async def _keyboard_listener(
    state: AppState,
    stop_evt: asyncio.Event,
) -> None:
    """Read single-letter commands from stdin: 'r' = recalibrate, 'q' = quit.

    Mirrors the same UX as `muse2 run`'s TUI keyboard handler so users
    don't have to learn a new gesture. Press the letter + Enter (line-
    buffered; we don't put the TTY into raw mode since that would steal
    Ctrl-C handling from the OS).

    Implementation note: `sys.stdin.readline()` is blocking and can't be
    cancelled cross-platform. We can't wrap it in `loop.run_in_executor`
    because `asyncio.run()` calls `shutdown_default_executor()` on exit,
    which would hang forever waiting for the blocked read to return. So
    we spawn a daemon thread (dies with the process), and bridge events
    back to asyncio with `loop.call_soon_threadsafe(...)`. The asyncio
    task itself just parks forever waiting for cancellation.

    Skipped when stdin isn't a TTY (piped / headless CI), because there's
    no human to type letters at us anyway.
    """
    if not sys.stdin.isatty():
        # Park forever; cancellation by the orchestrator is the only way out.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        return

    loop = asyncio.get_running_loop()
    print(
        "[keys] hotkeys active: 'r'+Enter recalibrate  |  'q'+Enter quit",
        flush=True,
    )

    def _on_recalibrate() -> None:
        if state.recalibrate_request.is_set():
            print("[keys] recalibrate already pending -- ignored", flush=True)
        else:
            print("[keys] recalibrate requested", flush=True)
            state.recalibrate_request.set()

    def _on_quit(reason: str) -> None:
        if not stop_evt.is_set():
            print(f"[keys] {reason} -- shutting down", flush=True)
            stop_evt.set()

    def _reader() -> None:
        # Daemon thread body. Bridges blocking stdin reads back to the
        # asyncio loop via call_soon_threadsafe (the only thread-safe
        # way to mutate asyncio primitives from outside the loop).
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                # Stdin closed (EOF, terminal lost, etc).
                loop.call_soon_threadsafe(_on_quit, "stdin closed")
                return
            if not line:
                # Clean EOF (Ctrl-D).
                loop.call_soon_threadsafe(_on_quit, "stdin EOF")
                return

            ch = line.strip().lower()
            if ch in ("r", "recal", "recalibrate"):
                loop.call_soon_threadsafe(_on_recalibrate)
            elif ch in ("q", "quit", "exit"):
                loop.call_soon_threadsafe(_on_quit, "quit requested")
                return
            elif ch == "":
                # Bare Enter -- ignore quietly.
                continue
            else:
                # Build the message on this thread, then dispatch a
                # zero-arg lambda so call_soon_threadsafe (which doesn't
                # accept kwargs) can still produce a flushed print.
                msg = (
                    f"[keys] unknown command {ch!r} -- "
                    "use 'r' (recalibrate) or 'q' (quit)"
                )
                loop.call_soon_threadsafe(lambda m=msg: print(m, flush=True))

    thread = threading.Thread(target=_reader, name="keys-stdin", daemon=True)
    thread.start()

    # Park until the orchestrator cancels us at shutdown. The daemon thread
    # dies when the process exits; nothing for us to clean up here.
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise


async def _cloud_audio_queue_drain(state: AppState) -> None:
    """Cloud-mode replacement for run_audio_playback_loop.

    In `--cloud` there's no sounddevice / no host audio device, so
    nothing drains state.audio_queue (the playback queue). The Lyria
    receive loop puts() into it with `await` -- which blocks
    indefinitely if nothing reads -- so without this drain, Lyria
    would back-pressure within ~3 seconds and stop generating.

    The actual audio that reaches browsers comes from the SEPARATE
    state.audio_broadcast_queue tee (drained by
    server.audio_broadcast.run_audio_broadcast_loop). This drain
    just discards the playback-queue copy.

    We deliberately don't pace this by sleeping -- Lyria is the
    pacing source (it generates real-time, ~one chunk per 0.5s of
    music). Reading as fast as the queue produces is correct.
    """
    drained = 0
    try:
        await state.start_requested.wait()
        print(
            "[audio-drain] cloud mode: draining playback queue to /dev/null "
            "(audio reaches visitors via WS broadcast)",
            flush=True,
        )
        while True:
            chunk = await state.audio_queue.get()
            try:
                drained += 1
            finally:
                state.audio_queue.task_done()
    except asyncio.CancelledError:
        print(f"[audio-drain] cancelled after {drained} chunks drained", flush=True)
        raise


async def _state_logger(
    state: AppState,
    period_s: float = config.PERFORM_LOG_PERIOD_S,
) -> None:
    """Print a one-line snapshot of AppState every period_s seconds.

    Useful during development so you can see EEG / audio features come
    alive without needing the TUI or the browser. Phase 7's WS broadcast
    will eventually obsolete the need for this in production, but it stays
    on as a free debug surface.
    """
    n = 0
    try:
        while True:
            await asyncio.sleep(period_s)
            n += 1
            print(
                f"[state] alpha={state.alpha:.2f}  beta={state.beta:.2f}  "
                f"theta={state.theta:.2f}  asym={state.asymmetry:.2f}  "
                f"|  rms={state.rms:.2f}  cent={state.centroid:.2f}  "
                f"ons={state.onset:.2f}",
                flush=True,
            )
    except asyncio.CancelledError:
        print(f"[state] cancelled (after {n} logs)", flush=True)
        raise


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _summarize_opts(opts: PerformOptions) -> None:
    if opts.cloud:
        print("[perform] CLOUD MODE      (--cloud forces simulated EEG, "
              "WS audio broadcast, no TUI, no auto-browser)")
    if opts.prompt.strip():
        print(f"[perform] prompt:        {opts.prompt!r}  (auto-start)")
    else:
        print("[perform] prompt:        <none>  (browser will provide)")
    print(f"[perform] simulate_eeg:  {opts.simulate_eeg}")
    print(f"[perform] lyria:         {'OFF' if opts.no_lyria else 'ON'}")
    bind_label = "0.0.0.0" if opts.cloud else "localhost"
    print(
        f"[perform] server:        "
        f"{'OFF' if opts.no_server else f'ON (http://{bind_label}:{opts.http_port}/)'}"
    )
    print(f"[perform] auto-browser:  {'NO' if opts.no_browser else 'YES'}")
    print(f"[perform] tui:           {'OFF (plain log)' if opts.no_tui else 'ON (rich live)'}")
    if opts.skip_seed:
        print("[perform] seed image:    SKIP (--skip-seed)")
    elif opts.no_seed_cache:
        print("[perform] seed image:    REGENERATE (--no-seed-cache)")
    else:
        print("[perform] seed image:    ON (cache enabled)")
    if opts.evolve_chunks > 0 and not opts.no_lyria:
        approx_s = opts.evolve_chunks * 2  # ~2s per Lyria chunk
        print(
            f"[perform] seed evolver:  ON (every {opts.evolve_chunks} chunks "
            f"≈ {approx_s}s of music)"
        )
    elif opts.evolve_chunks > 0 and opts.no_lyria:
        print("[perform] seed evolver:  OFF (--no-lyria; nothing to count chunks against)")
    else:
        print("[perform] seed evolver:  OFF (--evolve-chunks 0)")


def _spawn_tasks(
    opts: PerformOptions,
    state: AppState,
    stop_evt: asyncio.Event,
) -> list[asyncio.Task]:
    """Spawn the task graph based on the debug flags.

    Each phase replaces the corresponding `_heartbeat(...)` stub with the
    real implementation. Today (after Phase 4): EEG is real; Lyria,
    audio, and server are still stubs.
    """
    tasks: list[asyncio.Task] = []

    # Phase 4: EEG supervisor task. Owns whichever inner loop (real BLE
    # or simulated) is active right now and hot-swaps between them when
    # the user clicks the EEG-mode toggle in the browser. --simulate-eeg
    # picks the initial mode; either can be switched away from at runtime.
    initial_eeg_mode = "simulated" if opts.simulate_eeg else "real"
    tasks.append(asyncio.create_task(
        run_eeg_supervisor(state, initial_mode=initial_eeg_mode),
        name="eeg-sup",
    ))

    # Status surface: rich.Live panel by default; plain stdout printer when
    # --no-tui (useful for log piping or environments where rich's terminal
    # control misbehaves). Mutually exclusive -- only one owns the output.
    if opts.no_tui:
        tasks.append(asyncio.create_task(_state_logger(state), name="state"))
    else:
        tui_opts = PerformTuiOptions(
            show_lyria=not opts.no_lyria,
            show_audio_section=True,
        )
        tasks.append(asyncio.create_task(
            run_perform_tui(state, tui_opts), name="tui"
        ))

    # Keyboard hotkey listener -- 'r' to recalibrate, 'q' to quit.
    # Always on; gracefully no-ops when stdin isn't a TTY.
    tasks.append(asyncio.create_task(
        _keyboard_listener(state, stop_evt), name="keys"
    ))

    if not opts.no_lyria:
        # Phase 5: real Lyria session + sounddevice playback.
        # Both gate internally on state.start_requested (Phase 10), so
        # they sit idle until the user clicks Start in the browser
        # (or main.py auto-fires it when --prompt was passed at CLI).
        tasks.append(asyncio.create_task(
            run_lyria_loop(state), name="lyria"
        ))
        # Cloud deploys have no local audio device: skip sounddevice
        # entirely. The Lyria session still tees PCM into
        # audio_broadcast_queue, which the server's audio fan-out
        # task (spawned inside run_server_loop when cloud_mode=True)
        # ships to every browser as binary WS frames. The audio_queue
        # itself still needs to be drained, otherwise the producer
        # back-pressures forever -- the cloud_drain_task below does that.
        if opts.cloud:
            tasks.append(asyncio.create_task(
                _cloud_audio_queue_drain(state),
                name="audio-drain",
            ))
        else:
            tasks.append(asyncio.create_task(
                run_audio_playback_loop(state), name="audio-play"
            ))
        # Phase 6: numpy FFT tap. Sibling consumer of the lossy
        # state.audio_analysis_queue (separate from audio_play's queue),
        # writes rms / centroid / onset into AppState at ~20 Hz.
        tasks.append(asyncio.create_task(
            run_audio_analysis_loop(state), name="audio-fft"
        ))

    if not opts.no_server:
        # Phase 7: aiohttp HTTP + WebSocket broadcast of state.snapshot()
        # to the browser visualizer at SERVER_BROADCAST_HZ. Static files
        # served from `static/` (gitignored except for index.html etc).
        # Phase 10: pass stop_evt so the browser's Quit button can
        # request a graceful shutdown of the whole pipeline.
        # Cloud: server also binds 0.0.0.0 + spawns the audio fan-out.
        server_opts = ServerOptions(
            http_port=opts.http_port,
            cloud_mode=opts.cloud,
            # In cloud deploys, NEVER let any one visitor quit the
            # process for everyone else (handler refuses) -- so don't
            # even hand the stop_evt over.
            no_browser=opts.no_browser,
            stop_evt=None if opts.cloud else stop_evt,
        )
        tasks.append(asyncio.create_task(
            run_server_loop(state, server_opts), name="server"
        ))

    # Phase 8 (now Phase 10 lifecycle): one-shot Imagen call. Was
    # synchronous-pre-orchestrator; now an async task that gates on
    # state.start_requested so the browser can drive the prompt.
    if not opts.no_lyria:
        # Lyria-on path: seed image lifecycle is part of the music
        # pipeline. Honors --skip-seed by writing a "skipped" log line
        # and bumping seed_version to reload the on-disk image.
        tasks.append(asyncio.create_task(
            run_initial_seed_loop(
                state,
                use_cache=not opts.no_seed_cache,
                skip=opts.skip_seed,
            ),
            name="seed-image",
        ))

    # Phase 10: seed evolver. Watches AppState window and regenerates
    # static/seed.png every opts.evolve_chunks Lyria chunks of music.
    # Skipped if:
    #   --skip-seed         (no seed pipeline to evolve)
    #   --evolve-chunks 0   (operator opt-out)
    #   --no-lyria          (no chunks ever -> would idle forever)
    # Self-disables cleanly if GEMINI_API_KEY is missing.
    if opts.evolve_chunks > 0 and not opts.skip_seed and not opts.no_lyria:
        tasks.append(asyncio.create_task(
            run_seed_evolver_loop(state, interval_chunks=opts.evolve_chunks),
            name="seed-evolver",
        ))

    return tasks


async def _wait_for_shutdown(
    stop_evt: asyncio.Event,
    tasks: list[asyncio.Task],
) -> str:
    """Return when SIGINT fires OR any task exits / raises.

    Phase 4+ tasks (real EEG, Lyria, sounddevice) can fail at any point
    (BLE drop, API outage, audio device unplug). If we only awaited
    `stop_evt`, a task failure would silently hang the whole orchestrator
    instead of shutting it down. Race them together.
    """
    stop_task = asyncio.create_task(stop_evt.wait(), name="stop-watcher")
    try:
        done, _pending = await asyncio.wait(
            [stop_task, *tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not stop_task.done():
            stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task

    for t in done:
        if t is stop_task:
            return "sigint"
        exc = t.exception()
        name = t.get_name()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            print(f"[perform] task {name!r} raised: {exc!r}", flush=True)
            return f"task-failed:{name}"
        return f"task-exited:{name}"
    return "unknown"


async def _run_async(opts: PerformOptions) -> int:
    # --cloud is a meta-flag. Force the sub-flags so the operator can't
    # accidentally combine --cloud with something incompatible (e.g.
    # --cloud --no-server would be a paid Lyria session that no visitor
    # can hear). Also defends against future flags drifting out of sync.
    if opts.cloud:
        if opts.no_server:
            print(
                "[perform] FAIL: --cloud requires the server (--no-server is "
                "incompatible: visitors connect via the WS).",
                file=sys.stderr,
            )
            return 2
        opts.simulate_eeg = True
        opts.no_browser = True
        opts.no_tui = True

    # Phase 10 deadlock guard: --no-server + no --prompt has no path to
    # ever fire start_requested, so the orchestrator would idle forever.
    # Catch it BEFORE building the task graph so the user gets a clean
    # exit instead of a confusing "running 5 tasks" line followed by
    # nothing happening.
    if opts.no_server and not opts.prompt.strip():
        print(
            "[perform] FAIL: --no-server requires --prompt (no UI to "
            "click Start from). Pass either or both.",
            file=sys.stderr,
        )
        return 2

    # seed_prompt starts equal to the session prompt so the first evolve
    # cycle has a sensible "previous prompt" to evolve from. Each
    # evolver cycle then overwrites it with the evolved variant.
    # eeg_mode reflects the CLI choice so the FIRST WS snapshot the
    # browser sees already shows the right toggle state -- the
    # supervisor will (re)set this once it actually starts, but
    # pre-populating avoids a one-frame "real" flash for sim launches.
    state = AppState(
        prompt=opts.prompt,
        seed_prompt=opts.prompt,
        eeg_mode="simulated" if opts.simulate_eeg else "real",
        cloud_mode=opts.cloud,
    )
    _summarize_opts(opts)
    print()

    stop_evt = asyncio.Event()
    tasks = _spawn_tasks(opts, state, stop_evt)

    # Phase 10: if the operator gave a --prompt at the CLI, auto-fire
    # Start so the run behaves like the pre-Phase-10 days (zero-click
    # demo path). Otherwise we wait for the browser's Start button.
    if opts.prompt.strip():
        state.lyria_started = True
        state.start_requested.set()
        print("[perform] auto-started (CLI --prompt provided)")
    else:
        print(
            "[perform] waiting for Start in the browser "
            "(open http://localhost:"
            f"{opts.http_port}/ and click Start)..."
        )

    print(f"[perform] running {len(tasks)} task(s). Ctrl-C to stop.")
    if not opts.no_server and not opts.no_browser:
        print(
            f"[perform] (Phase 7 will auto-launch Chrome at "
            f"http://localhost:{opts.http_port}/)"
        )
    print()

    loop = asyncio.get_running_loop()

    def _on_sigint() -> None:
        if stop_evt.is_set():
            print("[perform] (already shutting down -- second Ctrl-C ignored)", flush=True)
            return
        print("\n[perform] SIGINT -- shutting down...", flush=True)
        stop_evt.set()

    sigint_installed = False
    sigterm_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
        sigint_installed = True
    except NotImplementedError:
        # Windows / odd environments. asyncio.run will fall back to the
        # default Python KeyboardInterrupt path; we catch it in run().
        pass
    try:
        # PaaS containers (Railway, Fly, Heroku) send SIGTERM, then SIGKILL
        # after a grace period. Treat SIGTERM exactly like SIGINT so the
        # cleanup paths run before we get killed.
        loop.add_signal_handler(signal.SIGTERM, _on_sigint)
        sigterm_installed = True
    except NotImplementedError:
        pass

    exit_code = 0
    try:
        reason = await _wait_for_shutdown(stop_evt, tasks)
        if reason.startswith("task-failed:"):
            exit_code = 4
            print(f"[perform] shutting down ({reason})", flush=True)
        elif reason.startswith("task-exited:"):
            # Unexpected: a task returned cleanly with no SIGINT. Treat
            # as a non-zero so the operator notices.
            exit_code = 5
            print(f"[perform] shutting down ({reason})", flush=True)
    finally:
        for t in tasks:
            t.cancel()
        # gather with return_exceptions so a slow-to-cancel task doesn't
        # mask a different one's exit reason.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for t, r in zip(tasks, results):
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                # Already logged by _wait_for_shutdown for the FIRST_COMPLETED
                # task; this catches secondary failures during cancellation.
                print(
                    f"[perform] task {t.get_name()!r} cleanup error: {r!r}",
                    flush=True,
                )

        if sigint_installed:
            with suppress(NotImplementedError):
                loop.remove_signal_handler(signal.SIGINT)
        if sigterm_installed:
            with suppress(NotImplementedError):
                loop.remove_signal_handler(signal.SIGTERM)

        print("[exit] stopped cleanly.")

    return exit_code


def run(opts: PerformOptions) -> int:
    """Sync entry point called by `cli.py _cmd_perform`.

    Phase 10: --prompt is now optional. Two valid paths:
      * --prompt provided    -> auto-fire Start, behaves like prior demos
      * --prompt omitted     -> launches the browser/server, waits for the
                                user to type a prompt + click Start
    """
    try:
        return asyncio.run(_run_async(opts))
    except KeyboardInterrupt:
        # Fallback for platforms where add_signal_handler isn't supported.
        # The Unix path normally exits via the loop's stop_evt instead.
        print("\n[exit] interrupted")
        return 130
