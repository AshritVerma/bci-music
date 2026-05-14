# Demo day checklist

Live brain-music demo: laptop + Muse 2 headset + speakers, you wearing
the band, audience watching the visualizer on a screen.

## ~30 minutes before

- [ ] **Charge the Muse 2.** Should be ≥80% (a Muse 2 burns through
      ~3-4% per hour of streaming; a low charge mid-demo manifests as
      a sudden BLE drop).
- [ ] **Charge the laptop** to 100% or plug in. The orchestrator runs
      Lyria + Imagen + a shader — laptop fans will spin. Plugged in
      avoids any thermal throttling.
- [ ] **Quit anything else holding the Bluetooth radio.** The Muse
      official app, Mind Monitor, any prior unkilled `muse2 perform`
      process. If unsure, run:

      ```bash
      muse2 bt-reset      # cycles macOS Bluetooth; ~5 s
      ```

- [ ] **Quit anything else holding the audio device** (Spotify,
      Logic, browsers playing video). PortAudio gets exclusive low-
      latency access more reliably this way.
- [ ] **Verify GEMINI_API_KEY + ANTHROPIC_API_KEY** in `.env`:

      ```bash
      grep -E '^(GEMINI|ANTHROPIC)_API_KEY=' .env
      ```

- [ ] **Latest code pulled:**

      ```bash
      git pull origin main
      git log -1 --format='%h %s'
      ```

## ~5 minutes before

- [ ] **Power on the Muse 2.** Hold the button until the LEDs do the
      slow-breathing pattern (NOT the steady-on or fast-blink — those
      mean charging or paired with something else respectively).
- [ ] **Check the headset is reachable** before the audience is
      watching:

      ```bash
      muse2 battery
      ```

      Expect a `[battery] Charge level: NN%` line within ~10 s. If it
      hangs or fails, run `muse2 bt-reset` and try again.

- [ ] **Position the band** with all four sensors in clean contact:
      AF7 + AF8 on the forehead (above the eyebrows, no hair underneath),
      TP9 + TP10 behind the ears. A common failure mode is hair
      between AF7/8 and skin — lifts the impedance and the alpha bar
      sits at 0.5 forever.

## Cold start (the actual launch)

- [ ] **Open the workspace and a fresh terminal.** No leftover stuck
      Python from earlier.
- [ ] Activate the venv:

      ```bash
      source .venv/bin/activate
      ```

- [ ] **Launch:**

      ```bash
      muse2 perform
      ```

      Watch for these log lines, in order, within ~15 s:

      ```
      [eeg-sup] starting in mode='real'
      [eeg] using REAL EEG via BrainFlow ...
      [eeg] connected, calibrating for 8.0s ...
      [server] listening on http://localhost:8000/
      [server] launched browser at http://localhost:8000/
      ```

      The browser should open to the Start panel automatically.

- [ ] **In the browser**, look at the top-right pills:
  - `Muse: connected` (green) — the band is streaming features
  - `WS: connected` (green) — the page is talking to the server
  - `EEG: real → use simulated` — confirms the live path is active

  If `Muse: lost` or `Muse: failed` appears, click the `EEG: real →
  use simulated` button to fall back to the synthetic generator and
  the demo can still run audibly. (You can swap back to real later
  if the band reconnects.)

- [ ] **Pick a prompt** — easiest: click one of the chips below the
      textarea (all five are pre-tested). Or type one. Then click
      **Start ↵** (or press Enter).

- [ ] **Expect within ~5-25 s:**
  - `warming up Lyria...` banner appears below the header
  - First Imagen seed image generates in the background
  - Banner disappears when Lyria's first audio chunk arrives
  - Audio starts ~1 s later (PortAudio pre-roll)
  - Visualizer cross-fades from the placeholder to the seed image

- [ ] **Press `h`** to hide the diagnostic HUD if you want a clean
      visual for the audience. Press `h` again to bring it back.

## During the demo

The brain-music coupling at the new sensitivity:
- **Eyes-closed / relaxed** → alpha rises → music brightens
- **Mental focus / problem-solving** → beta rises → music densifies
- **Drowsy / meditative** → theta rises → temperature climbs (more
  divergent within the prompt)
- **Right-leaning frontal asymmetry** → BPM accelerates;
  left-leaning slows it down

The seed image evolves every ~24 s based on cumulative drift; you
should see ~2-3 evolutions in a 60-90 s segment.

## Recovery moves (if something breaks live)

- **No audio, banner stuck on "warming up"**: a Lyria session has
  stalled. Wait 15 s for the watchdog to auto-reconnect; usually
  audio arrives within 5 s of the second connect. If 3+ stalls in a
  row (≥45 s of silence), Ctrl-C in the terminal and relaunch.

- **EEG bars all stuck at 0.50**: the headset lost contact OR the
  baseline normalized against a flat signal. Click **Recalibrate**
  in the header (8 s baseline window, no audio interruption).

- **EEG: lost / Muse: failed pill is red**: BLE drop. The supervisor
  auto-reconnects up to 3 times. If it gives up, click the EEG mode
  toggle to swap to simulated and continue. Re-seat the band; you
  can swap back later.

- **Sound is silent / clicky**: another app grabbed PortAudio between
  launches. `Cmd+Q` Spotify / Music / browser-with-video, then
  Ctrl-C and relaunch.

- **Visualizer is black**: open DevTools → Console. A red banner
  over the canvas means a shader compile error or the seed image
  failed to load. Refresh the page (`Cmd+R`); the WS auto-reconnects
  to the live server without disrupting Lyria.

- **Process won't quit cleanly**: Ctrl-C usually works in <3 s. If
  it hangs, second Ctrl-C escalates. Worst case:

      ```bash
      pkill -9 -f "muse2 perform"
      muse2 bt-reset      # in case the BLE link got stuck
      ```

## End of demo

- [ ] In the browser: click **Quit**. The orchestrator shuts down
      every task gracefully, the page shows a "Session ended" overlay.
- [ ] Or in the terminal: Ctrl-C. Same lifecycle.
- [ ] Power off the Muse (hold the button 3 s).
