"""MIDI CC output backend. Uses `mido` + `python-rtmidi`."""

from __future__ import annotations

from typing import List, Optional

import mido

from muse2_music_lab import config


def list_output_ports() -> List[str]:
    """Return available MIDI output port names."""
    return list(mido.get_output_names())


def _clamp_cc(value: float) -> int:
    v = int(round(float(value) * 127.0))
    return max(0, min(127, v))


class MidiOut:
    """Send CC and momentary-pulse messages to a named MIDI port.

    Tries to open `port_name` directly. If not found, opens a virtual port with
    that name (fine on macOS/Linux; Windows rtmidi does not support virtual
    ports and will raise).
    """

    def __init__(self, port_name: str = config.MIDI_PORT_NAME) -> None:
        self.port_name = port_name
        self._port = self._open(port_name)
        self._last_cc: dict[tuple[int, int], int] = {}

    @staticmethod
    def _open(port_name: str):
        names = mido.get_output_names()
        for name in names:
            if name == port_name or port_name in name:
                return mido.open_output(name)
        return mido.open_output(port_name, virtual=True)

    def send_cc(self, channel: int, cc: int, value_0_1: float) -> None:
        """Send a CC message. `channel` is 1-based; `value_0_1` is [0, 1]."""
        ch = max(1, min(16, int(channel))) - 1
        cc_num = max(0, min(127, int(cc)))
        v = _clamp_cc(value_0_1)
        key = (ch, cc_num)
        if self._last_cc.get(key) == v:
            return
        self._last_cc[key] = v
        self._port.send(mido.Message("control_change", channel=ch, control=cc_num, value=v))

    def send_pulse(self, channel: int, cc: int) -> None:
        """Momentary 127 -> 0 pulse for a trigger-style CC."""
        ch = max(1, min(16, int(channel))) - 1
        cc_num = max(0, min(127, int(cc)))
        self._port.send(mido.Message("control_change", channel=ch, control=cc_num, value=127))
        self._port.send(mido.Message("control_change", channel=ch, control=cc_num, value=0))
        self._last_cc[(ch, cc_num)] = 0

    def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            finally:
                self._port = None  # type: ignore[assignment]

    def __enter__(self) -> "MidiOut":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
