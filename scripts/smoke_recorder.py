"""End-to-end smoke for the in-browser AV recorder.

Boots a Playwright headless Chromium, points it at the live perform
server (caller starts perform separately), drives the UI:

  1. Wait for the page to load and WS to connect.
  2. Type a prompt + click Start.
  3. Wait for `lyria_ready` (audio actually flowing).
  4. Click Record. Wait ~6 s for audio + canvas frames to accumulate.
  5. Click Record again (toggle -> Stop).
  6. Poll window.recorder.status() until hasBlob:true.
  7. Pull the blob out of the page as base64 and write it to disk.
  8. Verify it's a non-trivial WebM (size > 50 KB; first bytes look right).

Run:
    python scripts/smoke_recorder.py

Headless Chromium has known limits with MediaRecorder + canvas.captureStream
(GPU-less compositor, no system audio) but the in-browser pipeline this
test exercises uses a software canvas + a synthesized audio stream, both
of which work in headless mode. If MediaRecorder isn't supported at all
we fail fast with a clear error.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

from playwright.async_api import async_playwright


URL = "http://localhost:8000/"
OUT_DIR = Path("/tmp")
PROMPT = "warm ambient drone with crystalline texture and a gentle pulse"


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                # MediaRecorder needs codecs; these unmute headless audio.
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--enable-features=MediaRecorderUseGpu",
            ],
        )
        ctx = await browser.new_context()
        page = await ctx.new_page()
        page.on("console", lambda m: print(f"[browser:{m.type}] {m.text}"))

        print(f"[smoke] navigating to {URL}")
        await page.goto(URL, wait_until="networkidle")

        # Wait for WS to connect: the conn pill flips to "WS: connected".
        print("[smoke] waiting for WS connect...")
        await page.wait_for_function(
            "() => document.getElementById('conn') && "
            "document.getElementById('conn').textContent.includes('connected')",
            timeout=15000,
        )

        # Make sure window.recorder exists at all -- fail fast on
        # MediaRecorder unsupported.
        rec_status = await page.evaluate("() => typeof window.recorder")
        if rec_status != "object":
            print(f"[smoke] FAIL: window.recorder is {rec_status!r}")
            return 2
        print("[smoke] window.recorder is loaded")

        # If a previous run already started the session (the perform
        # process is shared across smoke runs), the start panel is
        # hidden -- skip directly to Record. Otherwise type the prompt
        # and click Start.
        start_visible = await page.evaluate(
            "() => !document.getElementById('start-panel').classList.contains('hidden')"
        )
        if start_visible:
            print(f"[smoke] typing prompt: {PROMPT!r}")
            await page.fill("#start-prompt", PROMPT)
            await page.click("#start-btn")
            print("[smoke] clicked Start; waiting for lyria_ready...")
        else:
            print("[smoke] start panel already hidden (session live); skipping Start")

        # Wait for the Record button to become enabled (gated on
        # lyria_started). Either flow above lands here within a few
        # seconds; cold start can take up to 30 s while Lyria warms up.
        await page.wait_for_function(
            "() => !document.getElementById('record-btn').disabled",
            timeout=60000,
        )
        print("[smoke] Record button enabled (lyria_started=true)")

        # Click Record.
        print("[smoke] clicking Record...")
        await page.click("#record-btn")
        # Tiny pause so the click handler resolves before we inspect state.
        await asyncio.sleep(0.5)
        is_recording = await page.evaluate(
            "() => window.recorder.status().isRecording"
        )
        if not is_recording:
            print("[smoke] FAIL: recorder.start() did not transition to recording")
            return 3
        print("[smoke] recording. capturing 6 s of frames...")
        await asyncio.sleep(6.0)

        # Click Record again -> Stop.
        print("[smoke] clicking Record again (Stop)...")
        await page.click("#record-btn")

        # Wait for blob to be assembled (the MediaRecorder.stop event
        # is async; status().hasBlob flips true once it fires).
        print("[smoke] waiting for blob assembly...")
        await page.wait_for_function(
            "() => window.recorder.status().hasBlob === true",
            timeout=10000,
        )
        s = await page.evaluate("() => window.recorder.status()")
        print(f"[smoke] recorder status post-stop: {s}")

        if s.get("blobBytes", 0) < 50_000:
            print(
                f"[smoke] FAIL: blob is too small ({s.get('blobBytes')} bytes); "
                "expected > 50 KB for a 6-second recording"
            )
            return 4

        # Pull the blob bytes back to Python via base64.
        b64 = await page.evaluate(
            """async () => {
                // window.recorder doesn't expose the blob, but it's the
                // only blob we have right now -- we can reach it via the
                // closure trick below: trigger a save via a sniffed link
                // click. Cleaner: grab from the recorder via a dev-only
                // hook. For the smoke we re-create the blob from a
                // throwaway MediaRecorder snapshot... actually simplest:
                // call save() inside an iframe and intercept the blob URL.
                //
                // But we don't have a direct accessor. Instead, monkey-
                // patch URL.createObjectURL to capture the blob it sees.
                let captured = null;
                const orig = URL.createObjectURL;
                URL.createObjectURL = function (b) {
                    captured = b;
                    return orig.call(URL, b);
                };
                try {
                    window.recorder.save();
                } finally {
                    URL.createObjectURL = orig;
                }
                if (!captured) return null;
                const buf = await captured.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                return btoa(bin);
            }"""
        )
        if not b64:
            print("[smoke] FAIL: could not extract blob bytes from page")
            return 5

        data = base64.b64decode(b64)
        out = OUT_DIR / "smoke_recorder.webm"
        out.write_bytes(data)
        print(f"[smoke] saved {len(data)} bytes to {out}")

        # Validate WebM: starts with the EBML magic 0x1A 0x45 0xDF 0xA3.
        if data[:4] != b"\x1a\x45\xdf\xa3":
            print(f"[smoke] FAIL: file does not start with EBML magic; first bytes = {data[:8].hex()}")
            return 6

        print(f"[smoke] PASS: valid WebM, {len(data)} bytes ({len(data) / 1024:.1f} KB)")
        await browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
