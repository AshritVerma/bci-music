# brain-music

Live music + visuals driven by an EEG headset.

A single Python process reads EEG from an **Interaxon Muse 2** over
Bluetooth, generates **continuous music** with **Google Lyria RealTime**,
and renders **continuous visuals** in a Chromium tab via a **Three.js
+ GLSL shader**, all modulated in real time by what your brain is
doing. One text prompt at session start drives both the music and the
visuals.

```
+----------+     +-----------+     +----------------+     +------------+
| Muse 2   | --> | EEG loop  | --> |   AppState     | --> | Lyria      |
| (BLE)    |     | (4 Hz)    |     |  (asyncio)     |     | RealTime   |
+----------+     +-----------+     +-------+--------+     +-----+------+
                                           |                    |
                                           v                    v
                                   +---------------+    +---------------+
                                   | HTTP/WS srv   |    |  sounddevice  |
                                   | (20 Hz snap)  |    |   playback    |
                                   +-------+-------+    +-------+-------+
                                           |                    |
                                           v                    v
                                   +-------+-------+    +-------+-------+
                                   |  Browser UI   |    |   Audio FFT   |
                                   |  + Three.js   | <--+ (RMS/cent/    |
                                   |    shader     |    |    onset)     |
                                   +---------------+    +---------------+
```

No DAW. No MIDI. No TouchDesigner. No multi-process IPC. Open the
browser tab, type a prompt, hit Start, put the headset on.

## What you need

- **Hardware**
  - Interaxon **Muse 2** EEG headset, charged and powered on
  - macOS with Bluetooth (Apple Silicon recommended; tested on M4 Max)
  - Speakers or headphones (Lyria streams 48 kHz stereo)
- **Accounts / API keys**
  - **Gemini API key** with access to `lyria-realtime-exp` and
    `imagen-4.0-fast-generate-001` (Google AI Studio)
  - *(Optional but recommended)* **Anthropic API key** for the
    Claude-Opus prompt-guard and seed-evolver polishing
- **Software**
  - **Python 3.11** (the `[tool.uv]` venv pins this)
  - A **Chromium-family browser** (Chrome / Edge / Brave) for the visualizer

## Setup

```bash
git clone https://github.com/AshritVerma/brain-music.git
cd brain-music

uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e .

cp .env.example .env
# edit .env and fill in:
#   GEMINI_API_KEY=...
#   ANTHROPIC_API_KEY=...   (optional)
```

First-run note on macOS Bluetooth: the OS will prompt your terminal
(or your editor's integrated terminal) for Bluetooth access the first
time BrainFlow tries to scan. Approve it.

## Usage

### The full pipeline

```bash
muse2 perform
```

What happens:

1. The HTTP/WS server starts on `http://localhost:8765/`.
2. The browser tab opens automatically.
3. The EEG supervisor connects to the Muse 2 (or starts the simulator
   if `--simulate-eeg` was passed).
4. You type a text prompt in the centered Start panel and hit **Start ↵**.
5. Imagen generates a seed image from the prompt; Lyria opens a streaming
   session and begins emitting 48 kHz stereo PCM; the browser cross-fades
   from the placeholder to the real seed.
6. The visualizer modulates the seed in real time using your brain
   features (alpha/beta/theta/asymmetry) and the music's audio features
   (RMS / spectral centroid / onset). Every ~24 s of music the seed
   evolves into a new image based on where your brain has been drifting.

Browser controls (always available):

- **Start** — begins the Lyria session and seed image generation
- **Recalibrate** — rebaselines the EEG normalizer in place
- **Quit** — stops every task and exits the Python process cleanly
- **EEG: real / simulated** — hot-swap between the headset and the
  synthetic generator without restarting

### Useful flags

```bash
# Drive the entire pipeline off the synthetic EEG (no headset needed)
muse2 perform --simulate-eeg

# Visual-only smoke test (no audio bills, no Lyria latency)
muse2 perform --simulate-eeg --no-lyria

# Reuse the previous session's seed image instead of regenerating
muse2 perform --skip-seed

# Change how often the seed image evolves (default 12 chunks ~= 24s)
muse2 perform --evolve-chunks 24

# Skip the rich TUI (useful when piping logs)
muse2 perform --no-tui

# Pre-fill the prompt at the CLI; auto-starts (no Start panel)
muse2 perform --prompt "downtempo electronic with warm analog pads"
```

### EEG-only diagnostic TUI

```bash
muse2 run                # real Muse 2
muse2 simulate           # synthetic EEG
```

Live `rich` TUI showing band powers, asymmetry, blink/jaw triggers,
and the BLE connection state. Press **r** + Enter to recalibrate, **q**
+ Enter to quit.

### BLE operator tools

```bash
muse2 battery            # one-shot Muse 2 charge level
muse2 bt-reset           # clear stuck BLE connections after a crash
```

## Signal mapping

Every normalized EEG / audio feature is in `[0, 1]` and feeds both the
music and the visuals. Same brain-state value lights up correlated
parameters on each side, which is what makes the audio and visual feel
coupled even though they're generated independently.

| Feature   | Source       | Music (Lyria)        | Visualizer (shader)         |
| --------- | ------------ | -------------------- | --------------------------- |
| alpha     | EEG          | brightness           | blur radius                 |
| beta      | EEG          | density              | contrast + kaleido segments |
| theta     | EEG          | temperature          | tunnel twist + kaleido drift|
| asymmetry | EEG          | bpm tilt             | hue-shift L/R modulator     |
| blink     | EEG (peak)   | (none, future)       | screen flash                |
| jaw       | EEG (peak)   | (none, future)       | radial shockwave            |
| rms       | audio FFT    | (none — audio out)   | zoom-pulse + brightness     |
| centroid  | audio FFT    | (none — audio out)   | hue temperature shift       |
| onset     | audio FFT    | (none — audio out)   | chromatic-aberration kick   |

## Visual regimes

The shader doesn't have a single "look". Every 15–30 s the visualizer
cross-fades between four distinct **regimes**, each a different warp
of the seed texture:

- **calm** — subtle drift, image stays mostly stable
- **tunnel** — log-spiral zoom into infinity
- **kaleidoscope** — angular fold into 6–12 mirror segments
- **ripple** — concentric water-waves emanating from center

A slow autonomous LFO also walks the hue continuously so the colors
are always shifting, even at neutral EEG. Brain features modulate
intensity *within* whichever regime is active.

## Project layout

```
src/muse2_music_lab/
  config.py            # all tunable constants - edit first
  state.py             # AppState dataclass (cross-task shared state)
  main.py              # asyncio orchestrator entry point
  cli.py               # 'muse2' / 'muse2-music' argparse entry point
  perform_tui.py       # rich.Live TUI for `muse2 perform`
  tui.py               # rich.Live TUI for `muse2 run`
  battery.py           # `muse2 battery`
  bt_reset.py          # `muse2 bt-reset`
  simulate.py          # synthetic-EEG generator

  eeg/
    board.py           # BrainFlow wrapper for Muse 2 BLE
    features.py        # band powers, asymmetry, blink, jaw
    smoother.py        # EMA + baseline calibration + tanh normalize
    brainflow_loop.py  # async EEG loop + supervisor (real <-> sim)

  lyria/
    session.py         # Lyria RealTime session + reconnect supervisor
    mapping.py         # EEG features -> Lyria control parameters
    audio_play.py      # sounddevice playback draining Lyria PCM

  audio/
    fft.py             # real-time RMS / centroid / onset features

  music/
    prompt_guard.py    # Claude rewriter for Lyria-filtered prompts

  server/
    app.py             # aiohttp HTTP/WS server + browser action router

  visuals/
    seed_image.py      # Imagen 4 Fast seed generation + cache
    seed_evolver.py    # Claude-driven Imagen regenerations every N chunks

static/
  index.html           # browser UI shell
  style.css            # dark UI styling
  app.js               # WebSocket client + UI controls + seed cross-fade
  visualizer.js        # Three.js + GLSL multi-regime shader

scripts/
  lyria_smoke.py       # standalone Lyria RealTime API test
```

## Troubleshooting

- **"No Muse found" / BrainFlow hangs**: make sure the headset is
  charged, powered on (LED breathing pattern), and not held by another
  app (Muse, Mind Monitor, etc.). If the previous run crashed, try
  `muse2 bt-reset`. The `EEG: real / simulated` browser toggle lets
  you start in simulated mode and swap to real once the headset is back.
- **Lyria says nothing arrives / silent audio**: check that
  `GEMINI_API_KEY` is set and has Lyria RealTime access enabled (the
  model is `lyria-realtime-exp`). Run `python scripts/lyria_smoke.py`
  for a focused test.
- **Visualizer is black**: open DevTools, look for `[viz]` log lines.
  A red banner over the canvas means a shader compile error or a
  texture load failure. The fallback radial gradient kicks in if
  `static/seed.png` doesn't load — it's intentionally ugly so you
  can tell the difference.
- **EEG values pegged at 0.50 after recalibrate**: the headset lost
  contact and the smoother baselined against a flat signal. Re-seat
  the band, wait for the alpha/beta bars to wiggle, then recalibrate
  again.
- **Process lingers after Quit**: BrainFlow's underlying C++ BLE
  discovery thread can outlive the Python task by ~10–20 s. The
  Python orchestrator returns cleanly; the OS reaps the BLE thread
  shortly after.

## License

MIT
