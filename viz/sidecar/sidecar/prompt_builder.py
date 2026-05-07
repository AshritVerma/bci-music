"""Turn a VizSnapshot into a prompt string and diffusion parameters.

The prompt-source toggle is handled here:

  * auto   -> brain-driven bank interpolation, ignores user text.
  * manual -> user text verbatim; brain still drives `strength` / `guidance`.
  * mix    -> bank interpolation + user text appended as style suffix.

Banks are loaded from the YAML file passed on the command line. Minimal
schema:

    banks:
      calm:      "soft watercolor sky, gentle flow, pastel, minimal"
      energetic: "vivid neon cityscape, motion blur, high contrast, glitch"
      focused:   "clean geometric architecture, isometric, cool tones"
      abstract:  "abstract liquid marble, swirling pigment, iridescent"
    # Optional:
    blend_poles: [calm, energetic]
    style_default: "cinematic, volumetric light, 35mm film grain"
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from .state import VizSnapshot


@dataclass
class PromptPlan:
    """Everything the diffusion backend needs for one render step."""

    prompt: str
    negative_prompt: str
    strength: float          # img2img strength in [0, 1]
    guidance: float          # CFG scale; 0.0 for SDXL-Turbo
    color_temperature: float # extra hint for post-fx, 0..1


class PromptBuilder:
    def __init__(self, prompts_path: Path | str | None) -> None:
        self._banks: Dict[str, str] = {}
        self._blend_poles: Tuple[str, str] = ("calm", "energetic")
        self._style_default: str = ""
        self._negative: str = "low quality, blurry, watermark, text, logo"
        if prompts_path is not None:
            self._load(Path(prompts_path))

    def _load(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            return
        banks = data.get("banks") or {}
        if isinstance(banks, dict):
            self._banks = {str(k): str(v) for k, v in banks.items()}
        poles = data.get("blend_poles")
        if (
            isinstance(poles, list)
            and len(poles) == 2
            and all(isinstance(p, str) for p in poles)
            and all(p in self._banks for p in poles)
        ):
            self._blend_poles = (poles[0], poles[1])
        if "style_default" in data:
            self._style_default = str(data["style_default"])
        if "negative_prompt" in data:
            self._negative = str(data["negative_prompt"])

    def available_banks(self) -> List[str]:
        return list(self._banks.keys())

    def _interp_banks(self, blend: float) -> str:
        """Concatenate the two pole banks, weighting by `blend`.

        SDXL-Turbo prompt weighting is token-count based rather than
        numeric, so we approximate: the 'dominant' bank gets its full
        text, the other is appended as a short inflection.
        """
        a, b = self._blend_poles
        text_a = self._banks.get(a, "")
        text_b = self._banks.get(b, "")
        if not text_a and not text_b:
            return "abstract art"
        if blend <= 0.15:
            return text_a
        if blend >= 0.85:
            return text_b
        if blend < 0.5:
            return f"{text_a}, with hints of {text_b}"
        return f"{text_b}, with hints of {text_a}"

    def build(self, snap: VizSnapshot) -> PromptPlan:
        source = snap.source
        blend = float(snap.params.get("prompt_blend", 0.5))
        focus = float(snap.params.get("focus", 0.5))
        intensity = float(snap.params.get("intensity", 0.0))
        calm = float(snap.params.get("calm", 0.5))
        theta = float(snap.params.get("theta", 0.0))

        if source == "manual" and snap.base_prompt.strip():
            body = snap.base_prompt.strip()
        elif source == "mix":
            bank = self._interp_banks(blend)
            pieces = [bank]
            if snap.base_prompt.strip():
                pieces.append(snap.base_prompt.strip())
            body = ", ".join(pieces)
        else:  # auto or manual-with-empty-text fallback
            body = self._interp_banks(blend)

        style_pieces: List[str] = []
        if self._style_default:
            style_pieces.append(self._style_default)
        if snap.style_suffix.strip():
            style_pieces.append(snap.style_suffix.strip())
        style = ", ".join(style_pieces)
        prompt = f"{body}, {style}" if style else body

        # img2img strength: higher intensity / theta -> the image changes more
        # per step. We keep this on the gentle side so temporal coherence holds.
        strength = 0.35 + 0.4 * intensity + 0.15 * theta
        strength = max(0.25, min(0.85, strength))

        # SDXL-Turbo is trained with guidance_scale=0. Keep it so.
        guidance = 0.0

        # Color-temp hint for the optional post-fx in TD (not used by diffusion).
        color_temperature = calm  # high calm -> warm; low calm -> cool

        return PromptPlan(
            prompt=prompt,
            negative_prompt=self._negative,
            strength=strength,
            guidance=guidance,
            color_temperature=color_temperature,
        )
