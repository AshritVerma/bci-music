"""Cycle macOS Bluetooth to recover from a stuck BLE state.

Use this when the Muse 2 keeps failing to connect ("Peripheral Connection
failed" loop) after a previous run was killed mid-cleanup. Cycling the macOS
Bluetooth stack flushes CoreBluetooth's cached state and forces the headset
to be re-discovered fresh.

Requires ``blueutil`` (install via ``brew install blueutil``). Falls back to
printing manual instructions if it's missing.
"""

from __future__ import annotations

import shutil
import subprocess
import time


def run() -> int:
    blueutil = shutil.which("blueutil")
    if blueutil is None:
        print("[bt-reset] `blueutil` not found.")
        print("           Install with: brew install blueutil")
        print("           Or manually toggle Bluetooth off+on in System Settings.")
        return 2

    print("[bt-reset] Bluetooth off...", flush=True)
    subprocess.run([blueutil, "-p", "0"], check=False)
    time.sleep(2.0)
    print("[bt-reset] Bluetooth on...", flush=True)
    subprocess.run([blueutil, "-p", "1"], check=False)
    time.sleep(3.0)
    print("[bt-reset] Done. Now power-cycle the Muse 2 (long-press until LEDs go")
    print("           dark, wait 5s, turn back on), then re-run `muse2-music run`.")
    return 0
