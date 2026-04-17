"""CLI: stream from Muse 2 via muselsl, or run the LSL→OSC bridge."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from muselsl import list_muses, stream


def _cmd_stream(args: argparse.Namespace) -> int:
    muses = list_muses()
    if not muses:
        print(
            "No Muse found. Power on the headset and ensure Bluetooth pairing.",
            file=sys.stderr,
        )
        return 1

    address = args.address
    if not address:
        picked = None
        if args.name:
            for m in muses:
                if m.get("name") == args.name:
                    picked = m
                    break
            if picked is None:
                print(f"No Muse named {args.name!r}. Available:", file=sys.stderr)
                for m in muses:
                    print(f"  - {m.get('name')} {m.get('address')}", file=sys.stderr)
                return 1
        else:
            picked = muses[0]
        address = picked["address"]
        print(f"Using {picked.get('name', 'Muse')} at {address}")

    stream(
        address,
        ppg_enabled=args.ppg,
        acc_enabled=args.acc,
        gyro_enabled=args.gyro,
    )
    return 0


def _cmd_bridge(args: argparse.Namespace) -> int:
    from muse2_music_lab.bridge import run_bridge

    return run_bridge(
        host=args.host,
        port=args.port,
        osc_path=args.osc_path,
        lsl_timeout=args.lsl_timeout,
        rate=args.rate,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="muse2-music",
        description="Muse 2 tools: LSL streaming and OSC bridge for music production.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_stream = sub.add_parser("stream", help="Start LSL streams from the Muse (muselsl)")
    p_stream.add_argument(
        "--address",
        default=None,
        help="Bluetooth address of the Muse (default: first device)",
    )
    p_stream.add_argument(
        "--name",
        default=None,
        help="Pick a Muse by advertised name instead of the first device",
    )
    p_stream.add_argument(
        "--ppg",
        action="store_true",
        help="Stream PPG (Muse 2)",
    )
    p_stream.add_argument(
        "--acc",
        action="store_true",
        help="Stream accelerometer",
    )
    p_stream.add_argument(
        "--gyro",
        action="store_true",
        help="Stream gyroscope",
    )
    p_stream.set_defaults(func=_cmd_stream)

    p_bridge = sub.add_parser(
        "bridge",
        help="Read EEG from LSL and send smoothed activity to OSC",
    )
    p_bridge.add_argument("--host", default="127.0.0.1", help="OSC destination host")
    p_bridge.add_argument("--port", type=int, default=9000, help="OSC destination UDP port")
    p_bridge.add_argument(
        "--osc-path",
        default="/muse/eeg",
        dest="osc_path",
        help="OSC path for four floats (TP9, AF7, AF8, TP10)",
    )
    p_bridge.add_argument(
        "--lsl-timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for an EEG LSL stream",
    )
    p_bridge.add_argument(
        "--rate",
        type=float,
        default=30.0,
        help="Maximum OSC messages per second",
    )
    p_bridge.set_defaults(func=_cmd_bridge)

    return p


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    ns = parser.parse_args(argv)
    raise SystemExit(ns.func(ns))
