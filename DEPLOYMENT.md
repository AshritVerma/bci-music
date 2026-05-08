# Deployment / live session checklist

The exact sequence to launch a full brain → DAW + brain → AI visual session.

## 0. One-time setup (already done)

- `.venv/` exists with `muse2-music` installed
- `viz/sidecar/.venv/` exists with `muse2-viz-sidecar` installed
- IAC Driver enabled in Audio MIDI Setup (`muse2-music list-midi` shows `IAC Driver Bus 1`)
- SDXL-Turbo weights cached at `~/.cache/huggingface/hub/` (~6 GB, downloaded on first sidecar run)
- TouchDesigner project assembled per `viz/touchdesigner/README.md` (do this once, save the `.toe` somewhere)

## 1. Verify before each session (~2 min)

```bash
cd /Users/ashrit/Code/muse2-music-lab
source .venv/bin/activate

# Check the IAC bus is alive and named correctly
muse2-music list-midi

# Verify Bluetooth pairing + brain pipeline (with headset on)
muse2-music run --tui
# Sit still 8s for calibration. Confirm:
#   - bars for alpha/beta/theta/focus/calm move with your mental state
#   - blink fires the BLINK trigger
#   - jaw clench fires the JAW trigger
#   - last_ptp / last_rms μV readings make sense for your fit
# Ctrl-C to stop.
```

If blink or jaw don't trigger reliably, edit `BLINK_THRESHOLD_UV` / `JAW_THRESHOLD_UV` in `src/muse2_music_lab/config.py` based on the live μV readings.

## 2. Live session — three terminals

### Terminal 1 — diffusion sidecar

```bash
cd /Users/ashrit/Code/muse2-music-lab
viz/sidecar/.venv/bin/muse2-viz-sidecar
```

Wait for `[diffusion] ready` then `[loop] fps=7.0 source=auto …`. Defaults:

- 384×384, TAESD VAE, 1 step → ~7 fps on M4 Max
- Listening on UDP port 9100
- Publishing as Syphon server `Muse2Viz`
- Watching `viz/prompts/live.txt` for manual prompt updates
- Default mode `auto` (brain-only, no manual text needed)

### Terminal 2 — brain pipeline (the headline command)

```bash
cd /Users/ashrit/Code/muse2-music-lab
source .venv/bin/activate
muse2-music run --viz --backend midi --tui
```

What this does in one process:

- Connects to Muse 2 via BLE (BrainFlow)
- Calibrates baseline for 8 seconds — sit still
- Streams features at 30 Hz to:
  - **MIDI CCs** on `IAC Driver Bus 1` for Logic Pro
  - **`/viz/*` OSC** on port 9100 for the sidecar
- Live TUI shows feature bars + trigger meters + diagnostic μV

### Terminal 3 (optional, debugging only) — MIDI monitor

```bash
muse2-music monitor-midi
```

Live `rich` table of every CC arriving on the IAC bus. Useful when something doesn't seem to reach Logic — confirms the MIDI side is hot before blaming the DAW.

### Open the apps

- **TouchDesigner**: open the saved project. The Syphon Spout In TOP (sender name `Muse2Viz`) starts showing live frames; the OSC In CHOP (port 9100) starts showing brain values.
- **Logic Pro**: open your project. MIDI Learn maps brain CCs to plugin params (Smart Controls → MIDI assignments → Learn → wiggle a brain feature). Recommended starter mappings:
    - `focus` → CC 74 → filter cutoff
    - `calm` → CC 91 → reverb wet
    - `alpha` → CC 20 → LFO rate / pad swell
    - `beta` → CC 21 → drive / saturation
    - `theta` → CC 22 → delay feedback
    - `blink` → CC 64 → sample one-shot / effect bypass
    - `jaw` → CC 65 → stutter / gate

## 3. Headset-free testing

When you're tweaking TD or Logic and don't want to wear the band:

```bash
# Replace 'run' with 'simulate' — pumps synthetic brain through the same pipes
muse2-music simulate --viz --backend midi
```

Synthetic frames sweep alpha/beta/theta/focus/calm sinusoidally and fire blink/jaw periodically (every 4s and 7s by default). Everything downstream sees realistic brain-shaped data.

## 4. Switching prompt source mid-session

The sidecar's `--viz-prompt-source` defaults to `auto` (brain-only). To switch live without restart, send an OSC string:

```bash
.venv/bin/python3 -c "
from pythonosc import udp_client
c = udp_client.SimpleUDPClient('127.0.0.1', 9100)
c.send_message('/viz/prompt/source', 'manual')   # or 'mix' or 'auto'
c.send_message('/viz/prompt/base', 'a melting clockface in a sci-fi desert, cinematic')
"
```

Or in `manual` / `mix` mode, just write text to `viz/prompts/live.txt`; the sidecar picks it up within a second.

## 5. Cleanup

`Ctrl-C` in each terminal. Each process handles SIGINT cleanly:

- `muse2-music run` releases the BLE connection (important — orphaned processes will block the next BLE pairing)
- `muse2-viz-sidecar` flushes Syphon, prints final frame count
- `muse2-music monitor-midi` releases the IAC input handle

If the BLE seems stuck on the next start, kill any stray Python processes:

```bash
pkill -9 -f muse2-music
```

## Troubleshooting quick reference

| Symptom | Check |
|---|---|
| `(no MIDI output ports found)` | Audio MIDI Setup → IAC Driver → Device is online |
| Sidecar `[syphon] failed to start` | macOS only; on Apple Silicon should always work — restart the process |
| Sidecar fps below 5 | First frame is always slow (~350ms); steady state should be ~140ms at 384 |
| TD Syphon Spout In TOP black | Sender name mismatch (must equal `Muse2Viz`) or sidecar not running |
| TD OSC In CHOP empty | Port mismatch (must equal `9100`) or `--viz` flag not on the `muse2-music run` command |
| BrainFlow hangs on startup | Other Muse app holding BLE; close Muse app / Mind Monitor; `pkill -9 -f muse2-music` |
| Blink/jaw never fire | Watch `last_ptp` / `last_rms` μV in TUI; lower `BLINK_THRESHOLD_UV` / `JAW_THRESHOLD_UV` in `config.py` |

## Verified end-to-end

Confirmed working as of `9585d28` + monitor/simulate additions:

- Brain features → MIDI CC on IAC bus → captured by external mido reader (1592 msgs/15s, all 7 expected CCs)
- Brain features → `/viz/*` OSC → sidecar `VizState` → `PromptBuilder` → SDXL-Turbo on MPS → Syphon publish (sustained 7.1 fps at 384×384)
- Live `auto / manual / mix` prompt source toggle
- Both monitor-midi and simulate subcommands open ports cleanly and shut down on SIGINT

Manual steps that remain for the user:
- Build the TouchDesigner network once (recipe in `viz/touchdesigner/README.md`)
- MIDI-Learn assignments in Logic Pro
