// Phase 9 HUD client.
//
// Single-file vanilla JS, no build step. Connects to the WebSocket
// served by src/muse2_music_lab/server/app.py, receives one
// state.snapshot() JSON message per broadcast tick (~10-20 Hz), and:
//
//   1. Updates the DOM rows in the HUD panels (numeric readouts +
//      bars), same as in Phase 7.
//   2. Pushes the same state into window.visualizer.setTargets() so
//      the Three.js shader uniforms get new targets to glide toward.
//
// The visualizer is loaded as an ES module (visualizer.js) and
// exposes window.visualizer.{setTargets, refreshSeed}. We don't await
// it -- if the visualizer hasn't finished bootstrapping yet (shader
// compile + texture load), setTargets() is a no-op and we'll catch
// up on the next message.

(() => {
  "use strict";

  const WS_PATH = "/ws";
  const RECONNECT_BASE_MS = 500;
  const RECONNECT_MAX_MS = 5000;
  const STALE_MS = 1500; // treat as disconnected if no message in this window

  const els = {
    conn: document.getElementById("conn"),
    muse: document.getElementById("muse"),
    rate: document.getElementById("rate"),
    prompt: document.getElementById("prompt"),
    promptTransition: document.getElementById("prompt-transition"),
    hudToggle: document.getElementById("hud-toggle"),
    recalibrateBtn: document.getElementById("recalibrate-btn"),
    quitBtn: document.getElementById("quit-btn"),
    eegModeBtn: document.getElementById("eeg-mode-btn"),
    startPanel: document.getElementById("start-panel"),
    startPrompt: document.getElementById("start-prompt"),
    startBtn: document.getElementById("start-btn"),
    startStatus: document.getElementById("start-status"),
    endedOverlay: document.getElementById("ended-overlay"),
    audioOverlay: document.getElementById("audio-enable-overlay"),
    audioBtn: document.getElementById("audio-enable-btn"),
    warmingBanner: document.getElementById("warming-banner"),
    rows: new Map(),
  };
  document.querySelectorAll(".row[data-key]").forEach((row) => {
    els.rows.set(row.dataset.key, {
      root: row,
      value: row.querySelector(".value"),
      fill: row.querySelector(".fill"),
      fillCenter: row.querySelector(".fill-center"),
    });
  });

  // ---- HUD show/hide -------------------------------------------------
  // Default: HUD visible. Toggle with the header button or `h` key.
  // Persisted to localStorage so the choice survives page reloads
  // (handy when the orchestrator restarts mid-demo).
  const HUD_KEY = "muse2.hudHidden";
  function setHud(hidden) {
    document.body.classList.toggle("hud-hidden", hidden);
    els.hudToggle.textContent = hidden ? "show HUD" : "hide HUD";
    try { localStorage.setItem(HUD_KEY, hidden ? "1" : "0"); } catch (_e) {}
  }
  setHud(localStorage.getItem(HUD_KEY) === "1");
  els.hudToggle.addEventListener("click", () => {
    setHud(!document.body.classList.contains("hud-hidden"));
  });
  document.addEventListener("keydown", (e) => {
    // Ignore when the user is typing into something. We don't have
    // any inputs today, but cheap insurance against future ones.
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
    if (e.key === "h" || e.key === "H") {
      setHud(!document.body.classList.contains("hud-hidden"));
    }
  });

  // ---- rolling rate counter -----------------------------------------
  let lastFrameTs = 0;
  const rateBuf = [];
  function recordFrame() {
    const now = performance.now();
    if (lastFrameTs > 0) {
      rateBuf.push(now - lastFrameTs);
      if (rateBuf.length > 30) rateBuf.shift();
    }
    lastFrameTs = now;
  }
  function currentHz() {
    if (rateBuf.length === 0) return 0;
    const avg = rateBuf.reduce((a, b) => a + b, 0) / rateBuf.length;
    return avg > 0 ? 1000 / avg : 0;
  }

  function setConn(ok) {
    els.conn.textContent = ok ? "WS: connected" : "WS: disconnected";
    els.conn.className = ok ? "conn conn-good" : "conn conn-bad";
  }

  // ---- Phase 10: Muse-band status pill ------------------------------
  // Mirrors the WS pill's visual treatment but walks through more
  // states (idle/searching/found/connected/lost/reconnecting/simulated/
  // failed). Keep the mapping data-driven so adding a state on the
  // backend doesn't require a JS change beyond the table.
  const MUSE_STATES = {
    idle:         { label: "idle",         cls: "conn conn-idle" },
    searching:    { label: "searching",    cls: "conn conn-warn" },
    found:        { label: "found",        cls: "conn conn-warn" },
    connected:    { label: "connected",    cls: "conn conn-good" },
    lost:         { label: "lost",         cls: "conn conn-bad"  },
    reconnecting: { label: "reconnecting", cls: "conn conn-warn" },
    simulated:    { label: "simulated",    cls: "conn conn-warn" },
    failed:       { label: "failed",       cls: "conn conn-bad"  },
  };
  function setMuse(stateStr) {
    const m = MUSE_STATES[stateStr] || MUSE_STATES.idle;
    els.muse.textContent = `Muse: ${m.label}`;
    els.muse.className = m.cls;
    // Recalibrate is a no-op on simulated EEG and meaningless until
    // the band is at least found; gate the button accordingly.
    const canRecal =
      stateStr === "connected" ||
      stateStr === "found" ||
      stateStr === "reconnecting";
    els.recalibrateBtn.disabled = !canRecal;
  }
  setMuse("idle");

  // ---- EEG mode toggle (real <-> simulated) ------------------------
  // Reflects state.eeg_mode from the WS snapshot. Click to flip; we
  // optimistically lock the button until the next snapshot confirms
  // the supervisor actually picked up the swap, so a fat-fingered
  // double-click can't queue two swaps.
  let eegModeSwitchInFlight = false;
  let lastEegMode = null;
  function setEegMode(mode) {
    // Button text describes the ACTION the click would take, not the
    // current mode -- "Switch to real" makes it obvious what happens
    // next. The Muse pill conveys the current state.
    if (eegModeSwitchInFlight) {
      els.eegModeBtn.textContent = "Switching...";
      els.eegModeBtn.disabled = true;
      return;
    }
    if (mode === "simulated") {
      els.eegModeBtn.textContent = "EEG: simulated → use real";
    } else if (mode === "real") {
      els.eegModeBtn.textContent = "EEG: real → use simulated";
    } else {
      els.eegModeBtn.textContent = "EEG: ?";
    }
    els.eegModeBtn.disabled = false;
  }
  els.eegModeBtn.addEventListener("click", () => {
    if (eegModeSwitchInFlight) return;
    if (!lastEegMode) return;
    const target = lastEegMode === "real" ? "simulated" : "real";
    const prompt = target === "real"
      ? "Switch EEG to the live Muse 2 band? Calibration takes ~8s."
      : "Switch EEG to the synthetic generator?";
    if (!confirm(prompt)) return;
    if (!sendAction({ action: "set_eeg_mode", mode: target })) {
      return;  // not connected; sendAction already silently no-ops
    }
    eegModeSwitchInFlight = true;
    setEegMode(lastEegMode);  // re-render -> "Switching..."
  });

  // ---- Phase 10: outbound action helpers ---------------------------
  // Send a JSON action over the live WS, no-op if not connected.
  // Returns true if we actually attempted the send.
  function sendAction(payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify(payload));
      return true;
    } catch (_e) {
      return false;
    }
  }

  // Briefly show a status line under the textarea. Cleared on the
  // next user input or after a few seconds.
  let startStatusTimer = null;
  function setStartStatus(text, kind = "info") {
    if (startStatusTimer) { clearTimeout(startStatusTimer); startStatusTimer = null; }
    els.startStatus.textContent = text || "";
    els.startStatus.className = `start-status${kind ? " " + kind : ""}`;
    if (text) {
      startStatusTimer = setTimeout(() => {
        els.startStatus.textContent = "";
        els.startStatus.className = "start-status";
        startStatusTimer = null;
      }, 4000);
    }
  }

  function hideStartPanel() {
    if (els.startPanel.classList.contains("hidden")) return;
    els.startPanel.classList.add("hidden");
    // Returning focus to the body avoids the textarea staying
    // focus-stuck under the visualizer (where it would still receive
    // keystrokes if the user hits Enter again).
    if (document.activeElement && document.activeElement.blur) {
      document.activeElement.blur();
    }
  }

  // ---- mid-session prompt change (click-to-edit header prompt) ------
  // The header prompt span is clickable; click swaps it for a textarea,
  // Enter submits a `change_prompt` action that triggers a Lyria
  // weighted-prompt crossfade, Escape (or blur) cancels back to display
  // mode. We track our own editing flag because the WS state stream
  // tries to overwrite the prompt span on every snapshot -- if we
  // didn't gate it, the user's typing would get clobbered ~10 times
  // per second.
  const promptEdit = {
    editing: false,
    // Cached so we can restore the display text on cancel without
    // having to wait for the next snapshot to repopulate it.
    savedDisplayText: "",
    // The most recent values we've seen from the WS state stream.
    // The editor reads `target || seedPrompt || prompt` as its
    // prefill so clicking-to-edit during a transition lets you
    // redirect from where you're currently headed (latest-wins).
    livePrompt: "",
    liveSeedPrompt: "",
    liveTarget: "",
    lyriaReady: false,
    lyriaStarted: false,
  };

  function canEditPrompt() {
    return promptEdit.lyriaStarted && promptEdit.lyriaReady;
  }

  function refreshPromptClass() {
    const can = canEditPrompt();
    els.prompt.classList.toggle("prompt-editable-ready", can);
    els.prompt.classList.toggle("prompt-editable-locked", !can);
    els.prompt.title = can
      ? "Click to change the prompt mid-session. Music crossfades to the new prompt over ~16s. Enter to submit, Escape to cancel."
      : (promptEdit.lyriaStarted
          ? "Lyria is still warming up; the prompt becomes editable as soon as audio starts."
          : "Press Start first to begin a session, then you can change the prompt at any time.");
  }

  function exitPromptEdit({ commit }) {
    if (!promptEdit.editing) return;
    promptEdit.editing = false;
    els.prompt.classList.remove("prompt-editing");
    // Removing the textarea by overwriting innerHTML is safe because
    // we built the original content with textContent (no embedded
    // markup); applyState() will refresh the text on the next tick.
    els.prompt.textContent = promptEdit.savedDisplayText;
    if (!commit) {
      // Briefly flash the prompt span so the user sees the cancel
      // landed (otherwise an Escape feels like nothing happened).
      els.prompt.classList.add("prompt-edit-cancelled");
      setTimeout(() => els.prompt.classList.remove("prompt-edit-cancelled"), 250);
    }
  }

  function enterPromptEdit() {
    if (promptEdit.editing) return;
    if (!canEditPrompt()) {
      // Brief visual nudge so the user knows the click registered.
      els.prompt.classList.add("prompt-edit-locked-flash");
      setTimeout(() => els.prompt.classList.remove("prompt-edit-locked-flash"), 350);
      return;
    }
    promptEdit.editing = true;
    promptEdit.savedDisplayText = els.prompt.textContent;
    // Prefill priority: in-flight target > evolver-drifted seed >
    // user's original prompt. This matches "edit from where you're
    // currently headed" so a quick redirect during a transition
    // doesn't make the user re-type the in-flight target.
    const prefill = (
      promptEdit.liveTarget ||
      promptEdit.liveSeedPrompt ||
      promptEdit.livePrompt ||
      ""
    );
    // Build the textarea inline. One row + auto-resize via the
    // input handler keeps the header from jumping vertically when
    // the prompt is long.
    els.prompt.classList.add("prompt-editing");
    els.prompt.innerHTML = "";
    const ta = document.createElement("textarea");
    ta.className = "prompt-edit-input";
    ta.rows = 1;
    ta.value = prefill;
    ta.spellcheck = false;
    ta.autocomplete = "off";
    ta.placeholder = "type a new prompt, then press Enter";
    els.prompt.appendChild(ta);
    // Tiny "Enter ↵" hint badge so the affordance is discoverable.
    const hint = document.createElement("span");
    hint.className = "prompt-edit-hint";
    hint.textContent = "↵";
    els.prompt.appendChild(hint);
    ta.focus();
    ta.setSelectionRange(prefill.length, prefill.length);

    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submitPromptChange(ta.value);
      } else if (e.key === "Escape") {
        e.preventDefault();
        exitPromptEdit({ commit: false });
      }
    });
    ta.addEventListener("blur", () => {
      // Blur cancels (so clicking elsewhere abandons the edit). If the
      // submission already started, exitPromptEdit was called with
      // commit=true and this is a no-op.
      if (promptEdit.editing) exitPromptEdit({ commit: false });
    });
  }

  function submitPromptChange(rawText) {
    const text = (rawText || "").trim();
    if (!text) {
      exitPromptEdit({ commit: false });
      return;
    }
    if (text === promptEdit.livePrompt && !promptEdit.liveTarget) {
      // No-op submission; just close the editor.
      exitPromptEdit({ commit: true });
      return;
    }
    if (!sendAction({ action: "change_prompt", prompt: text })) {
      // Not connected; keep the editor open so the user can retry
      // when the WS comes back.
      return;
    }
    // Optimistic close: the next snapshot will start showing the
    // prompt-transition badge, which doubles as the success indicator.
    exitPromptEdit({ commit: true });
  }

  els.prompt.addEventListener("click", enterPromptEdit);
  els.prompt.addEventListener("keydown", (e) => {
    // Space / Enter on the focused span enters edit mode (matches
    // standard role=button keyboard semantics).
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      enterPromptEdit();
    }
  });
  refreshPromptClass();

  // ---- cloud-mode state -------------------------------------------
  // True once the server has told us we're talking to a --cloud
  // deployment (via state.snapshot().cloud_mode = true). When true:
  //   * Quit and EEG-mode toggle are hidden (single visitor must NOT
  //     be able to break the experience for everyone else)
  //   * audio.js owns playback (Lyria PCM streams over WS, plays via
  //     Web Audio in the browser instead of sounddevice on the host)
  //   * The audio-enable overlay is shown after the first audio_init
  //     message (deferred until the AudioContext is suspended, which
  //     it always is until a user gesture).
  let cloudMode = false;
  let audioInitSeen = false;
  function applyCloudMode(enabled) {
    if (enabled === cloudMode) return;
    cloudMode = enabled;
    document.body.classList.toggle("cloud-mode", enabled);
    if (enabled) {
      // Hide controls that don't make sense for shared deploys.
      els.quitBtn.hidden = true;
      els.eegModeBtn.hidden = true;
    }
  }

  // ---- audio-enable overlay (cloud-mode only) ----------------------
  // Shown after the WS sends audio_init AND the AudioContext is in the
  // "suspended" state (browser autoplay policy). One click resumes the
  // context and hides the overlay.
  function refreshAudioOverlay() {
    if (!audioInitSeen || !window.audio) {
      els.audioOverlay.hidden = true;
      return;
    }
    const status = window.audio.status();
    const needsClick = status.enabled && status.state !== "running";
    els.audioOverlay.hidden = !needsClick;
  }
  els.audioBtn.addEventListener("click", async () => {
    if (window.audio && window.audio.resume) {
      await window.audio.resume();
    }
    refreshAudioOverlay();
  });
  // Subscribe to audio.js state changes so the overlay hides as soon
  // as the context transitions to "running".
  if (window.audio && window.audio.onState) {
    window.audio.onState(() => refreshAudioOverlay());
  }

  // ---- Phase 10: session-ended state -------------------------------
  // Once the user clicks Quit (or the perform process exits some other
  // way), we don't want the WS to silently keep retrying every few
  // seconds against a dead server. Set this latch and the reconnect
  // scheduler short-circuits.
  let sessionEnded = false;
  function endSession(reason) {
    sessionEnded = true;
    setConn(false);
    setMuse("idle");
    els.recalibrateBtn.disabled = true;
    els.quitBtn.disabled = true;
    els.endedOverlay.hidden = false;
    if (reason) {
      console.log(`[muse2] session ended: ${reason}`);
    }
  }

  // Wire the header buttons.
  els.recalibrateBtn.addEventListener("click", () => {
    if (sendAction({ action: "recalibrate" })) {
      // Backend will print [eeg] recalibrate requested...
      els.recalibrateBtn.disabled = true;
      // Re-enable on the next state snapshot (the Muse pill recomputes
      // canRecal). 8s baseline window is the natural cooldown.
    }
  });

  els.quitBtn.addEventListener("click", () => {
    if (!confirm("Stop the perform process?")) return;
    sendAction({ action: "quit" });
    // Latch BEFORE the WS close fires so the close handler doesn't
    // schedule another reconnect attempt against the dying server.
    endSession("user clicked Quit");
  });

  // ---- Phase 10: Start panel wiring --------------------------------
  // Send button is disabled when the textarea is empty. We don't gate
  // on EEG-connected; the music + visuals can still run with neutral
  // EEG values if the band isn't on yet.
  function refreshStartButton() {
    const hasText = els.startPrompt.value.trim().length > 0;
    const wsOk = ws && ws.readyState === WebSocket.OPEN;
    els.startBtn.disabled = !(hasText && wsOk);
  }
  els.startPrompt.addEventListener("input", () => {
    setStartStatus("");
    refreshStartButton();
  });
  // Demo-tested prompt chips: click to fill the textarea (does NOT
  // auto-start). The operator can hit Enter / click Start once they
  // see the value populated, or edit it first.
  document.querySelectorAll(".prompt-chip[data-prompt]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const text = chip.dataset.prompt || "";
      els.startPrompt.value = text;
      els.startPrompt.focus();
      // Drop the cursor at the end so a quick edit at the tail of the
      // prompt is one keystroke away.
      els.startPrompt.setSelectionRange(text.length, text.length);
      setStartStatus("");
      refreshStartButton();
    });
  });
  // Enter submits, Shift+Enter inserts a newline. Mirrors ChatGPT.
  els.startPrompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitStart();
    }
  });
  els.startBtn.addEventListener("click", submitStart);

  function submitStart() {
    const prompt = els.startPrompt.value.trim();
    if (!prompt) {
      setStartStatus("Type a prompt first.", "error");
      return;
    }
    if (!sendAction({ action: "start", prompt })) {
      setStartStatus("Not connected to the server -- still trying...", "error");
      return;
    }
    setStartStatus("Starting...", "info");
    els.startBtn.disabled = true;
  }

  // Periodic UI refresh: rate display + stale-connection check.
  setInterval(() => {
    const hz = currentHz();
    els.rate.textContent = hz > 0 ? `${hz.toFixed(0)} Hz` : "— Hz";
    if (lastFrameTs > 0 && performance.now() - lastFrameTs > STALE_MS) {
      setConn(false);
    }
  }, 250);

  // ---- value -> DOM helpers ----
  function fmtFloat(v) { return Number.isFinite(v) ? v.toFixed(2) : "—"; }
  function clip01(v) {
    if (!Number.isFinite(v)) return 0;
    return Math.max(0, Math.min(1, v));
  }
  function pct(v) { return `${(clip01(v) * 100).toFixed(1)}%`; }

  function getNested(obj, path) {
    return path.split(".").reduce((acc, k) => (acc == null ? acc : acc[k]), obj);
  }

  // Track the last-seen seed_version so we can detect bumps from the
  // Phase 10 evolver. A bump means the server wrote a new
  // /static/seed.png; we cache-bust + cross-fade in the visualizer.
  let lastSeedVersion = null;

  function applyState(s) {
    if (!s) return;

    // Push to the visualizer FIRST so the render-thread smoothing has
    // the freshest target on the very next frame. Tolerant: visualizer
    // may not be initialized yet; the call no-ops in that case.
    if (window.visualizer && window.visualizer.setTargets) {
      window.visualizer.setTargets(s);
    }

    // Phase 10: seed evolver. When seed_version bumps, refresh the
    // visualizer texture (cross-fades automatically). Skip the very
    // first message we see so the initial WS-open refreshSeed already
    // queued by the open handler doesn't get duplicated.
    if (Number.isFinite(s.seed_version)) {
      if (lastSeedVersion !== null && s.seed_version > lastSeedVersion) {
        if (window.visualizer && window.visualizer.refreshSeed) {
          window.visualizer.refreshSeed();
        }
      }
      lastSeedVersion = s.seed_version;
    }

    // Stash the live prompt fields where the click-to-edit handler
    // can read them (it uses these as prefill priorities and as the
    // "did anything change?" gate on submit).
    if (typeof s.prompt === "string") promptEdit.livePrompt = s.prompt;
    if (typeof s.seed_prompt === "string") promptEdit.liveSeedPrompt = s.seed_prompt;
    if (typeof s.prompt_change_target === "string") promptEdit.liveTarget = s.prompt_change_target;
    const lyriaReadyPrev = promptEdit.lyriaReady;
    const lyriaStartedPrev = promptEdit.lyriaStarted;
    promptEdit.lyriaReady = !!s.lyria_ready;
    promptEdit.lyriaStarted = !!s.lyria_started;
    if (
      lyriaReadyPrev !== promptEdit.lyriaReady ||
      lyriaStartedPrev !== promptEdit.lyriaStarted
    ) {
      refreshPromptClass();
    }

    // Live-update the prompt readout: the original prompt shows once
    // (constant), but seed_prompt may evolve per cycle. Show whichever
    // is the most recent. Truncate so it doesn't push the buttons off.
    // Skipped while the user is mid-edit so we don't clobber their
    // typing on every snapshot.
    const displayPrompt = s.seed_prompt || s.prompt;
    if (
      !promptEdit.editing &&
      displayPrompt &&
      els.prompt.dataset.last !== displayPrompt
    ) {
      els.prompt.textContent = `prompt: ${displayPrompt}`;
      els.prompt.dataset.last = displayPrompt;
      // Refresh the editable-tooltip too (overwrites the simple "full
      // text on hover" tooltip from the previous DOM-text-only path).
      refreshPromptClass();
    }

    // Prompt-transition badge: visible only while a crossfade is in
    // flight. Format: "→ <new prompt> (37%)" so the audience sees both
    // where the music is heading AND how far along we are. Truncates
    // long targets via CSS (max-width + ellipsis).
    if (els.promptTransition) {
      const progress = Number.isFinite(s.prompt_transition_progress)
        ? s.prompt_transition_progress
        : 0;
      const target = (typeof s.prompt_change_target === "string"
        ? s.prompt_change_target
        : "");
      if (progress > 0 && target) {
        const pctLabel = `${Math.round(progress * 100)}%`;
        els.promptTransition.textContent = `→ ${target} (${pctLabel})`;
        els.promptTransition.title = `Crossfading to: "${target}" -- ${pctLabel} complete`;
        els.promptTransition.hidden = false;
      } else {
        els.promptTransition.hidden = true;
      }
    }

    // Phase 10: drive the Muse-band pill from the snapshot.
    if (typeof s.eeg_connection_state === "string") {
      setMuse(s.eeg_connection_state);
    }

    // EEG-mode toggle: clear the in-flight lock as soon as the
    // supervisor's mode flip lands in a snapshot, then re-render the
    // button label. Two distinct snapshots in the same mode = stable;
    // one snapshot in a different mode = swap completed.
    if (typeof s.eeg_mode === "string") {
      if (eegModeSwitchInFlight && s.eeg_mode !== lastEegMode) {
        eegModeSwitchInFlight = false;
      }
      lastEegMode = s.eeg_mode;
      setEegMode(s.eeg_mode);
    }

    // Cloud-deploy detection from the WS snapshot. Updates header
    // affordances + the document body class so cloud-only CSS rules
    // can apply (currently just the .cloud-mode { display: none } on
    // the Quit button via the .hidden attribute set in JS).
    if (typeof s.cloud_mode === "boolean") {
      applyCloudMode(s.cloud_mode);
    }

    // Phase 10: hide the Start panel once Lyria has been started --
    // either by this browser (we already disabled the button on submit)
    // or by another browser session connected to the same perform
    // process, or by main.py's --prompt auto-start path.
    if (s.lyria_started) {
      hideStartPanel();
    }

    // Warming-up banner: visible only between Start-clicked and the
    // first audio chunk landing. Lyria's lyria-realtime-exp model
    // sometimes takes 15-20s on a cold start (the supervisor's stall
    // watchdog auto-reconnects through the bad ones), so we tell the
    // audience the system is alive while they wait.
    if (els.warmingBanner) {
      const warming = !!s.lyria_started && !s.lyria_ready;
      els.warmingBanner.hidden = !warming;
    }

    for (const [key, refs] of els.rows) {
      const v = getNested(s, key);
      if (v === undefined || v === null) continue;

      if (key === "blink" || key === "jaw") {
        const fired = !!v;
        refs.root.classList.toggle("fired", fired);
        refs.value.textContent = fired ? "●" : "·";
        if (refs.fill) refs.fill.style.width = fired ? "100%" : "0%";
        continue;
      }

      if (key === "lyria.bpm") {
        refs.value.textContent = Number.isFinite(v) && v > 0 ? `${v}` : "—";
        const bpmNorm = clip01((v - 60) / 80);
        if (refs.fill) refs.fill.style.width = pct(bpmNorm);
        continue;
      }

      if (key === "lyria.chunks") {
        refs.value.textContent = Number.isFinite(v) ? `${v}` : "0";
        continue;
      }

      if (key === "lyria.temperature") {
        refs.value.textContent = fmtFloat(v);
        const tNorm = clip01((v - 0.6) / 1.2);
        if (refs.fill) refs.fill.style.width = pct(tNorm);
        continue;
      }

      if (key === "asymmetry") {
        refs.value.textContent = fmtFloat(v);
        const center = clip01(v);
        const half = 0.5;
        const width = Math.abs(center - half) / half;
        if (refs.fillCenter) {
          refs.fillCenter.style.width = pct(width);
          if (center >= 0.5) {
            refs.fillCenter.style.left = "50%";
            refs.fillCenter.style.right = "auto";
          } else {
            refs.fillCenter.style.right = "50%";
            refs.fillCenter.style.left = "auto";
          }
        }
        continue;
      }

      refs.value.textContent = fmtFloat(v);
      if (refs.fill) refs.fill.style.width = pct(v);
    }
  }

  // ---- WebSocket lifecycle ----
  let ws = null;
  let reconnectMs = RECONNECT_BASE_MS;

  function connect() {
    const url = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${WS_PATH}`;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      scheduleReconnect();
      return;
    }
    // Cloud mode: server pushes raw int16 PCM as binary frames. ArrayBuffer
    // gives us zero-copy access to the bytes; the default "blob" would
    // require an async .arrayBuffer() round-trip per chunk.
    ws.binaryType = "arraybuffer";

    ws.addEventListener("open", () => {
      setConn(true);
      reconnectMs = RECONNECT_BASE_MS;
      // A fresh server connect implies (possibly) a new perform
      // process and a fresh seed.png. Reset the version tracker so
      // the next WS message's seed_version=0 isn't seen as a "bump"
      // from a previous-session high version.
      lastSeedVersion = null;
      // Also re-fetch the texture explicitly -- the version-bump path
      // won't fire if the new perform hasn't evolved yet (version stays
      // 0 for the first 30s).
      if (window.visualizer && window.visualizer.refreshSeed) {
        window.visualizer.refreshSeed();
      }
    });

    ws.addEventListener("message", (ev) => {
      // Three message shapes share /ws:
      //   1. ArrayBuffer  -> raw PCM chunk for cloud-mode audio
      //   2. JSON ack      -> { ack:true, ok, error?, info? }
      //   3. JSON state    -> state.snapshot()  (the steady stream)
      //   4. JSON audio_init -> { type:"audio_init", sample_rate, ... }
      //                         (one-shot, only in cloud mode, on connect)
      if (ev.data instanceof ArrayBuffer) {
        if (window.audio && window.audio.pushChunk) {
          window.audio.pushChunk(ev.data);
        }
        return;
      }

      let msg;
      try { msg = JSON.parse(ev.data); } catch (_e) { return; }

      // audio_init: one-shot header that tells the browser to spin up
      // its AudioContext at the right sample rate before the first
      // binary frame arrives. Local-mode pages never see this.
      if (msg && msg.type === "audio_init") {
        if (window.audio && window.audio.setup) {
          window.audio.setup({
            sampleRate: msg.sample_rate,
            channels: msg.channels,
          });
        }
        audioInitSeen = true;
        refreshAudioOverlay();
        return;
      }

      // Phase 10: server emits two message shapes -- state snapshots
      // (no `ack` field) and action acks ({ack:true, ok, error?, info?}).
      // Acks are infrequent; route them off the rate counter so they
      // don't get counted toward the broadcast Hz readout.
      if (msg && msg.ack === true) {
        handleAck(msg);
        return;
      }
      recordFrame();
      applyState(msg);
    });

    ws.addEventListener("close", () => {
      setConn(false);
      // Phase 10: a Quit click latches sessionEnded; honor it instead
      // of spamming reconnect attempts at a server that just told us
      // it's going away.
      if (sessionEnded) {
        return;
      }
      scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      try { ws.close(); } catch (_e) {}
    });
  }

  function scheduleReconnect() {
    if (sessionEnded) return;
    setTimeout(connect, reconnectMs);
    reconnectMs = Math.min(reconnectMs * 2, RECONNECT_MAX_MS);
  }

  // Re-evaluate the Start button enabled-state whenever the WS state
  // changes (open / closed) so a stale "wait for connection" hint
  // clears as soon as we're actually ready to send.
  function handleAck(msg) {
    if (msg.ok) {
      // Server accepted the action. For Start, a successful ack is the
      // signal to clear the textarea and let the panel fade away once
      // lyria_started lands in the next state snapshot.
      if (msg.info) setStartStatus(msg.info, "info");
      else setStartStatus("");
    } else {
      // Server rejected. Re-enable the Start button so the user can
      // edit + retry; surface the reason inline.
      setStartStatus(msg.error || "request rejected", "error");
      refreshStartButton();
      // Also unstick any in-flight EEG-mode switch so the toggle
      // doesn't sit on "Switching..." forever after a rejected swap
      // (e.g., target == current mode -> server returns "already in
      // X mode").
      if (eegModeSwitchInFlight) {
        eegModeSwitchInFlight = false;
        if (lastEegMode) setEegMode(lastEegMode);
      }
    }
  }

  connect();

  // Periodically refresh the Start button gating -- catches the
  // (rare) race where the WS opens AFTER the textarea was already
  // populated. Cheap; runs only while the panel is visible.
  setInterval(() => {
    if (!els.startPanel.classList.contains("hidden")) {
      refreshStartButton();
    }
  }, 500);
})();
