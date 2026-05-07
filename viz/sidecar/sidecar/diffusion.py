"""Diffusion backend abstraction + concrete implementations.

Two implementations:

1. `FakeBackend` -- produces procedural RGBA noise so the rest of the pipeline
   (OSC -> prompt builder -> Syphon -> TD) can be tested without PyTorch or
   weights. Use with `--backend fake`.
2. `DiffusersMpsBackend` -- real SDXL-Turbo via Hugging Face `diffusers`
   running on the MPS (Metal) backend. Default.

The interface is narrow so an MLX-based backend can drop in later.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .prompt_builder import PromptPlan


@dataclass
class RenderRequest:
    """Inputs for a single render step."""

    plan: PromptPlan
    prev_frame: Optional[np.ndarray]  # (H, W, 4) uint8, RGBA, or None for first step
    width: int
    height: int
    steps: int


class DiffusionBackend(ABC):
    @abstractmethod
    def render(self, req: RenderRequest) -> np.ndarray:
        """Return a (H, W, 4) uint8 RGBA frame."""


# ---------------------------------------------------------------------------
# Fake backend for testing the pipeline end-to-end without weights
# ---------------------------------------------------------------------------


class FakeBackend(DiffusionBackend):
    """Generates a tinted gradient whose hue drifts with prompt_blend/intensity.

    Intentionally cheap: validates OSC -> state -> prompt -> frame -> Syphon
    without touching PyTorch.
    """

    def __init__(self) -> None:
        self._t = 0.0

    def render(self, req: RenderRequest) -> np.ndarray:
        w, h = req.width, req.height
        self._t += 0.05
        strength = req.plan.strength
        color_t = req.plan.color_temperature  # 0 cool -> 1 warm

        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        gx = x / max(w - 1, 1)
        gy = y / max(h - 1, 1)

        r = np.clip(0.2 + 0.8 * (color_t * gx + (1 - color_t) * gy), 0, 1)
        g = np.clip(0.2 + 0.8 * ((1 - color_t) * gx + color_t * gy), 0, 1)
        b = np.clip(0.2 + 0.8 * abs(np.sin(self._t + gx * 3.14 + gy * 3.14)), 0, 1)

        amp = 0.5 + 0.5 * strength
        r = r * amp
        g = g * amp
        b = b * amp

        rgba = np.stack(
            [
                (r * 255).astype(np.uint8),
                (g * 255).astype(np.uint8),
                (b * 255).astype(np.uint8),
                np.full((h, w), 255, dtype=np.uint8),
            ],
            axis=-1,
        )
        # Simulate diffusion fps budget.
        time.sleep(0.05)
        return rgba


# ---------------------------------------------------------------------------
# Real backend: Hugging Face diffusers SDXL-Turbo on MPS
# ---------------------------------------------------------------------------


class DiffusersMpsBackend(DiffusionBackend):
    """SDXL-Turbo via `diffusers`, `torch.mps`. Uses img2img when a previous
    frame is available so temporal coherence holds; falls back to text2img
    on the first step.

    Realtime optimizations:
      * **TAESD** (`madebyollin/taesdxl`) replaces the heavy SDXL VAE for
        ~5-10x faster decode on MPS at minimal quality loss for live use.
      * **Prompt embedding cache** keyed on (prompt, negative_prompt). When
        the prompt hasn't changed (the common case in `auto` mode where
        only a few banks are interpolated), we skip text encoding entirely.
      * **Warmup pass** at init so the first user-visible frame doesn't
        eat the MPS kernel-compilation cost.
    """

    def __init__(
        self,
        model_id: str = "stabilityai/sdxl-turbo",
        device: str = "mps",
        dtype: str = "float16",
        vae: str = "tiny",
        warmup: bool = True,
    ) -> None:
        # Heavy imports are deferred so `--backend fake` works on machines
        # without the ML stack installed.
        import torch  # type: ignore
        from diffusers import (  # type: ignore
            AutoPipelineForImage2Image,
            AutoPipelineForText2Image,
            AutoencoderTiny,
        )

        torch_dtype = {"float16": torch.float16, "float32": torch.float32}[dtype]

        print(f"[diffusion] loading {model_id} on {device} ({dtype}, vae={vae})...", flush=True)
        self._txt2img = AutoPipelineForText2Image.from_pretrained(
            model_id, torch_dtype=torch_dtype, variant="fp16"
        ).to(device)

        if vae == "tiny":
            tiny = AutoencoderTiny.from_pretrained(
                "madebyollin/taesdxl", torch_dtype=torch_dtype
            ).to(device)
            self._txt2img.vae = tiny

        self._img2img = AutoPipelineForImage2Image.from_pipe(self._txt2img).to(device)
        self._txt2img.set_progress_bar_config(disable=True)
        self._img2img.set_progress_bar_config(disable=True)

        self._torch = torch
        self._device = device
        self._dtype = torch_dtype

        # (prompt, negative_prompt) -> (prompt_embeds, neg_embeds, pooled, neg_pooled)
        self._prompt_cache: dict[tuple[str, str], tuple] = {}
        self._prompt_cache_max = 16

        if warmup:
            self._warmup()

        print("[diffusion] ready", flush=True)

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    def _warmup(self) -> None:
        """Run one tiny txt2img pass so MPS kernel compilation happens here,
        not on the first user-visible frame."""
        print("[diffusion] warming up MPS kernels...", flush=True)
        try:
            self._txt2img(
                prompt="warmup",
                num_inference_steps=1,
                guidance_scale=0.0,
                width=512,
                height=512,
            )
        except Exception as e:
            print(f"[diffusion] warmup skipped: {e}", flush=True)

    def _encode_prompt(self, prompt: str, negative_prompt: str):
        """Cached SDXL prompt encoding. SDXL needs four embed tensors."""
        key = (prompt, negative_prompt or "")
        cached = self._prompt_cache.get(key)
        if cached is not None:
            return cached
        with self._torch.no_grad():
            (
                prompt_embeds,
                negative_prompt_embeds,
                pooled_prompt_embeds,
                negative_pooled_prompt_embeds,
            ) = self._txt2img.encode_prompt(
                prompt=prompt,
                negative_prompt=negative_prompt or None,
                device=self._device,
                num_images_per_prompt=1,
                do_classifier_free_guidance=False,
            )
        result = (
            prompt_embeds,
            negative_prompt_embeds,
            pooled_prompt_embeds,
            negative_pooled_prompt_embeds,
        )
        if len(self._prompt_cache) >= self._prompt_cache_max:
            # Evict an arbitrary entry (we don't need LRU precision here).
            self._prompt_cache.pop(next(iter(self._prompt_cache)))
        self._prompt_cache[key] = result
        return result

    @staticmethod
    def _to_pil(arr: np.ndarray):
        from PIL import Image  # type: ignore

        if arr.shape[-1] == 4:
            arr = arr[..., :3]
        return Image.fromarray(arr)

    @staticmethod
    def _from_pil(img) -> np.ndarray:
        arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
        h, w, _ = arr.shape
        rgba = np.empty((h, w, 4), dtype=np.uint8)
        rgba[..., :3] = arr
        rgba[..., 3] = 255
        return rgba

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def render(self, req: RenderRequest) -> np.ndarray:
        plan = req.plan
        steps = max(1, int(req.steps))

        prompt_embeds, neg_embeds, pooled, neg_pooled = self._encode_prompt(
            plan.prompt, plan.negative_prompt or ""
        )

        if req.prev_frame is None:
            out = self._txt2img(
                prompt_embeds=prompt_embeds,
                pooled_prompt_embeds=pooled,
                num_inference_steps=steps,
                guidance_scale=plan.guidance,
                width=req.width,
                height=req.height,
            ).images[0]
        else:
            prev = self._to_pil(req.prev_frame)
            # img2img needs enough scheduler steps that int(steps * strength) >= 1.
            eff_steps = max(steps, int(1 / max(plan.strength, 0.01)) + 1)
            out = self._img2img(
                prompt_embeds=prompt_embeds,
                pooled_prompt_embeds=pooled,
                image=prev,
                num_inference_steps=eff_steps,
                strength=plan.strength,
                guidance_scale=plan.guidance,
            ).images[0]
        return self._from_pil(out)


def build_backend(
    name: str,
    *,
    width: int,
    height: int,
    vae: str = "tiny",
) -> DiffusionBackend:
    name = (name or "").lower()
    if name == "fake":
        return FakeBackend()
    if name in ("diffusers", "mps", "diffusers-mps", ""):
        return DiffusersMpsBackend(vae=vae)
    raise ValueError(f"Unknown diffusion backend: {name!r}")
