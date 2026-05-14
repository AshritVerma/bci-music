"""Phase 7: aiohttp HTTP + WebSocket server.

Exposes one task -- `run_server_loop(state, opts)` -- that:

  * Builds an aiohttp `web.Application` with three routes:
      GET /          -> serves static/index.html
      GET /static/*  -> serves the rest of the static dir
      GET /ws        -> WebSocket upgrade; client appended to broadcast set
  * Spawns a sibling broadcast task that JSON-serializes
    `state.snapshot()` at SERVER_BROADCAST_HZ and fans it out to every
    connected client. Failed sends prune the offending client; the
    broadcast loop never raises on a single bad client.
  * Optionally launches Chrome at the local URL once the listener is
    accepting (skipped when `opts.no_browser` is True).
  * On cancellation, cleanly tears down the runner, the listening
    socket, and any in-flight WebSockets. Mirrors the lifecycle shape
    of the EEG and Lyria tasks so SIGINT shutdown is uniform across
    the orchestrator.

Single-client and multi-client both work -- the broadcast set is just
a Python set, so 1 browser, 5 browsers, or 0 browsers all behave the
same way (with 0 clients the broadcast loop silently does nothing).

Phase 9 will replace static/index.html's body with a Three.js + GLSL
renderer. The WebSocket schema (the JSON snapshot) doesn't change, so
the server itself stays exactly as is.
"""

from __future__ import annotations

import asyncio
import json
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from aiohttp import WSMsgType, web

from muse2_music_lab import config
from muse2_music_lab.server.audio_broadcast import (
    audio_init_message,
    run_audio_broadcast_loop,
)
from muse2_music_lab.state import AppState


@dataclass
class ServerOptions:
    """Subset of PerformOptions the server cares about."""

    http_port: int = 8000
    # Cloud mode: also bind to 0.0.0.0 (so PaaS containers can reach
    # the listener) and spawn the audio broadcaster that ships Lyria
    # PCM as binary WS frames to every connected browser.
    cloud_mode: bool = False
    no_browser: bool = False
    # Phase 10: orchestrator stop event so a Quit action from the
    # browser can request a graceful shutdown of the whole perform
    # process (mirroring the 'q'+Enter terminal hotkey). Optional --
    # if not provided, Quit messages are logged but ignored.
    stop_evt: "asyncio.Event | None" = None


# ---------------------------------------------------------------------------
# Broadcast set helpers
# ---------------------------------------------------------------------------

# We attach a `set[WebSocketResponse]` to the application instance so the
# WebSocket handler and the broadcast task share one source of truth.
# Using app["..."] keeps it scoped to this server (no module-level globals
# that would leak across reconnect attempts in the same process).
_CLIENTS_KEY = "muse2_clients"


def _clients(app: web.Application) -> set[web.WebSocketResponse]:
    return app[_CLIENTS_KEY]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _index(request: web.Request) -> web.FileResponse:
    """Serve static/index.html at the root."""
    static_dir: Path = request.app["static_dir"]
    return web.FileResponse(static_dir / "index.html")


async def _health(request: web.Request) -> web.Response:
    """Healthcheck endpoint for Railway / any PaaS load balancer.

    Returns 200 + a tiny JSON body whenever the server is alive and the
    AppState dataclass is intact. Used by the Railway healthcheck path
    in railway.json. Cheap enough to call once a second forever.
    """
    state: AppState = request.app["state"]
    body = {
        "status": "ok",
        "uptime_s": round(time.monotonic() - state.session_start_ts, 1),
        "lyria_started": state.lyria_started,
        "lyria_chunks": state.lyria_chunks,
        "cloud_mode": state.cloud_mode,
    }
    return web.json_response(body)


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint: accept, register, dispatch actions, deregister.

    Two-way protocol:
      Server -> client: state.snapshot() JSON at SERVER_BROADCAST_HZ.
      Client -> server: JSON {"action": "start"|"recalibrate"|"quit", ...}.
        Anything else is logged and ignored.
    """
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    clients = _clients(request.app)
    state: AppState = request.app["state"]
    opts: ServerOptions = request.app["server_opts"]
    clients.add(ws)
    peer = request.remote or "?"
    print(f"[server] ws connect from {peer} (clients={len(clients)})", flush=True)

    # Send the audio_init header immediately so the browser can spin
    # up its AudioContext at the right sample rate / channel count
    # before any binary PCM frames arrive.
    #
    # Sent in BOTH modes now (was previously gated behind cloud_mode):
    # the browser-side recorder needs the PCM stream for the in-browser
    # MediaRecorder pipeline that fuses canvas video + Lyria audio into
    # one downloadable WebM file. The `playback` field tells local-mode
    # pages to ingest-but-not-play (sounddevice on the host owns the
    # speakers); cloud-mode pages still play through the browser.
    try:
        await ws.send_str(audio_init_message(playback=state.cloud_mode))
    except Exception as e:
        print(f"[server] failed to send audio_init: {e!r}", flush=True)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await _handle_action(ws, state, opts, msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        clients.discard(ws)
        print(f"[server] ws disconnect from {peer} (clients={len(clients)})", flush=True)

    return ws


async def _handle_action(
    ws: web.WebSocketResponse,
    state: AppState,
    opts: ServerOptions,
    raw: str,
) -> None:
    """Parse a JSON action from the browser and apply it to AppState.

    Errors (bad JSON, unknown action, missing field) are logged + acked
    back to the client so the UI can surface them, but never raise --
    a malformed message from one client must not affect anything else.
    """
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError) as e:
        print(f"[server] bad WS action JSON: {e!r}; raw={raw[:120]!r}", flush=True)
        await _ack(ws, ok=False, error=f"invalid JSON: {e}")
        return

    action = (msg.get("action") or "").strip().lower()

    if action == "start":
        # Browser-supplied prompt overrides any CLI default. Empty prompt
        # is rejected here so the seed image task doesn't have to worry
        # about it. The browser is also expected to disable its Start
        # button on empty input, but defense-in-depth is cheap.
        prompt = (msg.get("prompt") or "").strip()
        if not prompt:
            await _ack(ws, ok=False, error="empty prompt")
            return
        if state.start_requested.is_set():
            # Idempotent ack -- a second Start click after the first is
            # most likely a double-click race in the browser; honor it
            # silently rather than confusing the user with an error.
            await _ack(ws, ok=True, info="already started")
            return
        state.prompt = prompt
        state.seed_prompt = prompt
        state.lyria_started = True
        state.start_requested.set()
        print(f"[server] action=start  prompt={prompt!r}", flush=True)
        await _ack(ws, ok=True)
        return

    if action == "recalibrate":
        # Same path the 'r'+Enter terminal hotkey takes. The EEG loop
        # snoops state.recalibrate_request between samples and runs the
        # baseline capture inline. Simulated EEG ignores the event.
        if state.eeg_connection_state == "simulated":
            await _ack(ws, ok=False, error="recalibrate is a no-op on simulated EEG")
            return
        state.recalibrate_request.set()
        print("[server] action=recalibrate", flush=True)
        await _ack(ws, ok=True)
        return

    if action == "set_eeg_mode":
        # Browser-driven EEG source toggle. Hot-swaps the inner EEG
        # task (real BLE <-> synthetic) without restarting the
        # process. Validation is strict so the supervisor never sees
        # a value it doesn't expect.
        if state.cloud_mode:
            # Public deploys can't reach a real Muse 2; the toggle is
            # locked in cloud mode anyway, but defense-in-depth.
            await _ack(ws, ok=False, error="EEG mode is locked to 'simulated' in cloud mode")
            return
        target = (msg.get("mode") or "").strip().lower()
        if target not in ("real", "simulated"):
            await _ack(ws, ok=False, error=f"mode must be 'real' or 'simulated' (got {target!r})")
            return
        if target == state.eeg_mode:
            # Idempotent: same-mode toggle is a no-op, not an error.
            await _ack(ws, ok=True, info=f"already in {target!r} mode")
            return
        state.eeg_mode_target = target
        state.eeg_mode_change_request.set()
        print(f"[server] action=set_eeg_mode  target={target!r}", flush=True)
        await _ack(ws, ok=True)
        return

    if action == "change_prompt":
        # Mid-session prompt change. Browser sends the new prompt; the
        # Lyria receive loop snoops the event, ramps a weighted
        # crossfade over `chunks` audio chunks (default = config), and
        # finalizes by replacing state.prompt.
        #
        # Validation rules (defense-in-depth, mirrors browser checks):
        #   * Must be after Start (no prompt to change otherwise).
        #   * Lyria session must have produced audio (else there's no
        #     receive loop to snoop the event yet -- pushing weighted
        #     prompts before first audio is the same antipattern that
        #     stalled sessions in DEMO_CHECKLIST work).
        #   * Prompt must be non-empty after strip().
        #   * Crossfade chunks clamped to [1, 64] -- 64 chunks ~= 2 min,
        #     longer than that is almost certainly a typo.
        if not state.lyria_started:
            await _ack(ws, ok=False, error="press Start first; no session to update")
            return
        if not state.lyria_ready.is_set():
            await _ack(ws, ok=False, error="Lyria still warming up; try again in a moment")
            return
        prompt = (msg.get("prompt") or "").strip()
        if not prompt:
            await _ack(ws, ok=False, error="empty prompt")
            return
        if prompt == state.prompt and state.prompt_change_target == "":
            await _ack(ws, ok=True, info="prompt unchanged")
            return
        try:
            chunks = int(msg.get("chunks") or config.LYRIA_PROMPT_CHANGE_DEFAULT_CHUNKS)
        except (TypeError, ValueError):
            chunks = config.LYRIA_PROMPT_CHANGE_DEFAULT_CHUNKS
        chunks = max(1, min(64, chunks))
        # Atomic-enough write: there's only one Lyria session task that
        # ever reads these fields, and asyncio is cooperative, so as long
        # as we set them before .set()-ing the event the receive loop
        # will see a consistent triple.
        state.prompt_change_target = prompt
        state.prompt_change_chunks = chunks
        state.prompt_transition_progress = 0.0
        state.prompt_change_request.set()
        print(
            f"[server] action=change_prompt  prompt={prompt!r} "
            f"chunks={chunks}",
            flush=True,
        )
        await _ack(ws, ok=True, info=f"crossfading over {chunks} chunks")
        return

    if action == "set_threshold":
        # Browser TUNE panel: hot-tune one of the live thresholds the
        # operator can slide mid-demo (blink trigger, jaw trigger,
        # Lyria sensitivity gain). Each key has a distinct allowed
        # range; values are clamped server-side so a malicious /
        # buggy client can't push absurd values that would either
        # peg every signal to the trigger (very low) or never fire
        # (very high).
        #
        # Locked in cloud mode: a single shared visitor must not be
        # able to retune the experience for everyone else. Same
        # rationale as Quit + EEG-mode toggle.
        if state.cloud_mode:
            await _ack(
                ws, ok=False, error="threshold tuning is disabled in cloud mode"
            )
            return
        key = (msg.get("key") or "").strip()
        try:
            value = float(msg.get("value"))
        except (TypeError, ValueError):
            await _ack(ws, ok=False, error=f"value must be a number (got {msg.get('value')!r})")
            return
        # Per-key clamp ranges. Mirrored in static/app.js as the
        # slider min/max so the user can't drag past these either.
        # If you widen the range here remember to widen the slider
        # bounds in JS or the slider will pin at its old max while
        # the server happily accepts the wider value via direct WS.
        ranges = {
            "blink_threshold_uv": (50.0, 4000.0),
            "jaw_threshold_uv": (20.0, 2000.0),
            "lyria_sensitivity_gain": (0.5, 4.0),
        }
        if key not in ranges:
            await _ack(ws, ok=False, error=f"unknown threshold key: {key!r}")
            return
        lo, hi = ranges[key]
        clamped = max(lo, min(hi, value))
        setattr(state, f"live_{key}", clamped)
        print(
            f"[server] action=set_threshold  key={key!r} value={value:.3f} "
            f"-> clamped={clamped:.3f}",
            flush=True,
        )
        await _ack(ws, ok=True)
        return

    if action == "quit":
        # Set the orchestrator's stop event so EVERY task (EEG, Lyria,
        # audio, server, evolver, ...) cancels in unison. We ack BEFORE
        # setting it so the browser sees the response before the WS
        # closes underneath it.
        if state.cloud_mode:
            # In a public deployment, ANY visitor must NOT be able to
            # kill the service for everyone else. Refuse politely.
            await _ack(ws, ok=False, error="quit is disabled in cloud mode")
            return
        await _ack(ws, ok=True)
        if opts.stop_evt is not None:
            print("[server] action=quit -- requesting orchestrator shutdown", flush=True)
            opts.stop_evt.set()
        else:
            print(
                "[server] action=quit ignored: orchestrator stop_evt not "
                "wired (this is a code-side oversight, not a user error)",
                flush=True,
            )
        return

    # Unknown action -- log + ack so the browser can show a toast.
    print(f"[server] unknown WS action: {action!r}", flush=True)
    await _ack(ws, ok=False, error=f"unknown action: {action}")


async def _ack(
    ws: web.WebSocketResponse,
    *,
    ok: bool,
    error: str = "",
    info: str = "",
) -> None:
    """Send a structured ack for an action the client just sent.

    Keeps the protocol tight -- the browser gets a deterministic
    confirmation that its message was processed, and can react to
    failures (e.g., empty prompt) by re-enabling the Start button.
    """
    payload = {"ack": True, "ok": ok}
    if error:
        payload["error"] = error
    if info:
        payload["info"] = info
    try:
        await ws.send_str(json.dumps(payload, separators=(",", ":")))
    except Exception:
        # Client gone in the microsecond between handle_action and ack;
        # the discard in the outer finally will clean up. Silent.
        pass


# ---------------------------------------------------------------------------
# Broadcast task
# ---------------------------------------------------------------------------


async def _broadcast_loop(app: web.Application, state: AppState) -> None:
    """Push state.snapshot() to every connected client at fixed cadence."""
    interval = 1.0 / max(config.SERVER_BROADCAST_HZ, 1.0)
    sent = 0
    drops = 0

    while True:
        await asyncio.sleep(interval)
        clients = _clients(app)
        if not clients:
            continue

        # Snapshot the AppState once per tick. Cheap (it's a dict copy)
        # and gives every client the same instant-in-time view.
        try:
            payload = json.dumps(state.snapshot(), separators=(",", ":"))
        except (TypeError, ValueError) as e:
            # Should be impossible -- snapshot() is JSON-friendly by
            # construction -- but if a future field accidentally adds
            # a non-serializable type, we want the server to keep going.
            print(f"[server] snapshot serialize failed: {e!r}", flush=True)
            continue

        # Iterate a snapshot of the set so the handler's discard() under
        # us during await can't change the iterator.
        for ws in tuple(clients):
            try:
                await ws.send_str(payload)
                sent += 1
            except (ConnectionResetError, RuntimeError) as e:
                # Client gone but didn't run its finally yet, or aiohttp
                # raised on a closed transport. Either way, evict.
                clients.discard(ws)
                drops += 1
                print(
                    f"[server] dropped dead client ({type(e).__name__}); "
                    f"sent={sent} drops={drops}",
                    flush=True,
                )


# ---------------------------------------------------------------------------
# Browser auto-launch
# ---------------------------------------------------------------------------


async def _maybe_open_browser(opts: ServerOptions) -> None:
    """Open Chrome at the local URL after a tiny stabilization delay."""
    if opts.no_browser:
        return
    await asyncio.sleep(config.SERVER_BROWSER_OPEN_DELAY_S)
    url = f"http://localhost:{opts.http_port}/"
    try:
        webbrowser.open(url, new=1, autoraise=True)
        print(f"[server] launched browser at {url}", flush=True)
    except Exception as e:
        # webbrowser is best-effort; missing browsers / sandbox restrictions
        # shouldn't kill the server.
        print(
            f"[server] couldn't auto-launch browser ({e!r}); "
            f"open {url} manually",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Top-level orchestrator task
# ---------------------------------------------------------------------------


def _resolve_static_dir() -> Path:
    """Repo-root-relative `static/` directory (per config.SERVER_STATIC_DIR).

    Walks up from this file (`src/muse2_music_lab/server/app.py`) -> repo
    root, then appends config.SERVER_STATIC_DIR. Resolved once at task
    startup; if the directory doesn't exist we surface a clear error
    rather than letting aiohttp 404 every static request.
    """
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    static_dir = repo_root / config.SERVER_STATIC_DIR
    if not static_dir.is_dir():
        raise FileNotFoundError(
            f"Static directory not found: {static_dir}. "
            "Phase 7 expects static/index.html + static/app.js + "
            "static/style.css to exist (the visualizer files are tracked "
            "in git; only generated assets are gitignored)."
        )
    if not (static_dir / "index.html").is_file():
        raise FileNotFoundError(
            f"Static directory exists but is missing index.html: {static_dir}."
        )
    return static_dir


async def run_server_loop(state: AppState, opts: ServerOptions) -> None:
    """Run the HTTP + WebSocket server until cancelled.

    Failure modes (each surfaces to the orchestrator's task-failed path):
      * Static dir / index.html missing -> FileNotFoundError at startup
      * Port already bound -> OSError with a clear message
      * aiohttp internal error -> propagates as-is

    Clean shutdown: cancellation triggers the `finally` block, which
    closes every connected WebSocket, stops the TCPSite, and tears
    down the AppRunner. Order matters -- closing WS first lets clients
    see a normal close frame instead of a TCP RST.
    """
    static_dir = _resolve_static_dir()

    app = web.Application()
    app[_CLIENTS_KEY] = set()
    app["static_dir"] = static_dir
    # Phase 10: WS handler reads these to dispatch inbound action
    # messages (Start / Recalibrate / Quit).
    app["state"] = state
    app["server_opts"] = opts

    app.router.add_get("/", _index)
    app.router.add_get("/health", _health)
    app.router.add_get(config.SERVER_WS_PATH, _websocket)
    app.router.add_static("/static/", path=static_dir, show_index=False)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    # Cloud mode binds to 0.0.0.0 so PaaS containers (Railway, Fly, etc.)
    # can route external traffic to the listener; local dev keeps localhost
    # so we don't unexpectedly expose a perform session over LAN.
    bind_host = "0.0.0.0" if opts.cloud_mode else "localhost"
    site = web.TCPSite(runner, host=bind_host, port=opts.http_port)

    try:
        try:
            await site.start()
        except OSError as e:
            print(
                f"[server] FAILED to bind {bind_host}:{opts.http_port}: {e}. "
                "Is another perform / dev server already running? "
                "(Use --http-port to pick a different port, or --no-server "
                "to skip this task entirely.)",
                flush=True,
            )
            await runner.cleanup()
            raise

        print(
            f"[server] listening on http://{bind_host}:{opts.http_port}/  "
            f"(ws {config.SERVER_WS_PATH}, broadcast "
            f"{config.SERVER_BROADCAST_HZ:.0f} Hz, cloud_mode={opts.cloud_mode})",
            flush=True,
        )

        broadcaster = asyncio.create_task(
            _broadcast_loop(app, state), name="server-broadcast"
        )
        opener = asyncio.create_task(
            _maybe_open_browser(opts), name="server-open-browser"
        )
        # Always spawn the audio fan-out task. In cloud mode it's the
        # primary playback path; in local mode it carries PCM to the
        # browser for the in-browser recorder (canvas + audio -> WebM).
        # The broadcaster drops chunks when no client is connected, so
        # it's CPU-cheap in the no-recording-no-cloud-visitor case.
        audio_bcaster: "asyncio.Task | None" = asyncio.create_task(
            run_audio_broadcast_loop(app, state),
            name="server-audio-bcast",
        )

        # Park forever: cancellation comes from the orchestrator on SIGINT.
        # The broadcaster task does the actual work; we just hold the
        # site + runner open for it.
        try:
            await asyncio.Event().wait()
        finally:
            # Tear down the inner tasks first; mirrors the lyria session
            # shutdown pattern so we never orphan a child task.
            children = [broadcaster, opener, audio_bcaster]
            for t in children:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*children, return_exceptions=True)

    except asyncio.CancelledError:
        print("[server] cancelled", flush=True)
        raise
    finally:
        # Close connected websockets cleanly so browsers see a normal
        # close frame and the auto-reconnect logic in app.js doesn't
        # spam reconnect attempts during a clean process exit.
        clients = list(_clients(app))
        for ws in clients:
            try:
                await ws.close(code=1001, message=b"server shutdown")
            except Exception:
                pass
        try:
            await site.stop()
        except Exception:
            pass
        try:
            await runner.cleanup()
        except Exception:
            pass
        if not state.tui_active:
            print(f"[server] released (uptime "
                  f"{time.monotonic() - state.session_start_ts:.1f}s)",
                  flush=True)
