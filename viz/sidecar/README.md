# muse2-viz-sidecar

Diffusion sidecar for the muse2 visual layer.

- Listens on OSC for `/viz/params/*` and `/viz/prompt/*`.
- Runs SDXL-Turbo (via Hugging Face `diffusers` on the MPS backend) at 384×384 by default (~7 fps on M4 Max with TAESD VAE + prompt-embedding cache); 512×512 for native quality at ~5 fps.
- Publishes RGB frames to a named Syphon server so TouchDesigner can pick them up via a Syphon Spout In TOP.

## Install

This package has its own heavy ML dependencies (PyTorch, diffusers, Syphon).
Install it in a **separate** virtual environment from the main `muse2-music-lab` package.

```bash
cd viz/sidecar
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

First run will download the SDXL-Turbo weights (~6 GB) into the Hugging Face cache.

## Run

```bash
# Defaults to 384x384 + tiny VAE + 1 step (~7 fps on M4 Max):
muse2-viz-sidecar

# Native trained resolution (slower but sharper):
muse2-viz-sidecar --width 512 --height 512

# All flags explicit:
muse2-viz-sidecar \
    --listen-host 127.0.0.1 \
    --listen-port 9100 \
    --prompts ../prompts/default.yaml \
    --syphon-name Muse2Viz \
    --width 384 --height 384 \
    --steps 1 \
    --vae tiny
```

Then in TouchDesigner, add a **Syphon Spout In TOP** and set its `Sender Name` to `Muse2Viz`.

## Prompt source modes

The active mode is controlled at runtime by `/viz/prompt/source` (string: `auto` | `manual` | `mix`). Default is `auto` so the sidecar works brain-only with no UI.

- `auto` – the sidecar ignores `/viz/prompt/base` and interpolates between the named prompt banks in `--prompts` using `/viz/params/prompt_blend`.
- `manual` – uses `/viz/prompt/base` verbatim. Brain params only modulate diffusion strength / guidance, not the prompt text.
- `mix` – bank interpolation, with `/viz/prompt/base` and `/viz/prompt/style` appended as suffixes.

You can also write text to `viz/prompts/live.txt` to update `/viz/prompt/base` locally without needing a UI; the sidecar watches the file.

## Why diffusers + MPS instead of MLX?

- `diffusers` is pip-installable and has robust img2img and SDXL-Turbo support.
- MLX is faster on Apple Silicon, but there's no official PyPI package for its stable-diffusion example as of now — it's vendored through the `mlx-examples` repo.
- The `DiffusionBackend` interface in `sidecar/diffusion.py` is designed so an MLX implementation can slot in with no changes to the OSC server or frame publisher.
