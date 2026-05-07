"""Live terminal UI for the brain → DAW pipeline.

Requires the `tui` extra (rich). Shows a continuously updating table of
feature values with their mapping targets and calibration status. Press
`r` + Enter to re-calibrate without restarting.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from muse2_music_lab import config, mapping
from muse2_music_lab.board import Board
from muse2_music_lab.features import (
    BlinkDetector,
    JawClenchDetector,
    compute_frame,
)
from muse2_music_lab.main import RunOptions, _calibrate, _open_backends
from muse2_music_lab.smoother import EMA, Normalizer


class _StdinReader(threading.Thread):
    """Background thread that sets a recalibrate flag when the user types 'r'."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop = False
        self.recalibrate = False
        self.quit = False

    def run(self) -> None:
        while not self._stop:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            ch = line.strip().lower()
            if ch == "r":
                self.recalibrate = True
            elif ch in ("q", "quit", "exit"):
                self.quit = True

    def stop(self) -> None:
        self._stop = True


def _bar(value: float, width: int = 20) -> str:
    v = max(0.0, min(1.0, float(value)))
    filled = int(round(v * width))
    return "█" * filled + "░" * (width - filled)


def _trigger_meter(name: str, triggered: bool, live_uv: float, threshold_uv: float) -> str:
    """Show live amplitude vs threshold for pulse-type signals."""
    if triggered:
        return f"[bold]FIRED[/bold]  ({live_uv:.0f}μV / thr {threshold_uv:.0f})"
    ratio = live_uv / threshold_uv if threshold_uv > 0 else 0.0
    bar = _bar(min(ratio, 1.0))
    return f"{bar} {live_uv:6.0f}μV / thr {threshold_uv:.0f}"


def _render_table(
    values: dict[str, float],
    triggers: dict[str, bool],
    baselines_known: bool,
    sr: int,
    blink_ptp_uv: float = 0.0,
    jaw_rms_uv: float = 0.0,
) -> Table:
    table = Table(
        title=f"muse2-music · {sr} Hz · {config.OUTPUT_BACKEND.upper()}",
        expand=True,
    )
    table.add_column("Signal", style="bold", no_wrap=True)
    table.add_column("Value", no_wrap=True)
    table.add_column("Meter", no_wrap=True)
    table.add_column("Mapping", no_wrap=True)

    for name, spec in mapping.MAPPINGS.items():
        route_str = f"ch{spec['channel']} cc{spec['cc']}   {spec['osc']}"
        if spec["type"] == "cc":
            v = values.get(name, 0.0)
            value_text = f"{v:.2f}"
            meter = _bar(v)
        else:
            triggered = triggers.get(name, False)
            value_text = "●" if triggered else "·"
            if name == "blink":
                meter = _trigger_meter(
                    name, triggered, blink_ptp_uv, config.BLINK_THRESHOLD_UV
                )
            elif name == "jaw":
                meter = _trigger_meter(
                    name, triggered, jaw_rms_uv, config.JAW_THRESHOLD_UV
                )
            else:
                meter = "[bold]FIRED[/bold]" if triggered else ""
        table.add_row(name, value_text, meter, route_str)

    status = "calibrated" if baselines_known else "not calibrated"
    table.caption = (
        f"[{status}]   press 'r'+Enter to re-calibrate   'q'+Enter to quit"
    )
    return table


def run_with_tui(opts: Optional[RunOptions] = None) -> int:
    opts = opts or RunOptions()
    console = Console()

    console.print("Brain mapping:")
    console.print(Text(mapping.describe()))

    midi, osc = _open_backends(opts)

    board = Board()
    try:
        info = board.start()
    except Exception as e:
        console.print(f"[red]Board failed to start:[/red] {e}")
        if midi is not None:
            midi.close()
        if osc is not None:
            osc.close()
        return 2

    blink = BlinkDetector()
    jaw = JawClenchDetector(sampling_rate=info.sampling_rate)

    emas = {name: EMA(alpha=opts.smoothing_alpha) for name in mapping.CONTINUOUS_NAMES}
    normalizer: Normalizer
    try:
        normalizer = _calibrate(board, info.sampling_rate, blink, jaw, opts)
    except Exception as e:
        console.print(f"[red]Calibration failed:[/red] {e}")
        board.stop()
        if midi is not None:
            midi.close()
        if osc is not None:
            osc.close()
        return 3

    reader = _StdinReader()
    reader.start()

    interval = 1.0 / max(opts.send_rate_hz, 1.0)
    next_tick = time.monotonic()
    latest_values: dict[str, float] = {n: 0.0 for n in mapping.CONTINUOUS_NAMES}
    latest_triggers: dict[str, bool] = {n: False for n in mapping.PULSE_NAMES}

    try:
        with Live(
            _render_table(
                latest_values,
                latest_triggers,
                True,
                info.sampling_rate,
                blink.last_ptp,
                jaw.last_rms,
            ),
            console=console,
            refresh_per_second=min(opts.send_rate_hz, 15),
            screen=False,
        ) as live:
            while not reader.quit:
                if reader.recalibrate:
                    reader.recalibrate = False
                    live.stop()
                    try:
                        normalizer = _calibrate(
                            board, info.sampling_rate, blink, jaw, opts
                        )
                    finally:
                        live.start()

                now = time.monotonic()
                if now < next_tick:
                    time.sleep(min(interval, next_tick - now))
                    continue
                next_tick += interval
                if next_tick < now:
                    next_tick = now + interval

                window = board.get_window(opts.window_size)
                if window.shape[1] < opts.window_size:
                    continue

                frame = compute_frame(window, info.sampling_rate, blink, jaw)

                normalized = {}
                for name in mapping.CONTINUOUS_NAMES:
                    raw = getattr(frame, name, 0.0)
                    smoothed = emas[name].update(float(raw))
                    normalized[name] = max(
                        0.0, min(1.0, normalizer.normalize(name, smoothed))
                    )
                latest_values = normalized
                latest_triggers = {"blink": frame.blink, "jaw": frame.jaw}

                mapping.route(frame, normalized, midi=midi, osc=osc)

                live.update(
                    _render_table(
                        latest_values,
                        latest_triggers,
                        True,
                        info.sampling_rate,
                        blink.last_ptp,
                        jaw.last_rms,
                    )
                )
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        board.stop()
        if midi is not None:
            midi.close()
        if osc is not None:
            osc.close()
        console.print("\n[exit] Stopped cleanly.")
    return 0
