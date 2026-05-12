# Roadmap

Single source of truth for everything that needs to be built, tested, and tuned. Check items off as you go. New ideas: append to the relevant section, don't re-order historical sections.

Cross-references:
- `DEPLOYMENT.md` — exact 3-terminal launch sequence for live sessions
- `viz/touchdesigner/README.md` — node-by-node TD assembly recipe
- `viz/touchdesigner/osc_schema.md` — canonical `/viz/*` contract
- `viz/sidecar/README.md` — sidecar install + flags

Type tags: `CODE` `GUI` `TEST` `CREATIVE` `RESEARCH` `DOCS`

---

## Section A — Required to use what's already built

These unlock the experience the project currently supports. No more code to write here, just GUI work + verification.

- [ ] **A1.** Headset sanity check after the gap *(TEST · 5 min)*
  - Cmd: `source .venv/bin/activate && muse2-music run --tui`
  - Verify: BLE pairs, 8s calibration completes, all 5 feature bars move with mental state, blink + jaw triggers fire, μV diagnostics readable
  - Blocks: A2, A3, A5
- [ ] **A2.** Re-tune blink / jaw thresholds if needed *(CREATIVE · 5–10 min)*
  - Watch `last_ptp` (blink) and `last_rms` (jaw) μV values in TUI while making the gestures
  - Edit `BLINK_THRESHOLD_UV` / `JAW_THRESHOLD_UV` in `src/muse2_music_lab/config.py`
  - Restart and repeat until reliable
  - Blocks: A6 (creative consistency)
- [ ] **A3.** Assemble TouchDesigner project *(GUI · 15–30 min, one-time)*
  - Follow `viz/touchdesigner/README.md` node-by-node
  - Save the `.toe` somewhere stable (outside the repo — TD's binary format doesn't diff)
  - Blocks: A4
- [ ] **A4.** First full live brain → AI visual session *(TEST · 15 min, the headline payoff)*
  - 3 terminals from `DEPLOYMENT.md` Section 2: sidecar, `muse2-music run --viz --backend midi --tui`, optionally `monitor-midi`
  - Open the TD project, hit Perform Mode (F1) for fullscreen
  - Verify: Syphon stream visible in TD, OSC In CHOP shows brain values, image visibly responds when you change mental state
- [ ] **A5.** Logic Pro MIDI Learn assignments *(GUI · 15–30 min, one-time)*
  - Smart Controls → MIDI Learn → wiggle a brain feature → Logic latches the CC
  - Suggested mappings (from `mapping.py`): focus→74→cutoff, calm→91→reverb wet, alpha→20→LFO rate, beta→21→drive, theta→22→delay fb, blink→64→sample/bypass, jaw→65→stutter
- [ ] **A6.** First joint music + visual session *(TEST · open-ended)*
  - Headset on, sidecar + `muse2-music run --viz --backend both --tui` + Logic + TD all running
  - Make sounds and visuals at the same time, with the same brain

---

## Section B — Phase 2: DAW audio as second visual driver

The architecture was designed for this — drops in cleanly. ~3–4 hours total.

- [ ] **B1.** Install BlackHole virtual audio device *(GUI · 5 min, one-time)*
  - Free from Existential Audio (`brew install blackhole-2ch`)
- [ ] **B2.** Route Logic Pro stereo bus through BlackHole *(GUI · 10 min, one-time)*
  - Audio MIDI Setup → Multi-Output Device combining built-in + BlackHole
  - Set as Logic's output device
- [ ] **B3.** Add Audio Device In CHOP in TD reading BlackHole *(GUI · 5 min)*
- [ ] **B4.** Add Audio Spectrum CHOP + Analyze CHOP for RMS / centroid / onset *(GUI · 15 min)*
- [ ] **B5.** Build "source-router" Container COMP in TD *(GUI · 30 min)*
  - Per-`/viz/params/*` channel: dropdown of brain / audio / mix
  - The interesting part — runtime swappable drivers per parameter
- [ ] **B6.** Update `viz/touchdesigner/README.md` with the audio chain *(DOCS · 15 min)*
- [ ] **B7.** Test Phase 2 *(TEST · 30 min)*
  - Verify: kick drum visibly drives `/viz/params/intensity`, bass-heavy mix shifts color temp, switching the source-router live actually swaps drivers

---

## Section C — Phase 3a: MIDI → /viz/* bus

Thin layer once Phase 2 is done. Mostly TD work.

- [ ] **C1.** Add MIDI In DAT in TD reading IAC Driver Bus 1 *(GUI · 10 min)*
- [ ] **C2.** Map note velocity → `/viz/trigger/*` for note-level visual events *(GUI · 20 min)*
- [ ] **C3.** Add MIDI source option to Phase 2's source-router *(GUI · 10 min)*

---

## Section D — Phase 3b: Auto-caption feedback loop

Most ambitious / most novel. Multi-day project. Real risk of degenerate loops; requires real iteration.

- [ ] **D1.** Pick a vision-language model that runs fast on M4 Max MPS *(RESEARCH · 2–4 hr)*
  - Candidates: Florence-2 (~100ms captions, fastest), MiniCPM-V 2.6 via `mlx-vlm` (best quality), LLaVA
  - Decision criterion: target <300ms caption latency so it can keep up at sub-fps caption rate
- [ ] **D2.** Add `viz/sidecar/sidecar/captioner.py` *(CODE · 3–6 hr)*
  - Async VLM service consuming the latest rendered frame
  - Must run on a separate thread/process — must NOT block the diffusion loop
- [ ] **D3.** Wire captions back into `prompt_builder.py` as `feedback` mode *(CODE · 2 hr)*
  - Extends the existing `auto / manual / mix` toggle
  - Use OSC: `/viz/prompt/source feedback`
- [ ] **D4.** Tune to avoid degenerate loops *(CREATIVE · open-ended)*
  - Common failure: caption keeps describing the same thing → image stops evolving
  - Mitigations to try: caption decay, brain-state perturbation injected into prompt, periodic forced re-anchoring to a bank
- [ ] **D5.** Test full chain *(TEST · 1 hr)*
  - Brain → image → caption → prompt → image
  - Watch for collapse; capture sample chains as proof of working / not-working states

---

## Section E — Optimization (mentioned but optional)

- [ ] **E1.** `/viz/status/*` echo from sidecar *(CODE · 30 min)*
  - Sidecar publishes `/viz/status/fps`, `/viz/status/prompt`, `/viz/status/source` over OSC for anyone listening (TD HUD, monitoring, etc.)
- [ ] **E2.** Build TD HUD that shows current prompt + fps using E1 *(GUI · 15 min)*
  - Lives on a corner of the output, useful in performance
- [ ] **E3.** Investigate MLX SDXL-Turbo *(RESEARCH+CODE · 1–2 days)*
  - Realistic ~1.4× speedup over diffusers MPS (~10 fps at 384, ~7 fps at 512)
  - No PyPI package; would vendor `mlx-examples`. Several-day project
- [ ] **E4.** Investigate CoreML SDXL-Turbo *(RESEARCH+CODE · 2–4 days)*
  - Apple `ml-stable-diffusion`; SDXL-Turbo not officially supported, requires custom LCM scheduler
  - Potentially big speedup, real risk of getting stuck on Turbo-specific compat
- [ ] **E5.** Pre-encode + cache the negative prompt *(CODE · 15 min)*
  - Currently re-encoded with positive on cache miss; marginal (~10ms/frame)
- [ ] **E6.** Auto-restart sidecar on crash *(CODE · 30 min)*
  - Either supervisord or a 5-line bash loop. Stage safety
- [ ] **E7.** Brain → visual latency profiling *(TEST · 1 hr)*
  - End-to-end measurement: blink → visible flash time. Identify slowest hop

---

## Section F — Creative tuning (open-ended, no "done" state)

These need the headset and a real session. None block anything else.

- [ ] **F1.** Tune `SMOOTHING_ALPHA` in `config.py` — responsive vs. laggy feel *(CREATIVE · per session)*
- [ ] **F2.** Tune diffusion `strength` floor/ceiling in `prompt_builder.py` (currently 0.25–0.85) *(CREATIVE · per session)*
- [ ] **F3.** Refine prompt banks in `viz/prompts/default.yaml` to match your aesthetic *(CREATIVE · per session)*
- [ ] **F4.** Refine `mapping.py` — does focus-on-cutoff feel right? Try focus-on-resonance? *(CREATIVE · per session)*
- [ ] **F5.** Refine `viz_mapping.py` `derive_extra()` — `prompt_blend` weighting (currently 0.7×alpha + 0.3×calm) *(CREATIVE · per session)*
- [ ] **F6.** Train deliberate blink/jaw "gestures" as performance moves; tune refractory periods *(CREATIVE · per session)*
- [ ] **F7.** Curate TD procedural feedback layer parameters (feedback amount, displacement strength) *(CREATIVE · per session)*

---

## Section G — Hygiene / nice-to-have

- [ ] **G1.** Confirm `viz/sidecar/muse2_viz_sidecar.egg-info/` is git-ignored *(CODE · 1 min)*
- [ ] **G2.** Save TD project as a `.tox` snapshot in `viz/touchdesigner/` *(GUI · 10 min)*
  - TD project files are binary, but `.tox` exports give a partial git-friendly checkpoint
- [ ] **G3.** Add a "render N seconds of brain to MP4" mode *(CODE · 1–2 hr)*
  - Headless capture for documentation / pitching
- [ ] **G4.** Pre-commit hook for ruff/black *(CODE · 15 min)*

---

## Recommended next-session paths

Pick one based on time available:

### ~10 min — minimal verification
- A1 → A2

### ~30 min — pick a side
- A1 → A2 → either A3 (visual) or A5 (DAW), whichever excites you more

### ~2 hours — full Phase 1 deployment
- A1 → A2 → A3 → A4 → A5 → A6
- End state: brain making music + visuals simultaneously in a single session

### Half-day — Phase 1 + audio Phase 2
- All of A, then B1 → B7
- End state: visual driven by brain OR DAW audio OR both, switchable live

### Multi-day — Phase 3 caption loop
- D1 → D2 → D3 → D4 → D5
- Highest novelty, highest risk. Don't start until A is fully done

---

## Done (for reference)

Already shipped on `main` (most recent commit first):

- `b263172` — `monitor-midi` + `simulate` subcommands + `DEPLOYMENT.md` (e2e diagnostics, headset-free testing)
- `9585d28` — sidecar default to 384×384 for ~7 fps headroom
- `0da43be` — sidecar 3× speedup: TAESD VAE + prompt cache + MPS warmup
- `2b06462` — TouchDesigner setup recipe
- `30a9dbb` — diffusion sidecar (SDXL-Turbo + Syphon)
- `d582607` — `/viz/*` OSC bus + `--viz` flag
- `50823a1` — live TUI diagnostics + README
- `cbac7c0` — main runtime loop + CLI rewrite
- `43951ca` — output backends + signal routing
- `313614f` — smoother / EMA / normalization
- `78f7d11` — feature extraction
- `5076820` — BrainFlow board wrapper
- `080d74b` — config.py
- `6a10931` — drop muselsl, adopt BrainFlow
