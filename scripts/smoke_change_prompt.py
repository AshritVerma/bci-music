"""End-to-end smoke for the mid-session change_prompt feature.

Flow:
  1. Connect to the local perform WebSocket (caller starts the perform
     process separately, e.g. `muse2 perform --simulate-eeg --no-browser
     --no-tui --no-evolve` in another terminal -- we don't spawn it
     here so the user keeps interactive control of the run).
  2. Send a `start` action with an initial prompt.
  3. Wait for `lyria_ready` to flip true in a state snapshot.
  4. Send a `change_prompt` action with a 4-chunk crossfade.
  5. Watch the snapshots: confirm `prompt_transition_progress` ramps
     0.25 -> 0.5 -> 0.75 -> 1.0 then resets to 0.0, and that
     `prompt_change_target` is set during the ramp and clears at end.
  6. Print PASS / FAIL summary.

Run:
    python scripts/smoke_change_prompt.py [--port 8000] [--start-prompt "..."] [--target-prompt "..."]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

import aiohttp


async def _drain_ack(ws: aiohttp.ClientWebSocketResponse, kind: str) -> dict:
    """Wait for and return the next {'ack': True, ...} message."""
    while True:
        msg = await ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        if data.get("ack") is True:
            print(f"[smoke] ack({kind}): {data}", flush=True)
            return data


async def _wait_until(
    ws: aiohttp.ClientWebSocketResponse,
    pred,
    label: str,
    timeout_s: float,
) -> dict:
    """Drain snapshots until pred(state) is True or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"timeout waiting for {label} after {timeout_s:.1f}s")
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            raise TimeoutError(f"timeout waiting for {label} after {timeout_s:.1f}s")
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        if data.get("ack") is True:
            continue
        if pred(data):
            return data


async def _watch_transition(
    ws: aiohttp.ClientWebSocketResponse,
    target: str,
    timeout_s: float,
) -> list[float]:
    """Collect prompt_transition_progress samples until the badge clears.

    Returns the unique progress samples observed (in arrival order).

    A healthy ramp looks like e.g. [0.25, 0.5, 0.75, 0.0] -- note that
    the engine sets progress=1.0 then immediately resets to 0.0 in the
    same coroutine without yielding, so the broadcast loop NEVER
    snapshots a 1.0 (and that's correct -- the user-visible state is
    "transitioning" then "done", with no observable "100%" plateau).
    Finalization is therefore detected by observing progress drop back
    to 0.0 AFTER having seen at least one positive value, with the
    snapshot's `prompt` field now equal to the requested target.
    """
    seen: list[float] = []
    last = -1.0
    saw_positive = False
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(
                f"transition didn't finalize within {timeout_s:.1f}s"
            )
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"transition didn't finalize within {timeout_s:.1f}s"
            )
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        if data.get("ack") is True:
            continue
        progress = data.get("prompt_transition_progress")
        if progress is None:
            continue
        if progress != last:
            seen.append(progress)
            print(
                f"[smoke] progress={progress:.2f}  target={data.get('prompt_change_target')!r}  "
                f"prompt={data.get('prompt')[:40]!r}",
                flush=True,
            )
            last = progress
        if progress > 0:
            saw_positive = True
            continue
        if progress == 0.0 and saw_positive:
            # Finalize: progress dropped from positive back to 0 AND
            # state.prompt should now match the requested target.
            if data.get("prompt") == target:
                print("[smoke] finalize confirmed: state.prompt now = target", flush=True)
                return seen
            else:
                print(
                    f"[smoke] WARN: progress=0 but state.prompt is "
                    f"{data.get('prompt')!r} (expected {target!r})",
                    flush=True,
                )
                return seen


async def main(port: int, start_prompt: str, target_prompt: str, chunks: int) -> int:
    url = f"ws://localhost:{port}/ws"
    print(f"[smoke] connecting to {url} ...", flush=True)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, heartbeat=15) as ws:
            print("[smoke] connected", flush=True)

            print(f"[smoke] -> start  prompt={start_prompt!r}", flush=True)
            await ws.send_json({"action": "start", "prompt": start_prompt})
            await _drain_ack(ws, "start")

            print("[smoke] waiting for lyria_ready ...", flush=True)
            await _wait_until(
                ws,
                lambda s: bool(s.get("lyria_ready")),
                "lyria_ready",
                timeout_s=60.0,
            )
            print("[smoke] lyria_ready -> sending change_prompt", flush=True)

            await ws.send_json(
                {"action": "change_prompt", "prompt": target_prompt, "chunks": chunks}
            )
            ack = await _drain_ack(ws, "change_prompt")
            if not ack.get("ok"):
                print(f"[smoke] FAIL: change_prompt rejected: {ack}", flush=True)
                return 2

            print(
                f"[smoke] watching transition ({chunks} chunks; expect ~{chunks * 2}s)",
                flush=True,
            )
            try:
                samples = await _watch_transition(
                    ws, target=target_prompt, timeout_s=chunks * 6.0 + 30.0
                )
            except TimeoutError as e:
                print(f"[smoke] FAIL: {e}", flush=True)
                return 3

            print(f"[smoke] progress samples: {samples}", flush=True)

            # Validation: should have seen at least chunks-1 distinct
            # positive progress values (one per ramp step before
            # finalization), and the final value should be 0.0.
            positive = [p for p in samples if p > 0]
            if len(positive) < chunks - 1:
                print(
                    f"[smoke] FAIL: only {len(positive)} positive progress "
                    f"values (expected at least {chunks - 1})",
                    flush=True,
                )
                return 4
            if samples[-1] != 0.0:
                print(
                    f"[smoke] FAIL: last progress sample is "
                    f"{samples[-1]} (expected 0.0)",
                    flush=True,
                )
                return 5
            print(
                f"[smoke] PASS: crossfade ramped through {positive} "
                "and finalized cleanly",
                flush=True,
            )
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--start-prompt",
        default="warm ambient drone with crystalline texture",
    )
    p.add_argument(
        "--target-prompt",
        default="bright minimal techno, hypnotic 16th-note bassline",
    )
    p.add_argument("--chunks", type=int, default=4)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    rc = asyncio.run(main(args.port, args.start_prompt, args.target_prompt, args.chunks))
    sys.exit(rc)
