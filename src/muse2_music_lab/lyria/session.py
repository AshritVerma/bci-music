"""Lyria RealTime WebSocket session manager.

Open a single Lyria session per `perform` invocation. Three concurrent
inner tasks run for the lifetime of the session:

  * `_receive_loop`  -- drains server messages. Audio chunks go into
                        state.audio_queue; filtered_prompt events fire
                        the prompt-guard rewrite path.

  * `_control_loop`  -- awaits state.eeg_tick, snapshots AppState,
                        translates via mapping.state_to_lyria_config,
                        and pushes the resulting config back to Lyria.
                        One push per fresh EEG sample (PROJECT_PLAN §3.6
                        decision: drive control rate by the producer
                        rather than a separate fixed timer).

  * `_log_loop`      -- once a second, summarizes what we just pushed.
                        Useful during dev so you can correlate the
                        [state] line from the orchestrator with the
                        [lyria-ctrl] line at the same moment.

Reconnect strategy: same shape as the EEG reconnect supervisor in
brainflow_loop.py. On a session-fatal exception, log + back off + try
again, up to N consecutive failures. Audio queue contents from the
dead session are NOT replayed; the next session starts fresh and
audio_play continues seamlessly with the new bytes once they arrive
(brief silence in between).

Environment:
  - GEMINI_API_KEY -- required. Loaded from .env via python-dotenv.
  - ANTHROPIC_API_KEY -- optional. Enables the prompt-guard rewrite
                        layer if Lyria filters the prompt at startup.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from dotenv import load_dotenv

from muse2_music_lab import config
from muse2_music_lab.lyria.mapping import (
    initial_lyria_config,
    state_to_lyria_config,
    state_to_lyria_params,
)
from muse2_music_lab.music import PromptGuard
from muse2_music_lab.state import AppState


class _MissingApiKey(RuntimeError):
    """Caller should treat this as a fatal config error, not a transient drop."""


class _SessionStalled(RuntimeError):
    """No audio chunks arrived within LYRIA_FIRST_CHUNK_TIMEOUT_S of play().

    Treated as a TRANSIENT failure by the reconnect supervisor (i.e.
    reconnect immediately, don't count toward the auth-failure budget):
    Lyria's lyria-realtime-exp model occasionally accepts a session and
    then never produces audio. The reconnect supervisor's job here is to
    catch that case fast enough that the audience doesn't notice the
    silence."""


def _load_api_key() -> str:
    """Read GEMINI_API_KEY from .env (or the existing environment).

    Raises `_MissingApiKey` with a friendly message if absent. The error
    propagates through the orchestrator's task-failed handler so the user
    sees a clear "set this env var" message instead of a stack trace.
    """
    load_dotenv()
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise _MissingApiKey(
            "GEMINI_API_KEY is missing. Drop it in .env at the repo root "
            "(see .env.example) or export it in the shell before running "
            "`muse2 perform`."
        )
    return api_key


def _maybe_load_prompt_guard() -> Optional[PromptGuard]:
    """Build a PromptGuard if ANTHROPIC_API_KEY is set, else return None.

    The guard is the optional safety net that rewrites Lyria-filtered
    prompts (e.g. "in the style of Daft Punk" -> the underlying sonic
    fingerprint). Without it, a filtered prompt produces zero audio and
    the session is dead -- but the smoke script's design means the user
    sees a clear FILTERED log line, so it's recoverable.
    """
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not anthropic_key:
        return None
    return PromptGuard(anthropic_key)


# ---------------------------------------------------------------------------
# Inner tasks (run concurrently inside one Lyria session)
# ---------------------------------------------------------------------------


def _is_clean_websocket_close(exc: BaseException) -> bool:
    """True if `exc` is the SDK's wrapper around a normal-closure (1000/1001).

    The google-genai live-music client converts a WebSocket close frame
    (sent by the server in response to `session.stop()` or by us during
    orchestrator shutdown) into `APIError(code=1000, ...)`. That's a
    clean end-of-stream, not an error condition we want to surface as a
    "session error -> reconnect" or print as "Task exception was never
    retrieved". 1001 ("going away") is treated the same.
    """
    code = getattr(exc, "code", None)
    return code in (1000, 1001)


async def _receive_loop(
    session,
    state: AppState,
    guard: Optional[PromptGuard],
    rewrites_state: dict,
    audio_seen: asyncio.Event,
) -> None:
    """Drain server messages. Audio -> state.audio_queue; filters -> guard.

    Returns normally on a clean WebSocket close (the SDK turns close-code
    1000 into an APIError; we treat that as end-of-stream). Anything
    else propagates so the supervisor can decide whether to reconnect.
    """
    chunks = 0
    bytes_total = 0
    first_chunk_ts: Optional[float] = None
    unknown = 0

    try:
        async for message in session.receive():
            # Handle filtered_prompt first: it's a server-side veto with no
            # audio payload. If we have a guard and budget left, ask Claude
            # for a rewrite and push it into the same session.
            if message.filtered_prompt is not None:
                fp = message.filtered_prompt
                reason = getattr(fp, "filtered_reason", None)
                print(f"[lyria] FILTERED prompt: {fp}", flush=True)

                if guard is None or rewrites_state["used"] >= rewrites_state["max"]:
                    print(
                        "[lyria] no rewrite available -- "
                        "session will produce no audio. Set ANTHROPIC_API_KEY "
                        "in .env to enable auto-rewrite, or pick a different "
                        "--prompt that doesn't name an artist/song/album.",
                        flush=True,
                    )
                    # Don't break -- just stop trying to recover and let the
                    # session sit. The user can Ctrl-C; the orchestrator
                    # shutdown path handles cleanup.
                    continue

                try:
                    result = await guard.rewrite(rewrites_state["active"], reason=reason)
                except Exception as e:
                    print(
                        f"[lyria] [prompt-guard] FAIL: {e!r}. "
                        "Session will produce no audio.",
                        flush=True,
                    )
                    continue

                from google.genai import types
                rewrites_state["used"] += 1
                rewrites_state["active"] = result.rewritten

                await session.set_weighted_prompts(
                    prompts=[types.WeightedPrompt(text=result.rewritten, weight=1.0)]
                )
                print(
                    f"[lyria] pushed rewrite {rewrites_state['used']}/"
                    f"{rewrites_state['max']}: {result.rewritten!r}",
                    flush=True,
                )
                await asyncio.sleep(0)
                continue

            if message.server_content and message.server_content.audio_chunks:
                for chunk in message.server_content.audio_chunks:
                    data = chunk.data
                    if not data:
                        continue
                    if first_chunk_ts is None:
                        first_chunk_ts = time.monotonic()
                        mime = chunk.mime_type or "(unset)"
                        print(
                            f"[lyria] FIRST audio chunk ({len(data)} bytes, "
                            f"mime={mime})",
                            flush=True,
                        )
                        if not mime.lower().startswith(config.LYRIA_MIME_PREFIX):
                            print(
                                f"[lyria] WARN: unexpected mime {mime!r}; "
                                f"audio_play assumes {config.LYRIA_MIME_PREFIX}* "
                                "and will produce noise if the format changed.",
                                flush=True,
                            )
                        # Signal to the perform TUI that Lyria is producing
                        # audio. Stays set across reconnects -- a brief silence
                        # during reconnect doesn't tear down the TUI.
                        state.lyria_ready.set()
                        # Session-local "first audio arrived" event. Fires
                        # once per session and unblocks the control loop's
                        # warmup gate (see _control_loop) so EEG-driven
                        # config pushes only start after Lyria has proven
                        # it's actually producing audio for THIS session.
                        audio_seen.set()

                    chunks += 1
                    bytes_total += len(data)
                    state.lyria_chunks = chunks

                    # put() blocks if the audio queue is full, which is the
                    # right behavior: PortAudio is the real-time pacing source,
                    # we don't want to outpace it and balloon memory.
                    await state.audio_queue.put(data)

                    # Tee into the analysis queue. Lossy by design: if the
                    # FFT task is behind (slow GC, terminal redraw stall,
                    # whatever), drop the OLDEST chunk and push the new one.
                    # Analysis is allowed to skip frames; playback is not.
                    try:
                        state.audio_analysis_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        try:
                            state.audio_analysis_queue.get_nowait()
                            state.audio_analysis_queue.put_nowait(data)
                        except asyncio.QueueEmpty:
                            pass

                    # Cloud-mode tee: same lossy semantics for the WS
                    # fan-out queue. The broadcaster (server/audio_broadcast)
                    # drains this and ships each chunk as a binary WS
                    # frame to every connected browser. In local-dev runs
                    # nothing reads this queue and the lossy drop logic
                    # keeps memory bounded; the cost is negligible (a
                    # put_nowait + a discard).
                    try:
                        state.audio_broadcast_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        try:
                            state.audio_broadcast_queue.get_nowait()
                            state.audio_broadcast_queue.put_nowait(data)
                        except asyncio.QueueEmpty:
                            pass
            else:
                # Empty or unknown message; don't spam the log.
                unknown += 1
                if unknown <= 3:
                    print(f"[lyria] non-audio message: {message}", flush=True)

            # Yield aggressively so _control_loop's config pushes land between
            # audio chunk batches instead of stacking up behind a hot receive.
            await asyncio.sleep(0)
    except Exception as e:
        # The google-genai SDK raises APIError(code=1000) when the WebSocket
        # closes cleanly -- normally because the supervisor called
        # session.stop() during orchestrator shutdown. That's not an error
        # condition; swallow it so the operator doesn't see a "Task
        # exception was never retrieved" trace after pressing 'q' or
        # Ctrl-C. Anything else propagates to the supervisor's reconnect
        # path.
        if _is_clean_websocket_close(e):
            return
        raise


async def _stall_watchdog(audio_seen: asyncio.Event, timeout_s: float) -> None:
    """Raise _SessionStalled if no audio chunk arrives within `timeout_s`.

    Runs as a sibling of the receive + control loops. The session uses
    FIRST_COMPLETED, so this task must NEVER return normally during a
    healthy session -- if it did, the asyncio.wait() would treat the
    successful "audio arrived" check as the session ending and cancel
    everything else.

    Two valid terminations:
      1. Timer expires -> raise _SessionStalled (supervisor reconnects)
      2. Outer task cancels us at session teardown / process shutdown
    """
    try:
        await asyncio.wait_for(audio_seen.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        raise _SessionStalled(
            f"No audio chunk arrived within {timeout_s:.0f}s of session.play(); "
            "forcing reconnect to recover from the lyria-realtime-exp model "
            "occasionally accepting a session it then never produces audio for."
        )

    # Audio is flowing; our watchdog responsibility is over. Park
    # indefinitely so the session's FIRST_COMPLETED race only fires
    # on real terminations (clean close, real error, or shutdown
    # cancellation), not on our successful exit.
    await asyncio.Event().wait()


async def _control_loop(
    session,
    state: AppState,
    audio_seen: asyncio.Event,
) -> None:
    """Gated, rate-limited config pushes from AppState.

    Two correctness gates protect Lyria's warmup:

      1. WAIT for `audio_seen` before pushing ANYTHING. The
         lyria-realtime-exp model is sensitive to control pushes that
         arrive between session.play() and the first audio chunk: the
         server can either delay generation indefinitely or drop the
         WebSocket with a keepalive timeout. Deferring all pushes
         until we've seen at least one chunk costs ~3-8s of "no EEG
         response" at session start (typically the user is still
         settling in, not noticing) and trades it for reliable music
         delivery.

      2. RATE-LIMIT to LYRIA_CTRL_PUSH_INTERVAL_S between pushes.
         eeg_tick fires at 4 Hz; pushing config at 4 Hz also tends to
         destabilize Lyria. 1 Hz tracks musical perception (the model
         needs at least a beat or two to render a config change in
         audible form) without poking the server too hard.
    """
    pushes = 0
    last_summary_t = time.monotonic()
    last_summary_pushes = 0
    last_push_t = 0.0

    print(
        "[lyria-ctrl] awaiting first audio chunk before sending control pushes...",
        flush=True,
    )
    await audio_seen.wait()
    print(
        f"[lyria-ctrl] first audio observed; control pushes enabled "
        f"(rate-limit {config.LYRIA_CTRL_PUSH_INTERVAL_S:.2f}s)",
        flush=True,
    )

    while True:
        await state.eeg_tick.wait()
        state.eeg_tick.clear()

        # Coalesce: if multiple eeg_ticks fired during the rate-limit
        # window, we want the most-recent one, not a backlog. The most
        # recent values are already in AppState (the EEG loop overwrites
        # in place); just skip this iteration if it's too soon.
        now = time.monotonic()
        if now - last_push_t < config.LYRIA_CTRL_PUSH_INTERVAL_S:
            continue
        last_push_t = now

        # Snapshot the values we care about before any other coroutine
        # has a chance to mutate them. (asyncio guarantees serial execution
        # between awaits, but being explicit makes the timing trivial to
        # reason about.)
        a, b, t, asym = state.alpha, state.beta, state.theta, state.asymmetry

        params = state_to_lyria_params(
            alpha=a, beta=b, theta=t, asymmetry=asym
        )

        cfg = state_to_lyria_config(state)
        try:
            await session.set_music_generation_config(config=cfg)
        except Exception as e:
            print(
                f"[lyria-ctrl] push failed: {e!r}. "
                "Re-raising to trigger session reconnect.",
                flush=True,
            )
            raise
        pushes += 1

        # Mirror the pushed params back into AppState so the TUI (and any
        # future WebSocket broadcaster) can show the live mapping without
        # re-running state_to_lyria_params every refresh.
        state.lyria_bpm = params.bpm
        state.lyria_density = float(params.density)
        state.lyria_brightness = float(params.brightness)
        state.lyria_temperature = float(params.temperature)

        # Periodic summary so the operator can see brain -> Lyria mapping
        # without drowning in per-tick logs. Skipped when the perform TUI
        # owns the screen -- the live panel shows the same data and the
        # summary line would just smear the panel.
        if state.tui_active:
            continue
        now = time.monotonic()
        if now - last_summary_t >= 2.0:
            recent = pushes - last_summary_pushes
            print(
                f"[lyria-ctrl] {recent} push(es) in {now - last_summary_t:.1f}s | "
                f"alpha={a:.2f} beta={b:.2f} theta={t:.2f} asym={asym:.2f} -> "
                f"bpm={params.bpm} dens={params.density:.2f} "
                f"bri={params.brightness:.2f} temp={params.temperature:.2f}",
                flush=True,
            )
            last_summary_t = now
            last_summary_pushes = pushes


# ---------------------------------------------------------------------------
# Outer supervisor (manages reconnect)
# ---------------------------------------------------------------------------


async def _run_one_session(
    state: AppState,
    api_key: str,
    guard: Optional[PromptGuard],
    rewrites_state: dict,
) -> None:
    """Open one Lyria session and run receive + control concurrently.

    Returns normally only on a clean Cancellation (orchestrator shutdown).
    Any other failure mode (auth, network, model error) propagates as an
    exception so the supervisor can decide whether to reconnect.
    """
    # Late SDK import so a missing google-genai install fails inside the
    # task (where the orchestrator can log + shutdown cleanly) instead of
    # at module import time.
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1alpha"},
    )

    print(
        f"[lyria] connecting to {config.LYRIA_MODEL_ID} ...",
        flush=True,
    )
    async with client.aio.live.music.connect(
        model=config.LYRIA_MODEL_ID
    ) as session:
        print("[lyria] WebSocket session connected", flush=True)

        # Cookbook-prescribed order: prompts -> config -> play -> spawn tasks.
        await session.set_weighted_prompts(
            prompts=[types.WeightedPrompt(text=state.prompt, weight=1.0)]
        )
        await session.set_music_generation_config(config=initial_lyria_config())
        await session.play()
        print(
            f"[lyria] play() called. prompt={state.prompt!r}",
            flush=True,
        )

        # Session-local "first audio chunk arrived" event. The control
        # task waits on this before its first push (warmup gate); the
        # watchdog uses it to detect a stalled session that needs
        # reconnect.
        audio_seen = asyncio.Event()

        recv_task = asyncio.create_task(
            _receive_loop(session, state, guard, rewrites_state, audio_seen),
            name="lyria-recv",
        )
        ctrl_task = asyncio.create_task(
            _control_loop(session, state, audio_seen),
            name="lyria-ctrl",
        )
        watchdog_task = asyncio.create_task(
            _stall_watchdog(audio_seen, config.LYRIA_FIRST_CHUNK_TIMEOUT_S),
            name="lyria-watchdog",
        )
        inner_tasks = [recv_task, ctrl_task, watchdog_task]

        first_failure: Optional[BaseException] = None
        try:
            # Race them: if either dies we bail out of the session and let
            # the supervisor decide whether to reconnect. (Cancelled by the
            # outer task on orchestrator shutdown.)
            done, _pending = await asyncio.wait(
                inner_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Stash the first non-cancellation, non-clean-close exception
            # to re-raise after we've cleanly torn down the other inner
            # task and the session itself. We don't raise here directly so
            # the cleanup path runs unconditionally.
            for t in done:
                exc = t.exception()
                if exc is None or isinstance(exc, asyncio.CancelledError):
                    continue
                if _is_clean_websocket_close(exc):
                    continue
                first_failure = exc
                break
        finally:
            # Cancel any inner task still running, then drain ALL of them
            # via gather(return_exceptions=True). This is what stops the
            # asyncio "Task exception was never retrieved" warning at
            # shutdown: even if recv_task dies with the SDK's clean-close
            # APIError(1000) AFTER session.stop() runs, the gather() here
            # retrieves the exception so the loop doesn't log it.
            for t in inner_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*inner_tasks, return_exceptions=True)

            try:
                await session.stop()
            except Exception:
                pass

        if first_failure is not None:
            raise first_failure


async def run_lyria_loop(state: AppState) -> None:
    """Main entry: open Lyria session, manage reconnect, surface fatal errors.

    Reconnect policy mirrors `eeg/brainflow_loop.py`:
      - Catch any non-cancellation exception
      - Log it, sleep with linear backoff
      - Retry up to LYRIA_RECONNECT_MAX_ATTEMPTS consecutive times
      - On exhaustion, raise -- orchestrator's task-failed path shuts the
        whole pipeline down
    """
    # Phase 10: gate on the user clicking Start in the browser (or
    # main.py auto-firing it when --prompt was provided on the CLI).
    # state.prompt is read AFTER this awaitable resolves so a
    # browser-supplied prompt overrides any pre-existing default.
    print("[lyria] waiting for start...", flush=True)
    await state.start_requested.wait()
    print("[lyria] start received -- opening session", flush=True)

    if not state.prompt.strip():
        # Defense-in-depth: server.app rejects empty prompts before
        # firing start_requested, but if anything else ever sets the
        # event we want to fail loudly rather than burn API quota.
        raise RuntimeError("Lyria session needs a non-empty prompt.")

    try:
        api_key = _load_api_key()
    except _MissingApiKey as e:
        print(f"[lyria] FATAL: {e}", flush=True)
        raise

    guard = _maybe_load_prompt_guard()
    if guard is not None:
        print(
            f"[lyria] prompt-guard enabled (max rewrites: "
            f"{config.LYRIA_MAX_PROMPT_REWRITES})",
            flush=True,
        )
    else:
        print(
            "[lyria] prompt-guard disabled (no ANTHROPIC_API_KEY). "
            "Filtered prompts will produce silence.",
            flush=True,
        )

    # Mutable rewrite state shared with _receive_loop. Living in a dict
    # keeps the rewrite count consistent across reconnects of the same
    # session (so a flapping connection doesn't reset the budget and let
    # us infinite-loop on an unfixable filter).
    rewrites_state = {
        "active": state.prompt,
        "used": 0,
        "max": config.LYRIA_MAX_PROMPT_REWRITES,
    }

    consecutive_failures = 0

    while True:
        try:
            await _run_one_session(state, api_key, guard, rewrites_state)
            # _run_one_session returns normally only on its session
            # context exiting cleanly. That's "session ended" -- treat
            # it as a fatal end of stream rather than reconnecting,
            # otherwise we'd reconnect forever for no reason.
            print("[lyria] session ended cleanly", flush=True)
            return

        except asyncio.CancelledError:
            print("[lyria] cancelled", flush=True)
            raise

        except _SessionStalled as e:
            # Special-case: this is a known-flaky upstream behavior, not
            # an indication that the integration is broken. Fast-reconnect
            # (no backoff scaling) and DON'T count toward the consecutive-
            # failure budget so we keep trying even after several stalls.
            # The audience is in silence right now; getting back into
            # production matters more than being polite to the API.
            print(f"[lyria] session stalled: {e}", flush=True)
            print(
                "[lyria] fast-reconnecting (stall doesn't count toward "
                "the failure budget; this is the known lyria-realtime-exp "
                "warmup-stall workaround)",
                flush=True,
            )
            await asyncio.sleep(0.5)
            continue

        except Exception as e:
            consecutive_failures += 1
            if consecutive_failures > config.LYRIA_RECONNECT_MAX_ATTEMPTS:
                print(
                    f"[lyria] giving up after {consecutive_failures} consecutive "
                    f"failures (last: {type(e).__name__}: {e})",
                    flush=True,
                )
                raise

            backoff = config.LYRIA_RECONNECT_BACKOFF_S * consecutive_failures
            print(
                f"[lyria] session error ({type(e).__name__}): {e}",
                flush=True,
            )
            print(
                f"[lyria] reconnect attempt {consecutive_failures}/"
                f"{config.LYRIA_RECONNECT_MAX_ATTEMPTS} in {backoff:.1f}s...",
                flush=True,
            )
            await asyncio.sleep(backoff)
