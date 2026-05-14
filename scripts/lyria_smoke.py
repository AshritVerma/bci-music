#!/usr/bin/env python3
"""Lyria RealTime smoke test (Phase 2 verify).

Validates the highest-risk external dependency for the new pipeline before
we touch any orchestrator code:

  * GEMINI_API_KEY loads from .env
  * google-genai (>=2.2) connects to `models/lyria-realtime-exp`
  * PCM chunks arrive over the WebSocket and play through sounddevice
  * Mid-stream `set_music_generation_config()` calls audibly change the music
  * No 429s, auth errors, or audio underruns

Run:
    python scripts/lyria_smoke.py --prompt "downtempo electronic with warm analog synth pads"

Defaults:
    duration  = 30 s
    prompt    = "downtempo electronic with warm analog synth pads"

Audible expectations (these are the verify gates only you can confirm):
    +0s   music starts; warm, mid-tempo
    +10s  brightness 0.5 -> 1.0   (treble lift, more presence)
    +20s  brightness 0.2, density 0.9   (dark + thick)
    +30s  clean exit

Lyria streams 48 kHz stereo little-endian s16 PCM. We read the mime_type
off the first chunk to verify and warn loudly if the SDK ever changes
that and silently corrupts audio.

Architecture follows google-gemini/cookbook/quickstarts/Get_started_LyriaRealTime.py:
the receive loop runs as its own asyncio Task in parallel with a control
task that fires the mid-stream config updates. A top-level asyncio.wait_for
enforces the duration timeout even if the server stops yielding messages.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from contextlib import suppress
from typing import Optional

import sounddevice as sd
from dotenv import load_dotenv
from google import genai
from google.genai import types

from muse2_music_lab.music import PromptGuard


MODEL_ID = "models/lyria-realtime-exp"

LYRIA_SAMPLE_RATE = 48000
LYRIA_CHANNELS = 2
LYRIA_DTYPE = "int16"
EXPECTED_MIME_PREFIX = "audio/l16"

# Buffer the first chunks for a moment before playing so network jitter
# doesn't cause an underrun on the very first frame. Cookbook uses 1s.
INITIAL_BUFFER_S = 1.0


class _Stats:
    """Mutable counters shared across the receive / control / main tasks."""

    def __init__(self) -> None:
        self.chunks_received = 0
        self.bytes_received = 0
        self.underruns = 0
        self.first_chunk_ts: Optional[float] = None
        self.first_mime: Optional[str] = None
        self.fired: list[tuple[float, dict[str, float]]] = []
        self.filtered_prompts: list[str] = []
        self.unknown_messages = 0
        # Prompt-guard state (filled in by run() before the receive loop
        # starts). active_prompt is the current prompt Lyria is generating
        # against; it gets replaced by the guard rewrite on a filter event.
        self.active_prompt: str = ""
        self.rewrites_used: int = 0
        self.rewrites_max: int = 1


async def _receive_loop(
    session,
    stats: _Stats,
    stream: Optional[sd.RawOutputStream],
    start: float,
    guard: Optional[PromptGuard],
) -> None:
    """Drain server messages, push audio bytes to sounddevice.

    On a `filtered_prompt` event from Lyria, hand the original prompt
    (and Google's filter reason) to the PromptGuard, push the rewrite
    back into the same session via `set_weighted_prompts`, and keep
    listening. Bails after `stats.rewrites_max` rewrites to prevent
    infinite loops if the rewrite itself keeps getting filtered.
    """
    async for message in session.receive():
        if message.filtered_prompt is not None:
            fp = message.filtered_prompt
            stats.filtered_prompts.append(repr(fp))
            reason = getattr(fp, "filtered_reason", None)

            if guard is None or stats.rewrites_used >= stats.rewrites_max:
                # Either rewrites are disabled or we already burned the
                # retry budget. Surface a clear log and let the receive
                # loop end naturally (no audio is coming).
                print(
                    f"[smoke] FILTERED prompt (no rewrite available): {fp}",
                    flush=True,
                )
                break

            try:
                result = await guard.rewrite(stats.active_prompt, reason=reason)
            except Exception as e:
                # Anthropic errors (auth, quota, network) bubble out so
                # the main task's cleanup path tears down cleanly with
                # exit code 3 preserved.
                print(f"[smoke] [prompt-guard] FAIL: {e!r}", flush=True)
                raise

            stats.rewrites_used += 1
            stats.active_prompt = result.rewritten

            await session.set_weighted_prompts(
                prompts=[types.WeightedPrompt(text=result.rewritten, weight=1.0)]
            )
            print(
                f"[smoke] pushed rewrite to Lyria session "
                f"(attempt {stats.rewrites_used}/{stats.rewrites_max})",
                flush=True,
            )
            # Don't fall through to the audio-chunk branch; this message
            # carried no audio.
            await asyncio.sleep(0)
            continue

        if message.server_content and message.server_content.audio_chunks:
            for chunk in message.server_content.audio_chunks:
                data = chunk.data
                if not data:
                    continue
                if stats.first_chunk_ts is None:
                    stats.first_chunk_ts = time.monotonic()
                    stats.first_mime = chunk.mime_type or "(unset)"
                    latency = stats.first_chunk_ts - start
                    print(
                        f"[smoke] FIRST chunk at +{latency:.2f}s",
                        flush=True,
                    )
                    print(f"[smoke]   mime_type:   {stats.first_mime}")
                    print(f"[smoke]   chunk bytes: {len(data)}")
                    if not stats.first_mime.lower().startswith(EXPECTED_MIME_PREFIX):
                        print(
                            f"[smoke]   WARNING: unexpected mime "
                            f"{stats.first_mime!r}; expected something starting "
                            f"with {EXPECTED_MIME_PREFIX!r}. sounddevice is "
                            f"configured for s16le PCM and may produce noise.",
                            flush=True,
                        )
                    if stream is not None:
                        # Cookbook trick: brief pause on first chunk so PortAudio
                        # has time to build its initial buffer before we start
                        # streaming chunks at real-time pace.
                        await asyncio.sleep(INITIAL_BUFFER_S)

                stats.chunks_received += 1
                stats.bytes_received += len(data)

                if stream is not None:
                    try:
                        stream.write(data)
                    except sd.PortAudioError as e:
                        stats.underruns += 1
                        print(
                            f"[smoke] sounddevice error #{stats.underruns}: {e}",
                            flush=True,
                        )
        elif message.filtered_prompt is None:
            stats.unknown_messages += 1
            if stats.unknown_messages <= 3:
                print(f"[smoke] non-audio message: {message}", flush=True)

        # Yield to the control task so its config-change pushes can land
        # promptly even when we're hot-looping on tiny audio chunks.
        await asyncio.sleep(0)


async def _control_loop(session, stats: _Stats) -> None:
    """Fire two mid-stream config changes that prove updates take effect."""
    await asyncio.sleep(10.0)
    await session.set_music_generation_config(
        config=types.LiveMusicGenerationConfig(brightness=1.0)
    )
    stats.fired.append((time.monotonic(), {"brightness": 1.0}))
    print("[smoke] +10s  set_music_generation_config(brightness=1.0)", flush=True)

    await asyncio.sleep(10.0)
    await session.set_music_generation_config(
        config=types.LiveMusicGenerationConfig(brightness=0.2, density=0.9)
    )
    stats.fired.append((time.monotonic(), {"brightness": 0.2, "density": 0.9}))
    print(
        "[smoke] +20s  set_music_generation_config(brightness=0.2, density=0.9)",
        flush=True,
    )


async def run(
    prompt: str,
    duration_s: float,
    no_audio: bool,
    max_rewrites: int,
) -> int:
    load_dotenv()
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        print(
            "[smoke] FAIL: GEMINI_API_KEY missing. Drop it in .env at the repo root.",
            file=sys.stderr,
        )
        return 2

    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    guard: Optional[PromptGuard] = None
    if anthropic_key:
        guard = PromptGuard(anthropic_key)
    else:
        # Not fatal -- the smoke test still proves the Lyria path. Just
        # warn loudly so an unflagged prompt run doesn't silently lose
        # the rewrite safety net.
        print(
            "[smoke] WARN: ANTHROPIC_API_KEY missing -- prompt-guard "
            "rewrite layer disabled. Filtered prompts will fail hard.",
            file=sys.stderr,
        )

    print(f"[smoke] model:    {MODEL_ID}")
    print(f"[smoke] prompt:   {prompt!r}")
    print(f"[smoke] duration: {duration_s:.0f}s")
    print(f"[smoke] no_audio: {no_audio}")
    print(f"[smoke] api_key:  ...{api_key[-4:]} ({len(api_key)} chars)")
    if guard is not None:
        print(
            f"[smoke] guard:    claude-opus-4-7 enabled "
            f"(max rewrites: {max_rewrites})"
        )

    client = genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1alpha"},
    )

    stream: Optional[sd.RawOutputStream] = None
    if not no_audio:
        stream = sd.RawOutputStream(
            samplerate=LYRIA_SAMPLE_RATE,
            channels=LYRIA_CHANNELS,
            dtype=LYRIA_DTYPE,
            blocksize=0,
            latency="low",
        )
        stream.start()
        print(
            f"[smoke] sounddevice OutputStream open: "
            f"{LYRIA_SAMPLE_RATE} Hz, {LYRIA_CHANNELS} ch, {LYRIA_DTYPE}"
        )

    stats = _Stats()
    stats.active_prompt = prompt
    stats.rewrites_max = max_rewrites
    exit_code = 0

    try:
        async with client.aio.live.music.connect(model=MODEL_ID) as session:
            print("[smoke] WebSocket session connected", flush=True)

            # Cookbook order: prompts -> config -> play -> THEN spawn tasks.
            await session.set_weighted_prompts(
                prompts=[types.WeightedPrompt(text=prompt, weight=1.0)]
            )
            await session.set_music_generation_config(
                config=types.LiveMusicGenerationConfig(
                    temperature=1.0,
                    bpm=80,
                    density=0.5,
                    brightness=0.5,
                )
            )
            await session.play()
            start = time.monotonic()
            print("[smoke] play() called -- streaming...", flush=True)

            recv_task = asyncio.create_task(
                _receive_loop(session, stats, stream, start, guard)
            )
            ctrl_task = asyncio.create_task(_control_loop(session, stats))

            try:
                # Hard duration cap. wait_for cancels the receive task if the
                # server never yields messages, so the test always exits.
                await asyncio.wait_for(recv_task, timeout=duration_s)
            except asyncio.TimeoutError:
                pass
            finally:
                ctrl_task.cancel()
                recv_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await ctrl_task
                with suppress(asyncio.CancelledError, Exception):
                    await recv_task
                with suppress(Exception):
                    await session.stop()
    finally:
        if stream is not None:
            stream.stop()
            stream.close()

    print()
    print("[smoke] DONE")
    print(f"[smoke]   chunks received: {stats.chunks_received}")
    print(f"[smoke]   bytes received:  {stats.bytes_received}")
    if stats.first_chunk_ts is not None and stats.chunks_received > 0:
        elapsed = time.monotonic() - stats.first_chunk_ts
        kbps = (stats.bytes_received * 8 / 1000) / max(elapsed, 1e-6)
        print(f"[smoke]   avg rate:        {kbps:.1f} kbit/s (expected ~1536)")
    print(f"[smoke]   config changes:  {len(stats.fired)}")
    print(f"[smoke]   underruns:       {stats.underruns}")
    print(f"[smoke]   filtered:        {len(stats.filtered_prompts)}")
    print(f"[smoke]   rewrites used:   {stats.rewrites_used}/{stats.rewrites_max}")
    if stats.rewrites_used > 0:
        print(f"[smoke]   final prompt:    {stats.active_prompt!r}")
    print(f"[smoke]   unknown msgs:    {stats.unknown_messages}")

    if stats.chunks_received == 0:
        print("[smoke] FAIL: no audio chunks ever arrived.", file=sys.stderr)
        exit_code = 3
    elif len(stats.fired) < 2 and duration_s >= 22:
        print(
            f"[smoke] WARN: only {len(stats.fired)}/2 mid-stream config changes fired.",
            file=sys.stderr,
        )

    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(
        description="Lyria RealTime smoke test for Phase 2 verify.",
    )
    p.add_argument(
        "--prompt",
        default="downtempo electronic with warm analog synth pads",
        help="Single weighted prompt sent at session start.",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Seconds to play before stopping (default: 30).",
    )
    p.add_argument(
        "--no-audio",
        action="store_true",
        dest="no_audio",
        help=(
            "Skip sounddevice playback (validates auth + schema + chunk arrival "
            "without speakers; useful for headless / CI checks)."
        ),
    )
    p.add_argument(
        "--max-rewrites",
        type=int,
        default=1,
        dest="max_rewrites",
        help=(
            "Retry budget for the prompt-guard rewrite layer. If Lyria "
            "filters the prompt, Claude Opus rewrites it and we resubmit "
            "up to this many times (default: 1)."
        ),
    )
    args = p.parse_args()
    try:
        return asyncio.run(
            run(args.prompt, args.duration, args.no_audio, args.max_rewrites)
        )
    except KeyboardInterrupt:
        print("\n[smoke] interrupted by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
