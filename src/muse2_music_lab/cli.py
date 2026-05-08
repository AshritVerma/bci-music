"""`muse2-music` CLI: run the BrainFlow → feature → DAW pipeline."""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from muse2_music_lab import config


def _cmd_run(args: argparse.Namespace) -> int:
    from muse2_music_lab.main import RunOptions, run

    opts = RunOptions(
        backend=args.backend,
        midi_port=args.midi_port,
        osc_host=args.osc_host,
        osc_port=args.osc_port,
        calibrate_seconds=args.calibrate_seconds,
        send_rate_hz=args.rate,
        window_size=args.window,
        smoothing_alpha=args.smoothing,
        tui=args.tui,
        viz=args.viz,
        viz_host=args.viz_host,
        viz_port=args.viz_port,
        viz_prompt_source=args.viz_prompt_source,
    )

    if args.tui:
        try:
            from muse2_music_lab.tui import run_with_tui

            return run_with_tui(opts)
        except ImportError as e:
            print(
                f"[tui] rich not available ({e}). Install with: "
                "pip install -e '.[tui]'. Falling back to plain output."
            )

    return run(opts)


def _cmd_list_midi(_: argparse.Namespace) -> int:
    from muse2_music_lab.output_midi import list_output_ports

    ports = list_output_ports()
    if not ports:
        print("(no MIDI output ports found)")
        print(
            "On macOS, enable the IAC Driver in Audio MIDI Setup "
            "(Applications → Utilities → Audio MIDI Setup → Window → Show MIDI Studio)."
        )
        return 1
    print("Available MIDI output ports:")
    for name in ports:
        marker = "  *" if name == config.MIDI_PORT_NAME or config.MIDI_PORT_NAME in name else "   "
        print(f"{marker} {name}")
    print(f"\nDefault in config.py: {config.MIDI_PORT_NAME!r}")
    return 0


def _cmd_monitor_midi(args: argparse.Namespace) -> int:
    from muse2_music_lab.midi_monitor import run as monitor_run
    return monitor_run(args.midi_port)


def _cmd_simulate(args: argparse.Namespace) -> int:
    from muse2_music_lab.simulate import SimulateOptions, run as sim_run
    opts = SimulateOptions(
        backend=args.backend,
        midi_port=args.midi_port,
        osc_host=args.osc_host,
        osc_port=args.osc_port,
        send_rate_hz=args.rate,
        duration_s=args.duration,
        viz=args.viz,
        viz_host=args.viz_host,
        viz_port=args.viz_port,
        viz_prompt_source=args.viz_prompt_source,
    )
    return sim_run(opts)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="muse2-music",
        description=(
            "Stream EEG from a Muse 2 via BrainFlow, extract musical features, "
            "and route them to a DAW as MIDI CC or OSC."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="Run the live brain → DAW loop")
    pr.add_argument(
        "--backend",
        choices=("midi", "osc", "both"),
        default=config.OUTPUT_BACKEND,
        help=f"Output backend (default: {config.OUTPUT_BACKEND})",
    )
    pr.add_argument(
        "--midi-port",
        default=config.MIDI_PORT_NAME,
        dest="midi_port",
        help=f"MIDI output port name (default: {config.MIDI_PORT_NAME!r})",
    )
    pr.add_argument(
        "--osc-host",
        default=config.OSC_HOST,
        dest="osc_host",
        help=f"OSC destination host (default: {config.OSC_HOST})",
    )
    pr.add_argument(
        "--osc-port",
        type=int,
        default=config.OSC_PORT,
        dest="osc_port",
        help=f"OSC destination UDP port (default: {config.OSC_PORT})",
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
        help="Output messages per second",
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
    pr.add_argument(
        "--tui",
        action="store_true",
        help="Show a live rich-powered TUI (requires the 'tui' extra)",
    )
    pr.add_argument(
        "--viz",
        action="store_true",
        default=config.VIZ_ENABLED,
        help="Publish /viz/* OSC for the TouchDesigner visual layer (in addition to the DAW backend)",
    )
    pr.add_argument(
        "--viz-host",
        default=config.VIZ_HOST,
        dest="viz_host",
        help=f"Viz OSC destination host (default: {config.VIZ_HOST})",
    )
    pr.add_argument(
        "--viz-port",
        type=int,
        default=config.VIZ_PORT,
        dest="viz_port",
        help=f"Viz OSC destination UDP port (default: {config.VIZ_PORT})",
    )
    pr.add_argument(
        "--viz-prompt-source",
        choices=("auto", "manual", "mix"),
        default=config.VIZ_PROMPT_SOURCE_DEFAULT,
        dest="viz_prompt_source",
        help=(
            "Prompt source for the sidecar. 'auto' = brain-only bank interpolation "
            "(no user text needed); 'manual' = use /viz/prompt/base text only; "
            f"'mix' = both. Default: {config.VIZ_PROMPT_SOURCE_DEFAULT}"
        ),
    )
    pr.set_defaults(func=_cmd_run)

    pl = sub.add_parser(
        "list-midi", help="List available MIDI output ports (for IAC setup checks)"
    )
    pl.set_defaults(func=_cmd_list_midi)

    pm = sub.add_parser(
        "monitor-midi",
        help="Live-display MIDI input on a port (sanity-check IAC bus without Logic open)",
    )
    pm.add_argument(
        "--midi-port",
        default=config.MIDI_PORT_NAME,
        dest="midi_port",
        help=f"MIDI input port name (default: {config.MIDI_PORT_NAME!r})",
    )
    pm.set_defaults(func=_cmd_monitor_midi)

    ps = sub.add_parser(
        "simulate",
        help=(
            "Synthesize a brain (sine sweeps + periodic triggers) through the real "
            "mapping/output pipeline. Headset-free testing of MIDI/OSC/viz paths."
        ),
    )
    ps.add_argument(
        "--backend",
        choices=("midi", "osc", "both"),
        default=config.OUTPUT_BACKEND,
        help=f"Output backend (default: {config.OUTPUT_BACKEND})",
    )
    ps.add_argument(
        "--midi-port",
        default=config.MIDI_PORT_NAME,
        dest="midi_port",
        help=f"MIDI output port name (default: {config.MIDI_PORT_NAME!r})",
    )
    ps.add_argument(
        "--osc-host",
        default=config.OSC_HOST,
        dest="osc_host",
        help=f"DAW-side OSC host (default: {config.OSC_HOST})",
    )
    ps.add_argument(
        "--osc-port",
        type=int,
        default=config.OSC_PORT,
        dest="osc_port",
        help=f"DAW-side OSC port (default: {config.OSC_PORT})",
    )
    ps.add_argument(
        "--rate",
        type=float,
        default=config.SEND_RATE_HZ,
        help="Output messages per second",
    )
    ps.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Stop after N seconds (0 = run until Ctrl-C)",
    )
    ps.add_argument(
        "--viz",
        action="store_true",
        default=config.VIZ_ENABLED,
        help="Also publish /viz/* OSC for the visual layer",
    )
    ps.add_argument(
        "--viz-host",
        default=config.VIZ_HOST,
        dest="viz_host",
        help=f"Viz OSC destination host (default: {config.VIZ_HOST})",
    )
    ps.add_argument(
        "--viz-port",
        type=int,
        default=config.VIZ_PORT,
        dest="viz_port",
        help=f"Viz OSC destination UDP port (default: {config.VIZ_PORT})",
    )
    ps.add_argument(
        "--viz-prompt-source",
        choices=("auto", "manual", "mix"),
        default=config.VIZ_PROMPT_SOURCE_DEFAULT,
        dest="viz_prompt_source",
        help=f"Initial sidecar prompt source (default: {config.VIZ_PROMPT_SOURCE_DEFAULT})",
    )
    ps.set_defaults(func=_cmd_simulate)

    return p


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    ns = parser.parse_args(argv)
    raise SystemExit(ns.func(ns))
