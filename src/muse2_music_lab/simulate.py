"""Synthetic brain loop for headset-free TUI testing.

Generates believable FeatureFrame values from sine sweeps + periodic
triggers and renders them through the same TUI table that the live `run`
flow uses. Lets you verify the diagnostic display, calibration UI, and
overall pipeline without putting the headset on.

After Phase 0 cleanup, simulate is TUI-only -- the previous MIDI/OSC
output paths are gone. The new generative pipeline (`muse2-music perform`)
has its own `--simulate-eeg` flag for headset-free testing of the live
demo.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

from rich.console import Console
from rich.live import Live

from muse2_music_lab.eeg.features import FeatureFrame
from muse2_music_lab.tui import CONTINUOUS_NAMES, render_table


@dataclass
class SimulateOptions:
    send_rate_hz: float = 30.0
    duration_s: float = 0.0  # 0 means run until Ctrl-C
    # Tightened 2026-05-14: 4.0/7.0 was too sparse for the live demo.
    # 2.0/3.5 fires roughly twice as often (≈30 blinks/min, ≈17
    # jaws/min) so the trigger meters visibly punch on the TUI / browser
    # without looking dead. Real headset usage stays unchanged --
    # those numbers come from BlinkDetector / JawClenchDetector, not
    # from this synthetic generator.
    blink_period_s: float = 2.0
    jaw_period_s: float = 3.5


# Trigger envelope shape: idle baseline + small shimmer + decaying spike at
# each event boundary. Tuned so the live μV value crosses the configured
# threshold for ~3-5 frames around the boolean event, then settles back.
#
#   peak_uv   - amplitude of the spike at each event boundary
#   idle_uv   - resting baseline (well below threshold)
#   decay_s   - time constant of the exponential spike decay
#   shimmer_uv / shimmer_period_s - small sine wiggle on top of idle so the
#                                    meter is visually alive between events
def _trigger_envelope(
    t: float,
    period: float,
    peak_uv: float,
    idle_uv: float,
    decay_s: float,
    shimmer_uv: float,
    shimmer_period_s: float,
) -> float:
    phase = t % period
    spike = peak_uv * math.exp(-phase / max(decay_s, 1e-3))
    shimmer = shimmer_uv * math.sin(2 * math.pi * t / max(shimmer_period_s, 1e-3))
    return max(0.0, idle_uv + shimmer + spike)


_TRIGGER_WINDOW_S = 0.30
"""Width of the per-period boolean window for synthetic blink/jaw events.

Sized to be reliably larger than one PERFORM_TICK_S (0.25s, the
``--simulate-eeg`` cadence) so every period boundary catches at least
one tick. A narrower window (the original ``1/30s = 33ms``) effectively
worked only at the 30Hz standalone simulate TUI rate and silently
dropped most events at 4Hz, which made the browser HUD look like the
synthetic blink/jaw triggers were broken. At 30Hz the wider window
keeps the boolean True for ~9 consecutive frames, which actually
matches the visual decay of the μV envelope below better than the
old single-frame pulse."""


def synthetic_frame(
    t: float,
    blink_period: float = 2.0,
    jaw_period: float = 3.5,
) -> tuple[FeatureFrame, dict[str, float], float, float]:
    """Return (FeatureFrame, normalized_dict, blink_uv, jaw_uv) for time `t`.

    Sweep periods are mutually prime-ish so the visual stays busy rather
    than locking into an obvious LFO pattern. The two extra μV values feed
    the live trigger meters in the TUI -- without them the blink/jaw rows
    would always show "0μV" even when the event boolean fires.
    """
    alpha = 0.5 + 0.4 * math.sin(2 * math.pi * t / 8.0)
    beta = 0.5 + 0.4 * math.sin(2 * math.pi * t / 5.0 + 1.0)
    theta = 0.5 + 0.3 * math.sin(2 * math.pi * t / 11.0 + 0.5)
    focus = 0.5 + 0.4 * math.sin(2 * math.pi * t / 6.0 + 0.3)
    calm = 0.5 + 0.4 * math.sin(2 * math.pi * t / 9.0 + 2.1)

    blink = (t % blink_period) < _TRIGGER_WINDOW_S
    jaw = (t % jaw_period) < _TRIGGER_WINDOW_S

    # Realistic-feeling μV envelopes for the trigger meters. Peak values
    # sit comfortably above the (post-tuning) thresholds in config.py so
    # the FIRED label, the meter saturation, and the boolean event line up.
    # Re-tuned 2026-05-13 evening alongside the BLINK/JAW threshold bump
    # to keep this visual contract intact.
    blink_uv = _trigger_envelope(
        t,
        period=blink_period,
        peak_uv=1800.0,       # spike to ~1850μV (idle + peak), thr is 1000
        idle_uv=50.0,
        decay_s=0.18,
        shimmer_uv=8.0,
        shimmer_period_s=1.7,
    )
    jaw_uv = _trigger_envelope(
        t,
        period=jaw_period,
        peak_uv=400.0,        # spike to ~470μV (idle + peak), thr is 220
        idle_uv=70.0,
        decay_s=0.40,
        shimmer_uv=12.0,
        shimmer_period_s=2.3,
    )

    normalized = {
        "alpha": max(0.0, min(1.0, alpha)),
        "beta": max(0.0, min(1.0, beta)),
        "theta": max(0.0, min(1.0, theta)),
        "focus": max(0.0, min(1.0, focus)),
        "calm": max(0.0, min(1.0, calm)),
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
    return frame, normalized, blink_uv, jaw_uv


def run(opts: Optional[SimulateOptions] = None) -> int:
    opts = opts or SimulateOptions()
    console = Console()

    title = (
        f"muse2-music simulate · synthetic · "
        f"{opts.send_rate_hz:.0f} Hz · TUI"
    )
    print(
        f"[sim]   simulating brain at {opts.send_rate_hz:.0f} Hz"
        + (
            f" for {opts.duration_s:.0f}s"
            if opts.duration_s > 0
            else " (Ctrl-C to stop)"
        ),
        flush=True,
    )

    interval = 1.0 / max(opts.send_rate_hz, 1.0)
    start = time.monotonic()
    next_tick = start
    frame_count = 0

    try:
        with Live(
            render_table(
                {n: 0.0 for n in CONTINUOUS_NAMES},
                {"blink": False, "jaw": False},
                title,
                show_keys_help=False,
            ),
            console=console,
            refresh_per_second=min(opts.send_rate_hz, 15),
            screen=False,
        ) as live:
            while True:
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

                frame, normalized, blink_uv, jaw_uv = synthetic_frame(
                    elapsed, opts.blink_period_s, opts.jaw_period_s
                )
                frame_count += 1

                live.update(
                    render_table(
                        normalized,
                        {"blink": frame.blink, "jaw": frame.jaw},
                        title,
                        blink_ptp_uv=blink_uv,
                        jaw_rms_uv=jaw_uv,
                        show_keys_help=False,
                    )
                )
    except KeyboardInterrupt:
        pass
    finally:
        console.print(
            f"\n[exit]  sent {frame_count} synthetic frames. stopped cleanly."
        )

    return 0
