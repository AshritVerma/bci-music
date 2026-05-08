"""Synthetic brain loop for headset-free testing.

Generates believable FeatureFrame values from sine sweeps + periodic
triggers and pushes them through the same `mapping.route()` and
`viz_mapping.route()` paths the real `run()` loop uses. Lets you verify
the entire output chain (MIDI to IAC + viz OSC to sidecar to Syphon)
without wearing the headset.

Useful for: TouchDesigner project assembly, Logic Pro MIDI-learn, smoke
tests, and any debugging where putting the band on is friction.
"""

from __future__ import annotations

import math
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

from muse2_music_lab import config, mapping, viz_mapping
from muse2_music_lab.features import FeatureFrame


@dataclass
class SimulateOptions:
    backend: str = config.OUTPUT_BACKEND
    midi_port: str = config.MIDI_PORT_NAME
    osc_host: str = config.OSC_HOST
    osc_port: int = config.OSC_PORT
    send_rate_hz: float = config.SEND_RATE_HZ
    duration_s: float = 0.0  # 0 means run until Ctrl-C

    viz: bool = config.VIZ_ENABLED
    viz_host: str = config.VIZ_HOST
    viz_port: int = config.VIZ_PORT
    viz_prompt_source: str = config.VIZ_PROMPT_SOURCE_DEFAULT

    blink_period_s: float = 4.0
    jaw_period_s: float = 7.0


def _open_backends(opts: SimulateOptions):
    midi = osc = None
    backend = opts.backend.lower()
    if backend in ("midi", "both"):
        from muse2_music_lab.output_midi import MidiOut

        midi = MidiOut(opts.midi_port)
        print(f"[midi]  sending to: {midi.port_name}")
    if backend in ("osc", "both"):
        from muse2_music_lab.output_osc import OscOut

        osc = OscOut(opts.osc_host, opts.osc_port)
        print(f"[osc]   sending to {opts.osc_host}:{opts.osc_port}")
    if midi is None and osc is None:
        raise ValueError(
            f"Unknown backend {opts.backend!r}; use 'midi', 'osc', or 'both'."
        )
    return midi, osc


def _synthetic_frame(t: float, blink_period: float, jaw_period: float):
    """Return (FeatureFrame, normalized_dict) for time `t` (seconds).

    Sweep periods are mutually prime-ish so the visual stays visually busy
    rather than locking into an obvious LFO pattern.
    """
    alpha = 0.5 + 0.4 * math.sin(2 * math.pi * t / 8.0)
    beta = 0.5 + 0.4 * math.sin(2 * math.pi * t / 5.0 + 1.0)
    theta = 0.5 + 0.3 * math.sin(2 * math.pi * t / 11.0 + 0.5)
    focus = 0.5 + 0.4 * math.sin(2 * math.pi * t / 6.0 + 0.3)
    calm = 0.5 + 0.4 * math.sin(2 * math.pi * t / 9.0 + 2.1)

    # Periodic discrete triggers, slight jitter so they don't align to render ticks
    blink = (t % blink_period) < (1.0 / 30.0)
    jaw = (t % jaw_period) < (1.0 / 30.0)

    normalized = {
        "alpha": max(0.0, min(1.0, alpha)),
        "beta":  max(0.0, min(1.0, beta)),
        "theta": max(0.0, min(1.0, theta)),
        "focus": max(0.0, min(1.0, focus)),
        "calm":  max(0.0, min(1.0, calm)),
    }
    frame = FeatureFrame(
        alpha=normalized["alpha"],
        beta=normalized["beta"],
        theta=normalized["theta"],
        focus=normalized["focus"],
        calm=normalized["calm"],
        blink=bool(blink),
        jaw=bool(jaw),
    )
    return frame, normalized


def _install_sigint(stop_flag: list[bool]) -> None:
    def _handler(_signum, _frame):
        stop_flag[0] = True
    try:
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        pass


def run(opts: Optional[SimulateOptions] = None) -> int:
    opts = opts or SimulateOptions()

    print("Brain mapping (DAW):")
    print(mapping.describe())

    try:
        midi, osc = _open_backends(opts)
    except Exception as e:
        print(f"[sim]   failed to open backends: {e}", file=sys.stderr)
        return 2

    viz = None
    if opts.viz:
        from muse2_music_lab.viz_bridge import VizBridge
        viz = VizBridge(opts.viz_host, opts.viz_port)
        print(f"[viz]   sending to {opts.viz_host}:{opts.viz_port}")
        print("Brain mapping (viz):")
        print(viz_mapping.describe())
        viz.send_prompt("/viz/prompt/source", opts.viz_prompt_source)
        print(f"[viz]   prompt source: {opts.viz_prompt_source}")

    print(
        f"\n[sim]   simulating brain at {opts.send_rate_hz:.0f} Hz"
        + (f" for {opts.duration_s:.0f}s" if opts.duration_s > 0 else " (Ctrl-C to stop)")
    )

    stop_flag = [False]
    _install_sigint(stop_flag)

    interval = 1.0 / max(opts.send_rate_hz, 1.0)
    start = time.monotonic()
    next_tick = start
    last_log = start
    frame_count = 0

    try:
        while not stop_flag[0]:
            now = time.monotonic()
            elapsed = now - start
            if opts.duration_s > 0 and elapsed >= opts.duration_s:
                break
            if now < next_tick:
                time.sleep(min(interval, next_tick - now))
                continue
            next_tick += interval
            if next_tick < now:
                next_tick = now + interval

            frame, normalized = _synthetic_frame(
                elapsed, opts.blink_period_s, opts.jaw_period_s
            )
            mapping.route(frame, normalized, midi=midi, osc=osc)
            if viz is not None:
                viz_mapping.route(frame, normalized, viz=viz)
            frame_count += 1

            if now - last_log >= 1.0:
                last_log = now
                summary = "  ".join(
                    f"{k}={normalized[k]:.2f}"
                    for k in ("alpha", "beta", "theta", "focus", "calm")
                )
                tags = []
                if frame.blink:
                    tags.append("BLINK")
                if frame.jaw:
                    tags.append("JAW")
                tail = ("  [" + " ".join(tags) + "]") if tags else ""
                print(f"[sim]   t={elapsed:5.1f}s  {summary}{tail}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if midi is not None:
            midi.close()
        if osc is not None:
            osc.close()
        if viz is not None:
            viz.close()
        print(f"\n[exit]  sent {frame_count} synthetic frames. stopped cleanly.")

    return 0
