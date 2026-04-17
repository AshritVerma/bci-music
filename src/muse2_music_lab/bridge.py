"""Read Muse EEG from an LSL stream and emit smoothed activity as OSC floats."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional, Sequence

import numpy as np
from pylsl import StreamInlet, resolve_byprop
from pythonosc import udp_client


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LSL EEG → OSC bridge")
    p.add_argument("--host", default="127.0.0.1", help="OSC destination host")
    p.add_argument("--port", type=int, default=9000, help="OSC destination UDP port")
    p.add_argument(
        "--osc-path",
        default="/muse/eeg",
        dest="osc_path",
        help="OSC path for four float values (TP9, AF7, AF8, TP10)",
    )
    p.add_argument(
        "--lsl-timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for an EEG LSL stream",
    )
    p.add_argument(
        "--rate",
        type=float,
        default=30.0,
        help="Maximum OSC messages per second (approximate)",
    )
    return p.parse_args(argv)


def run_bridge(
    *,
    host: str = "127.0.0.1",
    port: int = 9000,
    osc_path: str = "/muse/eeg",
    lsl_timeout: float = 8.0,
    rate: float = 30.0,
) -> int:
    streams = resolve_byprop("type", "EEG", timeout=lsl_timeout)
    if not streams:
        print(
            "No EEG LSL stream found. Start the headset first, e.g.: muse2-music stream",
            file=sys.stderr,
        )
        return 1

    inlet = StreamInlet(streams[0], max_buflen=360)
    osc = udp_client.SimpleUDPClient(host, port)

    # Adaptive normalization: track decaying peaks per channel
    n_ch = int(inlet.info().channel_count())
    peaks = np.ones(n_ch, dtype=np.float64) * 1e-6
    min_interval = 1.0 / max(rate, 1.0)
    last_send = 0.0

    print(
        f"Bridging {n_ch}-ch EEG → osc://{host}:{port}{osc_path}",
        flush=True,
    )

    try:
        while True:
            chunk, ts = inlet.pull_chunk(max_samples=64, timeout=1.0)
            if not chunk:
                continue
            data = np.asarray(chunk, dtype=np.float64)
            # Mean absolute amplitude per channel in this chunk
            m = np.mean(np.abs(data), axis=0)
            peaks = np.maximum(peaks * 0.992, m + 1e-9)
            normed = np.clip(m / peaks, 0.0, 1.0)

            now = time.monotonic()
            if now - last_send >= min_interval:
                # Muse EEG names are TP9, AF7, AF8, TP10 — send four floats if present
                floats = [float(normed[i]) for i in range(min(4, len(normed)))]
                if len(floats) < 4:
                    floats.extend([0.0] * (4 - len(floats)))
                osc.send_message(osc_path, *floats[:4])
                last_send = now
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def main() -> None:
    args = _parse_args()
    raise SystemExit(
        run_bridge(
            host=args.host,
            port=args.port,
            osc_path=args.osc_path,
            lsl_timeout=args.lsl_timeout,
            rate=args.rate,
        )
    )
