"""`muse2-music` CLI.

Subcommands:
  run        Live EEG -> TUI diagnostics (legacy, no music output).
  perform    NEW Muse -> Lyria -> browser visualizer pipeline (Phase 3+).
  simulate   Synthetic brain -> TUI (headset-free dev).
  battery    Quick BLE telemetry read of the Muse 2 battery.
  bt-reset   Cycle macOS Bluetooth to recover from stuck BLE state.
"""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from muse2_music_lab import config


# ---------------------------------------------------------------------------
# Subcommand dispatchers
# ---------------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    from muse2_music_lab.tui import RunOptions, run_with_tui

    opts = RunOptions(
        calibrate_seconds=args.calibrate_seconds,
        send_rate_hz=args.rate,
        window_size=args.window,
        smoothing_alpha=args.smoothing,
    )
    return run_with_tui(opts)


def _cmd_perform(args: argparse.Namespace) -> int:
    from muse2_music_lab.main import PerformOptions, run as perform_run

    opts = PerformOptions(
        prompt=args.prompt,
        http_port=args.http_port,
        no_browser=args.no_browser,
        simulate_eeg=args.simulate_eeg,
        no_lyria=args.no_lyria,
        no_server=args.no_server,
        no_tui=args.no_tui,
        skip_seed=args.skip_seed,
        no_seed_cache=args.no_seed_cache,
        evolve_chunks=args.evolve_chunks,
    )
    return perform_run(opts)


def _cmd_simulate(args: argparse.Namespace) -> int:
    from muse2_music_lab.simulate import SimulateOptions, run as sim_run

    opts = SimulateOptions(
        send_rate_hz=args.rate,
        duration_s=args.duration,
    )
    return sim_run(opts)


def _cmd_battery(_: argparse.Namespace) -> int:
    from muse2_music_lab.battery import run as battery_run

    return battery_run()


def _cmd_bt_reset(_: argparse.Namespace) -> int:
    from muse2_music_lab.bt_reset import run as bt_reset_run

    return bt_reset_run()


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="muse2",
        description=(
            "Stream EEG from a Muse 2 via BrainFlow. The 'perform' subcommand "
            "drives the Lyria + browser-visualizer demo; 'run' shows a TUI of "
            "the live signals; 'simulate' does the same with a synthetic brain."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ------- run (legacy EEG TUI diagnostics) -------
    pr = sub.add_parser(
        "run",
        help="Live EEG -> TUI diagnostics (no music output).",
    )
    pr.add_argument(
        "--calibrate-seconds",
        type=float,
        default=config.CALIBRATION_DURATION,
        dest="calibrate_seconds",
        help="Baseline calibration duration in seconds",
    )
    pr.add_argument(
        "--rate",
        type=float,
        default=config.SEND_RATE_HZ,
        help="TUI refresh rate in Hz",
    )
    pr.add_argument(
        "--window",
        type=int,
        default=config.WINDOW_SIZE,
        help="Samples per feature window",
    )
    pr.add_argument(
        "--smoothing",
        type=float,
        default=config.SMOOTHING_ALPHA,
        help="EMA alpha (lower = smoother, more lag)",
    )
    pr.set_defaults(func=_cmd_run)

    # ------- perform (new Lyria + visualizer pipeline) -------
    pp = sub.add_parser(
        "perform",
        help="NEW: Muse -> Lyria RealTime music + Three.js shader visualizer.",
    )
    pp.add_argument(
        "--prompt",
        default="",
        help=(
            "Session prompt. Drives both Lyria's stylistic basis and the seed "
            "image used by the visualizer (e.g. 'downtempo electronic with "
            "warm analog synth pads'). OPTIONAL since Phase 10: omit to type "
            "the prompt in the browser and click Start manually."
        ),
    )
    pp.add_argument(
        "--http-port",
        type=int,
        default=8000,
        dest="http_port",
        help="aiohttp server port (default: 8000)",
    )
    pp.add_argument(
        "--no-browser",
        action="store_true",
        dest="no_browser",
        help="Skip auto-launching Chrome to the visualizer URL.",
    )
    pp.add_argument(
        "--simulate-eeg",
        action="store_true",
        dest="simulate_eeg",
        help="Use synthetic EEG instead of opening the Muse 2 (headset-free dev).",
    )
    pp.add_argument(
        "--no-lyria",
        action="store_true",
        dest="no_lyria",
        help="Skip Lyria music generation (visual + EEG only; for visualizer dev).",
    )
    pp.add_argument(
        "--no-server",
        action="store_true",
        dest="no_server",
        help="Skip the aiohttp + WebSocket server (for headless EEG/Lyria dev).",
    )
    pp.add_argument(
        "--no-tui",
        action="store_true",
        dest="no_tui",
        help=(
            "Disable the rich.Live status panel and fall back to a plain "
            "'[state] alpha=...' log line every couple of seconds. Use when "
            "piping output to a file or in a terminal that doesn't render "
            "the panel cleanly."
        ),
    )
    pp.add_argument(
        "--skip-seed",
        action="store_true",
        dest="skip_seed",
        help=(
            "Skip Phase 8's Imagen seed image call. Saves ~3-5s of startup "
            "wall time and avoids API quota usage; the visualizer will "
            "fall back to whatever static/seed.png already exists from a "
            "prior session, or render blank if there's none."
        ),
    )
    pp.add_argument(
        "--no-seed-cache",
        action="store_true",
        dest="no_seed_cache",
        help=(
            "Force re-generation of the seed image even if a cached PNG "
            "exists for this prompt. Useful when iterating on the Imagen "
            "model id or aspect ratio."
        ),
    )
    pp.add_argument(
        "--evolve-chunks",
        type=int,
        default=config.EVOLVE_INTERVAL_CHUNKS,
        dest="evolve_chunks",
        help=(
            "Phase 10: regenerate the seed image every N Lyria audio "
            "chunks (~2s of music each, so 12 chunks ≈ 24s) based on "
            "how the EEG/audio features have moved. Pass 0 to disable. "
            "Cost scales inversely with N: ~$3/hr at 12, ~$1.5/hr at 24 "
            "(default: %(default)s)."
        ),
    )
    pp.set_defaults(func=_cmd_perform)

    # ------- simulate (synthetic EEG TUI) -------
    ps = sub.add_parser(
        "simulate",
        help="Synthetic brain -> TUI (headset-free smoke test of the diagnostic UI).",
    )
    ps.add_argument(
        "--rate",
        type=float,
        default=30.0,
        help="TUI refresh rate in Hz",
    )
    ps.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Stop after N seconds (0 = run until Ctrl-C)",
    )
    ps.set_defaults(func=_cmd_simulate)

    # ------- battery -------
    pb = sub.add_parser(
        "battery",
        help="Connect briefly to the Muse 2 and print its battery percentage.",
    )
    pb.set_defaults(func=_cmd_battery)

    # ------- bt-reset -------
    pbt = sub.add_parser(
        "bt-reset",
        help=(
            "Cycle macOS Bluetooth to recover from a stuck BLE state "
            "(use after a Ctrl-C-killed run that won't reconnect)."
        ),
    )
    pbt.set_defaults(func=_cmd_bt_reset)

    return p


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    ns = parser.parse_args(argv)
    raise SystemExit(ns.func(ns))
