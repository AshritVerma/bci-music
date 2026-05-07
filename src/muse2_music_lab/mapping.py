"""Signal -> output destination routing table.

===========================================================================
EDIT ME. These are creative decisions, not engineering ones.
Change CC numbers, OSC addresses, or the type of signal freely.
===========================================================================

Each entry maps a feature name (as produced by `features.FeatureFrame`) to
the MIDI CC and OSC address it should be sent to. `type` is either:
  - "cc"    : continuous, scaled to 0..127 and sent as CC (value in [0, 1])
  - "pulse" : boolean trigger, sent as a 127 -> 0 momentary CC pulse

Continuous values are expected to be already normalized to [0, 1] by the
`smoother.Normalizer`. Pulses come directly from the feature frame.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from muse2_music_lab import config
from muse2_music_lab.features import FeatureFrame


Mapping = Dict[str, Any]

MAPPINGS: Dict[str, Mapping] = {
    "focus": {"type": "cc",    "channel": config.MIDI_CHANNEL_DEFAULT, "cc": 74, "osc": "/brain/focus"},
    "calm":  {"type": "cc",    "channel": config.MIDI_CHANNEL_DEFAULT, "cc": 91, "osc": "/brain/calm"},
    "alpha": {"type": "cc",    "channel": config.MIDI_CHANNEL_DEFAULT, "cc": 20, "osc": "/brain/alpha"},
    "beta":  {"type": "cc",    "channel": config.MIDI_CHANNEL_DEFAULT, "cc": 21, "osc": "/brain/beta"},
    "theta": {"type": "cc",    "channel": config.MIDI_CHANNEL_DEFAULT, "cc": 22, "osc": "/brain/theta"},
    "blink": {"type": "pulse", "channel": config.MIDI_CHANNEL_DEFAULT, "cc": 64, "osc": "/brain/blink"},
    "jaw":   {"type": "pulse", "channel": config.MIDI_CHANNEL_DEFAULT, "cc": 65, "osc": "/brain/jaw"},
}


CONTINUOUS_NAMES = tuple(k for k, v in MAPPINGS.items() if v["type"] == "cc")
PULSE_NAMES = tuple(k for k, v in MAPPINGS.items() if v["type"] == "pulse")


def route(
    frame: FeatureFrame,
    normalized: Dict[str, float],
    midi: Optional[Any] = None,
    osc: Optional[Any] = None,
) -> None:
    """Dispatch a frame to whichever backend(s) are provided.

    `normalized` holds continuous values already in [0, 1].
    `midi` should quack like `output_midi.MidiOut`; `osc` like `output_osc.OscOut`.
    """
    for name, spec in MAPPINGS.items():
        if spec["type"] == "cc":
            value = float(normalized.get(name, 0.0))
            if midi is not None:
                midi.send_cc(spec["channel"], spec["cc"], value)
            if osc is not None:
                osc.send(spec["osc"], value)
        elif spec["type"] == "pulse":
            triggered = bool(getattr(frame, name, False))
            if not triggered:
                continue
            if midi is not None:
                midi.send_pulse(spec["channel"], spec["cc"])
            if osc is not None:
                osc.send_pulse(spec["osc"])


def describe() -> str:
    """Human-readable summary of the routing table (for logs/TUI)."""
    lines = []
    for name, spec in MAPPINGS.items():
        t = spec["type"].upper()
        lines.append(
            f"  {name:<6} [{t:5}] ch{spec['channel']} cc{spec['cc']:<3}  {spec['osc']}"
        )
    return "\n".join(lines)
