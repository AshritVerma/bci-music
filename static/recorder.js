// In-browser AV recorder: combines the visualizer canvas with Lyria's
// PCM audio stream into a single downloadable WebM file.
//
// Why in-browser, not server-side:
//   * The server already has the audio (raw PCM into state.audio_queue
//     / state.audio_broadcast_queue). It does NOT have the rendered
//     visualizer -- that's a Three.js shader + texture pipeline that
//     only runs in the browser. To capture both into one file we
//     either ship every canvas frame back to the server (high CPU,
//     awkward sync) or do it all in the browser (canvas.captureStream
//     + MediaStreamDestination + MediaRecorder, native browser plumbing).
//   * The "demo souvenir" use case only needs ONE recording at a time
//     from ONE browser, so the in-browser path's lack of fan-out is
//     not a limitation here.
//
// Pipeline (when the user clicks Record):
//
//      Lyria PCM (binary WS frames, 48 kHz s16 stereo)
//                 |
//                 v
//      [ Int16 -> Float32 buffer ]   <-- pushChunk()
//                 |
//                 v
//      AudioContext      MediaStreamDestination
//                 \           /
//                  \         /
//                   v       v
//                  AudioBufferSourceNode (one per chunk)
//                            |
//                            v
//                  audio MediaStreamTrack ----+
//                                              \
//      #viz-canvas.captureStream(60)            \
//          |                                     v
//          +---------> video MediaStreamTrack -> MediaStream
//                                                    |
//                                                    v
//                                            MediaRecorder
//                                                    |
//                                                    v
//                                        Blob (video/webm; vp9 + opus)
//
// Stop -> blob held in memory; Save -> anchor download.
//
// Local-mode pages: audio.js refuses to play audio (sounddevice on
// the host owns the speakers). recorder.js still gets the same WS
// binary frames via app.js's broadcast routing. The recorder's
// AudioContext is connected ONLY to MediaStreamDestination, NEVER to
// ctx.destination, so capturing audio in local mode does NOT cause
// double-playback.
//
// Cloud-mode pages: audio.js also runs and plays through speakers via
// its OWN AudioContext (separate from this one). recorder.js again
// connects only to MediaStreamDestination -- decoding the same PCM
// twice (once for playback, once for capture) costs negligible CPU at
// 48 kHz stereo, and keeps the two pipelines fully decoupled.

(function () {
  "use strict";

  // Internal state. Encapsulated in this IIFE so the global namespace
  // only sees `window.recorder = { ... }` at the bottom.
  const state = {
    // From audio_init -- format of incoming PCM frames. Stashed
    // eagerly so the user can click Record at any time without the
    // recorder needing a round-trip to ask the server for the format.
    sampleRate: 48000,
    channels: 2,
    formatKnown: false,
    // Built lazily on first .start() (the click on Record IS the
    // user gesture the AudioContext needs for autoplay policy).
    ctx: null,
    streamDest: null,         // MediaStreamAudioDestinationNode
    nextStartTime: 0,         // scheduling cursor (ctx.currentTime-based)
    canvasStream: null,       // from canvas.captureStream(fps)
    combinedStream: null,     // canvas video + recorder audio
    mediaRecorder: null,
    chunks: [],               // MediaRecorder ondataavailable buffer
    blob: null,               // final blob (set on stop)
    blobMime: "",             // final blob's mime type (for the download)
    isRecording: false,
    startedAtMs: 0,           // wall clock for the duration display
    elapsedMs: 0,             // updated on stop so save UI knows length
    // UI listeners (the header buttons subscribe to redraw on state change).
    listeners: new Set(),
  };

  // How far ahead of currentTime to schedule the next chunk so the
  // MediaStreamDestination doesn't get audible gaps when the WS
  // delivers chunks in slightly bursty cadence. Smaller than audio.js's
  // playback buffer because the recorder is going INTO a file -- a
  // brief schedule-in-the-past just yields a sub-millisecond gap that
  // the encoder smooths over.
  const RECORD_AHEAD_S = 0.05;
  // If we somehow drift seconds ahead (huge WS burst on session start),
  // re-anchor the cursor so we don't accumulate stale audio.
  const RECORD_MAX_AHEAD_S = 0.50;

  // Filename prefix for the saved file. Easy to grep in Downloads.
  const FILENAME_PREFIX = "brAIn-music";

  // --------- subscriber/notification plumbing ---------------------

  function notify() {
    const snap = status();
    for (const cb of state.listeners) {
      try { cb(snap); } catch (e) { console.warn("[recorder] listener threw:", e); }
    }
  }

  function onChange(cb) {
    state.listeners.add(cb);
    return () => state.listeners.delete(cb);
  }

  function status() {
    return {
      isRecording: state.isRecording,
      hasBlob: !!state.blob,
      blobBytes: state.blob ? state.blob.size : 0,
      elapsedMs: state.isRecording
        ? performance.now() - state.startedAtMs
        : state.elapsedMs,
      formatKnown: state.formatKnown,
    };
  }

  // --------- audio_init wiring (called by app.js) -----------------

  // Called when the server's audio_init JSON arrives. Just stashes the
  // format; we don't build the AudioContext until .start() because
  // browsers refuse to start one without a user gesture (autoplay
  // policy). Re-receiving the same audio_init is fine -- last-write-wins.
  function setup({ sampleRate, channels }) {
    if (typeof sampleRate === "number" && sampleRate > 0) state.sampleRate = sampleRate;
    if (typeof channels === "number" && channels > 0) state.channels = channels;
    state.formatKnown = true;
    notify();
  }

  // Called for every binary WS frame app.js receives. Decodes the
  // int16 PCM into a Float32 AudioBuffer and schedules it on the
  // recorder's AudioContext (via the MediaStreamDestination, never
  // through ctx.destination -- recorder must be silent locally).
  // No-op when not currently recording.
  function pushChunk(arrayBuffer) {
    if (!state.isRecording || !state.ctx || !state.streamDest) return;
    const ch = state.channels || 2;
    const view = new Int16Array(arrayBuffer);
    const totalSamples = view.length;
    if (totalSamples === 0) return;
    const framesPerChannel = totalSamples / ch;
    if (!Number.isInteger(framesPerChannel)) {
      console.warn(`[recorder] chunk length ${totalSamples} not divisible by ch=${ch}`);
      return;
    }

    const buffer = state.ctx.createBuffer(ch, framesPerChannel, state.sampleRate);
    for (let c = 0; c < ch; c++) {
      const channelData = buffer.getChannelData(c);
      let i = c;
      for (let f = 0; f < framesPerChannel; f++, i += ch) {
        channelData[f] = view[i] / 32768.0;
      }
    }

    // Schedule into the recorder context's timeline. Diverges from
    // audio.js's playback scheduler (which targets human-perceptible
    // latency); here we just need the encoder to see continuous audio.
    const ahead = state.nextStartTime - state.ctx.currentTime;
    if (ahead > RECORD_MAX_AHEAD_S) {
      // Hard reset on huge bursts -- the file would otherwise contain
      // seconds of stale buffered audio after a network blip.
      state.nextStartTime = state.ctx.currentTime + RECORD_AHEAD_S;
    } else {
      const earliest = state.ctx.currentTime + 0.005;
      if (state.nextStartTime < earliest) {
        state.nextStartTime = earliest + RECORD_AHEAD_S;
      }
    }

    const src = state.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(state.streamDest);
    src.start(state.nextStartTime);
    state.nextStartTime += buffer.duration;
  }

  // --------- start / stop / save ---------------------------------

  // Pick the best mimeType the browser supports so we don't end up
  // with a zero-byte blob from an unsupported codec request. Order
  // matters: VP9+Opus is the highest-quality web-supported combo,
  // VP8+Opus is the universal fallback.
  function pickMimeType() {
    const candidates = [
      "video/webm;codecs=vp9,opus",
      "video/webm;codecs=vp8,opus",
      "video/webm;codecs=vp9",
      "video/webm;codecs=vp8",
      "video/webm",
    ];
    if (typeof MediaRecorder === "undefined") return "";
    for (const m of candidates) {
      try {
        if (MediaRecorder.isTypeSupported(m)) return m;
      } catch (_e) { /* keep going */ }
    }
    return "";
  }

  // Find the visualizer canvas. Resolved at start() time (not at
  // module load) so the canvas exists by the time we look (the
  // visualizer ES module is async-loaded).
  function getCanvas() {
    return document.getElementById("viz-canvas");
  }

  // Start a new recording. Returns true on success, false on a soft
  // failure (no canvas, no MediaRecorder support). Does NOT throw on
  // typical errors -- the UI updates from notify() and the caller can
  // re-enable the button.
  async function start() {
    if (state.isRecording) return true;
    if (typeof MediaRecorder === "undefined") {
      console.error("[recorder] MediaRecorder is not supported in this browser");
      alert("Sorry -- your browser doesn't support MediaRecorder. Try a recent Chrome.");
      return false;
    }
    const canvas = getCanvas();
    if (!canvas) {
      console.error("[recorder] could not find #viz-canvas");
      return false;
    }
    if (!state.formatKnown) {
      // Defensive: the audio_init is sent on connect, so by the time
      // the user clicks Record (which gates on lyria_started anyway)
      // we should always have it.
      console.warn("[recorder] starting without audio_init -- using defaults (48000/2)");
    }

    try {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) {
        console.error("[recorder] no AudioContext support");
        return false;
      }

      // AudioContext at the Lyria sample rate so chunks don't have to
      // be resampled. latencyHint:"playback" is fine here -- we don't
      // care about input latency, only continuous output to the encoder.
      state.ctx = new Ctor({ sampleRate: state.sampleRate, latencyHint: "playback" });
      state.streamDest = state.ctx.createMediaStreamDestination();
      state.nextStartTime = state.ctx.currentTime + RECORD_AHEAD_S;

      // Canvas frames at 60 fps to match requestAnimationFrame on
      // typical displays. captureStream is a live view -- the same
      // canvas keeps rendering its visualizer animation, the recorder
      // just sees every flushed frame.
      state.canvasStream = canvas.captureStream(60);
      const videoTracks = state.canvasStream.getVideoTracks();
      const audioTracks = state.streamDest.stream.getAudioTracks();
      if (videoTracks.length === 0) {
        console.error("[recorder] canvas.captureStream returned no video tracks");
        return false;
      }
      state.combinedStream = new MediaStream([...videoTracks, ...audioTracks]);

      const mimeType = pickMimeType();
      const opts = mimeType ? { mimeType } : {};
      state.mediaRecorder = new MediaRecorder(state.combinedStream, opts);
      state.chunks = [];
      state.blob = null;
      state.blobMime = mimeType || "video/webm";

      state.mediaRecorder.addEventListener("dataavailable", (ev) => {
        if (ev.data && ev.data.size > 0) state.chunks.push(ev.data);
      });
      state.mediaRecorder.addEventListener("error", (ev) => {
        console.error("[recorder] MediaRecorder error:", ev);
      });
      state.mediaRecorder.addEventListener("stop", () => {
        // Assemble the final blob; clear the running flag.
        try {
          state.blob = new Blob(state.chunks, { type: state.blobMime });
        } catch (e) {
          console.error("[recorder] failed to assemble blob:", e);
          state.blob = null;
        }
        state.elapsedMs = performance.now() - state.startedAtMs;
        state.isRecording = false;
        // Tear down the AudioContext to release CPU; we'll build a
        // fresh one on the next .start(). The MediaRecorder + streams
        // are also done, drop refs so GC reclaims them.
        try { state.canvasStream.getTracks().forEach((t) => t.stop()); } catch (_e) {}
        try { state.ctx.close(); } catch (_e) {}
        state.ctx = null;
        state.streamDest = null;
        state.canvasStream = null;
        state.combinedStream = null;
        state.mediaRecorder = null;
        console.log(
          `[recorder] stopped. chunks=${state.chunks.length} bytes=${state.blob ? state.blob.size : 0} mime=${state.blobMime}`
        );
        notify();
      });

      // timeslice = 1000ms -> a dataavailable event every second so
      // the chunks list grows incrementally instead of waiting for
      // .stop() to flush one giant payload. Helps memory + means a
      // crash mid-recording loses at most ~1s.
      state.mediaRecorder.start(1000);
      state.isRecording = true;
      state.startedAtMs = performance.now();
      state.elapsedMs = 0;
      state.blob = null;
      console.log(
        `[recorder] started. mime=${state.blobMime} sr=${state.sampleRate} ch=${state.channels}`
      );
      notify();
      return true;
    } catch (e) {
      console.error("[recorder] start failed:", e);
      // Best-effort cleanup so the state isn't left half-alive.
      try { state.canvasStream && state.canvasStream.getTracks().forEach((t) => t.stop()); } catch (_e) {}
      try { state.ctx && state.ctx.close(); } catch (_e) {}
      state.ctx = null;
      state.streamDest = null;
      state.canvasStream = null;
      state.combinedStream = null;
      state.mediaRecorder = null;
      state.isRecording = false;
      notify();
      return false;
    }
  }

  // Request the MediaRecorder to flush. The actual blob assembly
  // happens in the "stop" event handler above (which then calls
  // notify() so the UI updates).
  function stop() {
    if (!state.isRecording || !state.mediaRecorder) return false;
    try {
      state.mediaRecorder.requestData();   // flush whatever's pending
      state.mediaRecorder.stop();
      return true;
    } catch (e) {
      console.error("[recorder] stop failed:", e);
      // Force the state-consistent path.
      state.isRecording = false;
      notify();
      return false;
    }
  }

  // Trigger a browser download for the most recent recording's blob.
  // Returns true if the download was initiated, false if there's no
  // blob to save (caller can show a tooltip).
  function save() {
    if (!state.blob) return false;
    const stamp = formatTimestamp(new Date());
    const ext = (state.blobMime.includes("webm")) ? "webm" : "mp4";
    const filename = `${FILENAME_PREFIX}_${stamp}.${ext}`;
    const url = URL.createObjectURL(state.blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Defer the revoke a bit so Safari's download flow has time to
    // resolve the blob URL before it's freed.
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    console.log(`[recorder] save() -> ${filename} (${state.blob.size} bytes)`);
    return true;
  }

  // Drop the in-memory blob. Used after Save (so the user can record
  // again from a clean slate) or after a Discard click (future).
  function clear() {
    state.blob = null;
    state.elapsedMs = 0;
    notify();
  }

  // Pad an integer to two digits. Local helper for the timestamp.
  function pad2(n) { return n < 10 ? "0" + n : "" + n; }

  // Format a Date as 'YYYY-MM-DD_HH-mm-ss' in the user's local TZ.
  // No colons (Windows / macOS Finder both choke on those in
  // filenames historically) and no spaces (shell-friendly).
  function formatTimestamp(d) {
    return (
      d.getFullYear() +
      "-" + pad2(d.getMonth() + 1) +
      "-" + pad2(d.getDate()) +
      "_" + pad2(d.getHours()) +
      "-" + pad2(d.getMinutes()) +
      "-" + pad2(d.getSeconds())
    );
  }

  window.recorder = {
    setup,
    pushChunk,
    start,
    stop,
    save,
    clear,
    onChange,
    status,
  };
})();
