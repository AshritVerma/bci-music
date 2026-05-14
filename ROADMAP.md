# Roadmap

The previous TouchDesigner / Logic Pro / MIDI-CC architecture has been
retired. Live work is on the **Muse 2 -> Lyria RealTime music + Three.js
shader visualizer** pipeline, all running in a single Python process with
the visual rendered in a Chromium tab.

## Authoritative documents

- `PROJECT_PLAN.md` -- design, repo layout, mapping table, runbook.
- `.cursor/plans/muse-lyria-pivot_*.plan.md` -- per-phase implementation
  plan with verify gates.

## Current state (Phase 0 + 1 done)

- `viz/`, all Syphon publishers, MIDI/OSC backends, and the TouchDesigner
  project are gone.
- `board.py` / `features.py` / `smoother.py` are now under
  `src/muse2_music_lab/eeg/`.
- CLI surface:
  - `muse2-music run` -- live EEG -> TUI diagnostics (no music).
  - `muse2-music perform --prompt "..."` -- stub (Phase 3 lands the orchestrator).
  - `muse2-music simulate` -- synthetic EEG -> TUI.
  - `muse2-music battery`, `muse2-music bt-reset` -- BLE operator tools.

## Phases ahead

| Phase | Deliverable |
| ----- | ----------- |
| 2 | Lyria smoke test (`scripts/lyria_smoke.py`). |
| 3 | `state.AppState` + `main.py` asyncio orchestrator skeleton. |
| 4 | `eeg/brainflow_loop.py` writes normalized features into `AppState`. |
| 5 | `music/lyria_client.py` + `audio_playback.py` + `mappings.py`. |
| 6 | `music/audio_analysis.py` (RMS / spectral centroid / onset). |
| 7 | `server/app.py` + minimal `visualizer/index.html`. |
| 8 | `visuals/seed_image.py` (MLX-Diffusion or diffusers-MPS fallback). |
| 9 | Three.js full-screen quad + custom GLSL fragment shader. |
| 10 | Polish: auto-launch Chrome, graceful shutdown, normalization tuning. |
