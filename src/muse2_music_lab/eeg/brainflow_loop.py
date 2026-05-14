"""Async EEG feature loop: pumps normalized features into AppState.

Two flavors, identical downstream contract:

  * `run_real_eeg_loop(state)` -- live Muse 2 over BLE via BrainFlow.
    Performs an 8-second baseline calibration on startup, then streams
    per-feature-window updates at PERFORM_TICK_S (default 4 Hz).

  * `run_simulated_eeg_loop(state)` -- synthetic features at the same
    cadence with no headset / no calibration. Used by `--simulate-eeg`
    for headset-free dev of everything downstream (Lyria, audio, server).

Both produce identical effects on AppState:
  - state.{alpha, beta, theta, asymmetry} updated to values in [0, 1]
  - state.eeg_tick set after each successful update

Downstream consumers (Phase 5 Lyria control loop, Phase 7 WS broadcaster)
are edge-triggered: they `await state.eeg_tick.wait()`, snapshot what they
need, then `state.eeg_tick.clear()` themselves before re-awaiting. This
gives us automatic coalescing: if a downstream push is in flight when the
next tick lands, the event re-sets and the consumer picks up the latest
state on its next iteration -- never a queue of stale pushes.

BrainFlow is a blocking C++ library; we run its calls in the default
thread executor so the event loop stays responsive. Cleanup runs inside
the existing `_sigint_shielded` context in Board.stop(), so a Ctrl-C
mid-shutdown still releases the BLE peripheral cleanly.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Callable, Optional

from brainflow.exit_codes import BrainFlowError

from muse2_music_lab import config
from muse2_music_lab.eeg.board import Board, BoardInfo
from muse2_music_lab.eeg.features import (
    BlinkDetector,
    JawClenchDetector,
    compute_asymmetry,
    compute_frame,
)
from muse2_music_lab.eeg.smoother import Calibrator, EMA, Normalizer
from muse2_music_lab.simulate import synthetic_frame
from muse2_music_lab.state import AppState


_CONTINUOUS_NAMES: tuple[str, ...] = ("alpha", "beta", "theta")


# ---------------------------------------------------------------------------
# Real Muse 2 path
# ---------------------------------------------------------------------------


async def _read_window(loop: asyncio.AbstractEventLoop, board: Board):
    """Off-thread window read so the event loop doesn't block on BrainFlow."""
    return await loop.run_in_executor(
        None, board.get_window, config.WINDOW_SIZE
    )


async def _calibrate(
    loop: asyncio.AbstractEventLoop,
    board: Board,
    sampling_rate: int,
    blink: BlinkDetector,
    jaw: JawClenchDetector,
    state: AppState,
) -> Normalizer:
    """Collect baseline samples for CALIBRATION_DURATION seconds.

    Yields to the event loop between samples so other tasks (state logger,
    server stub, etc.) keep heart-beating during the wait.

    Sets `state.calibrating` + start-ts + total so the browser can render
    a "sit still, eyes open" countdown banner. The flag is cleared in a
    finally-block so a cancelled/exceptional calibration still releases
    the banner -- otherwise a dropped BLE link mid-calibration would
    leave the user staring at a frozen "calibrating..." screen.
    """
    print(
        f"[eeg] calibrating for {config.CALIBRATION_DURATION:.0f}s "
        "(sit still, eyes open)...",
        flush=True,
    )
    state.calibration_total_s = float(config.CALIBRATION_DURATION)
    state.calibration_started_ts = time.monotonic()
    state.calibrating = True
    try:
        calibrator = Calibrator(names=_CONTINUOUS_NAMES)
        end = state.calibration_started_ts + config.CALIBRATION_DURATION
        while time.monotonic() < end:
            window = await _read_window(loop, board)
            if window.shape[1] >= config.WINDOW_SIZE:
                frame = compute_frame(window, sampling_rate, blink, jaw)
                calibrator.add(
                    {
                        "alpha": frame.alpha,
                        "beta": frame.beta,
                        "theta": frame.theta,
                    }
                )
            await asyncio.sleep(config.PERFORM_TICK_S)

        norm = Normalizer(calibrator.finish())
        summary = "  ".join(
            f"{n}=μ{b.mean:+.2f}/σ{b.std:.2f}"
            for n, b in norm.baselines.items()
        )
        print(f"[eeg] calibrated. baseline {summary}", flush=True)
        return norm
    finally:
        state.calibrating = False


async def _stream_body(
    loop: asyncio.AbstractEventLoop,
    state: AppState,
    board: Board,
    info: BoardInfo,
    blink: BlinkDetector,
    jaw: JawClenchDetector,
    emas: dict[str, EMA],
    asym_ema: EMA,
    normalizer_holder: list[Normalizer],
    on_first_tick: Callable[[], None],
) -> None:
    """Inner per-tick streaming loop. Runs until cancelled or BLE drops.

    `normalizer_holder` is a single-element list so we can swap in a new
    normalizer mid-stream when the user requests a recalibrate without
    losing the reference held by the supervisor across reconnects.

    `on_first_tick` is fired exactly once after the first successful state
    write of this attempt -- the supervisor uses it to reset its consecutive
    failure counter so a transient drop doesn't permanently raise the bar
    for future drops in the same session.
    """
    print(
        f"[eeg] streaming features into AppState at "
        f"{1.0 / config.PERFORM_TICK_S:.0f} Hz  "
        "(press 'r'+Enter to recalibrate, 'q'+Enter to quit)",
        flush=True,
    )

    first_tick_done = False
    while True:
        # Mid-stream recalibrate: keyboard listener sets the event. We
        # re-run the same 8s baseline capture, atomically swap in the new
        # normalizer, and reset the EMAs so the new baseline takes effect
        # cleanly instead of being smeared across stale smoothed values
        # for the next ~1s.
        #
        # During the 8s recalibration window, all AppState values freeze
        # at whatever they were at the moment 'r' was pressed, and no
        # eeg_tick fires. Downstream consumers (Phase 5 Lyria) keep their
        # last config for those 8 seconds, which is desired -- you wouldn't
        # want Lyria thrashing on garbage values mid-recalibrate anyway.
        if state.recalibrate_request.is_set():
            state.recalibrate_request.clear()
            print("[eeg] recalibrate requested -- holding stream...", flush=True)
            normalizer_holder[0] = await _calibrate(
                loop, board, info.sampling_rate, blink, jaw, state
            )
            for ema in emas.values():
                ema.reset()
            asym_ema.reset()
            print("[eeg] resumed streaming with new baseline", flush=True)

        # Live threshold sync: the browser TUNE panel writes new values
        # directly into state.live_* via the WS set_threshold action.
        # Re-applying every tick is cheap (two attribute writes) and
        # keeps a slider drag taking effect within ~one tick (<= 250 ms),
        # without needing a separate event/queue/wakeup channel.
        blink.threshold = float(state.live_blink_threshold_uv)
        jaw.threshold = float(state.live_jaw_threshold_uv)

        window = await _read_window(loop, board)
        if window.shape[1] < config.WINDOW_SIZE:
            await asyncio.sleep(config.PERFORM_TICK_S)
            continue

        frame = compute_frame(window, info.sampling_rate, blink, jaw)

        smoothed_alpha = emas["alpha"].update(frame.alpha)
        smoothed_beta = emas["beta"].update(frame.beta)
        smoothed_theta = emas["theta"].update(frame.theta)

        norm = normalizer_holder[0]
        state.alpha = max(0.0, min(1.0, norm.normalize("alpha", smoothed_alpha)))
        state.beta = max(0.0, min(1.0, norm.normalize("beta", smoothed_beta)))
        state.theta = max(0.0, min(1.0, norm.normalize("theta", smoothed_theta)))

        asym_raw = compute_asymmetry(window, info.sampling_rate)
        state.asymmetry = max(0.0, min(1.0, asym_ema.update(asym_raw)))

        # Discrete trigger surface (consumed by the perform TUI; reserved
        # for Phase 6+ to drive event-level musical accents).
        state.blink_triggered = bool(frame.blink)
        state.jaw_triggered = bool(frame.jaw)
        state.blink_ptp_uv = float(blink.last_ptp)
        state.jaw_rms_uv = float(jaw.last_rms)

        state.eeg_tick.set()

        if not first_tick_done:
            first_tick_done = True
            # Browser pill flips to green on the first real tick post-
            # connect. Doing it here (instead of right after `board.start`)
            # makes the indicator honest -- "connected" means features
            # are actually flowing.
            state.eeg_connection_state = "connected"
            on_first_tick()

        await asyncio.sleep(config.PERFORM_TICK_S)


async def run_real_eeg_loop(state: AppState) -> None:
    """Connect to Muse 2, calibrate once, then stream forever -- with reconnect.

    Lifecycle layers:

      * Outer supervisor (this function): owns the cross-reconnect state
        (smoothers, baseline normalizer, blink/jaw detectors). Catches
        transient BLE failures, tears down the dead Board, sleeps with
        backoff, and reattempts -- without ever re-running calibration,
        so the user's baseline survives a momentary headset slip or
        macOS BLE hiccup.

      * One connect attempt per outer iteration: builds a fresh Board,
        starts it, runs initial calibration (only on the very first
        successful connect of the whole session), then enters _stream_body.

      * _stream_body: ticks at PERFORM_TICK_S, honors recalibrate_request,
        writes AppState, sets eeg_tick.

    Reconnect policy: up to EEG_RECONNECT_MAX_ATTEMPTS *consecutive* failed
    attempts triggers shutdown (lets the orchestrator stop everything
    cleanly). A single successful tick post-reconnect resets the counter,
    so a long session with occasional 1-off drops keeps running indefinitely.
    """
    loop = asyncio.get_running_loop()

    # State that survives reconnects -- created once at the top:
    blink = BlinkDetector()
    jaw: Optional[JawClenchDetector] = None
    emas = {n: EMA(alpha=config.SMOOTHING_ALPHA) for n in _CONTINUOUS_NAMES}
    asym_ema = EMA(alpha=config.SMOOTHING_ALPHA)
    normalizer_holder: list[Optional[Normalizer]] = [None]

    consecutive_failures = 0

    def _reset_failure_counter() -> None:
        nonlocal consecutive_failures
        if consecutive_failures > 0:
            print(
                f"[eeg] reconnect successful "
                f"(was {consecutive_failures} consecutive failure(s))",
                flush=True,
            )
            consecutive_failures = 0

    while True:
        board = Board()
        try:
            is_reconnect = normalizer_holder[0] is not None
            if is_reconnect:
                print("[eeg] reconnecting to Muse 2 over BLE...", flush=True)
                state.eeg_connection_state = "reconnecting"
            else:
                print("[eeg] connecting to Muse 2 over BLE...", flush=True)
                state.eeg_connection_state = "searching"

            info = await loop.run_in_executor(None, board.start)
            print(
                f"[eeg] connected: {len(info.eeg_channels)} EEG ch @ "
                f"{info.sampling_rate} Hz",
                flush=True,
            )
            # Browser pill: BLE handshake done. Will flip to "connected"
            # below once the first feature tick fires (in _stream_body).
            state.eeg_connection_state = "found"

            if jaw is None:
                jaw = JawClenchDetector(sampling_rate=info.sampling_rate)

            if normalizer_holder[0] is None:
                # First successful connect of the session: do initial
                # calibration. Failure here propagates through the same
                # except path as a stream-time drop.
                normalizer_holder[0] = await _calibrate(
                    loop, board, info.sampling_rate, blink, jaw, state
                )
                # Signal to the perform TUI (and any other waiter) that
                # AppState is now producing meaningful normalized values.
                # Reconnects don't re-fire this -- it stays set for the
                # life of the session.
                state.eeg_ready.set()
            else:
                print(
                    "[eeg] resumed with prior baseline (no recalibration)",
                    flush=True,
                )

            await _stream_body(
                loop,
                state,
                board,
                info,
                blink,
                jaw,
                emas,
                asym_ema,
                normalizer_holder,  # type: ignore[arg-type]
                _reset_failure_counter,
            )
            # _stream_body never returns normally; if it does, fall through
            # to teardown and exit.
            return

        except asyncio.CancelledError:
            print("[eeg] cancelled", flush=True)
            print("[eeg] releasing Muse session...", flush=True)
            await loop.run_in_executor(None, board.stop)
            print("[eeg] released.", flush=True)
            raise

        except (ConnectionError, BrainFlowError) as e:
            # Browser pill: connection just died. The next outer-loop
            # iteration will flip to "reconnecting" once it actually
            # tries (after backoff sleep), so "lost" is the brief
            # window between drop and retry.
            state.eeg_connection_state = "lost"
            print("[eeg] releasing Muse session...", flush=True)
            await loop.run_in_executor(None, board.stop)
            print("[eeg] released.", flush=True)

            consecutive_failures += 1
            if consecutive_failures > config.EEG_RECONNECT_MAX_ATTEMPTS:
                state.eeg_connection_state = "failed"
                print(
                    f"[eeg] giving up after {consecutive_failures} consecutive "
                    f"BLE failures (last: {type(e).__name__}: {e})",
                    flush=True,
                )
                # Re-raise as ConnectionError so the orchestrator's
                # task-failed handler logs a coherent reason and shuts
                # the rest of the pipeline down cleanly.
                raise ConnectionError(
                    f"Muse 2 BLE link unrecoverable after "
                    f"{consecutive_failures} attempts: {e}"
                ) from e

            backoff = config.EEG_RECONNECT_BACKOFF_S * consecutive_failures
            print(
                f"[eeg] BLE issue ({type(e).__name__}): {e}",
                flush=True,
            )
            print(
                f"[eeg] reconnect attempt "
                f"{consecutive_failures}/{config.EEG_RECONNECT_MAX_ATTEMPTS} "
                f"in {backoff:.1f}s...",
                flush=True,
            )
            await asyncio.sleep(backoff)
            # Loop iterates: builds a new Board() and tries again.


# ---------------------------------------------------------------------------
# Synthetic / headset-free path
# ---------------------------------------------------------------------------


async def run_simulated_eeg_loop(state: AppState) -> None:
    """Synthetic features at PERFORM_TICK_S cadence. No BLE, no calibration."""
    print(
        f"[eeg] using SIMULATED EEG (--simulate-eeg) at "
        f"{1.0 / config.PERFORM_TICK_S:.0f} Hz",
        flush=True,
    )
    # No calibration in sim, so AppState is meaningful from tick #1; let the
    # TUI activate immediately on this path.
    state.eeg_ready.set()
    # Browser pill: distinct value so the operator sees they're on
    # synthetic data, not a real headset. Renders amber, not green.
    state.eeg_connection_state = "simulated"

    start = time.monotonic()
    n = 0
    try:
        while True:
            t = time.monotonic() - start
            frame, normalized, blink_uv, jaw_uv = synthetic_frame(t)

            state.alpha = normalized["alpha"]
            state.beta = normalized["beta"]
            state.theta = normalized["theta"]
            # Asymmetry: slow LFO around the natural 0.5 idle midpoint.
            # Period intentionally coprime with the alpha/beta/theta
            # periods in synthetic_frame so the demo doesn't lock into
            # an obvious shared rhythm.
            state.asymmetry = max(
                0.0,
                min(1.0, 0.5 + 0.3 * math.sin(2 * math.pi * t / 13.0)),
            )

            state.blink_triggered = bool(frame.blink)
            state.jaw_triggered = bool(frame.jaw)
            state.blink_ptp_uv = float(blink_uv)
            state.jaw_rms_uv = float(jaw_uv)

            state.eeg_tick.set()
            n += 1
            await asyncio.sleep(config.PERFORM_TICK_S)
    except asyncio.CancelledError:
        print(f"[eeg] cancelled (after {n} ticks)", flush=True)
        raise


# ---------------------------------------------------------------------------
# EEG supervisor: hot-swap between real BLE and synthetic at runtime
# ---------------------------------------------------------------------------
#
# The orchestrator owns ONE long-lived task, the supervisor. The supervisor
# in turn owns whichever inner EEG loop is active right now (real or
# simulated) and watches `state.eeg_mode_change_request` for browser-side
# toggle clicks. On a swap request:
#   1. Cancel the current inner task.
#   2. Wait for it to drain (BLE teardown, etc.).
#   3. Update `state.eeg_mode` to the new mode.
#   4. Spawn the new inner task.
# Re-spawning is the simplest correct primitive -- the inner tasks own a
# lot of mode-specific state (Calibrator/Normalizer/EMA for real,
# nothing for simulated) that isn't worth preserving across a swap.
#
# Failure tolerance:
#   * If the real inner task hard-fails (BLE unrecoverable), the
#     supervisor catches the ConnectionError, sets eeg_connection_state
#     to "failed", and idles waiting for the user to switch modes
#     instead of crashing the whole orchestrator. This is a strict
#     improvement over the pre-toggle behavior (which exit-coded the
#     whole `perform` process on a flapping band).
#   * If the simulated inner task ever returns/raises (it shouldn't;
#     it's an infinite loop), same handling.

_VALID_MODES = ("real", "simulated")


async def _spawn_inner(state: AppState, mode: str) -> asyncio.Task:
    """Create the appropriate inner EEG task for the given mode."""
    if mode == "simulated":
        return asyncio.create_task(run_simulated_eeg_loop(state), name="eeg-sim")
    return asyncio.create_task(run_real_eeg_loop(state), name="eeg-board")


async def _drain(task: asyncio.Task) -> Optional[BaseException]:
    """Cancel + await an inner task; return any non-cancel exception.

    Used both for orchestrator-driven shutdown and for mode-swap
    teardown. Swallows CancelledError (which is the expected outcome
    of cancelling) but surfaces real exceptions so the supervisor can
    decide whether to re-raise or idle.
    """
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return None
    except BaseException as exc:  # noqa: BLE001 -- supervisor needs full visibility
        return exc
    return None


async def run_eeg_supervisor(state: AppState, *, initial_mode: str) -> None:
    """Long-lived EEG manager. Owns the inner loop; swaps on browser request.

    `initial_mode` is "real" or "simulated", typically derived from
    PerformOptions.simulate_eeg at boot. The browser flips it at runtime
    by sending `{"action": "set_eeg_mode", "mode": "real"|"simulated"}`,
    which sets `state.eeg_mode_target` + fires
    `state.eeg_mode_change_request`.
    """
    if initial_mode not in _VALID_MODES:
        raise ValueError(
            f"run_eeg_supervisor: initial_mode must be one of {_VALID_MODES}; "
            f"got {initial_mode!r}"
        )

    state.eeg_mode = initial_mode
    print(f"[eeg-sup] starting in mode={state.eeg_mode!r}", flush=True)

    inner = await _spawn_inner(state, state.eeg_mode)

    try:
        while True:
            change_wait = asyncio.create_task(
                state.eeg_mode_change_request.wait(), name="eeg-mode-change-wait",
            )
            try:
                done, _pending = await asyncio.wait(
                    [inner, change_wait],
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # Orchestrator shutdown: cancel both children and exit.
                await _drain(change_wait)
                await _drain(inner)
                print("[eeg-sup] cancelled", flush=True)
                raise

            # Path A: user requested a mode change.
            if change_wait in done and not change_wait.cancelled():
                state.eeg_mode_change_request.clear()
                target = (state.eeg_mode_target or "").strip().lower()
                if target not in _VALID_MODES:
                    print(
                        f"[eeg-sup] ignoring mode change to {target!r} "
                        f"(must be one of {_VALID_MODES})",
                        flush=True,
                    )
                    continue
                if target == state.eeg_mode and not inner.done():
                    print(
                        f"[eeg-sup] no-op: already in mode {target!r}",
                        flush=True,
                    )
                    continue

                print(
                    f"[eeg-sup] swapping {state.eeg_mode!r} -> {target!r}",
                    flush=True,
                )
                # Tear the current inner down before flipping the mode
                # field, so any reader sees a consistent state.
                drain_exc = await _drain(inner)
                if drain_exc is not None:
                    print(
                        f"[eeg-sup] inner cleanup error during swap: "
                        f"{type(drain_exc).__name__}: {drain_exc}",
                        flush=True,
                    )
                # Reset transient signaling so the new task can fire it
                # afresh. Don't touch state.eeg_ready -- once True for
                # the session, it stays True (downstream tasks have
                # already passed their `eeg_ready.wait()`).
                state.eeg_tick.clear()
                state.eeg_connection_state = "idle"
                state.eeg_mode = target
                inner = await _spawn_inner(state, state.eeg_mode)
                continue

            # Path B: inner task ended on its own. With the toggle in
            # place, the most likely cause is real EEG hard-failing
            # after exhausting reconnects. Idle in "failed" state and
            # wait for the user to switch modes instead of crashing
            # the whole orchestrator.
            await _drain(change_wait)
            inner_exc = inner.exception() if inner.done() else None
            if inner_exc is None:
                # Clean exit (shouldn't happen for either mode).
                print(
                    f"[eeg-sup] inner mode={state.eeg_mode!r} exited cleanly; "
                    "idling until user switches mode",
                    flush=True,
                )
            else:
                print(
                    f"[eeg-sup] inner mode={state.eeg_mode!r} failed: "
                    f"{type(inner_exc).__name__}: {inner_exc}",
                    flush=True,
                )
            state.eeg_connection_state = "failed"
            print(
                "[eeg-sup] idle. Use the EEG-mode toggle in the browser "
                "to switch to the other source.",
                flush=True,
            )
            try:
                await state.eeg_mode_change_request.wait()
            except asyncio.CancelledError:
                print("[eeg-sup] cancelled while idle", flush=True)
                raise
            state.eeg_mode_change_request.clear()
            target = (state.eeg_mode_target or "").strip().lower()
            if target not in _VALID_MODES or target == state.eeg_mode:
                # User asked for the same broken mode again, or junk.
                # Bounce back to idle; the loop's next iteration will
                # park on the wait again.
                print(
                    f"[eeg-sup] idle wakeup ignored (target={target!r})",
                    flush=True,
                )
                continue
            print(
                f"[eeg-sup] recovering: swapping {state.eeg_mode!r} -> "
                f"{target!r} after idle",
                flush=True,
            )
            state.eeg_tick.clear()
            state.eeg_connection_state = "idle"
            state.eeg_mode = target
            inner = await _spawn_inner(state, state.eeg_mode)
    finally:
        # Belt-and-braces: if we exit the while loop via any path,
        # don't orphan the inner task.
        if not inner.done():
            await _drain(inner)
