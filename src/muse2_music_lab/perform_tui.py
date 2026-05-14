"""Live rich.Live TUI for `muse2 perform`.

Activation order (intentional):

  1. Process starts -> boot logs scroll normally (BLE discovery, GATT
     subscribe, calibration spinner, Lyria connect handshake, prompt-guard
     status, etc.). The user sees exactly what's happening.

  2. Once `state.eeg_ready` fires (and `state.lyria_ready` too, when Lyria
     is enabled), this task takes over the bottom of the terminal with a
     rich.Live panel that refreshes at REFRESH_HZ.

  3. Other tasks keep using bare `print()` for events (errors, reconnects,
     recalibrate notices, filtered-prompt warnings). rich's Live region
     stays anchored at the bottom and lets those scroll above it. We
     also flip `state.tui_active = True` so periodic-summary tasks
     (currently just lyria/_control_loop) suppress their own prints
     while we own the screen.

  4. On cancellation (SIGINT or task-failed), we stop Live and clear
     the active flag so any final shutdown prints render cleanly.

Layout (single rich.Table, sectioned by separator rows):

    EEG
        alpha       0.42    [█████░░░░░░░░░]
        beta        0.61    [█████████░░░░░]
        theta       0.30    [████░░░░░░░░░░]
        asymmetry   0.55    [████████░░░░░░]   (centered indicator)

    Triggers
        blink       ·       [meter] / thr 225μV
        jaw         FIRED   [meter] / thr 160μV

    Lyria  (only when not --no-lyria)
        prompt      "downtempo electronic with warm analog synth pads"
        bpm         103
        density     0.42    [█████░░░░░░░░░]
        brightness  0.65    [█████████░░░░░]
        temperature 0.97    [████░░░░░░░░░░]   (range 0.6-1.8)
        chunks      328 received   queue 4/64

    Audio (Phase 6 placeholder)
        rms / centroid / onset all 0 today; will animate when Phase 6 lands.

    Footer:
        [r] recalibrate   [q] quit   |   uptime 02:14
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from rich.console import Console
from rich.live import Live
from rich.table import Table

from muse2_music_lab import config
from muse2_music_lab.state import AppState


REFRESH_HZ = 10           # how often the panel re-renders
BAR_WIDTH = 22            # characters per bar
TITLE = "muse2 perform"


@dataclass
class PerformTuiOptions:
    """Knobs the orchestrator passes through (a subset of PerformOptions)."""

    show_lyria: bool        # mirrors `not opts.no_lyria`
    show_audio_section: bool = True   # always-on placeholder until Phase 6


# ---------------------------------------------------------------------------
# Cell renderers
# ---------------------------------------------------------------------------


def _bar(value: float, width: int = BAR_WIDTH) -> str:
    v = max(0.0, min(1.0, float(value)))
    filled = int(round(v * width))
    return "█" * filled + "░" * (width - filled)


def _centered_bar(value: float, width: int = BAR_WIDTH) -> str:
    """Bar where 0.5 sits visually at the middle (for asymmetry)."""
    v = max(0.0, min(1.0, float(value)))
    half = width // 2
    if v >= 0.5:
        right_fill = int(round((v - 0.5) * 2 * half))
        left = "░" * half
        right = "█" * right_fill + "░" * (half - right_fill)
    else:
        left_fill = int(round((0.5 - v) * 2 * half))
        left = "░" * (half - left_fill) + "█" * left_fill
        right = "░" * half
    # The middle pipe character is a stable visual midline anchor.
    return left + "│" + right


def _trigger_meter(triggered: bool, live_uv: float, threshold_uv: float) -> str:
    if triggered:
        return f"[bold yellow]FIRED[/bold yellow]  ({live_uv:.0f}μV / thr {threshold_uv:.0f})"
    ratio = live_uv / threshold_uv if threshold_uv > 0 else 0.0
    return f"{_bar(min(ratio, 1.0))} {live_uv:6.0f}μV / thr {threshold_uv:.0f}"


def _format_uptime(seconds: float) -> str:
    s = max(0, int(seconds))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)] + "…"


# ---------------------------------------------------------------------------
# Table builder
# ---------------------------------------------------------------------------


def _render(state: AppState, opts: PerformTuiOptions) -> Table:
    table = Table(
        title=f"{TITLE}   ·   prompt: {_truncate(state.prompt, 60)!r}",
        title_style="bold",
        expand=True,
        show_lines=False,
        pad_edge=False,
    )
    table.add_column("Signal", style="bold cyan", no_wrap=True, width=14)
    table.add_column("Value", no_wrap=True, width=10)
    table.add_column("Meter", no_wrap=True)

    # ---------- EEG section ----------
    table.add_row("[dim]── EEG ──[/dim]", "", "")
    table.add_row("alpha",     f"{state.alpha:.2f}",     _bar(state.alpha))
    table.add_row("beta",      f"{state.beta:.2f}",      _bar(state.beta))
    table.add_row("theta",     f"{state.theta:.2f}",     _bar(state.theta))
    table.add_row("asymmetry", f"{state.asymmetry:.2f}", _centered_bar(state.asymmetry))

    # ---------- Trigger section ----------
    table.add_row("[dim]── Triggers ──[/dim]", "", "")
    table.add_row(
        "blink",
        "●" if state.blink_triggered else "·",
        _trigger_meter(
            state.blink_triggered,
            state.blink_ptp_uv,
            config.BLINK_THRESHOLD_UV,
        ),
    )
    table.add_row(
        "jaw",
        "●" if state.jaw_triggered else "·",
        _trigger_meter(
            state.jaw_triggered,
            state.jaw_rms_uv,
            config.JAW_THRESHOLD_UV,
        ),
    )

    # ---------- Lyria section ----------
    if opts.show_lyria:
        table.add_row("[dim]── Lyria ──[/dim]", "", "")
        if not state.lyria_ready.is_set():
            table.add_row("status", "[yellow]warming[/yellow]", "(awaiting first audio chunk)")
        else:
            # Map temperature back to a 0-1 range for the bar (it lives
            # in [LYRIA_TEMPERATURE_MIN, LYRIA_TEMPERATURE_MAX]).
            t_lo = config.LYRIA_TEMPERATURE_MIN
            t_hi = config.LYRIA_TEMPERATURE_MAX
            t_norm = (state.lyria_temperature - t_lo) / max(t_hi - t_lo, 1e-9)
            t_norm = max(0.0, min(1.0, t_norm))

            # Same for bpm (linear in [LYRIA_BPM_MIN, LYRIA_BPM_MAX]).
            b_lo = config.LYRIA_BPM_MIN
            b_hi = config.LYRIA_BPM_MAX
            b_norm = (state.lyria_bpm - b_lo) / max(b_hi - b_lo, 1)
            b_norm = max(0.0, min(1.0, b_norm))

            table.add_row("bpm",         f"{state.lyria_bpm:>3d}",                _bar(b_norm))
            table.add_row("density",     f"{state.lyria_density:.2f}",            _bar(state.lyria_density))
            table.add_row("brightness",  f"{state.lyria_brightness:.2f}",         _bar(state.lyria_brightness))
            table.add_row("temperature", f"{state.lyria_temperature:.2f}",        _bar(t_norm))

            qsize = state.audio_queue.qsize()
            qmax = config.LYRIA_AUDIO_QUEUE_MAX
            table.add_row(
                "stream",
                f"{state.lyria_chunks} ch",
                f"queue {qsize:>2d}/{qmax} {_bar(qsize / qmax, width=10)}",
            )

    # ---------- Audio-feature section (Phase 6) ----------
    if opts.show_audio_section:
        table.add_row("[dim]── Audio ──[/dim]", "", "")
        if opts.show_lyria and not state.audio_ready.is_set():
            # Lyria is on but no audio frame has been analyzed yet: keep the
            # placeholder visible so the user sees the section is initializing,
            # not broken.
            table.add_row("status", "[yellow]warming[/yellow]", "(awaiting first audio frame)")
        else:
            table.add_row("rms",      f"{state.rms:.2f}",      _bar(state.rms))
            table.add_row("centroid", f"{state.centroid:.2f}", _bar(state.centroid))
            table.add_row("onset",    f"{state.onset:.2f}",    _bar(state.onset))

    # ---------- Footer ----------
    # The square brackets around r/q are escaped (\\[ ... \\]) because rich's
    # markup parser would otherwise treat them as style tags and hide them.
    uptime = _format_uptime(time.monotonic() - state.session_start_ts)
    table.caption = (
        r"[bold cyan]\[r][/bold cyan]+Enter recalibrate   "
        r"[bold cyan]\[q][/bold cyan]+Enter quit   "
        f"[dim]│[/dim]   uptime {uptime}"
    )
    return table


# ---------------------------------------------------------------------------
# Task entry point
# ---------------------------------------------------------------------------


async def run_perform_tui(state: AppState, opts: PerformTuiOptions) -> None:
    """Wait for EEG to be ready, then drive a live panel.

    Activation is gated on `state.eeg_ready` only -- once the headset is
    calibrated (or instantly on the simulated path), the panel opens. The
    Lyria section internally renders "warming up..." until `lyria_ready`
    fires, so a slow / failing Lyria never blocks the panel from appearing.
    The operator always at least sees their brain.

    On cancellation, clears `state.tui_active` so any tail-end prints
    from cleanup tasks render normally.
    """
    # Wait until we have something meaningful to show. Prevents the panel
    # from flashing zeros during startup.
    print("[tui] waiting for EEG ready...", flush=True)
    await state.eeg_ready.wait()
    print("[tui] panel active. Boot logs above; live state below.\n", flush=True)

    console = Console()
    state.tui_active = True

    refresh_per_second = max(1, int(REFRESH_HZ))
    sleep_s = 1.0 / refresh_per_second

    try:
        with Live(
            _render(state, opts),
            console=console,
            refresh_per_second=refresh_per_second,
            screen=False,           # cooperate with bare print() above us
            transient=False,        # leave the final frame visible on exit
            vertical_overflow="visible",
        ) as live:
            while True:
                live.update(_render(state, opts))
                await asyncio.sleep(sleep_s)
    except asyncio.CancelledError:
        # Let Live's __exit__ tear down the rendering region cleanly,
        # then clear the active flag so subsequent prints (e.g. "stopped
        # cleanly.") don't get suppressed by the lyria-ctrl gate.
        raise
    finally:
        state.tui_active = False
