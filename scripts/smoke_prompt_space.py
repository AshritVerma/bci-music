"""Regression guard: space bar swallowed in inline prompt edit.

Boots a minimal server with a fake "lyria_started + lyria_ready" state
(so the prompt becomes editable), opens Chrome, clicks the header
prompt, types "foo bar baz", and asserts the textarea value is exactly
"foo bar baz" -- spaces and all.

Why this test exists: the inline edit textarea is a CHILD of the
prompt span. The span has its own keydown listener (Space / Enter ->
enter edit mode, role=button keyboard semantics). Without an explicit
e.target check, keydown events from the textarea bubble up to the
span, the span's preventDefault() fires, and spaces never reach the
textarea -- silently breaking the entire mid-session prompt-change
flow for any multi-word prompt. This test catches that regression.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from muse2_music_lab.server.app import ServerOptions, run_server_loop  # noqa: E402
from muse2_music_lab.state import AppState  # noqa: E402

PORT = 8767


async def main() -> None:
    state = AppState()
    state.eeg_connection_state = "simulated"
    state.eeg_ready.set()
    state.lyria_started = True
    state.lyria_ready.set()
    state.prompt = "warm ambient drone"
    opts = ServerOptions(http_port=PORT, no_browser=True)
    server_task = asyncio.create_task(run_server_loop(state, opts))
    await asyncio.sleep(0.5)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto(f"http://localhost:{PORT}/")
        # Wait for snapshot to land so the prompt is editable + not in
        # warming state.
        await page.wait_for_function(
            "() => document.getElementById('prompt').classList.contains("
            "'prompt-editable-ready')",
            timeout=5000,
        )
        # Click the prompt span to enter edit mode.
        await page.click("#prompt")
        await page.wait_for_selector(".prompt-edit-input", state="visible", timeout=2000)
        # Empty the prefill so we can type from scratch.
        await page.evaluate(
            "() => { const t = document.querySelector('.prompt-edit-input');"
            " t.value = ''; t.focus(); }"
        )
        # Now type a phrase with spaces, key by key, going through the
        # exact same keyboard-event path the user does.
        await page.keyboard.type("foo bar baz")
        value = await page.evaluate(
            "() => document.querySelector('.prompt-edit-input').value"
        )
        print(f"[smoke] textarea value after typing: {value!r}")
        assert value == "foo bar baz", (
            f"space bar bug! expected 'foo bar baz', got {value!r}"
        )
        # Cancel out (Escape) so we don't actually submit a change_prompt.
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.2)
        await browser.close()

    server_task.cancel()
    try:
        await server_task
    except (asyncio.CancelledError, Exception):
        pass

    print("[smoke] PASS: spaces are inserted into the prompt editor")


if __name__ == "__main__":
    asyncio.run(main())
