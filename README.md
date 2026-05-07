# Muse2 Music Lab

Turn an **Interaxon Muse 2** into a DAW controller. A Python app reads live EEG
from the headset via **BrainFlow**, extracts musically useful features
(alpha/beta/theta band power, focus/calm indices, blink and jaw-clench
triggers), and routes them to a DAW as **MIDI CC** or **OSC** in real time.

Drop `muse2-music run`, map the CCs to plugin knobs in Logic Pro's Smart
Controls, and your brain is now a controller.

## Prerequisites

- **Muse 2** charged and paired over Bluetooth (macOS: System Settings →
  Bluetooth). BrainFlow uses native BLE on macOS — no BLED dongle required.
- **Python 3.9+**
- **macOS only, one-time MIDI setup**: enable the IAC Driver so Logic (or any
  DAW) can receive MIDI from this app:
  1. Open **Audio MIDI Setup** (Applications → Utilities).
  2. **Window → Show MIDI Studio**.
  3. Double-click **IAC Driver**.
  4. Tick **Device is online**.
  5. Under **Ports**, keep or rename the default bus (e.g. `Bus 1`). The full
     port name will be something like `IAC Driver Bus 1` — that's what
     `MIDI_PORT_NAME` in `config.py` expects.
- **Bluetooth permission**: on the first run, macOS will prompt your terminal
  (or your editor's integrated terminal) for Bluetooth access. Approve it.

## Setup

```bash
cd muse2-music-lab
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
# optional live terminal UI:
pip install -e '.[tui]'
```

## Usage

### Verify your MIDI setup

```bash
muse2-music list-midi
```

You should see at least `IAC Driver Bus 1` (or whatever you named your IAC
bus). A `*` marker shows which port the default `config.py` would use.

### Run the brain → DAW pipeline

```bash
muse2-music run
```

What happens:
1. Opens the configured output backend (MIDI by default).
2. Connects to the Muse 2 via BrainFlow and starts streaming EEG.
3. Runs a 5-second calibration — **sit still, relax, eyes open**.
4. Loops at ~30 Hz: window → band powers → focus/calm/blink/jaw →
   smoothing + normalization → routed to MIDI/OSC.
5. Press `Ctrl-C` to stop.

Useful flags:

```bash
# Send to OSC instead of MIDI
muse2-music run --backend osc --osc-host 127.0.0.1 --osc-port 9000

# Both at once
muse2-music run --backend both

# Different MIDI port
muse2-music run --midi-port "IAC Driver Bus 2"

# Longer calibration (better baseline)
muse2-music run --calibrate-seconds 10

# Live TUI (requires the [tui] extra)
muse2-music run --tui
```

Inside the TUI, press `r` + Enter to re-calibrate, or `q` + Enter to quit.

### Hook it up in Logic Pro

1. Open a project with a soft synth or an effect plugin on a track.
2. Click **Smart Controls** (knob icon at the top).
3. Click the small **[i]** info icon → **Open**, then click a Smart Control
   knob to select it.
4. Click **Learn** (in the MIDI assignments pane) and move something in the
   real world — or just run `muse2-music run`; Logic will latch onto the
   first CC that arrives.
5. Repeat for each feature. A starter patch to try:
   - `focus` (CC 74) → filter cutoff
   - `calm`  (CC 91) → reverb send / wet dry
   - `alpha` (CC 20) → LFO rate or pad swell
   - `blink` (CC 64) → sample one-shot / effect bypass
   - `jaw`   (CC 65) → tempo nudge / stutter

The mapping table lives in [`mapping.py`](src/muse2_music_lab/mapping.py) —
edit it freely. All numeric tuning constants (window size, smoothing factor,
blink/jaw thresholds) live in
[`config.py`](src/muse2_music_lab/config.py).

## Visual layer (TouchDesigner)

Optional. Adds a real-time AI-diffusion visual hosted in TouchDesigner,
driven by your brain signal. Lives under [`viz/`](viz/).

Three processes cooperate, connected by a small OSC schema
([`viz/touchdesigner/osc_schema.md`](viz/touchdesigner/osc_schema.md)):

1. **`muse2-music run --viz`** — same pipeline as above, additionally
   publishing `/viz/params/*` (continuous) and `/viz/trigger/*` (blinks,
   jaw) to `127.0.0.1:9100`. Runs alongside the DAW backend; doesn't
   replace it.
2. **`muse2-viz-sidecar`** — standalone Python service
   ([`viz/sidecar/`](viz/sidecar/)) that runs SDXL-Turbo via Hugging
   Face `diffusers` on the MPS backend, receives the OSC, and publishes
   frames as a named Syphon server.
3. **TouchDesigner project** — receives the Syphon frames + the same
   OSC bus and composites everything for output. Recipe in
   [`viz/touchdesigner/README.md`](viz/touchdesigner/README.md).

### Prompt-source toggle

The sidecar supports three modes, switchable at runtime via
`/viz/prompt/source` or at startup via `--viz-prompt-source` on
`muse2-music run`:

- **`auto`** (default, brain-only): the sidecar ignores any manual
  prompt text and interpolates between named prompt banks in
  [`viz/prompts/default.yaml`](viz/prompts/default.yaml) using a
  brain-derived `prompt_blend` signal. No user input required.
- **`manual`**: use only the text written to
  [`viz/prompts/live.txt`](viz/prompts/live.txt) (or sent over
  `/viz/prompt/base`). Brain still modulates diffusion strength and
  the TD post-fx layer.
- **`mix`**: bank interpolation with the manual prompt appended as a
  style suffix.

### Quick start (visual layer)

```bash
# Terminal 1: the sidecar (first run downloads ~6 GB of SDXL-Turbo weights)
cd viz/sidecar
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -e .
muse2-viz-sidecar --backend fake     # smoke-test with procedural output first
# when ready for real AI:
muse2-viz-sidecar                    # defaults to SDXL-Turbo + MPS + port 9100

# Terminal 2: the brain pipeline, now with viz publishing
source .venv/bin/activate            # the main package venv, not the sidecar's
muse2-music run --viz --backend osc  # or --backend both for DAW + viz
```

Then open the TouchDesigner recipe in
[`viz/touchdesigner/README.md`](viz/touchdesigner/README.md) and
follow the 15-minute node-by-node assembly. Edit
`viz/prompts/live.txt` any time; the sidecar picks up changes within a
second.

## Project layout

```
src/muse2_music_lab/
  config.py        # all tunable constants — edit first
  board.py         # BrainFlow connection to the Muse 2
  features.py      # band powers, focus/calm, blink, jaw clench
  smoother.py      # EMA + baseline normalization
  mapping.py       # signal → CC / OSC routing table (edit freely)
  output_midi.py   # MIDI CC backend (mido + rtmidi)
  output_osc.py    # OSC backend (python-osc)
  viz_bridge.py    # /viz/* OSC bridge (used when --viz)
  viz_mapping.py   # signal → /viz/* routing table
  main.py          # the 30 Hz runtime loop
  tui.py           # optional rich-based live TUI
  cli.py           # 'muse2-music' argparse entry point

viz/                      # optional visual layer (TD + diffusion sidecar)
  touchdesigner/
    README.md       # TD project assembly recipe
    osc_schema.md   # canonical /viz/* OSC contract
  sidecar/          # standalone Python package (its own venv)
    sidecar/*.py
    pyproject.toml
  prompts/
    default.yaml    # prompt banks (edit freely)
    live.txt        # manual prompt (watched by sidecar)
```

## Troubleshooting

- **"No Muse found" or BrainFlow hang**: make sure the headset is charged,
  powered on (single LED blink pattern = ready to pair), and paired in
  macOS Bluetooth settings. Kill other Muse apps (Muse app, Mind Monitor,
  etc.) that might hold the BLE connection.
- **No MIDI received by Logic**: run `muse2-music list-midi` and check the
  port name matches what's in `config.py` (or pass `--midi-port`). Confirm
  the IAC Driver is **online** in Audio MIDI Setup.
- **Signals pegged at 0 or 1**: the baseline was bad. Re-run with a longer
  calibration (`--calibrate-seconds 10`) while sitting completely still.
- **Blink/jaw never trigger**: lower `BLINK_THRESHOLD_UV` or
  `JAW_THRESHOLD_UV` in `config.py`. They depend on headset fit.

## License

MIT
