"""Live MIDI input monitor.

Opens a named MIDI input port (typically the same IAC bus that
`muse2-music run` writes to) and displays incoming CC + note traffic in a
live `rich` table. Useful for verifying the MIDI side of the pipeline
without needing Logic Pro open.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Tuple

import mido

from muse2_music_lab import config, mapping
from muse2_music_lab.output_midi import list_input_ports


# (channel, cc) -> latest state
_KEY = Tuple[int, int]


@dataclass
class _CCState:
    value: int = 0
    last_ts: float = 0.0
    count: int = 0


def _open_input(port_name: str):
    """Open a named MIDI input port. Falls back to a virtual port on POSIX."""
    names = list_input_ports()
    for name in names:
        if name == port_name or port_name in name:
            return mido.open_input(name)
    # Last-ditch: try opening a virtual port with that name (POSIX only).
    return mido.open_input(port_name, virtual=True)


def _cc_label(channel_1based: int, cc: int) -> str:
    """Look up a friendly name from mapping.MAPPINGS, or return blank."""
    for name, spec in mapping.MAPPINGS.items():
        if (
            int(spec.get("channel", 0)) == int(channel_1based)
            and int(spec.get("cc", -1)) == int(cc)
        ):
            return name
    return ""


def run(port_name: str, refresh_hz: float = 15.0) -> int:
    """Block forever monitoring `port_name`. Returns 0 on clean Ctrl-C."""
    try:
        from rich.console import Console
        from rich.live import Live
        from rich.table import Table
        from rich import box
    except ImportError:
        print(
            "[midi-monitor] 'rich' is required for the live display.\n"
            "Install with: pip install -e '.[tui]'",
            file=sys.stderr,
        )
        return 1

    print(f"[midi-monitor] available input ports: {list_input_ports()}")
    try:
        inp = _open_input(port_name)
    except (OSError, IOError) as e:
        print(f"[midi-monitor] failed to open {port_name!r}: {e}", file=sys.stderr)
        print(
            "On macOS, enable the IAC Driver in Audio MIDI Setup "
            "(Window -> Show MIDI Studio -> double-click IAC Driver -> "
            "tick 'Device is online').",
            file=sys.stderr,
        )
        return 2
    print(f"[midi-monitor] listening on {port_name!r}. Press Ctrl-C to stop.")

    state: Dict[_KEY, _CCState] = {}
    note_state: Dict[int, dict] = {}
    pulse_recent: Dict[_KEY, float] = {}
    started = time.monotonic()
    msg_count = 0

    console = Console()

    def render() -> Table:
        now = time.monotonic()
        elapsed = now - started

        t = Table(
            title=(
                f"MIDI input: {port_name!r}   "
                f"({msg_count} msgs in {elapsed:.0f}s)"
            ),
            box=box.SIMPLE_HEAVY,
            expand=True,
            title_style="bold cyan",
        )
        t.add_column("Ch", justify="right", width=3)
        t.add_column("CC", justify="right", width=4)
        t.add_column("Name", style="bold")
        t.add_column("Value", justify="right", width=6)
        t.add_column("Norm", justify="right", width=6)
        t.add_column("Bar", min_width=20)
        t.add_column("Hits", justify="right", width=5)
        t.add_column("Age", justify="right", width=6)

        for (ch, cc), s in sorted(state.items()):
            label = _cc_label(ch, cc)
            norm = s.value / 127.0
            age = now - s.last_ts
            # Pulse rows: detect via short interval between consecutive 127s
            pulse_age = now - pulse_recent.get((ch, cc), 0.0) if pulse_recent.get((ch, cc)) else None
            if pulse_age is not None and pulse_age < 0.4:
                bar = "[bold red]" + "*" * 20 + "[/bold red]"
                value_str = "PULSE"
                norm_str = "-"
            else:
                fill = int(norm * 20)
                bar = "[green]" + "#" * fill + "[/green]" + "." * (20 - fill)
                value_str = str(s.value)
                norm_str = f"{norm:.2f}"

            t.add_row(
                str(ch),
                str(cc),
                label,
                value_str,
                norm_str,
                bar,
                str(s.count),
                f"{age:.1f}s" if age < 99 else "—",
            )

        if not state:
            t.add_row("—", "—", "[dim]waiting for MIDI…[/dim]", "", "", "", "", "")

        return t

    interval = 1.0 / refresh_hz
    try:
        with Live(render(), console=console, refresh_per_second=refresh_hz, screen=False) as live:
            last_render = 0.0
            while True:
                got_message = False
                for msg in inp.iter_pending():
                    msg_count += 1
                    if msg.type == "control_change":
                        ch_1 = msg.channel + 1
                        key = (ch_1, msg.control)
                        s = state.setdefault(key, _CCState())
                        # detect pulse: a 127 immediately followed by 0 (within ~1 frame)
                        if s.value > 0 and msg.value == 0 and (time.monotonic() - s.last_ts) < 0.05:
                            pulse_recent[key] = time.monotonic()
                        s.value = msg.value
                        s.last_ts = time.monotonic()
                        s.count += 1
                        got_message = True
                    elif msg.type in ("note_on", "note_off"):
                        note_state[msg.note] = {
                            "type": msg.type,
                            "velocity": getattr(msg, "velocity", 0),
                            "channel": msg.channel + 1,
                            "ts": time.monotonic(),
                        }
                        got_message = True

                now = time.monotonic()
                if got_message or (now - last_render) > interval:
                    live.update(render())
                    last_render = now
                else:
                    time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        inp.close()
        print(f"\n[midi-monitor] stopped. saw {msg_count} messages.")
    return 0
