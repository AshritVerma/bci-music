"""End-to-end smoke for the right-side TUNE panel.

Boots a minimal server, opens Chrome headless, and verifies the
threshold-tuning surface end to end:

  1. Tune button is visible in the header (non-cloud mode).
  2. Clicking it opens the right-side drawer.
  3. The first WS snapshot syncs slider + number + reset-default
     for every row to the server's tunables map.
  4. Dragging the blink-threshold slider sends a set_threshold
     action; the server clamps + writes state.live_blink_threshold_uv.
  5. Typing into the jaw-threshold number input does the same.
  6. Out-of-range typed values are clamped at the server (defense in
     depth -- the JS slider also clamps but the WS path is direct).
  7. Reset button restores the config default.
  8. Closing via X re-hides the panel.

Decoupled from BLE / Lyria: only AppState + run_server_loop are
booted, no real EEG, no Lyria API key needed.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from muse2_music_lab import config  # noqa: E402
from muse2_music_lab.server.app import ServerOptions, run_server_loop  # noqa: E402
from muse2_music_lab.state import AppState  # noqa: E402

PORT = 8770


def _check_band_sensitivity_math() -> None:
    """Pure-Python check on the _apply_band_sensitivity formula."""
    from muse2_music_lab.eeg.brainflow_loop import _apply_band_sensitivity

    # sensitivity = 1.0 -> identity
    for y in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert abs(_apply_band_sensitivity(y, 1.0) - y) < 1e-9, y
    # sensitivity = 0.5 -> compress toward 0.5
    assert abs(_apply_band_sensitivity(1.0, 0.5) - 0.75) < 1e-9
    assert abs(_apply_band_sensitivity(0.0, 0.5) - 0.25) < 1e-9
    assert abs(_apply_band_sensitivity(0.5, 0.5) - 0.5) < 1e-9
    # sensitivity = 2.0 -> stretch + clip
    assert _apply_band_sensitivity(0.8, 2.0) == 1.0
    assert _apply_band_sensitivity(0.2, 2.0) == 0.0
    assert abs(_apply_band_sensitivity(0.6, 2.0) - 0.7) < 1e-9
    print("[smoke] _apply_band_sensitivity() math: OK")


async def main() -> None:
    _check_band_sensitivity_math()

    state = AppState()
    state.eeg_connection_state = "simulated"
    state.eeg_ready.set()
    # Forge a non-zero live peak so the meter UI has something to draw.
    state.blink_ptp_uv = 437.0
    state.jaw_rms_uv = 110.0

    opts = ServerOptions(http_port=PORT, no_browser=True)
    server_task = asyncio.create_task(run_server_loop(state, opts))
    await asyncio.sleep(0.4)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1600, "height": 900})
        page = await ctx.new_page()
        await page.goto(f"http://localhost:{PORT}/")

        await page.wait_for_selector("#tune-toggle", state="visible", timeout=4000)
        # Panel hidden by default.
        is_hidden_before = await page.evaluate(
            "() => document.getElementById('tune-panel').hidden"
        )
        assert is_hidden_before, "tune panel must start hidden"

        # Open the drawer.
        await page.click("#tune-toggle")
        await page.wait_for_selector("#tune-panel", state="visible", timeout=2000)
        # ARIA wired correctly.
        ae = await page.get_attribute("#tune-toggle", "aria-expanded")
        assert ae == "true", f"aria-expanded should be 'true', got {ae!r}"
        ah = await page.get_attribute("#tune-panel", "aria-hidden")
        assert ah == "false", f"aria-hidden should be 'false', got {ah!r}"

        # Wait for the first snapshot to land + sync the slider values.
        await page.wait_for_function(
            f"() => {{"
            f"  const r = document.querySelector("
            f"    '.tune-row[data-key=\"blink_threshold_uv\"] [data-role=\"slider\"]'"
            f"  );"
            f"  return r && Math.abs(parseFloat(r.value) - {config.BLINK_THRESHOLD_UV}) < 1;"
            f"}}",
            timeout=2000,
        )
        # ----- Test 1: drag the blink slider via direct value set + input event.
        target_blink = 1750
        await page.evaluate(
            f"() => {{"
            f"  const s = document.querySelector("
            f"    '.tune-row[data-key=\"blink_threshold_uv\"] [data-role=\"slider\"]'"
            f"  );"
            f"  s.value = '{target_blink}';"
            f"  s.dispatchEvent(new Event('input', {{bubbles: true}}));"
            f"  s.dispatchEvent(new Event('change', {{bubbles: true}}));"
            f"}}"
        )
        await asyncio.sleep(0.3)
        assert state.live_blink_threshold_uv == float(target_blink), (
            f"blink threshold not propagated: state={state.live_blink_threshold_uv} "
            f"expected {target_blink}"
        )
        # Number input should mirror the slider.
        num_value = await page.input_value(
            ".tune-row[data-key='blink_threshold_uv'] [data-role='number']"
        )
        assert int(float(num_value)) == target_blink, (
            f"number input did not mirror slider: {num_value!r}"
        )
        print(f"[smoke] slider drag -> state.live_blink_threshold_uv = {target_blink} OK")

        # ----- Test 2: type into the jaw number input.
        target_jaw = 350
        await page.evaluate(
            f"() => {{"
            f"  const n = document.querySelector("
            f"    '.tune-row[data-key=\"jaw_threshold_uv\"] [data-role=\"number\"]'"
            f"  );"
            f"  n.value = '{target_jaw}';"
            f"  n.dispatchEvent(new Event('input', {{bubbles: true}}));"
            f"  n.dispatchEvent(new Event('change', {{bubbles: true}}));"
            f"  n.blur();"
            f"}}"
        )
        await asyncio.sleep(0.3)
        assert state.live_jaw_threshold_uv == float(target_jaw), (
            f"jaw threshold not propagated: state={state.live_jaw_threshold_uv} "
            f"expected {target_jaw}"
        )
        print(f"[smoke] number input -> state.live_jaw_threshold_uv = {target_jaw} OK")

        # ----- Test 3: out-of-range typed value clamps server-side.
        # Server range for blink is [50, 4000].
        await page.evaluate(
            "() => {"
            "  const n = document.querySelector("
            "    '.tune-row[data-key=\"blink_threshold_uv\"] [data-role=\"number\"]'"
            "  );"
            "  n.value = '99999';"  # absurd; server should clamp to 4000
            "  n.dispatchEvent(new Event('input', {bubbles: true}));"
            "}"
        )
        await asyncio.sleep(0.3)
        assert state.live_blink_threshold_uv == 4000.0, (
            f"server should have clamped to 4000, got {state.live_blink_threshold_uv}"
        )
        print(f"[smoke] out-of-range typed -> server clamp -> {state.live_blink_threshold_uv} OK")

        # ----- Test 4: reset button restores default.
        await page.click(
            ".tune-row[data-key='blink_threshold_uv'] [data-role='reset']"
        )
        await asyncio.sleep(0.3)
        assert state.live_blink_threshold_uv == float(config.BLINK_THRESHOLD_UV), (
            f"reset failed: state={state.live_blink_threshold_uv} "
            f"expected {config.BLINK_THRESHOLD_UV}"
        )
        print(f"[smoke] reset -> state.live_blink_threshold_uv = {config.BLINK_THRESHOLD_UV} OK")

        # ----- Test 5: lyria sensitivity gain (float values).
        await page.evaluate(
            "() => {"
            "  const s = document.querySelector("
            "    '.tune-row[data-key=\"lyria_sensitivity_gain\"] [data-role=\"slider\"]'"
            "  );"
            "  s.value = '3.25';"
            "  s.dispatchEvent(new Event('input', {bubbles: true}));"
            "}"
        )
        await asyncio.sleep(0.3)
        assert abs(state.live_lyria_sensitivity_gain - 3.25) < 0.01, (
            f"gain not propagated: {state.live_lyria_sensitivity_gain}"
        )
        print(f"[smoke] gain slider -> state.live_lyria_sensitivity_gain = 3.25 OK")

        # ----- Test 6: live peak meter renders the forged value.
        peak_text = await page.text_content(
            ".tune-row[data-key='blink_threshold_uv'] [data-role='peak']"
        )
        # Forged blink_ptp_uv = 437; expect "437 µV" or close
        assert "437" in (peak_text or ""), (
            f"live peak meter did not show forged value: {peak_text!r}"
        )
        print(f"[smoke] live peak meter shows {peak_text!r} OK")

        # ----- Test 7: per-band sensitivity sliders (alpha/beta/theta).
        # Three identical knobs; verify all three propagate + the
        # math holds for one of them by checking the live peak text
        # updates after the slider change.
        for band, target in (("alpha", 0.5), ("beta", 1.5), ("theta", 0.7)):
            key = f"{band}_sensitivity"
            await page.evaluate(
                f"() => {{"
                f"  const s = document.querySelector("
                f"    '.tune-row[data-key=\"{key}\"] [data-role=\"slider\"]'"
                f"  );"
                f"  s.value = '{target}';"
                f"  s.dispatchEvent(new Event('input', {{bubbles: true}}));"
                f"}}"
            )
            await asyncio.sleep(0.25)
            actual = getattr(state, f"live_{band}_sensitivity")
            assert abs(actual - target) < 0.01, (
                f"{band} sensitivity not propagated: {actual} (expected {target})"
            )
            print(f"[smoke] {band} sensitivity slider -> state.live_{band}_sensitivity = {target} OK")

        # Live-peak meter on the alpha row must render the forged
        # state.alpha as a 0..1 number (0.95) -- not a "µV" reading.
        state.alpha = 0.95
        state.beta = 0.40
        state.theta = 0.10
        await asyncio.sleep(0.2)
        alpha_peak = await page.text_content(
            ".tune-row[data-key='alpha_sensitivity'] [data-role='peak']"
        )
        assert alpha_peak and "0.95" in alpha_peak, (
            f"alpha sensitivity meter must show normalized value, got {alpha_peak!r}"
        )
        print(f"[smoke] alpha live-peak meter shows {alpha_peak!r} OK")

        # ----- Test 8: out-of-range typed sensitivity clamps server-side.
        # Server range is [0.2, 3.0]; type something absurd.
        await page.evaluate(
            "() => {"
            "  const n = document.querySelector("
            "    '.tune-row[data-key=\"alpha_sensitivity\"] [data-role=\"number\"]'"
            "  );"
            "  n.value = '99';"
            "  n.dispatchEvent(new Event('input', {bubbles: true}));"
            "}"
        )
        await asyncio.sleep(0.25)
        assert state.live_alpha_sensitivity == 3.0, (
            f"server should have clamped alpha sensitivity to 3.0, got "
            f"{state.live_alpha_sensitivity}"
        )
        print(f"[smoke] out-of-range alpha sens -> server clamp -> 3.0 OK")

        # Reset alpha sensitivity to 1.0.
        await page.click(
            ".tune-row[data-key='alpha_sensitivity'] [data-role='reset']"
        )
        await asyncio.sleep(0.25)
        assert state.live_alpha_sensitivity == 1.0, (
            f"reset alpha sensitivity failed: {state.live_alpha_sensitivity}"
        )
        print(f"[smoke] alpha sensitivity reset -> 1.0 OK")

        # ----- Test 9: close X re-hides the panel.
        await page.click("#tune-close")
        await asyncio.sleep(0.2)
        is_hidden_after = await page.evaluate(
            "() => document.getElementById('tune-panel').hidden"
        )
        assert is_hidden_after, "panel must be hidden after clicking X"
        ae2 = await page.get_attribute("#tune-toggle", "aria-expanded")
        assert ae2 == "false", f"aria-expanded should reset to 'false', got {ae2!r}"
        print("[smoke] close X re-hides the panel OK")

        await browser.close()

    server_task.cancel()
    try:
        await server_task
    except (asyncio.CancelledError, Exception):
        pass

    print("[smoke] PASS: tune panel works end-to-end")


if __name__ == "__main__":
    asyncio.run(main())
