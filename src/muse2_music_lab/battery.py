"""Quick Muse 2 battery check via direct BLE (bleak).

BrainFlow's Muse 2 driver does not expose battery as a channel in any of its
3 presets, so we go around it: connect to the Muse over BLE, send the "h"
control command to start the headset's telemetry stream, listen for one
telemetry packet, and decode the battery field.

The wire format is documented in muselsl. Telemetry packets are 16 bytes,
big-endian, with the structure::

    bytes 0..1 : packet number (uint16)
    bytes 2..3 : battery raw   (uint16, divide by 512.0 for percent)
    bytes 4..5 : fuel gauge    (uint16, mV * 2.2)
    bytes 6..7 : ADC volt      (uint16)
    bytes 8..9 : temperature   (uint16, Celsius)
"""

from __future__ import annotations

import asyncio
import struct
from typing import Optional

# Muse GATT characteristics (same for Muse 2016 / Muse S / Muse 2).
_CONTROL_UUID = "273e0001-4c4d-454d-96be-f03bac821358"
_TELEMETRY_UUID = "273e000b-4c4d-454d-96be-f03bac821358"


def _muse_command(cmd: str) -> bytearray:
    """Build a Muse-format command (length byte + ASCII bytes + 0x0a)."""
    body = cmd.encode("ascii")
    return bytearray([len(body) + 1, *body, 0x0A])


async def _read_battery_async(
    name_substring: str = "muse",
    scan_timeout_s: float = 8.0,
    telemetry_timeout_s: float = 8.0,
) -> Optional[float]:
    from bleak import BleakClient, BleakScanner

    devices = await BleakScanner.discover(timeout=scan_timeout_s)
    target = None
    for d in devices:
        nm = (d.name or "").lower()
        if name_substring in nm:
            target = d
            break
    if target is None:
        raise ConnectionError(
            "No Muse device advertising. Make sure the headset is on and "
            "showing the slow blink pattern (not connected to anything else)."
        )

    battery_pct: Optional[float] = None
    received = asyncio.Event()

    def _on_telemetry(_handle, data: bytearray) -> None:
        nonlocal battery_pct
        if len(data) < 4:
            return
        raw_battery = struct.unpack(">H", bytes(data[2:4]))[0]
        battery_pct = raw_battery / 512.0
        received.set()

    async with BleakClient(target) as client:
        await client.start_notify(_TELEMETRY_UUID, _on_telemetry)
        # Muse 2 command sequence (same as muselsl): set preset, then start,
        # then data. Telemetry packets only fire while the data stream is
        # running. Halt the stream when we're done so the headset doesn't keep
        # broadcasting after we disconnect.
        await client.write_gatt_char(_CONTROL_UUID, _muse_command("p21"), response=False)
        await client.write_gatt_char(_CONTROL_UUID, _muse_command("s"), response=False)
        await client.write_gatt_char(_CONTROL_UUID, _muse_command("d"), response=False)
        try:
            await asyncio.wait_for(received.wait(), timeout=telemetry_timeout_s)
        except asyncio.TimeoutError:
            pass
        try:
            await client.write_gatt_char(_CONTROL_UUID, _muse_command("h"), response=False)
        except Exception:
            pass
        try:
            await client.stop_notify(_TELEMETRY_UUID)
        except Exception:
            pass

    return battery_pct


def read_battery(
    scan_timeout_s: float = 8.0,
    telemetry_timeout_s: float = 8.0,
) -> Optional[float]:
    """Synchronous wrapper around the async BLE reader."""
    return asyncio.run(
        _read_battery_async(
            scan_timeout_s=scan_timeout_s,
            telemetry_timeout_s=telemetry_timeout_s,
        )
    )


def run() -> int:
    """CLI entry point: print battery %, return shell exit code."""
    print("[battery] Scanning for Muse 2 over BLE...", flush=True)
    try:
        pct = read_battery()
    except ConnectionError as e:
        print(f"[battery] {e}")
        return 2
    except Exception as e:
        print(f"[battery] Failed: {type(e).__name__}: {e}")
        return 2

    if pct is None:
        print("[battery] Connected, but no telemetry packet arrived in time.")
        print("          Power-cycle the headset and try again.")
        return 1

    print(f"[battery] {pct:5.1f}%  {_bar(pct)}  ({_label(pct)})")
    return 0


def _bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _label(pct: float) -> str:
    if pct >= 60:
        return "good"
    if pct >= 30:
        return "ok"
    if pct >= 15:
        return "low — charge soon"
    return "critical — connection drops are likely until you charge"
