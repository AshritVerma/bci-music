"""Pure-function mapping: AppState (EEG features) -> Lyria config.

Kept I/O-free so the mapping can be reasoned about (and unit-tested) in
isolation from the WebSocket / asyncio plumbing. Anyone debugging "why
did Lyria sound like that" can run this on a snapshot of AppState in a
REPL and see exactly what got pushed.

Mapping (PROJECT_PLAN.md, section 3.6):

  alpha       -> brightness   eyes-closed relaxation -> brighter timbre
                              (perceptual lift in treble/presence)
  beta        -> density      active focus -> denser arrangement
                              (more concurrent layers / hits / rhythm)
  theta       -> temperature  drowsy/meditative -> wilder, more random
                              (bigger excursions inside the prompt's space)
  asymmetry   -> bpm          right-frontal alpha (>0.5) -> faster bpm
                              left-frontal alpha (<0.5)  -> slower bpm

Why these specific axes:
  - brightness/density both move continuously and Lyria responds within
    ~2s, which is the right cadence for slow EEG modulation.
  - temperature controlled by theta gives the demo a "trance state pushes
    the model out of the safe basin" feel, which is what the audience
    expects to hear from a brain-driven music box.
  - bpm is intentionally driven by asymmetry (not a band power) because
    asymmetry is the one feature centered at 0.5 with symmetric variance,
    so the bpm idles around the middle of LYRIA_BPM_MIN/MAX instead of
    drifting to one end.

Each AppState input is assumed already clamped to [0, 1] by the EEG
pipeline; we re-clamp defensively so a bug upstream can't push Lyria
into out-of-range territory and silently fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from muse2_music_lab import config


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation; t is clamped to [0, 1]."""
    t = max(0.0, min(1.0, float(t)))
    return a + (b - a) * t


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _expand(x: float, gain: float) -> float:
    """Symmetrically amplify deviation from the 0.5 neutral midpoint.

    Maps x in [0, 1] to `0.5 + (x - 0.5) * gain`, then clips back to
    [0, 1]. The neutral point is preserved (x=0.5 -> y=0.5) so the
    "doing nothing" Lyria state is unchanged regardless of gain.

    Why this curve and not a smooth one (e.g. tanh):

      * Linear is predictable: GAIN=2 means literally "deviations
        twice as large". The operator's mental model matches reality.
      * Saturation at the [0,1] boundary is fine -- Lyria's BPM /
        density / brightness / temperature all clamp to their
        configured ranges anyway, so a pre-Lyria saturation just
        moves "we hit the wall" upstream by one step.
      * The EEG normalizer (smoother.py) already applies a tanh
        squash; stacking another smooth curve here would add lag-
        looking compression without buying us musicality.

    GAIN values:
      1.0  -> identity (legacy 1:1 behavior)
      2.0  -> default (~2x apparent responsiveness)
      3.0  -> aggressive (saturates at modest brain shifts)
      <1.0 -> compresses toward the midpoint (deliberately calmer)
    """
    g = max(0.0, float(gain))
    y = 0.5 + (float(x) - 0.5) * g
    return _clip01(y)


@dataclass(frozen=True)
class LyriaParams:
    """Plain-data view of what we'd send to Lyria.

    Kept separate from `types.LiveMusicGenerationConfig` so:
      1. The mapping module doesn't import google-genai (faster import,
         testable without the SDK).
      2. The session module is the single place that knows how to convert
         this into the SDK-shaped object, isolating the version surface.
    """

    bpm: int
    density: float
    brightness: float
    temperature: float

    def as_dict(self) -> dict[str, float | int]:
        """Useful for logging."""
        return {
            "bpm": self.bpm,
            "density": round(self.density, 3),
            "brightness": round(self.brightness, 3),
            "temperature": round(self.temperature, 3),
        }


def state_to_lyria_params(
    *,
    alpha: float,
    beta: float,
    theta: float,
    asymmetry: float,
    gain: Optional[float] = None,
) -> LyriaParams:
    """Translate normalized EEG features in [0, 1] into LyriaParams.

    Pure: all the SDK / asyncio stuff lives in `session.py`. Same input
    always yields the same output, which makes the mapping easy to tune
    by playing recorded AppState snapshots through it.

    `gain` is the contrast knob applied symmetrically around 0.5 BEFORE
    interpolating into Lyria's parameter ranges (see _expand). Defaults
    to config.LYRIA_SENSITIVITY_GAIN; pass an explicit value to A/B in
    a REPL or unit tests without touching the global.
    """
    g = config.LYRIA_SENSITIVITY_GAIN if gain is None else float(gain)

    a = _expand(alpha, g)
    b = _expand(beta, g)
    t = _expand(theta, g)
    asym = _expand(asymmetry, g)

    bpm = int(round(_lerp(config.LYRIA_BPM_MIN, config.LYRIA_BPM_MAX, asym)))
    density = b
    brightness = a
    temperature = _lerp(
        config.LYRIA_TEMPERATURE_MIN,
        config.LYRIA_TEMPERATURE_MAX,
        t,
    )

    return LyriaParams(
        bpm=bpm,
        density=density,
        brightness=brightness,
        temperature=temperature,
    )


def state_to_lyria_config(state) -> "object":  # type: ignore[override]
    """Build a `types.LiveMusicGenerationConfig` from the current AppState.

    Imports the SDK lazily so this module can still be imported in
    environments without google-genai (the mapping logic itself remains
    inspectable). Returns the SDK-typed config object the session task
    can hand straight to `session.set_music_generation_config`.
    """
    from google.genai import types

    params = state_to_lyria_params(
        alpha=state.alpha,
        beta=state.beta,
        theta=state.theta,
        asymmetry=state.asymmetry,
    )
    return types.LiveMusicGenerationConfig(
        bpm=params.bpm,
        density=params.density,
        brightness=params.brightness,
        temperature=params.temperature,
    )


def initial_lyria_config() -> "object":  # type: ignore[override]
    """Baseline config used at session start, before EEG values exist.

    Mirrors `LYRIA_DEFAULT_*` so a fresh session sounds the same regardless
    of whether the EEG loop has produced its first tick yet.
    """
    from google.genai import types

    return types.LiveMusicGenerationConfig(
        bpm=config.LYRIA_DEFAULT_BPM,
        density=config.LYRIA_DEFAULT_DENSITY,
        brightness=config.LYRIA_DEFAULT_BRIGHTNESS,
        temperature=config.LYRIA_DEFAULT_TEMPERATURE,
    )
