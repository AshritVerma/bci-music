"""Thread-safe shared state between the OSC listener and the render loop.

The OSC server writes to `VizState` when packets arrive. The render loop
reads a snapshot each step. All fields have safe defaults so the render
loop can start before any OSC traffic has arrived.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class VizSnapshot:
    """Immutable copy of VizState for one render step."""

    params: Dict[str, float]
    source: str
    base_prompt: str
    style_suffix: str


class VizState:
    """Shared, thread-safe state. All writes go through the `set_*` methods."""

    def __init__(self, prompt_source: str = "auto") -> None:
        self._lock = threading.Lock()
        self._params: Dict[str, float] = {
            "intensity": 0.0,
            "calm": 0.5,
            "focus": 0.5,
            "alpha": 0.0,
            "theta": 0.0,
            "prompt_blend": 0.5,
        }
        self._source = prompt_source
        self._base_prompt = ""
        self._style_suffix = ""
        self._triggers: Dict[str, int] = {}

    def set_param(self, name: str, value: float) -> None:
        with self._lock:
            self._params[name] = max(0.0, min(1.0, float(value)))

    def set_source(self, mode: str) -> None:
        mode = (mode or "").strip().lower()
        if mode not in ("auto", "manual", "mix"):
            return
        with self._lock:
            self._source = mode

    def set_base_prompt(self, text: str) -> None:
        with self._lock:
            self._base_prompt = text or ""

    def set_style(self, text: str) -> None:
        with self._lock:
            self._style_suffix = text or ""

    def mark_trigger(self, name: str) -> None:
        with self._lock:
            self._triggers[name] = self._triggers.get(name, 0) + 1

    def consume_trigger(self, name: str) -> int:
        """Return and reset the trigger count for `name` since last call."""
        with self._lock:
            n = self._triggers.get(name, 0)
            self._triggers[name] = 0
            return n

    def snapshot(self) -> VizSnapshot:
        with self._lock:
            return VizSnapshot(
                params=dict(self._params),
                source=self._source,
                base_prompt=self._base_prompt,
                style_suffix=self._style_suffix,
            )
