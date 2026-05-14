"""Live terminal UI for EEG diagnostics.

Used by both `muse2-music run` (real Muse 2 over BLE) and `muse2-music
simulate` (synthetic brain). Shows a continuously updating table of feature
values with calibration status. Press `r` + Enter to re-calibrate without
restarting; `q` + Enter to quit.

This is the legacy diagnostic surface kept after the Phase 0 cleanup. It does
not produce any music or visual output -- that's `muse2-music perform`.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

import numpy as np
from rich.console import Console
from rich.live import Live
from rich.table import Table

from muse2_music_lab import config
from muse2_music_lab.eeg.board import Board, BoardInfo
from muse2_music_lab.eeg.features import (
    BlinkDetector,
    JawClenchDetector,
    FeatureFrame,
    compute_frame,
)
from muse2_music_lab.eeg.smoother import Baseline, Calibrator, EMA, Normalizer


CONTINUOUS_NAMES: tuple[str, ...] = ("focus", "calm", "alpha", "beta", "theta")
PULSE_NAMES: tuple[str, ...] = ("blink", "jaw")


@dataclass
class RunOptions:
    """Tunables for the live `run` flow."""

    calibrate_seconds: float = config.CALIBRATION_DURATION
    send_rate_hz: float = config.SEND_RATE_HZ
    window_size: int = config.WINDOW_SIZE
    smoothing_alpha: float = config.SMOOTHING_ALPHA


# ---------------------------------------------------------------------------
# Stdin keyboard shortcuts
# ---------------------------------------------------------------------------


class _StdinReader(threading.Thread):
    """Background thread: 'r' = recalibrate, 'q' = quit."""

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


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _bar(value: float, width: int = 20) -> str:
    v = max(0.0, min(1.0, float(value)))
    filled = int(round(v * width))
    return "█" * filled + "░" * (width - filled)


def _trigger_meter(triggered: bool, live_uv: float, threshold_uv: float) -> str:
    if triggered:
        return f"[bold]FIRED[/bold]  ({live_uv:.0f}μV / thr {threshold_uv:.0f})"
    ratio = live_uv / threshold_uv if threshold_uv > 0 else 0.0
    bar = _bar(min(ratio, 1.0))
    return f"{bar} {live_uv:6.0f}μV / thr {threshold_uv:.0f}"


def render_table(
    values: dict[str, float],
    triggers: dict[str, bool],
    title: str,
    blink_ptp_uv: float = 0.0,
    jaw_rms_uv: float = 0.0,
    *,
    calibrated: bool = True,
    show_keys_help: bool = True,
) -> Table:
    """Build the live table. Shared by `run` (real EEG) and `simulate`."""
    table = Table(title=title, expand=True)
    table.add_column("Signal", style="bold", no_wrap=True)
    table.add_column("Value", no_wrap=True)
    table.add_column("Meter", no_wrap=True)

    for name in CONTINUOUS_NAMES:
        v = values.get(name, 0.0)
        table.add_row(name, f"{v:.2f}", _bar(v))

    for name in PULSE_NAMES:
        triggered = triggers.get(name, False)
        value_text = "●" if triggered else "·"
        if name == "blink":
            meter = _trigger_meter(
                triggered, blink_ptp_uv, config.BLINK_THRESHOLD_UV
            )
        else:
            meter = _trigger_meter(
                triggered, jaw_rms_uv, config.JAW_THRESHOLD_UV
            )
        table.add_row(name, value_text, meter)

    status = "calibrated" if calibrated else "not calibrated"
    if show_keys_help:
        table.caption = (
            f"[{status}]   press 'r'+Enter to re-calibrate   'q'+Enter to quit"
        )
    else:
        table.caption = f"[{status}]"
    return table


# ---------------------------------------------------------------------------
# Real-board acquisition + calibration
# ---------------------------------------------------------------------------


def _wait_for_window(board: Board, window_size: int, timeout_s: float = 10.0) -> np.ndarray:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        w = board.get_window(window_size)
        if w.shape[1] >= window_size:
            return w
        time.sleep(0.05)
    raise TimeoutError(
        f"Timed out waiting for {window_size} samples from the board."
    )


def _calibrate(
    board: Board,
    sampling_rate: int,
    blink: BlinkDetector,
    jaw: JawClenchDetector,
    opts: RunOptions,
) -> Normalizer:
    print(
        f"[calib] Sit still and relax with eyes open for "
        f"{opts.calibrate_seconds:.1f}s...",
        flush=True,
    )
    _wait_for_window(board, opts.window_size)

    calibrator = Calibrator(names=("alpha", "beta", "theta", "focus", "calm"))
    interval = 1.0 / max(opts.send_rate_hz, 1.0)
    end = time.monotonic() + opts.calibrate_seconds
    while time.monotonic() < end:
        window = board.get_window(opts.window_size)
        if window.shape[1] < opts.window_size:
            time.sleep(interval)
            continue
        frame = compute_frame(window, sampling_rate, blink, jaw)
        calibrator.add(frame.continuous())
        time.sleep(interval)

    norm = Normalizer(calibrator.finish())
    print("[calib] Baseline captured:")
    for name, base in norm.baselines.items():
        print(f"        {name:<6} mean={base.mean:.3f}  std={base.std:.3f}")
    return norm


# ---------------------------------------------------------------------------
# Live `run` flow (real Muse 2)
# ---------------------------------------------------------------------------


def run_with_tui(opts: Optional[RunOptions] = None) -> int:
    """Connect to a real Muse 2 and drive the TUI loop."""
    opts = opts or RunOptions()
    console = Console()

    board = Board()
    try:
        info = board.start()
    except Exception as e:
        console.print(f"[red]Board failed to start:[/red] {e}")
        return 2

    blink = BlinkDetector()
    jaw = JawClenchDetector(sampling_rate=info.sampling_rate)
    emas = {n: EMA(alpha=opts.smoothing_alpha) for n in CONTINUOUS_NAMES}

    try:
        normalizer = _calibrate(board, info.sampling_rate, blink, jaw, opts)
    except Exception as e:
        console.print(f"[red]Calibration failed:[/red] {e}")
        board.stop()
        return 3

    reader = _StdinReader()
    reader.start()

    title = f"muse2-music run · {info.sampling_rate} Hz · TUI diagnostics"
    interval = 1.0 / max(opts.send_rate_hz, 1.0)
    next_tick = time.monotonic()
    latest_values: dict[str, float] = {n: 0.0 for n in CONTINUOUS_NAMES}
    latest_triggers: dict[str, bool] = {n: False for n in PULSE_NAMES}

    try:
        with Live(
            render_table(
                latest_values,
                latest_triggers,
                title,
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

                normalized: dict[str, float] = {}
                for name in CONTINUOUS_NAMES:
                    raw = getattr(frame, name, 0.0)
                    smoothed = emas[name].update(float(raw))
                    normalized[name] = max(
                        0.0, min(1.0, normalizer.normalize(name, smoothed))
                    )
                latest_values = normalized
                latest_triggers = {"blink": frame.blink, "jaw": frame.jaw}

                live.update(
                    render_table(
                        latest_values,
                        latest_triggers,
                        title,
                        blink.last_ptp,
                        jaw.last_rms,
                    )
                )
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        board.stop()
        console.print("\n[exit] Stopped cleanly.")
    return 0
