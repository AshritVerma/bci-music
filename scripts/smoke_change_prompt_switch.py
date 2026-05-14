"""End-to-end smoke for mid-flight target switching (latest-wins).

Sends an initial change_prompt, then 3 seconds later sends a SECOND
change_prompt with a different target. Expects:
  * The first transition's progress visibly resets (back to 0.25)
  * The final state.prompt is the SECOND target
  * The server log emits a "mid-transition target switch" line

Usage: same as smoke_change_prompt.py; assumes a perform process is
already running on localhost:8000.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import aiohttp


async def _drain_ack(ws, kind: str) -> dict:
    while True:
        msg = await ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        if data.get("ack") is True:
            print(f"[smoke] ack({kind}): {data}", flush=True)
            return data


async def _wait_until(ws, pred, label: str, timeout_s: float) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"timeout waiting for {label}")
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            raise TimeoutError(f"timeout waiting for {label}")
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        if data.get("ack") is True:
            continue
        if pred(data):
            return data


async def main(port: int) -> int:
    start_prompt = "warm ambient drone with crystalline texture"
    target_a = "bright minimal techno, hypnotic 16th-note bassline"
    target_b = "dreamy lo-fi hip-hop with vinyl crackle, mellow rhodes piano"

    url = f"ws://localhost:{port}/ws"
    print(f"[smoke] connecting to {url} ...", flush=True)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, heartbeat=15) as ws:
            print("[smoke] connected", flush=True)

            await ws.send_json({"action": "start", "prompt": start_prompt})
            await _drain_ack(ws, "start")

            print("[smoke] waiting for lyria_ready ...", flush=True)
            await _wait_until(ws, lambda s: bool(s.get("lyria_ready")), "lyria_ready", timeout_s=60)

            # Kick off transition A with 8 chunks (~16s window).
            print(f"[smoke] -> change_prompt A: {target_a!r}", flush=True)
            await ws.send_json({"action": "change_prompt", "prompt": target_a, "chunks": 8})
            await _drain_ack(ws, "change_prompt(A)")

            # Wait for progress to clear past 0 (transition has started).
            print("[smoke] waiting for transition A to start...", flush=True)
            await _wait_until(
                ws,
                lambda s: (s.get("prompt_transition_progress") or 0) > 0,
                "transition A start",
                timeout_s=10.0,
            )
            await asyncio.sleep(3.5)  # let A advance to ~step 2/8

            # Switch to target B mid-transition.
            print(f"[smoke] -> change_prompt B (mid-flight switch): {target_b!r}", flush=True)
            await ws.send_json({"action": "change_prompt", "prompt": target_b, "chunks": 4})
            await _drain_ack(ws, "change_prompt(B)")

            # Watch progress + target until finalized.
            seen_targets: list[str] = []
            seen_progress: list[float] = []
            last_t = None
            last_p = -1.0
            saw_b = False
            saw_b_positive = False
            deadline = asyncio.get_event_loop().time() + 30.0
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    print("[smoke] FAIL: never finalized", flush=True)
                    return 2
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    print("[smoke] FAIL: never finalized", flush=True)
                    return 2
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("ack") is True:
                    continue
                t = data.get("prompt_change_target") or ""
                p = data.get("prompt_transition_progress") or 0.0
                if t != last_t or p != last_p:
                    print(
                        f"[smoke] progress={p:.2f}  target={t!r}  "
                        f"prompt={(data.get('prompt') or '')[:36]!r}",
                        flush=True,
                    )
                    if t and t != last_t:
                        seen_targets.append(t)
                    seen_progress.append(p)
                    last_t = t
                    last_p = p
                if t == target_b:
                    saw_b = True
                    if p > 0:
                        saw_b_positive = True
                if p == 0 and saw_b_positive and data.get("prompt") == target_b:
                    print("[smoke] finalize: state.prompt == target_b", flush=True)
                    break

            if not saw_b:
                print(f"[smoke] FAIL: never saw target_b in snapshots", flush=True)
                return 3
            if target_a not in seen_targets:
                print(f"[smoke] FAIL: never saw target_a in snapshots", flush=True)
                return 4
            print(f"[smoke] PASS: latest-wins switch worked, targets={seen_targets}", flush=True)
    return 0


def _parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8000)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(asyncio.run(main(args.port)))
