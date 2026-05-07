"""FeatureFrame -> /viz/* routing table.

===========================================================================
EDIT ME. Creative decisions about which brain feature drives which visual
parameter live here. Parallel to `mapping.py` (DAW side) but targets the
visual layer's separate OSC port.
===========================================================================

Conventions:
  - "cc"    : continuous float in [0, 1] -> /viz/params/<name>
  - "pulse" : boolean trigger            -> /viz/trigger/<name>

`source` names the field on the normalized FeatureFrame-derived dict (for
continuous) or on FeatureFrame itself (for pulses). Most mappings just pass
the normalized feature straight through; `prompt_blend` is derived, see
`derive_extra()`.
"""

from __future__ import annotations

from typing import Any, Dict

from muse2_music_lab.features import FeatureFrame


VizMapping = Dict[str, Any]


VIZ_MAPPINGS: Dict[str, VizMapping] = {
    "intensity":    {"type": "cc",    "source": "beta",  "address": "/viz/params/intensity"},
    "calm":         {"type": "cc",    "source": "calm",  "address": "/viz/params/calm"},
    "focus":        {"type": "cc",    "source": "focus", "address": "/viz/params/focus"},
    "alpha":        {"type": "cc",    "source": "alpha", "address": "/viz/params/alpha"},
    "theta":        {"type": "cc",    "source": "theta", "address": "/viz/params/theta"},
    "prompt_blend": {"type": "cc",    "source": "prompt_blend", "address": "/viz/params/prompt_blend"},
    "blink":        {"type": "pulse", "source": "blink", "address": "/viz/trigger/blink"},
    "jaw":          {"type": "pulse", "source": "jaw",   "address": "/viz/trigger/jaw"},
}


VIZ_CONTINUOUS = tuple(k for k, v in VIZ_MAPPINGS.items() if v["type"] == "cc")
VIZ_PULSES = tuple(k for k, v in VIZ_MAPPINGS.items() if v["type"] == "pulse")


def derive_extra(normalized: Dict[str, float]) -> Dict[str, float]:
    """Compute synthetic values not present on FeatureFrame.

    Currently just `prompt_blend`, a slow variable that nudges the sidecar
    between two prompt banks. Alpha is the dominant driver (high alpha ->
    calmer/softer prompt) with a gentle calm bias so it isn't purely
    eyes-closed-dependent.
    """
    alpha = float(normalized.get("alpha", 0.0))
    calm = float(normalized.get("calm", 0.0))
    prompt_blend = 0.7 * alpha + 0.3 * calm
    return {"prompt_blend": max(0.0, min(1.0, prompt_blend))}


def describe() -> str:
    lines = []
    for name, spec in VIZ_MAPPINGS.items():
        t = spec["type"].upper()
        lines.append(
            f"  {name:<13} [{t:5}] {spec['source']:<13} -> {spec['address']}"
        )
    return "\n".join(lines)


def route(
    frame: FeatureFrame,
    normalized: Dict[str, float],
    viz: "Any",  # VizBridge, but typed loose to avoid circular import
) -> None:
    """Dispatch one frame's worth of values to the viz OSC bridge."""
    extra = derive_extra(normalized)
    for name, spec in VIZ_MAPPINGS.items():
        if spec["type"] == "cc":
            src = spec["source"]
            if src in normalized:
                value = normalized[src]
            elif src in extra:
                value = extra[src]
            else:
                continue
            viz.send_param(spec["address"], float(value))
        elif spec["type"] == "pulse":
            if bool(getattr(frame, spec["source"], False)):
                viz.send_trigger(spec["address"])
