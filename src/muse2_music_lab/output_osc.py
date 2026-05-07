"""OSC output backend. Mirrors the shape of `output_midi.MidiOut`."""

from __future__ import annotations

from typing import Optional

from pythonosc import udp_client

from muse2_music_lab import config


class OscOut:
    """Send continuous floats and discrete pulses over OSC/UDP."""

    def __init__(
        self,
        host: str = config.OSC_HOST,
        port: int = config.OSC_PORT,
    ) -> None:
        self.host = host
        self.port = int(port)
        self._client: Optional[udp_client.SimpleUDPClient] = (
            udp_client.SimpleUDPClient(host, self.port)
        )

    def send(self, address: str, value_0_1: float) -> None:
        """Send a float clamped to [0, 1] at `address`."""
        if self._client is None:
            return
        v = max(0.0, min(1.0, float(value_0_1)))
        self._client.send_message(address, v)

    def send_pulse(self, address: str) -> None:
        """Send a momentary 1.0 then 0.0 at `address`."""
        if self._client is None:
            return
        self._client.send_message(address, 1.0)
        self._client.send_message(address, 0.0)

    # Parity with MidiOut — accepts channel/cc-ish args but ignores them.
    def send_cc(self, channel: int, cc: int, value_0_1: float) -> None:  # pragma: no cover
        self.send(f"/cc/{int(channel)}/{int(cc)}", value_0_1)

    def close(self) -> None:
        self._client = None

    def __enter__(self) -> "OscOut":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
