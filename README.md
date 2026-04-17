# Muse2 Music Lab

Experiments using an **Interaxon Muse 2** as an input device for music production: stream EEG (and optional PPG / motion) over **Lab Streaming Layer (LSL)**, then map signals to **OSC** so a DAW or modular environment can use them.

## Prerequisites

- **Muse 2** charged and paired over Bluetooth (macOS: System Settings → Bluetooth).
- **Python 3.9+**
- On some systems you may need extra Bluetooth support; see [muse-lsl](https://github.com/alexandrebarachant/muse-lsl) for platform notes.

## Setup

```bash
cd muse2-music-lab
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

## Usage

### 1. List devices

```bash
muselsl list
```

### 2. Start an LSL stream from the headset

Full Muse 2 sensors (EEG + PPG + accelerometer + gyro):

```bash
muse2-music stream --ppg --acc --gyro
```

Or EEG only:

```bash
muse2-music stream
```

This wraps `muselsl` so you stay inside this project’s environment.

### 3. Bridge EEG → OSC (for Ableton, Max, SuperCollider, etc.)

With a stream running in another terminal, send smoothed per-channel activity to OSC (default `127.0.0.1:9000`):

```bash
muse2-music bridge
```

OSC path (default `/muse/eeg`): four floats `TP9, AF7, AF8, TP10` in roughly `0.0–1.0` (mean absolute amplitude over a short window, normalized). Override with `--osc-path`.

Point your DAW or `[udpreceive]` at the same host/port and map those floats to volume, macros, or MIDI via your environment’s OSC→MIDI tools.

## Project layout

- `src/muse2_music_lab/` — CLI and bridge code.
- Future: alternate mappers (frequency bands, blink detection, motion gestures).

## License

MIT
