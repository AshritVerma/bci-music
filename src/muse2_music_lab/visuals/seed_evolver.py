"""Phase 10: periodic seed-image evolution driven by EEG/audio drift.

Every `EVOLVE_INTERVAL_S` seconds the evolver:

  1. Snapshots the rolling window of (alpha, beta, theta, asymmetry,
     rms, centroid, onset) it has been collecting at EVOLVE_SAMPLE_HZ.
  2. Summarizes the window into a "drift descriptor" -- per-feature
     mean and trend (rising / stable / falling).
  3. Applies a hardcoded modifier vocabulary to turn the descriptor
     into a small list of natural-language modifiers ("softer",
     "more intricate", "warmer palette", etc.).
  4. Sends the modifiers + the previous prompt to Claude with one
     tight instruction: weave them into a new image prompt that
     keeps the original's identity but reflects the trajectory.
  5. Calls Imagen with the new prompt, atomically replaces
     static/seed.png, bumps state.seed_version + state.seed_prompt.
  6. The browser, on the next WS message, sees the version bump and
     calls visualizer.refreshSeed() -- the shader cross-fades from
     the old texture to the new one over EVOLVE_CROSSFADE_S.

Why the hybrid template+Claude path:

  * Templates alone get stale fast -- only ~5 distinct outputs per
    EEG axis means the prompt drifts predictably and the audience
    sees the pattern within a session.
  * Claude alone is too unconstrained -- it'll occasionally pivot the
    visual identity hard, which loses continuity.
  * Hybrid: template constrains direction (this performer's brain
    actually moved this way), Claude provides phrasing variety so
    you don't see the same words repeat.

Failure modes (all degrade gracefully, never crash perform):

  * No Anthropic key -> task logs once and exits cleanly. Imagen
    pipeline still runs the next-cycle path with a *templated only*
    prompt so the image still evolves.
  * Imagen quota / API error -> log once, skip this cycle, try again
    next interval. Old image stays on screen.
  * Claude error -> fall back to a minimal template-only prompt so
    we still call Imagen this cycle.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

from muse2_music_lab import config
from muse2_music_lab.state import AppState
from muse2_music_lab.visuals.seed_image import (
    SeedImageError,
    call_imagen,
    cache_path_for,
    load_api_key,
    write_seed_atomic,
)


# ---------------------------------------------------------------------------
# Rolling feature window
# ---------------------------------------------------------------------------


@dataclass
class _Sample:
    """One AppState snapshot for trend math. Time is monotonic seconds."""
    t: float
    alpha: float
    beta: float
    theta: float
    asymmetry: float
    rms: float
    centroid: float
    onset: float


@dataclass
class FeatureWindow:
    """Bounded rolling buffer of recent feature samples.

    Capped by wall time, not sample count, so it stays correct even if
    the producer's sampling rate changes. `add()` is O(1) amortized;
    `summarize()` walks the whole buffer (small, ~90 samples typical).
    """
    window_s: float = config.EVOLVE_WINDOW_S
    samples: list[_Sample] = field(default_factory=list)

    def add(self, state: AppState) -> None:
        now = time.monotonic()
        self.samples.append(_Sample(
            t=now,
            alpha=float(state.alpha),
            beta=float(state.beta),
            theta=float(state.theta),
            asymmetry=float(state.asymmetry),
            rms=float(state.rms),
            centroid=float(state.centroid),
            onset=float(state.onset),
        ))
        cutoff = now - self.window_s
        # Drop everything older than the window. Iterate from front since
        # samples are appended in time order.
        while self.samples and self.samples[0].t < cutoff:
            self.samples.pop(0)

    def is_warm(self, min_samples: int = 8, min_span_s: float = 10.0) -> bool:
        """At least N samples covering at least min_span_s seconds.

        Decoupled from window_s on purpose: window_s controls how far
        BACK we remember, but 10s is plenty to compute a useful trend
        (early-half vs late-half mean). Tying warmth to half the
        window meant short --evolve-interval values silently never
        fired their first cycle until window_s/2 had elapsed.
        """
        if len(self.samples) < min_samples:
            return False
        span = self.samples[-1].t - self.samples[0].t
        return span >= min_span_s


# ---------------------------------------------------------------------------
# Drift descriptor + modifier vocabulary
# ---------------------------------------------------------------------------


@dataclass
class FeatureDrift:
    """Mean + trend for one feature axis over the window."""
    mean: float           # average value 0..1
    delta: float          # late-half mean minus early-half mean (-1..+1)
    direction: str        # "rising" | "falling" | "stable"
    magnitude: float      # |delta|, used for picking strongest modifiers


@dataclass
class DriftDescriptor:
    """Per-axis drifts. Computed once per evolve cycle."""
    alpha: FeatureDrift
    beta: FeatureDrift
    theta: FeatureDrift
    asymmetry: FeatureDrift
    rms: FeatureDrift
    centroid: FeatureDrift
    onset: FeatureDrift


def _drift_for(values: list[float]) -> FeatureDrift:
    """Mean + halves-delta on a list. Same axis classification rules."""
    if not values:
        return FeatureDrift(0.5, 0.0, "stable", 0.0)
    mean = sum(values) / len(values)
    half = len(values) // 2
    if half == 0:
        return FeatureDrift(mean, 0.0, "stable", 0.0)
    early = sum(values[:half]) / half
    late = sum(values[half:]) / (len(values) - half)
    delta = late - early
    if abs(delta) < config.EVOLVE_NEUTRAL_THRESHOLD:
        direction = "stable"
    elif delta > 0:
        direction = "rising"
    else:
        direction = "falling"
    return FeatureDrift(mean=mean, delta=delta, direction=direction, magnitude=abs(delta))


def summarize_drift(window: FeatureWindow) -> DriftDescriptor:
    """Per-axis mean + slope over the window samples."""
    s = window.samples
    return DriftDescriptor(
        alpha=_drift_for([x.alpha for x in s]),
        beta=_drift_for([x.beta for x in s]),
        theta=_drift_for([x.theta for x in s]),
        asymmetry=_drift_for([x.asymmetry for x in s]),
        rms=_drift_for([x.rms for x in s]),
        centroid=_drift_for([x.centroid for x in s]),
        onset=_drift_for([x.onset for x in s]),
    )


# Modifier vocabulary. Each tuple is (axis, condition, list[phrases]).
# At each cycle we pick one phrase per matching condition (random for
# variety), then pass the top EVOLVE_MAX_MODIFIERS by drift magnitude
# to Claude. Vocabulary expansions are cheap; favor variety over precision.

_VOCAB: dict[str, dict[str, list[str]]] = {
    "alpha": {
        # Rising alpha = becoming more relaxed / eyes-closed.
        "rising": [
            "softer focus, dreamlike haze",
            "diffuse, glowing edges",
            "fading into mist",
            "blurred, contemplative",
        ],
        "falling": [
            "sharper, more defined edges",
            "crystalline detail emerging",
            "alert, focused composition",
        ],
        "high_mean": [
            "saturated with calm",
            "drifting, nebulous",
        ],
    },
    "beta": {
        # Rising beta = engaged / active focus.
        "rising": [
            "more intricate detail",
            "complex geometric subdivision",
            "dense fractal layering",
        ],
        "falling": [
            "simpler forms",
            "minimalist, more negative space",
            "cleaner silhouettes",
        ],
        "high_mean": [
            "kinetic energy",
            "alert, deliberate composition",
        ],
    },
    "theta": {
        # Rising theta = drowsy / dreamlike.
        "rising": [
            "surreal, impossible geometry",
            "Escher-like spatial folds",
            "dreamlike continuity breaks",
        ],
        "falling": [
            "grounded, naturalistic",
            "physically plausible composition",
        ],
        "high_mean": [
            "trance-like atmosphere",
            "shifting between states",
        ],
    },
    "asymmetry": {
        # >0.5 = warm/positive valence, <0.5 = cool/negative.
        "high_mean": [
            "warmer palette, amber and gold",
            "joyful, inviting tones",
            "sunlit atmosphere",
        ],
        "low_mean": [
            "cooler palette, deep blues and purples",
            "pensive, introspective light",
            "moonlit, melancholic",
        ],
    },
    "rms": {
        # Rising loudness = more energy / drama.
        "rising": [
            "more dynamic and expansive",
            "explosive energy building",
            "scale increasing",
        ],
        "falling": [
            "settling, more intimate",
            "quieting down",
        ],
        "high_mean": [
            "dramatic and bold",
        ],
    },
    "centroid": {
        # Rising spectral brightness = brighter / sharper sound.
        "rising": [
            "high contrast, shimmering highlights",
            "luminous, brighter accents",
        ],
        "falling": [
            "muted highlights, rich shadows",
            "warmer, smokier",
        ],
        "high_mean": [
            "iridescent, glittering surfaces",
        ],
        "low_mean": [
            "deep, shadowed tones",
        ],
    },
    "onset": {
        # Punctuated transients = rhythmic energy.
        "rising": [
            "punctuated by sharp accents",
            "rhythmic, percussive composition",
        ],
        "high_mean": [
            "staccato, syncopated visual rhythm",
        ],
    },
}


def _pick_modifiers(drift: DriftDescriptor) -> list[tuple[str, float]]:
    """Generate (modifier_text, magnitude) tuples in priority order.

    Magnitude controls picking when there are more candidates than
    EVOLVE_MAX_MODIFIERS. We compute one candidate per axis per
    matched condition, then sort by drift magnitude descending and
    return the top N.
    """
    candidates: list[tuple[str, float]] = []

    def _add(axis: str, cond: str, mag: float) -> None:
        bank = _VOCAB.get(axis, {}).get(cond) or []
        if not bank:
            return
        candidates.append((random.choice(bank), mag))

    axes = [
        ("alpha", drift.alpha),
        ("beta", drift.beta),
        ("theta", drift.theta),
        ("rms", drift.rms),
        ("centroid", drift.centroid),
        ("onset", drift.onset),
    ]
    for name, d in axes:
        if d.direction == "rising":
            _add(name, "rising", d.magnitude)
        elif d.direction == "falling":
            _add(name, "falling", d.magnitude)
        # high_mean phrasing only fires for already-high values, on top
        # of any direction phrase. Magnitude here is mean-distance from
        # neutral so a deeply settled state still contributes.
        if d.mean > 0.65:
            _add(name, "high_mean", d.mean - 0.5)
        elif d.mean < 0.35:
            _add(name, "low_mean", 0.5 - d.mean)

    # Asymmetry uses mean only (no rising/falling -- valence is a
    # state, not a trend). Either side can fire, never both.
    if drift.asymmetry.mean > 0.55:
        _add("asymmetry", "high_mean", drift.asymmetry.mean - 0.5)
    elif drift.asymmetry.mean < 0.45:
        _add("asymmetry", "low_mean", 0.5 - drift.asymmetry.mean)

    # Strongest first, dedup near-identical phrases (rare but possible
    # when the random.choice happens to repeat).
    candidates.sort(key=lambda x: x[1], reverse=True)
    seen: set[str] = set()
    unique: list[tuple[str, float]] = []
    for phrase, mag in candidates:
        key = phrase.lower().split(",")[0].strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append((phrase, mag))
    return unique[: config.EVOLVE_MAX_MODIFIERS]


# ---------------------------------------------------------------------------
# Prompt mutation
# ---------------------------------------------------------------------------


_CLAUDE_SYSTEM = (
    "You are an art director for a real-time, brain-controlled image generator. "
    "Each cycle you receive the previous image prompt and a list of stylistic "
    "directions inferred from how the performer's brain and the music have "
    "evolved. Your job is to write the NEXT image prompt: a single sentence "
    "(15-35 words) that keeps the core subject and visual identity recognizable "
    "but evolves it in the directions given. Do not reset the concept. Avoid "
    "artist names, copyrighted characters, or anything filterable. Output "
    "ONLY the new prompt as one sentence, no preamble, no quotes, no explanation."
)


def _template_only_prompt(original: str, modifiers: list[str]) -> str:
    """Fallback prompt construction if Claude is unavailable.

    Just appends the modifiers as a comma-separated tail. Less natural
    than Claude's output but still drives Imagen toward the new
    direction.
    """
    if not modifiers:
        return original
    tail = ", ".join(modifiers)
    return f"{original}, {tail}"


async def _polish_with_claude(
    client,
    *,
    previous_prompt: str,
    modifiers: list[str],
) -> Optional[str]:
    """Single Claude call to weave modifiers into a new prompt.

    Returns the new prompt, or None on any error (caller falls back to
    the template-only prompt). Logged but never raises.
    """
    if not modifiers:
        return None
    user_msg = (
        f"Previous prompt:\n  {previous_prompt}\n\n"
        f"Stylistic directions for this cycle:\n  - "
        + "\n  - ".join(modifiers)
        + "\n\nWrite the new image prompt now."
    )
    try:
        resp = await client.messages.create(
            model=config.EVOLVE_CLAUDE_MODEL,
            max_tokens=config.EVOLVE_CLAUDE_MAX_TOKENS,
            system=_CLAUDE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        print(f"[evolve] Claude polish failed ({type(e).__name__}): {e}", flush=True)
        return None

    # Extract first text block. Anthropic responses are a list of
    # content blocks; we asked for plain text so block[0].text is it.
    for block in resp.content:
        text = getattr(block, "text", None)
        if text:
            return text.strip().strip('"').strip("'").strip()
    return None


# ---------------------------------------------------------------------------
# Main task
# ---------------------------------------------------------------------------


async def run_seed_evolver_loop(
    state: AppState,
    *,
    interval_chunks: int = config.EVOLVE_INTERVAL_CHUNKS,
) -> None:
    """Regenerate static/seed.png every `interval_chunks` Lyria chunks.

    Locking the cadence to chunks (each ~2s of music) instead of wall
    time keeps the visual transitions feeling tied to the music --
    every N chunks the brain has been listening to, the visual evolves
    in response. Each cycle:

      1. Sample AppState into the rolling window while waiting for
         `interval_chunks` more chunks to land. Previous image stays
         on screen, fully cross-faded in, throughout this phase.
      2. Summarize drift -> modifiers -> Claude polish -> Imagen.
      3. Atomic seed.png write + version bump (browser cross-fades).
    """
    if interval_chunks <= 0:
        print("[evolve] disabled (chunks=0)", flush=True)
        return

    # Phase 10: don't start drifting before the user has even chosen a
    # prompt. Once start_requested fires, state.seed_prompt has been
    # populated by either the server (browser Start) or main.py
    # (--prompt CLI shortcut), so the evolver has a base to drift from.
    await state.start_requested.wait()

    # API key check up front so we don't wait 24s only to discover the
    # key is missing. Imagen key is mandatory; Claude key is optional
    # (we'll fall back to template-only mode if absent).
    try:
        gemini_key = load_api_key()
    except SeedImageError as e:
        print(f"[evolve] disabled: {e}", flush=True)
        return

    load_dotenv()
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    claude_client = None
    approx_seconds = interval_chunks * 2  # one chunk ≈ 2s, see audio/fft.py
    if anthropic_key:
        try:
            import anthropic
            claude_client = anthropic.AsyncAnthropic(api_key=anthropic_key)
            print(
                f"[evolve] enabled. every {interval_chunks} chunks "
                f"(~{approx_seconds}s of music)  "
                f"window={config.EVOLVE_WINDOW_S:.0f}s  "
                f"polish=claude ({config.EVOLVE_CLAUDE_MODEL})",
                flush=True,
            )
        except Exception as e:
            print(f"[evolve] anthropic SDK init failed: {e}", flush=True)
            claude_client = None
    if claude_client is None:
        print(
            f"[evolve] enabled. every {interval_chunks} chunks "
            f"(~{approx_seconds}s of music)  "
            f"window={config.EVOLVE_WINDOW_S:.0f}s  "
            f"polish=template-only (no ANTHROPIC_API_KEY)",
            flush=True,
        )

    window = FeatureWindow(window_s=config.EVOLVE_WINDOW_S)
    sample_period = 1.0 / max(0.1, config.EVOLVE_SAMPLE_HZ)

    # Wait for EEG to be producing real values before we start sampling.
    # Otherwise the first window is mostly zeros and the first evolved
    # prompt has nothing useful to chew on.
    await state.eeg_ready.wait()

    # Anchor the first cycle at "first chunk we observe + N chunks".
    # We don't wait for state.lyria_ready because (a) the evolver wants
    # to start sampling EEG immediately after start_requested for a
    # better window, and (b) the chunk-delta below naturally gates on
    # actual chunk arrival regardless of when we set the anchor.
    cycle = 0
    last_evolve_at_chunks = state.lyria_chunks
    loop = asyncio.get_running_loop()

    # Diagnostic: warn the operator once if Lyria isn't producing
    # chunks within ~60s of evolver start. Most often means --no-lyria
    # was passed (orchestrator shouldn't have spawned us in that case)
    # or Lyria's WebSocket failed and is in a reconnect loop.
    started_at = time.monotonic()
    chunks_warning_emitted = False

    try:
        while True:
            chunks_delta = state.lyria_chunks - last_evolve_at_chunks
            if chunks_delta >= interval_chunks and window.is_warm():
                cycle += 1
                last_evolve_at_chunks = state.lyria_chunks
                await _do_one_cycle(
                    state=state,
                    window=window,
                    cycle=cycle,
                    gemini_key=gemini_key,
                    claude_client=claude_client,
                    loop=loop,
                )
                # Reset the window so the *next* descriptor reflects
                # only post-regen brain activity, not the lead-up.
                window.samples.clear()
            else:
                window.add(state)
                if (
                    not chunks_warning_emitted
                    and state.lyria_chunks == 0
                    and time.monotonic() - started_at > 60.0
                ):
                    print(
                        "[evolve] no Lyria audio chunks have arrived in "
                        "the first 60s -- evolver will idle until the "
                        "Lyria stream produces audio",
                        flush=True,
                    )
                    chunks_warning_emitted = True
                await asyncio.sleep(sample_period)
    except asyncio.CancelledError:
        print(f"[evolve] cancelled (after {cycle} cycle(s))", flush=True)
        raise


async def _do_one_cycle(
    *,
    state: AppState,
    window: FeatureWindow,
    cycle: int,
    gemini_key: str,
    claude_client,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """One full mutate -> Imagen -> swap-on-disk cycle."""
    drift = summarize_drift(window)
    picks = _pick_modifiers(drift)
    modifiers = [p[0] for p in picks]

    print(
        f"[evolve] cycle {cycle}: alpha={drift.alpha.mean:.2f}({drift.alpha.direction}) "
        f"beta={drift.beta.mean:.2f}({drift.beta.direction}) "
        f"theta={drift.theta.mean:.2f}({drift.theta.direction}) "
        f"asym={drift.asymmetry.mean:.2f}",
        flush=True,
    )
    if modifiers:
        for m in modifiers:
            print(f"[evolve]   modifier: {m}", flush=True)
    else:
        print("[evolve]   no significant drift this cycle (image will hold)", flush=True)
        return

    # Build the new prompt: Claude polish if available, else template-only.
    previous = state.seed_prompt or state.prompt
    new_prompt: Optional[str] = None
    if claude_client is not None:
        new_prompt = await _polish_with_claude(
            claude_client,
            previous_prompt=previous,
            modifiers=modifiers,
        )
    if not new_prompt:
        new_prompt = _template_only_prompt(previous, modifiers)
    new_prompt = new_prompt.strip()
    print(f"[evolve]   new prompt: {new_prompt!r}", flush=True)

    # Imagen call. Cache hit possible (rare) when Claude happens to
    # produce an identical phrase as a previous cycle; saves the call.
    cached = cache_path_for(new_prompt)
    try:
        if cached.is_file():
            # Just copy the cached PNG into the canonical output path.
            image_bytes = cached.read_bytes()
            print(f"[evolve]   cache HIT ({cached.name})", flush=True)
        else:
            t0 = time.monotonic()
            # Imagen call is blocking HTTP; offload to executor so we
            # don't stall the event loop (TUI, WS, audio playback).
            image_bytes = await loop.run_in_executor(
                None, call_imagen, gemini_key, new_prompt
            )
            elapsed = time.monotonic() - t0
            print(f"[evolve]   imagen ok in {elapsed:.1f}s ({len(image_bytes)/1024:.0f} KB)", flush=True)
    except SeedImageError as e:
        print(f"[evolve]   imagen failed, holding previous image: {e}", flush=True)
        return

    # Atomic write + cache. Bump state.seed_version LAST so any
    # observer (the WS broadcaster) sees the version bump only after
    # the file on disk is fully written.
    write_seed_atomic(image_bytes, new_prompt, use_cache=True)
    state.seed_prompt = new_prompt
    state.seed_version += 1
    print(f"[evolve]   seed_version -> {state.seed_version}", flush=True)
