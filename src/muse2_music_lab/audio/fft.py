"""Phase 6: numpy FFT analysis of Lyria's PCM stream.

This task drains `state.audio_analysis_queue` (the lossy tee from
`lyria/session.py`) and writes three perceptual features back into
AppState:

  * state.rms       -- perceived loudness (log-compressed RMS, EMA-smoothed,
                       percentile-normalized to [0, 1])
  * state.centroid  -- spectral brightness (frequency-weighted FFT mag mean,
                       normalized against AUDIO_CENTROID_NORM_HZ)
  * state.onset     -- rate of energy increase (positive spectral flux,
                       percentile-normalized to [0, 1])

Why each one:

  * RMS drives anything that should pulse with loudness (visual scale,
    bloom intensity, beat-sync overlays).
  * Centroid drives "brightness" parameters in the visual: high centroid
    = lots of treble = sparkly / glassy / sharp; low centroid = bassy /
    warm / muddy. Maps naturally to color temperature or hue shift.
  * Onset is a positive-only derivative of spectral energy across bins.
    Spikes on percussive transients (kicks, snares, plucks) and stays
    near zero on sustained pads. Drives flash / strobe / particle-burst
    style visual events.

Implementation notes:

  * Lyria sends ~96k-sample stereo chunks (2s at 48 kHz). We subdivide
    each chunk into HOP_SAMPLES-sized analysis frames so the feature
    output is at ~20 Hz instead of ~0.5 Hz. The visualizer would feel
    obviously laggy at chunk-rate.

  * Stereo is downmixed to mono BEFORE analysis. The features we care
    about are perceptual / spectral; stereo image isn't relevant. Saves
    half the FFT work too.

  * Adaptive percentile baselining: each feature is normalized against
    a slow-moving high-water mark instead of a fixed gain. This means
    the [0, 1] output adapts to the average loudness/brightness of the
    current track section -- so a slow ambient passage and a loud beat
    drop both occupy the full visual range, just on different absolute
    scales. Fast attack / slow release behaves like a perceptual AGC.

  * Everything runs on the asyncio event loop. The FFT work for one
    50ms hop is ~30μs on M-class silicon, so we don't bother offloading
    to an executor; the latency cost would dwarf the work itself.
"""

from __future__ import annotations

import asyncio
import math

import numpy as np

from muse2_music_lab import config
from muse2_music_lab.state import AppState


# How many bytes per sample-frame (stereo s16 = 2 channels * 2 bytes = 4 bytes).
_BYTES_PER_FRAME = config.LYRIA_CHANNELS * 2

# Pre-compute the FFT bin frequencies for HOP_SAMPLES at LYRIA_SAMPLE_RATE.
# Used by spectral centroid (and any future spectral feature). Constant for
# the life of the session, so build it once.
_FFT_FREQS = np.fft.rfftfreq(
    config.AUDIO_ANALYSIS_HOP_SAMPLES,
    d=1.0 / config.LYRIA_SAMPLE_RATE,
)


# ---------------------------------------------------------------------------
# Helpers: PCM decode and feature math (pure-numpy, no asyncio coupling)
# ---------------------------------------------------------------------------


def _decode_chunk_to_mono(data: bytes) -> np.ndarray:
    """s16le stereo bytes -> float32 mono samples in [-1, 1].

    Returns an empty array if the input is malformed (e.g. odd length,
    wrong frame count). Lyria only ever sends well-formed chunks but
    the analysis loop is the wrong place to crash on a one-off bit
    flip; the visualizer will just show a still frame for that hop.
    """
    if not data or len(data) % _BYTES_PER_FRAME != 0:
        return np.empty(0, dtype=np.float32)

    # Interpret as int16 stereo -> reshape -> downmix to mono float32.
    # The 1/32768 scale puts samples in the conventional [-1, 1] range.
    pcm = np.frombuffer(data, dtype=np.int16)
    if pcm.size == 0:
        return np.empty(0, dtype=np.float32)
    stereo = pcm.reshape(-1, config.LYRIA_CHANNELS).astype(np.float32) * (1.0 / 32768.0)
    if config.LYRIA_CHANNELS == 1:
        return stereo[:, 0]
    return stereo.mean(axis=1)


def _rms_db(samples: np.ndarray) -> float:
    """Log-compressed RMS (≈ dBFS). Returns ~-90 for silence, ~0 for full-scale."""
    if samples.size == 0:
        return -90.0
    rms = float(np.sqrt(np.mean(samples * samples)))
    # 1e-9 floor avoids log(0). 20*log10(rms/1.0) is dBFS for [-1,1] PCM.
    return 20.0 * math.log10(max(rms, 1e-9))


def _spectral_centroid_hz(samples: np.ndarray) -> float:
    """Magnitude-weighted mean frequency of the rfft. Returns 0 on silence."""
    if samples.size == 0:
        return 0.0
    # Hann window suppresses spectral leakage from the rectangular cut.
    window = np.hanning(samples.size)
    spectrum = np.abs(np.fft.rfft(samples * window))
    total = float(spectrum.sum())
    if total < 1e-9:
        return 0.0
    # _FFT_FREQS is precomputed against the canonical hop size; if a hop
    # is short (last frame of a chunk that doesn't divide evenly), recompute
    # locally to keep the bin alignment correct.
    freqs = (
        _FFT_FREQS
        if spectrum.size == _FFT_FREQS.size
        else np.fft.rfftfreq(samples.size, d=1.0 / config.LYRIA_SAMPLE_RATE)
    )
    return float(np.sum(freqs * spectrum) / total)


def _spectral_magnitude(samples: np.ndarray) -> np.ndarray:
    """Windowed rfft magnitudes. Used by the onset detector for flux."""
    if samples.size == 0:
        return np.empty(0, dtype=np.float32)
    window = np.hanning(samples.size)
    return np.abs(np.fft.rfft(samples * window)).astype(np.float32)


def _spectral_flux(prev_mag: np.ndarray, mag: np.ndarray) -> float:
    """Sum of positive bin-wise differences (Dixon 2006).

    Captures *increases* in spectral energy, which is what perceptually
    constitutes an onset. Returns 0 if shapes don't match (first frame
    or hop-size change) so we don't double-count silence.
    """
    if prev_mag.size == 0 or prev_mag.size != mag.size:
        return 0.0
    diff = mag - prev_mag
    diff[diff < 0] = 0.0
    return float(diff.sum())


# ---------------------------------------------------------------------------
# Adaptive percentile baseline (perceptual-AGC-style normalizer)
# ---------------------------------------------------------------------------


class _AdaptiveBaseline:
    """Tracks a rolling high-water mark with asymmetric attack/release.

    Maps an arbitrary positive feature into [0, 1] by dividing by a
    slow-moving "loud" baseline. Attack > release so the baseline jumps
    up quickly when a loud section starts (preventing the output from
    pinning at 1.0 forever) and falls slowly when the section ends
    (so a quiet moment after a peak doesn't immediately re-saturate).
    """

    def __init__(self, attack: float, release: float, init: float = 1e-3) -> None:
        self.attack = float(attack)
        self.release = float(release)
        self.baseline = float(init)

    def normalize(self, value: float) -> float:
        v = max(float(value), 0.0)
        if v > self.baseline:
            self.baseline += self.attack * (v - self.baseline)
        else:
            self.baseline -= self.release * (self.baseline - v)
        if self.baseline < 1e-9:
            return 0.0
        return max(0.0, min(1.0, v / self.baseline))


class _ScalarEMA:
    """Trivial first-order IIR. Mirrors eeg.smoother.EMA without importing it."""

    def __init__(self, alpha: float) -> None:
        self.alpha = float(alpha)
        self._value: float | None = None

    def update(self, x: float) -> float:
        if self._value is None:
            self._value = float(x)
        else:
            self._value += self.alpha * (float(x) - self._value)
        return self._value


# ---------------------------------------------------------------------------
# Asyncio task entry point
# ---------------------------------------------------------------------------


async def run_audio_analysis_loop(state: AppState) -> None:
    """Drain state.audio_analysis_queue, hop-analyze, write features.

    Runs forever until cancelled. Sets `state.audio_ready` after the first
    frame produces a non-zero RMS so the perform TUI can swap its
    "warming" placeholder for live bars.
    """
    # Phase 10: queue is empty until Lyria starts producing audio, so
    # this gate is mostly cosmetic -- but explicit waiting keeps the
    # task tree symmetric and avoids a `[audio-fft] starting` log line
    # firing 30 seconds before the user clicks Start.
    await state.start_requested.wait()
    hop = config.AUDIO_ANALYSIS_HOP_SAMPLES
    rms_norm = _AdaptiveBaseline(
        config.AUDIO_BASELINE_ATTACK, config.AUDIO_BASELINE_RELEASE,
        init=0.05,  # ~-26 dBFS-ish; reasonable starting point for music
    )
    onset_norm = _AdaptiveBaseline(
        config.AUDIO_BASELINE_ATTACK, config.AUDIO_BASELINE_RELEASE,
        init=1.0,
    )
    rms_ema = _ScalarEMA(config.AUDIO_FEATURE_SMOOTHING)
    centroid_ema = _ScalarEMA(config.AUDIO_FEATURE_SMOOTHING)
    onset_ema = _ScalarEMA(config.AUDIO_FEATURE_SMOOTHING)

    # Carry leftover samples between Lyria chunks so a chunk that doesn't
    # divide evenly into hops doesn't waste its tail. Concatenated to the
    # next chunk's decoded samples before we re-frame.
    leftover = np.empty(0, dtype=np.float32)
    prev_mag = np.empty(0, dtype=np.float32)

    frames_processed = 0
    chunks_seen = 0

    try:
        while True:
            chunk = await state.audio_analysis_queue.get()
            try:
                if not chunk:
                    continue
                chunks_seen += 1

                samples = _decode_chunk_to_mono(chunk)
                if samples.size == 0:
                    continue

                if leftover.size:
                    samples = np.concatenate([leftover, samples])

                # Walk the buffer in HOP_SAMPLES strides.
                n_hops = samples.size // hop
                for h in range(n_hops):
                    frame = samples[h * hop : (h + 1) * hop]

                    # --- RMS (loudness) ---
                    rms_db = _rms_db(frame)
                    # Map dBFS to a positive scalar in [0, 1]-ish range so
                    # the adaptive baseline has something positive to track.
                    # -60 dBFS -> 0.0, 0 dBFS -> 1.0, log-curve in between.
                    rms_lin = max(0.0, (rms_db + 60.0) / 60.0)
                    rms_lin_smoothed = rms_ema.update(rms_lin)
                    state.rms = rms_norm.normalize(rms_lin_smoothed)

                    # --- Spectral centroid (brightness) ---
                    cent_hz = _spectral_centroid_hz(frame)
                    cent_smoothed = centroid_ema.update(cent_hz)
                    state.centroid = max(
                        0.0,
                        min(1.0, cent_smoothed / config.AUDIO_CENTROID_NORM_HZ),
                    )

                    # --- Onset strength (percussive events) ---
                    mag = _spectral_magnitude(frame)
                    flux = _spectral_flux(prev_mag, mag)
                    flux_smoothed = onset_ema.update(flux)
                    state.onset = onset_norm.normalize(flux_smoothed)
                    prev_mag = mag

                    frames_processed += 1
                    if frames_processed == 1 or (
                        not state.audio_ready.is_set() and rms_lin > 0.01
                    ):
                        # Don't fire audio_ready on a pre-roll silence frame;
                        # wait until we see actual signal so the TUI doesn't
                        # bounce between "warming" and the live bars.
                        if rms_lin > 0.01:
                            state.audio_ready.set()

                # Stash any tail samples that didn't fill a full hop.
                tail_start = n_hops * hop
                leftover = (
                    samples[tail_start:]
                    if tail_start < samples.size
                    else np.empty(0, dtype=np.float32)
                )
            finally:
                state.audio_analysis_queue.task_done()

    except asyncio.CancelledError:
        # Suppress the per-task summary during TUI ownership; the panel
        # already shows the live values, and a tail print would smear it.
        if not state.tui_active:
            print(
                f"[audio-fft] cancelled "
                f"({chunks_seen} chunks, {frames_processed} frames analyzed)",
                flush=True,
            )
        raise
