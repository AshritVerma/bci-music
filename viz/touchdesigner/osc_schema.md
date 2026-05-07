# `/viz/*` OSC schema

This is the canonical contract between three processes:

1. **`muse2-music run --viz`** — publishes brain-derived params (`/viz/params/*`) and triggers (`/viz/trigger/*`) at ~30 Hz.
2. **`viz-sidecar`** — publishes AI-generated frames over Syphon; *optionally* consumes `/viz/params/*` to modulate diffusion strength and `/viz/prompt/*` to change prompts.
3. **TouchDesigner** — consumes `/viz/params/*` and `/viz/trigger/*` directly for procedural effects, and consumes the Syphon stream from the sidecar.

All addresses are stable; extending the schema means **adding** new addresses, not renaming existing ones.

## Default transport

- Host: `127.0.0.1`
- Port: `9100` (separate from the DAW-facing OSC port `9000` so the viz layer can't get accidentally routed into the DAW)

## Continuous params — `/viz/params/*`

All values are floats in `[0.0, 1.0]`, already normalized and smoothed. Published at `SEND_RATE_HZ` (default 30 Hz) whenever `--viz` is enabled.

| Address                     | Source feature (Phase 1)        | Suggested use                          |
| --------------------------- | ------------------------------- | -------------------------------------- |
| `/viz/params/intensity`     | `beta` (normalized)             | overall energy, post-fx amount         |
| `/viz/params/calm`          | `calm`                          | color temperature, softness            |
| `/viz/params/focus`         | `focus`                         | detail, sharpness, guidance scale      |
| `/viz/params/alpha`         | `alpha`                         | slow drift, hue rotation               |
| `/viz/params/theta`         | `theta`                         | dream/abstraction amount               |
| `/viz/params/prompt_blend`  | slow function of `alpha`/`calm` | interpolation between prompt banks     |

## Discrete triggers — `/viz/trigger/*`

Sent as a momentary `1.0` followed by `0.0` at the trigger address.

| Address                 | Source         | Suggested use                                  |
| ----------------------- | -------------- | ---------------------------------------------- |
| `/viz/trigger/blink`    | blink detector | frame flash, scene cut                         |
| `/viz/trigger/jaw`      | jaw detector   | prompt-bank advance, big accent                |

## Prompt control — `/viz/prompt/*`

Published by the manual-input mechanism (text file watch or UI) at low rate. The sidecar reacts by swapping the prompt used for diffusion.

| Address               | Payload | Meaning                                                    |
| --------------------- | ------- | ---------------------------------------------------------- |
| `/viz/prompt/base`    | string  | user-supplied prompt text                                  |
| `/viz/prompt/style`   | string  | optional style suffix (e.g. "cinematic, volumetric light") |
| `/viz/prompt/source`  | string  | one of `auto`, `manual`, `mix` — runtime toggle            |

### Prompt-source semantics

- **`auto`** (default; brain-only experience): the sidecar ignores `/viz/prompt/base` and builds its prompt by interpolating between named prompt banks in `viz/prompts/default.yaml` using `/viz/params/prompt_blend`. No user text required.
- **`manual`**: the sidecar uses `/viz/prompt/base` verbatim. Brain still modulates diffusion strength, guidance, and the TD post-fx layer, but doesn't affect the prompt text.
- **`mix`**: the sidecar interpolates between prompt banks AND appends `/viz/prompt/base` (and `/viz/prompt/style` if present).

Switching mode is idempotent: publish `/viz/prompt/source` whenever you want to change. The sidecar always exposes its current mode on startup logs.

## Sidecar status — `/viz/status/*` (sidecar → anyone listening)

The sidecar optionally echoes its state. Useful for TD UI and debugging.

| Address                 | Payload | Meaning                            |
| ----------------------- | ------- | ---------------------------------- |
| `/viz/status/fps`       | float   | measured diffusion fps             |
| `/viz/status/prompt`    | string  | the prompt actually used last step |
| `/viz/status/source`    | string  | active prompt source mode          |
