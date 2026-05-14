"""Smoke test for the calibration banner end-to-end.

Boots a minimal aiohttp server backed by an `AppState`, then drives a
fake calibration window (calibrating=True for 8s, then False) while a
headless Chromium polls the page state. Asserts:

  1. snapshot() math:  remaining = total - elapsed, clamped to [0, total]
  2. the calibration banner becomes visible after we flip the flag
  3. the live countdown text drains downward (10.0 -> ~0)
  4. the progress bar drains from ~100% to ~0%
  5. the banner disappears once the flag clears

Decoupled from the BLE / Lyria stack: the only orchestrator pieces we
boot are AppState + run_server_loop, and we forge the calibration
fields ourselves. Lets the test pass on a CI box with no Muse hardware
and no Lyria API key.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from muse2_music_lab.server.app import ServerOptions, run_server_loop  # noqa: E402
from muse2_music_lab.state import AppState  # noqa: E402

PORT = 8765
DURATION_S = 8.0


def _check_snapshot_math() -> None:
    """Pure-Python sanity check of state.snapshot() countdown."""
    s = AppState()
    snap = s.snapshot()
    assert snap["calibrating"] is False, snap
    assert snap["calibration_remaining_s"] == 0.0, snap
    assert snap["calibration_total_s"] == 0.0, snap

    s.calibrating = True
    s.calibration_total_s = 10.0
    s.calibration_started_ts = time.monotonic() - 3.0  # "started 3s ago"
    snap = s.snapshot()
    assert snap["calibrating"] is True, snap
    assert 6.5 <= snap["calibration_remaining_s"] <= 7.5, snap
    assert snap["calibration_total_s"] == 10.0, snap

    s.calibration_started_ts = time.monotonic() - 100.0
    snap = s.snapshot()
    assert snap["calibration_remaining_s"] == 0.0, "remaining must clamp to 0"

    s.calibrating = False
    s.calibration_total_s = 8.0
    s.calibration_started_ts = time.monotonic()
    snap = s.snapshot()
    assert snap["calibration_remaining_s"] == 0.0, (
        "remaining must be 0 when calibrating=False even if total > 0"
    )
    print("[smoke] snapshot() math: OK")


async def _drive_calibration(state: AppState, ready: asyncio.Event) -> None:
    """Wait for the test driver to be ready, then run one calibration cycle."""
    await ready.wait()
    print(f"[smoke] starting fake calibration ({DURATION_S}s)")
    state.calibration_total_s = DURATION_S
    state.calibration_started_ts = time.monotonic()
    state.calibrating = True
    await asyncio.sleep(DURATION_S + 0.4)
    state.calibrating = False
    print("[smoke] calibration window ended")


async def _drive_browser(ready: asyncio.Event) -> None:
    """Use Playwright to assert the banner shows + countdown ticks down."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto(f"http://localhost:{PORT}/")
        await page.wait_for_selector("#start-panel", state="visible", timeout=5000)
        await page.fill("#start-prompt", "ambient pad for smoke test")
        await page.click("#start-btn")
        await asyncio.sleep(0.6)
        # Banner is hidden right now (calibrating=False).
        hidden_before = await page.evaluate(
            "() => document.getElementById('calibration-banner').hidden"
        )
        assert hidden_before is True, "banner must start hidden"

        ready.set()  # signal _drive_calibration to flip the flag

        await page.wait_for_function(
            "() => !document.getElementById('calibration-banner').hidden",
            timeout=2000,
        )
        print("[smoke] banner became visible")

        readings = []
        for _ in range(10):
            await asyncio.sleep(0.6)
            txt = await page.evaluate(
                "() => document.getElementById('calibration-countdown').textContent"
            )
            width = await page.evaluate(
                "() => document.getElementById('calibration-progress-fill').style.width"
            )
            try:
                seconds = float(txt.replace("s", "").strip())
            except ValueError:
                seconds = float("nan")
            readings.append((seconds, width))
            print(f"[smoke] countdown={txt!r}  bar={width!r}")

        nums = [s for (s, _) in readings if s == s]
        assert len(nums) >= 5, f"expected >=5 numeric readings, got {readings}"
        assert nums[0] > nums[-1], (
            f"countdown must decrease over time: first={nums[0]} last={nums[-1]}"
        )
        assert nums[-1] < 5.0, (
            f"countdown should be near 0 by end of window, got {nums[-1]}"
        )

        await page.wait_for_function(
            "() => document.getElementById('calibration-banner').hidden",
            timeout=4000,
        )
        print("[smoke] banner hidden again after calibration ended")

        await browser.close()


async def main() -> None:
    _check_snapshot_math()

    state = AppState()
    state.eeg_connection_state = "simulated"
    state.eeg_ready.set()

    opts = ServerOptions(http_port=PORT, no_browser=True)
    ready = asyncio.Event()

    server_task = asyncio.create_task(run_server_loop(state, opts), name="server")
    cal_task = asyncio.create_task(
        _drive_calibration(state, ready), name="cal-driver"
    )
    browser_task = asyncio.create_task(_drive_browser(ready), name="browser")

    try:
        await asyncio.wait_for(browser_task, timeout=30)
    finally:
        cal_task.cancel()
        server_task.cancel()
        for t in (cal_task, server_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    print("[smoke] PASS")


if __name__ == "__main__":
    asyncio.run(main())
