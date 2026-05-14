"""Lyria RealTime integration for the `perform` orchestrator.

Three responsibilities, one per submodule:

  * `mapping`       -- pure functions translating AppState (alpha/beta/theta/
                       asymmetry in [0, 1]) into Lyria's LiveMusicGenerationConfig.
                       Tested in isolation; no I/O.

  * `session`       -- asyncio task that opens a Lyria WebSocket, primes it
                       with the user's prompt, kicks off generation, and
                       on every state.eeg_tick pushes a fresh config based
                       on the current AppState. Audio chunks coming back
                       from Lyria are pushed to state.audio_queue.

  * `audio_play`    -- asyncio task that drains state.audio_queue to
                       sounddevice for actual speaker output.

The two tasks are siblings under the perform orchestrator, communicating
only via AppState. Either can be replaced or stubbed independently.
"""

from muse2_music_lab.lyria.audio_play import run_audio_playback_loop
from muse2_music_lab.lyria.mapping import state_to_lyria_config
from muse2_music_lab.lyria.session import run_lyria_loop

__all__ = [
    "run_lyria_loop",
    "run_audio_playback_loop",
    "state_to_lyria_config",
]
