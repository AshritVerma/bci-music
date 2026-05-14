"""Phase 7 server subpackage.

Exposes the aiohttp HTTP + WebSocket server that broadcasts
state.snapshot() to the browser visualizer, plus (in --cloud mode)
the binary-WS audio broadcaster that fans Lyria PCM out to every
connected browser instead of a local sounddevice.
"""

from muse2_music_lab.server.app import ServerOptions, run_server_loop
from muse2_music_lab.server.audio_broadcast import (
    audio_init_message,
    run_audio_broadcast_loop,
)

__all__ = [
    "ServerOptions",
    "run_server_loop",
    "audio_init_message",
    "run_audio_broadcast_loop",
]
