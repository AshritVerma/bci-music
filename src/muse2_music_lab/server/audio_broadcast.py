"""Broadcast Lyria PCM chunks to every connected WebSocket client.

Used in `--cloud` mode (Railway / any PaaS) where there's no local
audio device. The Lyria session tees its chunks into
`state.audio_broadcast_queue`; this task drains that queue and ships
each chunk as a binary WebSocket frame to every browser currently in
the broadcast set.

Protocol (matches static/audio.js):

    Server -> client (one-shot, on connect):
        TEXT  {"type": "audio_init", "sample_rate": 48000,
               "channels": 2, "format": "s16le"}

    Server -> client (continuous, while Lyria is producing):
        BINARY  raw little-endian int16 PCM bytes (interleaved stereo)

The BINARY frame body is exactly what Lyria sent us -- no re-encoding,
no header. The browser already knows the format from the audio_init
message it received on connect.

Why a separate task and not a method on the existing broadcast loop:
the JSON state broadcaster is small and CPU-cheap; the audio fanout
moves ~200 KB/s per client. Keeping them separate lets each have its
own backpressure semantics (lossy drop for audio, never-drop for state).

Failure model: a single client failing to receive a frame doesn't kill
the broadcast or affect other clients -- we just discard the bad client
from the broadcast set, same way the JSON broadcaster does.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from muse2_music_lab import config
from muse2_music_lab.state import AppState

_CLIENTS_KEY = "muse2_clients"


def _clients(app: web.Application) -> set[web.WebSocketResponse]:
    return app[_CLIENTS_KEY]


async def run_audio_broadcast_loop(
    app: web.Application,
    state: AppState,
) -> None:
    """Drain state.audio_broadcast_queue and fan out each chunk as a binary WS frame.

    Runs forever (until cancelled). Drops chunks when there are no
    clients connected so memory doesn't grow during quiet periods.
    """
    chunks_sent = 0
    bytes_sent = 0
    drops = 0

    print(
        "[audio-bcast] started (waits on state.audio_broadcast_queue; "
        "fans each chunk to every connected WS client)",
        flush=True,
    )

    try:
        while True:
            chunk = await state.audio_broadcast_queue.get()
            try:
                if not chunk:
                    continue

                clients = _clients(app)
                if not clients:
                    # No one to send to. Discard rather than buffer --
                    # otherwise the queue would fill and the producer
                    # (Lyria session) would back-pressure on a sink that
                    # has no real readers anyway. The visitor who connects
                    # next will start hearing audio from "now", not from
                    # the start of the session.
                    drops += 1
                    continue

                # Iterate a snapshot of the set so a disconnect during
                # send doesn't mutate the iterator under us.
                for ws in tuple(clients):
                    try:
                        await ws.send_bytes(chunk)
                    except (ConnectionResetError, RuntimeError):
                        # Dead client; the WebSocket handler will run its
                        # finally and remove this from the set, but we
                        # also discard here so we don't try sending the
                        # next chunk to it before that runs.
                        clients.discard(ws)

                chunks_sent += 1
                bytes_sent += len(chunk)
            finally:
                state.audio_broadcast_queue.task_done()

    except asyncio.CancelledError:
        kb = bytes_sent / 1024.0
        print(
            f"[audio-bcast] cancelled after {chunks_sent} chunks "
            f"({kb:.1f} KB sent, {drops} dropped due to zero clients)",
            flush=True,
        )
        raise


def audio_init_message(*, playback: bool) -> str:
    """Return the JSON 'audio_init' header to send to a client on connect.

    Tells the browser how to interpret the binary frames that follow
    (Lyria's fixed 48 kHz / 2 ch / s16 little-endian interleaved
    format) AND whether the browser should play those frames or merely
    capture them silently for the in-browser MediaRecorder.

    `playback`:
      * True  -> cloud / public-deploy mode: there's no host-side
                 sounddevice, the browser IS the speaker. audio.js
                 wires the AudioContext to ctx.destination.
      * False -> local mode: sounddevice on the host plays the audio.
                 The browser still needs the PCM stream to feed the
                 in-browser recorder (which combines it with the
                 canvas video into one WebM file), but it must NOT
                 play it through speakers or the user hears double.
    """
    import json
    return json.dumps(
        {
            "type": "audio_init",
            "sample_rate": config.LYRIA_SAMPLE_RATE,
            "channels": config.LYRIA_CHANNELS,
            "format": "s16le",
            "playback": bool(playback),
        },
        separators=(",", ":"),
    )
