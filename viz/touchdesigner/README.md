# TouchDesigner project setup

This is a node-by-node walkthrough for the Phase 1 visual layer. It is a recipe, not a binary `.toe` file — TouchDesigner's native save format is binary and doesn't diff meaningfully in git, so we keep the project definition here as text and you recreate it once.

Time to assemble from scratch: ~15 minutes.

## Prerequisites

- **TouchDesigner 2023.11600+** (Non-Commercial is fine).
- **Syphon** framework (comes with macOS; no separate install).
- **`muse2-viz-sidecar`** running and publishing as `Muse2Viz`. See `viz/sidecar/README.md`.
- **`muse2-music run --viz`** running (with Muse 2 on your head, post-calibration).

## Network layout

```
┌─────────────────────────────────────────────────────────────┐
│  project1                                                    │
│                                                              │
│  [OSC In CHOP]────┐                                          │
│   addr: /viz/*    │                                          │
│   port: 9100      ▼                                          │
│                 [Select CHOP]─────────┐                      │
│                 (per-param)           │                      │
│                                       ▼                      │
│                              [Math/Rename]──┐                │
│                                             │                │
│  [Syphon Spout In TOP]──┐                   │                │
│   sender: Muse2Viz      ▼                   ▼                │
│                      [Feedback TOP]◄──[Composite TOP]        │
│                            │                                 │
│                            ▼                                 │
│                      [Transform TOP]                         │
│                      (displace by params)                    │
│                            │                                 │
│                            ▼                                 │
│                      [Level TOP]                             │
│                      (color grade by params)                 │
│                            │                                 │
│                            ▼                                 │
│                      [Out TOP]  ────►  [Window COMP]         │
└─────────────────────────────────────────────────────────────┘
```

## Step-by-step

### 1. Receive OSC params

1. Drop an **OSC In CHOP**.
2. Parameters:
   - `Network Port`: `9100`
   - `Active`: On
   - `OSC Address Scope`: `/viz/*` (so you catch both `/params/*` and `/trigger/*`)
3. Verify: with `muse2-music run --viz` running, you should see channels like `viz/params/intensity`, `viz/params/calm`, etc., updating in the CHOP viewer.

### 2. Fan out params

1. Drop a **Select CHOP** after the OSC In CHOP.
2. Set `Channel Names` to the channel you want, e.g. `viz/params/intensity`.
3. Optionally follow with a **Rename CHOP** to give it a clean short name like `intensity`.
4. Duplicate for each param you care about (`calm`, `focus`, `alpha`, `theta`, `prompt_blend`).

Tip: you can skip fanout and just reference channels by index in downstream OPs via expressions like `op('oscin1')['viz/params/intensity']`. Pick whichever style you prefer.

### 3. Receive the AI frames

1. Drop a **Syphon Spout In TOP**.
2. Parameters:
   - `Sender Name`: `Muse2Viz` (must match the sidecar's `--syphon-name`)
   - `Active`: On
3. Verify: when the sidecar is rendering, you should see the diffusion output here.

### 4. Procedural layer on top

The diffusion backend runs at ~5–10 fps. The procedural layer below runs at 60 fps so perceived motion is smooth.

1. Drop a **Feedback TOP**.
2. Drop a **Composite TOP** with two inputs: the Feedback TOP output, and the Syphon Spout In TOP output. Set `Operation` to `Over` (or try `Screen` / `Add`).
3. Route the Composite TOP's output back into the Feedback TOP's "Target TOP".
4. Drop a **Transform TOP** after the Composite TOP. Bind its `Translate` parameters to `prompt_blend` and `intensity` via channel references or Python expressions, e.g.:
   - `tx`: `0.02 * op('oscin1')['viz/params/alpha']`
   - `ty`: `0.02 * op('oscin1')['viz/params/theta']`
5. Drop a **Level TOP** after the Transform TOP. Drive `Brightness` and `Gamma` from `focus` and `calm`:
   - `Brightness`: `0.8 + 0.4 * op('oscin1')['viz/params/focus']`
   - `Gamma`: `1.0 + 0.6 * (1 - op('oscin1')['viz/params/calm'])`

### 5. Trigger effects

Use the trigger channels (`viz/trigger/blink`, `viz/trigger/jaw`) to gate short effects.

1. Add a **Trigger CHOP** fed by the trigger channel.
2. Set `Attack` 0.02, `Release` 0.3 for a quick flash.
3. Bind the Trigger CHOP output to, e.g., the Level TOP's `Brightness` with a `+` multiplier so each blink causes a visible pop.

### 6. Output

1. Drop an **Out TOP** at the end of the chain.
2. Drop a **Window COMP**. Set its `Operator` to the Out TOP.
3. `Perform Mode` (F1) for fullscreen output on your projector/second display.

## Optional: echo prompt status

If you want a HUD showing the current prompt:

1. Add another **OSC In CHOP** listening on port `9101` (or reuse `9100` and select `viz/status/*` channels).
2. In the sidecar, add a small change to publish `/viz/status/prompt` (not wired in Phase 1 but reserved in the schema).
3. Feed a **Text TOP** with that channel for on-screen text.

## Troubleshooting

| Symptom                                | Likely cause                                          |
| -------------------------------------- | ----------------------------------------------------- |
| Syphon Spout In TOP is black           | Sidecar isn't running, or `--syphon-name` ≠ `Muse2Viz`|
| OSC In CHOP is empty                   | Port mismatch, or `muse2-music run` missing `--viz`   |
| Diffusion fps visibly low (<3)         | Try sidecar `--width 384 --height 384`                |
| Feedback layer goes all white          | Composite TOP `Pre-Multiply` mismatch; enable on both |
| Triggers fire too often                | Bump refractory in `config.py` (BLINK/JAW_REFRACTORY) |

## What Phase 2 will add

- A second **Audio Device In CHOP** (reading BlackHole) + **Audio Spectrum CHOP** + **Analyze CHOP** for level/centroid/onset.
- A "source router" Container COMP: per-param dropdown (brain / audio / MIDI / manual / mix) so you can swap drivers live.
