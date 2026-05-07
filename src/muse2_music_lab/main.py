"""Main runtime loop: board -> features -> smoother -> mapping -> backend."""

from __future__ import annotations

import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from muse2_music_lab import config, mapping, viz_mapping
from muse2_music_lab.board import Board
from muse2_music_lab.features import (
    BlinkDetector,
    JawClenchDetector,
    FeatureFrame,
    compute_frame,
)
from muse2_music_lab.smoother import Calibrator, EMA, Normalizer


@dataclass
class RunOptions:
    backend: str = config.OUTPUT_BACKEND
    midi_port: str = config.MIDI_PORT_NAME
    osc_host: str = config.OSC_HOST
    osc_port: int = config.OSC_PORT
    calibrate_seconds: float = config.CALIBRATION_DURATION
    send_rate_hz: float = config.SEND_RATE_HZ
    window_size: int = config.WINDOW_SIZE
    smoothing_alpha: float = config.SMOOTHING_ALPHA
    tui: bool = False
    viz: bool = config.VIZ_ENABLED
    viz_host: str = config.VIZ_HOST
    viz_port: int = config.VIZ_PORT
    viz_prompt_source: str = config.VIZ_PROMPT_SOURCE_DEFAULT


def _open_backends(opts: RunOptions):
    midi = None
    osc = None
    backend = opts.backend.lower()
    if backend in ("midi", "both"):
        from muse2_music_lab.output_midi import MidiOut

        midi = MidiOut(opts.midi_port)
        print(f"[midi]  sending to port: {midi.port_name}")
    if backend in ("osc", "both"):
        from muse2_music_lab.output_osc import OscOut

        osc = OscOut(opts.osc_host, opts.osc_port)
        print(f"[osc]   sending to {opts.osc_host}:{opts.osc_port}")
    if midi is None and osc is None:
        raise ValueError(
            f"Unknown OUTPUT_BACKEND {opts.backend!r}. Use 'midi', 'osc', or 'both'."
        )
    return midi, osc


def _wait_for_window(board: Board, window_size: int, timeout_s: float = 10.0) -> np.ndarray:
    """Block until the board has at least `window_size` samples available."""
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


def _install_sigint(stop_flag: list[bool]) -> None:
    def _handler(signum, frame):
        stop_flag[0] = True

    try:
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        pass


def run(opts: Optional[RunOptions] = None) -> int:
    opts = opts or RunOptions()

    print("Brain mapping (DAW):")
    print(mapping.describe())

    midi, osc = _open_backends(opts)

    viz = None
    if opts.viz:
        from muse2_music_lab.viz_bridge import VizBridge

        viz = VizBridge(opts.viz_host, opts.viz_port)
        print(f"[viz]   sending to {opts.viz_host}:{opts.viz_port}")
        print("Brain mapping (viz):")
        print(viz_mapping.describe())
        viz.send_prompt("/viz/prompt/source", opts.viz_prompt_source)
        print(f"[viz]   prompt source: {opts.viz_prompt_source}")

    board = Board()
    try:
        info = board.start()
    except Exception as e:
        print(f"[board] Failed to start: {e}", file=sys.stderr)
        if midi is not None:
            midi.close()
        if osc is not None:
            osc.close()
        if viz is not None:
            viz.close()
        return 2

    print(
        f"[board] Streaming {len(info.eeg_channels)} EEG channels at "
        f"{info.sampling_rate} Hz"
    )

    blink = BlinkDetector()
    jaw = JawClenchDetector(sampling_rate=info.sampling_rate)

    emas: dict[str, EMA] = {
        name: EMA(alpha=opts.smoothing_alpha) for name in mapping.CONTINUOUS_NAMES
    }

    try:
        normalizer = _calibrate(board, info.sampling_rate, blink, jaw, opts)
    except Exception as e:
        print(f"[calib] Failed: {e}", file=sys.stderr)
        board.stop()
        if midi is not None:
            midi.close()
        if osc is not None:
            osc.close()
        if viz is not None:
            viz.close()
        return 3

    stop_flag = [False]
    _install_sigint(stop_flag)

    interval = 1.0 / max(opts.send_rate_hz, 1.0)
    next_tick = time.monotonic()
    print(f"[loop]  Running at {opts.send_rate_hz:.0f} Hz. Press Ctrl-C to stop.")

    last_log = 0.0
    try:
        while not stop_flag[0]:
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
                normalized[name] = max(0.0, min(1.0, normalizer.normalize(name, smoothed)))

            mapping.route(frame, normalized, midi=midi, osc=osc)

            if viz is not None:
                viz_mapping.route(frame, normalized, viz=viz)

            if now - last_log >= 1.0:
                last_log = now
                summary = "  ".join(
                    f"{k}={normalized[k]:.2f}" for k in mapping.CONTINUOUS_NAMES
                )
                trig = []
                if frame.blink:
                    trig.append("BLINK")
                if frame.jaw:
                    trig.append("JAW")
                tail = ("  [" + " ".join(trig) + "]") if trig else ""
                print(f"[loop]  {summary}{tail}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        board.stop()
        if midi is not None:
            midi.close()
        if osc is not None:
            osc.close()
        if viz is not None:
            viz.close()
        print("\n[exit]  Stopped cleanly.")

    return 0
