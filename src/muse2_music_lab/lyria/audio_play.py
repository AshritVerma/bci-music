"""Drain `state.audio_queue` to sounddevice. Lifecycle-aware.

This is the asyncio task that actually moves bytes to the speakers. The
Lyria session task is the producer; this is the sole consumer.

Design choices:

  * `sd.RawOutputStream` -- speakerphone-quality stereo s16 PCM at 48 kHz,
    `latency='low'`, `blocksize=0` (let PortAudio pick). Validated by
    the Phase 2 smoke script.

  * `stream.write(...)` is BLOCKING in PortAudio. We can't call it on the
    asyncio loop directly without stalling everything else, so we hand
    each chunk to the default executor. PortAudio's internal ring buffer
    handles backpressure: write blocks if the device queue is full,
    which naturally pulls the pull-from-asyncio-Queue cadence in line
    with real-time audio output.

  * On startup we skip an initial `LYRIA_INITIAL_BUFFER_S` worth of
    chunks -- gives PortAudio a moment to spin up and fill its internal
    buffer before the first write, eliminating the click/glitch that
    happens if you hand it a chunk the millisecond it opens.

  * On cancellation the executor write may still be in flight; we don't
    interrupt it (PortAudio will swallow the partial chunk gracefully on
    `stream.stop()`). The shutdown path stops + closes the stream from
    a finally block, mirroring the smoke script.
"""

from __future__ import annotations

import asyncio
import time

import sounddevice as sd

from muse2_music_lab import config
from muse2_music_lab.state import AppState


async def run_audio_playback_loop(state: AppState) -> None:
    """Open a sounddevice OutputStream and pump state.audio_queue into it."""
    # Phase 10: don't open the audio device until the user clicks Start.
    # Otherwise PortAudio holds the speaker awake (and on macOS, takes
    # focus from any music the user happens to be listening to while
    # they read the prompt UI).
    await state.start_requested.wait()
    loop = asyncio.get_running_loop()
    stream = sd.RawOutputStream(
        samplerate=config.LYRIA_SAMPLE_RATE,
        channels=config.LYRIA_CHANNELS,
        dtype=config.LYRIA_DTYPE,
        blocksize=0,
        latency="low",
    )

    chunks_played = 0
    bytes_played = 0
    play_start_ts: float | None = None
    first_chunk_seen = False

    try:
        # Open the stream BEFORE we block on the first chunk so PortAudio
        # has time to spin up the audio device thread. Without this, the
        # first stream.write() after a cold start can take ~50ms longer
        # than the audio it's writing, producing an immediate underrun.
        stream.start()
        print(
            f"[audio-play] opened sounddevice stream: "
            f"{config.LYRIA_SAMPLE_RATE} Hz, "
            f"{config.LYRIA_CHANNELS} ch, {config.LYRIA_DTYPE}",
            flush=True,
        )

        while True:
            chunk = await state.audio_queue.get()
            try:
                if not chunk:
                    # Empty bytes is a valid sentinel from the producer to
                    # mean "I have nothing to send right now" -- skip.
                    continue

                if not first_chunk_seen:
                    first_chunk_seen = True
                    play_start_ts = time.monotonic()
                    print(
                        f"[audio-play] first chunk arrived "
                        f"({len(chunk)} bytes) -- "
                        f"buffering for {config.LYRIA_INITIAL_BUFFER_S:.1f}s "
                        "before playback",
                        flush=True,
                    )
                    # Pre-roll: let PortAudio fill its internal buffer before
                    # we start handing it chunks at real-time pace. Same trick
                    # the smoke script + cookbook use.
                    await asyncio.sleep(config.LYRIA_INITIAL_BUFFER_S)
                    print("[audio-play] streaming to speakers", flush=True)

                # Write is blocking in PortAudio; offload so the event loop
                # stays responsive (Lyria producer + EEG ticks + state logger
                # all need cycles).
                try:
                    await loop.run_in_executor(None, stream.write, chunk)
                except sd.PortAudioError as e:
                    # Device-level error (cable yanked, sample rate mismatch,
                    # etc). Best to fail loud so the orchestrator can shut
                    # the rest of the pipeline down -- silent audio with a
                    # running Lyria session is the worst possible state.
                    print(
                        f"[audio-play] PortAudio error: {e}",
                        flush=True,
                    )
                    raise

                chunks_played += 1
                bytes_played += len(chunk)
            finally:
                state.audio_queue.task_done()

    except asyncio.CancelledError:
        print(
            f"[audio-play] cancelled after {chunks_played} chunks "
            f"({bytes_played / 1024:.1f} KB)",
            flush=True,
        )
        raise
    finally:
        # Stop + close idempotent; safe to call even if start failed above.
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        if play_start_ts is not None:
            elapsed = time.monotonic() - play_start_ts
            kbps = (bytes_played * 8 / 1000) / max(elapsed, 1e-6)
            print(
                f"[audio-play] released. avg rate: {kbps:.0f} kbit/s "
                f"(target ~1536 = 48000 * 2ch * 16bit)",
                flush=True,
            )
        else:
            print("[audio-play] released (no audio ever played).", flush=True)
