"""OSC bridge for the visual layer.

Mirrors `output_osc.OscOut` in shape but targets a separate host/port so the
visual bus and the DAW bus don't collide. Used by `main.run()` only when
the `--viz` flag is set.

The bridge is intentionally thin: it knows addresses but not features. The
feature -> address decision lives in `viz_mapping.py`.
"""

from __future__ import annotations

from typing import Optional

from pythonosc import udp_client

from muse2_music_lab import config


class VizBridge:
    """Publish `/viz/params/*` floats and `/viz/trigger/*` pulses."""

    def __init__(
        self,
        host: str = config.VIZ_HOST,
        port: int = config.VIZ_PORT,
    ) -> None:
        self.host = host
        self.port = int(port)
        self._client: Optional[udp_client.SimpleUDPClient] = (
            udp_client.SimpleUDPClient(host, self.port)
        )

    def send_param(self, address: str, value_0_1: float) -> None:
        """Send a float clamped to [0, 1] at `address`."""
        if self._client is None:
            return
        v = max(0.0, min(1.0, float(value_0_1)))
        self._client.send_message(address, v)

    def send_trigger(self, address: str) -> None:
        """Send a momentary 1.0 then 0.0 at `address`."""
        if self._client is None:
            return
        self._client.send_message(address, 1.0)
        self._client.send_message(address, 0.0)

    def send_prompt(self, address: str, text: str) -> None:
        """Send a string (for `/viz/prompt/*`)."""
        if self._client is None:
            return
        self._client.send_message(address, str(text))

    def close(self) -> None:
        self._client = None

    def __enter__(self) -> "VizBridge":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
