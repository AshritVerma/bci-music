// Cloud-mode audio playback: jitter-buffered Web Audio sink for the
// raw int16 PCM frames the server sends as binary WebSocket messages.
//
// Protocol (matches src/muse2_music_lab/server/audio_broadcast.py):
//
//   Server -> client (one-shot, on connect):
//     TEXT  {"type":"audio_init","sample_rate":48000,"channels":2,"format":"s16le"}
//
//   Server -> client (continuous):
//     BINARY  raw little-endian int16 PCM bytes (interleaved)
//
// Browsers refuse to start an AudioContext without a user gesture
// (autoplay policy), so init() leaves the AudioContext suspended.
// app.js wires the page's "click to enable sound" overlay to call
// resume(), which resume()s the context and starts playing whatever
// has accumulated in the jitter buffer since.
//
// Local-mode pages (sounddevice on the host) load this script too but
// audio.js does nothing: setup() never runs because the server never
// sends an audio_init in non-cloud mode.

(function () {
  // Internal state. Encapsulated in this IIFE so the global namespace
  // only sees `window.audio = { ... }` at the bottom.
  const state = {
    ctx: null,           // AudioContext
    sampleRate: 0,       // from audio_init message
    channels: 0,         // from audio_init message
    nextStartTime: 0,    // scheduling cursor (ctx.currentTime-based)
    bufferedAhead: 0,    // seconds of audio scheduled but not yet played
    enabled: false,      // true once setup() has been called
    listeners: new Set(),// resume-state change listeners (for the overlay)
  };

  // How much audio to keep buffered in front of `currentTime` before we
  // start scheduling chunks back-to-back. Without this, network jitter
  // would translate directly into audible gaps. Higher = smoother but
  // adds latency on top of Lyria's inherent ~2s.
  const TARGET_AHEAD_S = 0.30;
  // If we drift too far ahead (e.g. a packet burst), drop the new
  // chunk -- the user would otherwise be listening to several seconds
  // of stale audio every time the WS catches up.
  const MAX_AHEAD_S = 1.20;

  function notifyState() {
    for (const cb of state.listeners) {
      try {
        cb({
          enabled: state.enabled,
          state: state.ctx ? state.ctx.state : "no-context",
          sampleRate: state.sampleRate,
          bufferedAhead: state.bufferedAhead,
        });
      } catch (e) {
        console.warn("[audio] listener threw:", e);
      }
    }
  }

  function onStateChange() {
    notifyState();
  }

  // Called once when the server sends the JSON audio_init message.
  // Builds the AudioContext at the right sample rate (48kHz from
  // Lyria) so the browser doesn't have to resample every chunk.
  function setup({ sampleRate, channels }) {
    if (state.enabled) return;
    state.sampleRate = sampleRate;
    state.channels = channels;
    try {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) {
        console.warn("[audio] no AudioContext support");
        return;
      }
      state.ctx = new Ctor({ sampleRate, latencyHint: "playback" });
      state.ctx.addEventListener("statechange", onStateChange);
      state.enabled = true;
      state.nextStartTime = state.ctx.currentTime + TARGET_AHEAD_S;
      console.log(
        `[audio] AudioContext ready (sr=${sampleRate} ch=${channels} state=${state.ctx.state})`
      );
      notifyState();
    } catch (e) {
      console.error("[audio] failed to create AudioContext:", e);
    }
  }

  // User gesture handler -- call from a click / tap / keypress.
  // Browsers transition the AudioContext from "suspended" to "running"
  // only inside such a callstack.
  async function resume() {
    if (!state.ctx) return false;
    if (state.ctx.state === "running") return true;
    try {
      await state.ctx.resume();
      console.log(`[audio] context resumed (state=${state.ctx.state})`);
      // Re-anchor the scheduling cursor so we don't try to schedule
      // into the past after a long pause.
      state.nextStartTime = state.ctx.currentTime + TARGET_AHEAD_S;
      notifyState();
      return state.ctx.state === "running";
    } catch (e) {
      console.warn("[audio] resume() failed:", e);
      return false;
    }
  }

  // Decode a binary WS frame (ArrayBuffer of interleaved s16) into a
  // pair of Float32Array channel buffers, schedule into the AudioContext.
  function pushChunk(arrayBuffer) {
    if (!state.enabled || !state.ctx) return;
    if (state.ctx.state !== "running") {
      // Context still suspended -- the user hasn't clicked Enable yet.
      // Drop the chunk; once they click resume(), playback starts from
      // "now", not from the entire backlog.
      return;
    }

    const ch = state.channels || 2;
    const view = new Int16Array(arrayBuffer);
    const totalSamples = view.length;
    if (totalSamples === 0) return;
    const framesPerChannel = totalSamples / ch;
    if (!Number.isInteger(framesPerChannel)) {
      console.warn(`[audio] chunk length ${totalSamples} not divisible by ch=${ch}`);
      return;
    }

    // De-interleave int16 -> float32 channel buffers, normalizing into
    // [-1, 1]. Could SIMD this with a WASM helper if it ever shows up
    // in profiles; at 48k stereo, the per-chunk cost is ~negligible.
    const buffer = state.ctx.createBuffer(ch, framesPerChannel, state.sampleRate);
    for (let c = 0; c < ch; c++) {
      const channelData = buffer.getChannelData(c);
      let i = c;
      for (let f = 0; f < framesPerChannel; f++, i += ch) {
        channelData[f] = view[i] / 32768.0;
      }
    }

    // Drop chunks if we're already too far ahead -- the alternative
    // is the listener hearing a steadily-increasing latency.
    state.bufferedAhead = Math.max(0, state.nextStartTime - state.ctx.currentTime);
    if (state.bufferedAhead > MAX_AHEAD_S) {
      console.warn(
        `[audio] dropping chunk (bufferedAhead=${state.bufferedAhead.toFixed(2)}s > ${MAX_AHEAD_S}s)`
      );
      // Hard reset the cursor so we recover instead of dropping forever.
      state.nextStartTime = state.ctx.currentTime + TARGET_AHEAD_S;
      return;
    }

    // If we've fallen behind (bufferedAhead = 0), nudge the cursor up
    // to currentTime + a small lead so the next .start() doesn't try
    // to schedule in the past.
    const earliestStart = state.ctx.currentTime + 0.005;
    if (state.nextStartTime < earliestStart) {
      state.nextStartTime = earliestStart + TARGET_AHEAD_S;
    }

    const src = state.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(state.ctx.destination);
    src.start(state.nextStartTime);
    state.nextStartTime += buffer.duration;
    state.bufferedAhead = state.nextStartTime - state.ctx.currentTime;
  }

  // Subscribe to context-state changes (overlay uses this).
  function onState(cb) {
    state.listeners.add(cb);
    return () => state.listeners.delete(cb);
  }

  function status() {
    return {
      enabled: state.enabled,
      state: state.ctx ? state.ctx.state : "no-context",
      sampleRate: state.sampleRate,
      channels: state.channels,
      bufferedAhead: state.bufferedAhead,
    };
  }

  window.audio = { setup, resume, pushChunk, onState, status };
})();
