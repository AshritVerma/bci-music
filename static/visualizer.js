// Phase 9: brain/audio-driven Three.js + GLSL visualizer.
//
// Architecture:
//   - One full-screen quad rendered with a custom fragment shader.
//   - The Phase 8 seed image (`/static/seed.png`) is loaded as a
//     sampler2D. The shader warps, blurs, and color-shifts it based
//     on six uniforms.
//   - app.js calls `visualizer.setTargets(state)` whenever a WS
//     message arrives (~10-20 Hz). Uniforms are SMOOTHED on the
//     render thread (60-120 fps) toward those targets so the visual
//     glides between brain ticks instead of stair-stepping.
//
// Uniform mapping (each is 0..1 from the EEG/audio pipeline):
//   uAlpha     relaxation   -> blur radius + softness
//   uBeta      focus        -> contrast + edge sharpness boost
//   uTheta     dreaminess   -> swirl warp amount + rotation speed
//   uRMS       loudness     -> radial zoom-pulse + brightness
//   uCentroid  spectral hue -> color-temperature shift (cool<->warm)
//   uOnset     transients   -> brief chromatic-aberration kick
//
// Bonus uniforms (not part of the six but available + used):
//   uAsymmetry valence (0..1, 0.5=neutral) -> color tilt L/R
//   uBlink     short pulse (1 frame on trigger) -> screen flash
//   uJaw       short pulse                       -> radial shockwave
//
// Why ES-module Three from CDN:
//   No build step. Browsers fetch from `unpkg.com` once and cache.
//   Pinned to a known version so behavior is reproducible.
//
// Fallback path: if `/static/seed.png` 404s (e.g. --skip-seed with no
// previous seed), we synthesize a procedural texture so the visualizer
// still has *something* to warp. The user can tell the difference --
// the fallback is a low-frequency gradient, not Imagen output.

import * as THREE from "https://unpkg.com/three@0.164.1/build/three.module.js";

// ---- shaders ----------------------------------------------------------

const VERT = /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = vec4(position, 1.0);
  }
`;

// Fragment shader: a single pass over the seed texture with multiple
// effects layered on top of each other. Designed to look interesting
// at the default neutral state (~0.5 across the board) and to react
// noticeably but not jarringly as values move.
//
// Performance: ~9 texture taps per pixel at full blur (alpha=1).
// On an M4 Max at 2560x1440 that's ~33M taps/frame, trivially under
// the GPU's bandwidth even at 120 Hz ProMotion.
const FRAG = /* glsl */ `
  precision highp float;

  varying vec2 vUv;

  // Phase 10: dual textures with cross-fade. uSeedA is the current
  // image, uSeedB is the incoming one (set when the evolver writes a
  // new seed.png). uSeedMix glides 0->1 over the crossfade duration,
  // then we swap so A becomes the new B and uSeedMix resets to 0.
  uniform sampler2D uSeedA;
  uniform sampler2D uSeedB;
  uniform float uSeedMix;       // 0 = fully A, 1 = fully B
  uniform float uTime;
  uniform vec2  uResolution;
  uniform float uAspect;       // image aspect (width/height) for letterbox correction
  uniform float uAspectB;      // aspect of the incoming texture (may differ from A briefly)

  uniform float uAlpha;        // 0..1
  uniform float uBeta;         // 0..1
  uniform float uTheta;        // 0..1
  uniform float uRMS;          // 0..1
  uniform float uCentroid;     // 0..1
  uniform float uOnset;        // 0..1
  uniform float uAsymmetry;    // 0..1 (0.5 = neutral)
  uniform float uBlink;        // 0..1 envelope (decays each frame in JS)
  uniform float uJaw;          // 0..1 envelope (decays each frame in JS)

  // ---- helpers ----

  // Cover-fit: scale the texture to fully cover the screen regardless
  // of aspect mismatch. Same algorithm as CSS object-fit:cover. Per-
  // texture so A and B can have different aspects briefly without
  // either getting stretched.
  vec2 coverUv(vec2 uv, float aspect) {
    float screenAspect = uResolution.x / uResolution.y;
    vec2 scale = vec2(1.0);
    if (screenAspect > aspect) {
      // screen wider than image -> shrink Y so width fills
      scale.y = aspect / screenAspect;
    } else {
      scale.x = screenAspect / aspect;
    }
    return (uv - 0.5) * scale + 0.5;
  }

  // 5-tap diagonal blur. Cheap, looks soft enough for the alpha effect.
  vec3 softBlur(sampler2D tex, vec2 uv, float radius) {
    vec3 sum = vec3(0.0);
    float r = radius / 512.0;
    sum += texture2D(tex, uv).rgb;
    sum += texture2D(tex, uv + vec2( r,  r)).rgb;
    sum += texture2D(tex, uv + vec2(-r,  r)).rgb;
    sum += texture2D(tex, uv + vec2( r, -r)).rgb;
    sum += texture2D(tex, uv + vec2(-r, -r)).rgb;
    sum += texture2D(tex, uv + vec2( r,  0)).rgb * 0.5;
    sum += texture2D(tex, uv + vec2(-r,  0)).rgb * 0.5;
    sum += texture2D(tex, uv + vec2( 0,  r)).rgb * 0.5;
    sum += texture2D(tex, uv + vec2( 0, -r)).rgb * 0.5;
    return sum / 7.0;
  }

  // Sample the cross-faded seed at a SHARED UV coordinate. Both
  // textures are independently cover-fitted to the screen aspect, so
  // the mix lerps in screen-space, not texture-space. This is what
  // keeps the cross-fade looking like a dissolve rather than a
  // squashed warp during the transition.
  vec3 sampleSeed(vec2 uv, float radius) {
    vec3 a = softBlur(uSeedA, coverUv(uv, uAspect),  radius);
    if (uSeedMix <= 0.0) return a;
    vec3 b = softBlur(uSeedB, coverUv(uv, uAspectB), radius);
    return mix(a, b, uSeedMix);
  }

  // Cheap RGB hue rotation by 'shift' radians (approximation, fine
  // for visual modulation -- not color-accurate).
  // (Note: do NOT use backticks in shader comments -- they close
  // the surrounding JS template literal and break the whole file.)
  vec3 hueShift(vec3 col, float shift) {
    const vec3 k = vec3(0.57735, 0.57735, 0.57735);
    float c = cos(shift);
    return col * c + cross(k, col) * sin(shift) + k * dot(k, col) * (1.0 - c);
  }

  void main() {
    // Work in normalized [0,1] screen-UV space. Per-texture cover-fit
    // happens inside sampleSeed() so A and B can have different aspect
    // ratios during a cross-fade without distortion.
    vec2 uv = vUv;
    vec2 toCenter = uv - 0.5;
    float dist = length(toCenter);
    float angle = atan(toCenter.y, toCenter.x);

    // ---- THETA: swirl warp + slow rotation ----
    // Rotation speed scales with theta. Distant pixels rotate more
    // (rate ~ dist) so the image "drips" outward when theta is high.
    float swirl = uTheta * (0.6 + 1.2 * sin(uTime * 0.25));
    angle += swirl * dist * 1.5 + uTime * 0.05 * uTheta;

    // ---- RMS: pulse zoom (in/out with loudness) ----
    // Subtle by default; uRMS=1.0 gives ~10% breathing.
    float zoom = 1.0 - 0.10 * uRMS;
    float radius = dist * zoom;

    // ---- JAW: brief radial shockwave (envelope-driven, decays in JS) ----
    radius += uJaw * 0.04 * sin(dist * 30.0 - uTime * 6.0);

    vec2 warpedUv = 0.5 + vec2(cos(angle), sin(angle)) * radius;

    // ---- ALPHA: blur amount (relaxation = soft) ----
    float blurR = uAlpha * 5.0 + 0.01;  // 0.01 so the +0 case still uses the kernel-cost-neutral path

    // ---- ONSET: chromatic aberration along radial direction ----
    vec2 abDir = normalize(toCenter + 1e-6) * uOnset * 0.012;
    vec3 colR = sampleSeed(warpedUv + abDir, blurR);
    vec3 colG = sampleSeed(warpedUv,         blurR);
    vec3 colB = sampleSeed(warpedUv - abDir, blurR);
    vec3 col = vec3(colR.r, colG.g, colB.b);

    // ---- BETA: contrast / mid-emphasis ----
    // Pulls colors away from grey when high; flat looks when low.
    float contrast = 0.7 + uBeta * 0.8;
    col = (col - 0.5) * contrast + 0.5;

    // ---- CENTROID: hue shift, with asymmetry as a modulator ----
    // Centroid 0.5 = no shift. <0.5 cools (toward blue), >0.5 warms (toward orange).
    float hue = (uCentroid - 0.5) * 2.0 + (uAsymmetry - 0.5) * 0.6;
    col = hueShift(col, hue);

    // ---- RMS: brightness (subtle baseline + loud lift) ----
    col *= 0.55 + uRMS * 0.7;

    // ---- BLINK: full-frame white flash (envelope) ----
    col += vec3(uBlink * 0.45);

    // ---- vignette: subtle, keeps focus center-screen ----
    float vig = 1.0 - smoothstep(0.55, 1.05, dist);
    col *= mix(0.7, 1.0, vig);

    gl_FragColor = vec4(col, 1.0);
  }
`;

// ---- module-scope visualizer state -----------------------------------

const state = {
  initialized: false,
  renderer: null,
  scene: null,
  camera: null,
  mesh: null,
  uniforms: null,
  // Targets that app.js writes to. The render loop interpolates the
  // live uniform values toward these each frame.
  targets: {
    alpha: 0.5, beta: 0.5, theta: 0.5,
    rms: 0.0, centroid: 0.5, onset: 0.0,
    asymmetry: 0.5,
  },
  // Trigger envelopes: blink/jaw arrive as instantaneous booleans;
  // we re-trigger an envelope each time, then decay it on the render
  // thread for a visible-but-not-strobing flash.
  blinkEnv: 0.0,
  jawEnv: 0.0,
  // Cross-fade state for Phase 10 evolver. crossfadeStart=0 means no
  // cross-fade in progress (uSeedMix stays at 0 -> only uSeedA shown).
  // crossfadeDur is set on each refreshSeed() so it's tunable from
  // outside without rebuilding the visualizer. Default mirrors
  // config.EVOLVE_CROSSFADE_S (6s) -- about 25% of one 12-chunk cycle
  // is spent fading, the remaining 75% the new image stays settled.
  crossfadeStart: 0,
  crossfadeDur: 6.0,
  refreshing: false,
  startTime: performance.now(),
  lastFrameTime: performance.now(),
};

// Smoothing factor per second. 8.0 == ~125ms time-constant.
// Higher = snappier, lower = smoother. 8 feels right for ~10 Hz WS feed
// rendered at 60-120 Hz.
const SMOOTH_PER_SEC = 8.0;

// Trigger envelope decay rate (per second). 4.0 == ~250ms half-life.
const ENV_DECAY_PER_SEC = 4.0;

// ---- texture loading -------------------------------------------------

function loadSeedTexture() {
  // Cache-bust on every load so a fresh perform's new seed isn't
  // shadowed by the previous session's image. This runs once per
  // page load; subsequent calls to refreshSeed() create a new texture.
  const url = `/static/seed.png?ts=${Date.now()}`;
  const loader = new THREE.TextureLoader();
  return new Promise((resolve) => {
    loader.load(
      url,
      (tex) => {
        if (THREE.SRGBColorSpace !== undefined) tex.colorSpace = THREE.SRGBColorSpace;
        tex.minFilter = THREE.LinearFilter;
        tex.magFilter = THREE.LinearFilter;
        tex.wrapS = THREE.ClampToEdgeWrapping;
        tex.wrapT = THREE.ClampToEdgeWrapping;
        // Aspect ratio is read from the source image so the cover-fit
        // in the shader keeps Imagen's 16:9 framing on any window size.
        // Defensive: tex.image may be HTMLImageElement (naturalWidth) or
        // ImageBitmap (width) depending on the Three.js version / browser.
        // Falling back to 16:9 keeps uAspect finite -- a NaN here would
        // make every texture sample land at NaN UVs and the canvas would
        // render pure black.
        const w = tex.image && (tex.image.naturalWidth || tex.image.width);
        const h = tex.image && (tex.image.naturalHeight || tex.image.height);
        const aspect = (w && h) ? (w / h) : (16 / 9);
        resolve({ tex, aspect, fallback: false });
      },
      undefined,
      (err) => {
        // 404 / network error -> generate a procedural fallback so
        // the page still shows something. Subtle radial gradient so
        // the visualizer's effects (blur, swirl, hue shift) still
        // produce visible motion.
        console.warn("[viz] seed texture failed to load, using fallback:", err);
        const tex = makeFallbackTexture();
        resolve({ tex, aspect: 16 / 9, fallback: true });
      }
    );
  });
}

function makeFallbackTexture() {
  // 256x256 procedural gradient. Not pretty, but enough for shaders
  // to chew on so the user can tell the visualizer is running.
  const size = 256;
  const data = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const dx = x / size - 0.5;
      const dy = y / size - 0.5;
      const d = Math.sqrt(dx * dx + dy * dy);
      const v = Math.max(0, 1.0 - d * 1.6);
      data[i + 0] = Math.floor(40 + v * 80);
      data[i + 1] = Math.floor(20 + v * 60);
      data[i + 2] = Math.floor(80 + v * 140);
      data[i + 3] = 255;
    }
  }
  const tex = new THREE.DataTexture(data, size, size, THREE.RGBAFormat);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.needsUpdate = true;
  return tex;
}

// ---- bootstrap -------------------------------------------------------

function showVisibleError(msg) {
  // Render an error banner over the canvas so the user can see what
  // broke without opening DevTools. Black canvas + invisible failure
  // is the worst experience; this turns it into "obvious red banner".
  let div = document.getElementById("viz-error");
  if (!div) {
    div = document.createElement("div");
    div.id = "viz-error";
    Object.assign(div.style, {
      position: "fixed",
      top: "60px",
      left: "16px",
      right: "16px",
      zIndex: "10",
      padding: "10px 14px",
      background: "rgba(120, 20, 20, 0.92)",
      color: "#fff",
      font: "12px ui-monospace, Menlo, Consolas, monospace",
      border: "1px solid rgba(255, 100, 100, 0.5)",
      borderRadius: "6px",
      whiteSpace: "pre-wrap",
      maxHeight: "40vh",
      overflow: "auto",
    });
    document.body.appendChild(div);
  }
  div.textContent = "[viz] " + msg;
}

async function init(canvas) {
  if (state.initialized) return;
  state.initialized = true;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: false,    // unnecessary for a textured quad; saves fill rate
      alpha: false,
      powerPreference: "high-performance",
    });
  } catch (e) {
    showVisibleError("WebGLRenderer construction failed: " + (e && e.message ? e.message : e));
    console.error("[viz] WebGLRenderer failed:", e);
    return;
  }
  // Cap pixel ratio at 2 -- on a Retina display 3x is wasteful for a
  // shader that's ultimately just sampling a 16:9 image.
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(window.innerWidth, window.innerHeight, false);
  if (THREE.SRGBColorSpace !== undefined) renderer.outputColorSpace = THREE.SRGBColorSpace;
  // Black clear color so missing pixels don't draw attention. (During
  // Phase 9 bring-up we used magenta to distinguish "renderer never
  // ran" from "shader output is black"; not needed in production.)
  renderer.setClearColor(0x000000, 1.0);

  // Orthographic camera that exactly covers the [-1,1] NDC range; the
  // vertex shader uses position directly so the camera could be any
  // setup, but the orthographic + plane geometry is the standard idiom.
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  const scene = new THREE.Scene();

  const { tex, aspect } = await loadSeedTexture();

  const uniforms = {
    // Dual-texture cross-fade for the seed evolver. uSeedA holds the
    // currently-displayed image; uSeedB is loaded on demand and faded
    // in via uSeedMix (0->1 over EVOLVE_CROSSFADE_S). On crossfade
    // completion, A := B, B := blank, mix := 0.
    uSeedA:      { value: tex },
    uSeedB:      { value: tex },         // start equal so the first frame is well-defined
    uSeedMix:    { value: 0.0 },
    uAspect:     { value: aspect },
    uAspectB:    { value: aspect },
    uTime:       { value: 0 },
    uResolution: { value: new THREE.Vector2(window.innerWidth, window.innerHeight) },
    uAlpha:      { value: 0.5 },
    uBeta:       { value: 0.5 },
    uTheta:      { value: 0.5 },
    uRMS:        { value: 0.0 },
    uCentroid:   { value: 0.5 },
    uOnset:      { value: 0.0 },
    uAsymmetry:  { value: 0.5 },
    uBlink:      { value: 0.0 },
    uJaw:        { value: 0.0 },
  };

  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader: VERT,
    fragmentShader: FRAG,
    depthTest: false,
    depthWrite: false,
  });
  const geometry = new THREE.PlaneGeometry(2, 2);
  const mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  state.renderer = renderer;
  state.scene = scene;
  state.camera = camera;
  state.mesh = mesh;
  state.uniforms = uniforms;

  // Force a single render to surface any shader compile errors NOW
  // rather than at first rAF. Three.js logs them to console.
  try {
    renderer.render(scene, camera);
  } catch (e) {
    showVisibleError("First render() threw: " + (e && e.message ? e.message : e));
    console.error("[viz] first render failed:", e);
  }

  window.addEventListener("resize", onResize);
  onResize();
  requestAnimationFrame(renderLoop);
}

function onResize() {
  if (!state.renderer) return;
  state.renderer.setSize(window.innerWidth, window.innerHeight, false);
  state.uniforms.uResolution.value.set(window.innerWidth, window.innerHeight);
}

// ---- render loop -----------------------------------------------------

function renderLoop(now) {
  requestAnimationFrame(renderLoop);
  if (!state.uniforms) return;

  const dt = Math.min(0.1, (now - state.lastFrameTime) / 1000);
  state.lastFrameTime = now;
  state.uniforms.uTime.value = (now - state.startTime) / 1000;

  // Exponential approach toward each target: a low-pass filter that's
  // frame-rate independent. `k` is the fraction of remaining error to
  // close this frame (1 - exp(-rate * dt)).
  const k = 1 - Math.exp(-SMOOTH_PER_SEC * dt);
  const u = state.uniforms;
  const t = state.targets;
  u.uAlpha.value     += (t.alpha     - u.uAlpha.value)     * k;
  u.uBeta.value      += (t.beta      - u.uBeta.value)      * k;
  u.uTheta.value     += (t.theta     - u.uTheta.value)     * k;
  u.uRMS.value       += (t.rms       - u.uRMS.value)       * k;
  u.uCentroid.value  += (t.centroid  - u.uCentroid.value)  * k;
  u.uOnset.value     += (t.onset     - u.uOnset.value)     * k;
  u.uAsymmetry.value += (t.asymmetry - u.uAsymmetry.value) * k;

  // Trigger envelopes decay each frame regardless of whether a new
  // trigger arrived. setTargets() pumps them to 1.0 on a fresh trigger.
  const decay = Math.exp(-ENV_DECAY_PER_SEC * dt);
  state.blinkEnv *= decay;
  state.jawEnv   *= decay;
  u.uBlink.value = state.blinkEnv;
  u.uJaw.value   = state.jawEnv;

  // Cross-fade animation. crossfadeStart > 0 means a fade is in
  // progress; tween uSeedMix from 0 -> 1 over crossfadeDur, then
  // promote B to A and reset.
  if (state.crossfadeStart > 0) {
    const elapsed = (now - state.crossfadeStart) / 1000;
    const t = Math.min(1.0, elapsed / state.crossfadeDur);
    // smoothstep for a softer in/out feel than linear lerp.
    u.uSeedMix.value = t * t * (3 - 2 * t);
    if (t >= 1.0) {
      // Promote: A = B (both pointers, the GL texture object is shared
      // until the next refreshSeed assigns a new B). uSeedMix back to 0
      // so the shader's `if (uSeedMix <= 0.0) return a;` early-out kicks
      // in and we save half the texture taps until the next evolve.
      u.uSeedA.value = u.uSeedB.value;
      u.uAspect.value = u.uAspectB.value;
      u.uSeedMix.value = 0;
      state.crossfadeStart = 0;
    }
  }

  state.renderer.render(state.scene, state.camera);
}

// ---- public API ------------------------------------------------------

/**
 * Update the smoothing targets from a state.snapshot() WS message.
 * Tolerant of missing fields -- a partial update leaves other targets
 * at their last value.
 */
function setTargets(s) {
  if (!s || !state.targets) return;
  const t = state.targets;
  if (Number.isFinite(s.alpha))     t.alpha     = clamp01(s.alpha);
  if (Number.isFinite(s.beta))      t.beta      = clamp01(s.beta);
  if (Number.isFinite(s.theta))     t.theta     = clamp01(s.theta);
  if (Number.isFinite(s.rms))       t.rms       = clamp01(s.rms);
  if (Number.isFinite(s.centroid))  t.centroid  = clamp01(s.centroid);
  if (Number.isFinite(s.onset))     t.onset     = clamp01(s.onset);
  if (Number.isFinite(s.asymmetry)) t.asymmetry = clamp01(s.asymmetry);

  // Trigger envelopes: pump to 1.0 on the leading edge.
  if (s.blink) state.blinkEnv = 1.0;
  if (s.jaw)   state.jawEnv   = 1.0;
}

/**
 * Re-fetch /static/seed.png and cross-fade to it.
 *
 * Called by app.js (a) on a fresh WS open (so a new perform's new seed
 * wins immediately) and (b) whenever `seed_version` bumps in the WS
 * stream (the Phase 10 evolver wrote a new image).
 *
 * Cross-fade duration is taken from `crossfadeS` if provided, otherwise
 * from state.crossfadeDur (default 4s). Concurrent refresh requests
 * are coalesced -- if a fade is already in flight, the new one queues
 * up after it completes.
 */
async function refreshSeed(crossfadeS) {
  if (!state.uniforms) return;
  if (state.refreshing) return;        // coalesce overlapping calls
  state.refreshing = true;
  try {
    const { tex, aspect } = await loadSeedTexture();

    // First-ever refresh: nothing to fade FROM, just install as A.
    // Detect by checking if uSeedA still points at the initial
    // identical-A-and-B placeholder (covered below) OR if there's
    // simply no fade duration requested.
    const u = state.uniforms;
    if (u.uSeedA.value === u.uSeedB.value && u.uSeedMix.value === 0) {
      // No prior content -- swap both A and B to the new texture so
      // there's no flash. (uSeedMix stays 0; only A is sampled.)
      const old = u.uSeedA.value;
      u.uSeedA.value = tex;
      u.uSeedB.value = tex;
      u.uAspect.value = aspect;
      u.uAspectB.value = aspect;
      if (old && old !== tex) old.dispose();
      return;
    }

    // Real cross-fade: stash the new texture as B and animate uSeedMix
    // 0 -> 1 over crossfadeDur. The renderLoop swap promotes B to A.
    const oldB = u.uSeedB.value;
    u.uSeedB.value = tex;
    u.uAspectB.value = aspect;
    if (oldB && oldB !== u.uSeedA.value && oldB !== tex) oldB.dispose();

    if (typeof crossfadeS === "number" && crossfadeS > 0) {
      state.crossfadeDur = crossfadeS;
    }
    state.crossfadeStart = performance.now();
  } finally {
    state.refreshing = false;
  }
}

function clamp01(v) {
  if (v < 0) return 0;
  if (v > 1) return 1;
  return v;
}

// Expose on window so app.js (which is a classic script tag, not an
// ES module) can call into us. Could also re-emit as events, but a
// direct method on window.visualizer is the smaller change.
window.visualizer = { init, setTargets, refreshSeed };

// Auto-init once the DOM has the canvas.
function autoBoot() {
  const canvas = document.getElementById("viz-canvas");
  if (!canvas) {
    showVisibleError("no <canvas id=\"viz-canvas\"> found in DOM");
    return;
  }
  init(canvas).catch((e) => {
    showVisibleError("init() rejected: " + (e && e.message ? e.message : e));
    console.error("[viz] init rejected:", e);
  });
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", autoBoot);
} else {
  autoBoot();
}
