"""Extract musically useful features from a rolling EEG window.

All functions are pure: they take a window (shape `(n_channels, n_samples)`) and
return plain floats or bools. Channel order is assumed to match
`config.EEG_CHANNEL_NAMES` (TP9, AF7, AF8, TP10).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, asdict
from typing import Dict, Iterable

import numpy as np
from brainflow.data_filter import DataFilter, WindowOperations

from muse2_music_lab import config


EPS = 1e-9


@dataclass
class FeatureFrame:
    """One frame of features. Values are raw (not yet normalized or smoothed)."""

    alpha: float
    beta: float
    theta: float
    focus: float
    calm: float
    blink: bool
    jaw: bool

    def continuous(self) -> Dict[str, float]:
        return {
            "alpha": self.alpha,
            "beta": self.beta,
            "theta": self.theta,
            "focus": self.focus,
            "calm": self.calm,
        }

    def discrete(self) -> Dict[str, bool]:
        return {"blink": self.blink, "jaw": self.jaw}

    def as_dict(self) -> Dict[str, float]:
        return asdict(self)


def _channel_indices(names: Iterable[str]) -> list[int]:
    lookup = {n: i for i, n in enumerate(config.EEG_CHANNEL_NAMES)}
    return [lookup[n] for n in names if n in lookup]


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p <<= 1
    return p


def _band_power(signal: np.ndarray, sampling_rate: int, band: tuple[float, float]) -> float:
    """Welch PSD band power for a 1-D signal. Returns 0.0 on degenerate input."""
    signal = np.ascontiguousarray(signal, dtype=np.float64)
    if signal.size < 16:
        return 0.0
    nfft = min(_next_pow2(signal.size), signal.size)
    if nfft < 16:
        return 0.0
    overlap = nfft // 2
    try:
        psd = DataFilter.get_psd_welch(
            signal,
            nfft,
            overlap,
            int(sampling_rate),
            WindowOperations.HANNING.value,
        )
        return float(DataFilter.get_band_power(psd, float(band[0]), float(band[1])))
    except Exception:
        return 0.0


def compute_band_powers(
    window: np.ndarray,
    sampling_rate: int,
    channels: Iterable[str] = config.FRONTAL_CHANNELS,
) -> Dict[str, float]:
    """Compute alpha/beta/theta averaged over `channels`.

    Returns a dict with keys `alpha`, `beta`, `theta`.
    """
    idx = _channel_indices(channels)
    if window.size == 0 or not idx:
        return {"alpha": 0.0, "beta": 0.0, "theta": 0.0}

    alpha = np.mean([_band_power(window[i], sampling_rate, config.BAND_ALPHA) for i in idx])
    beta = np.mean([_band_power(window[i], sampling_rate, config.BAND_BETA) for i in idx])
    theta = np.mean([_band_power(window[i], sampling_rate, config.BAND_THETA) for i in idx])
    return {"alpha": float(alpha), "beta": float(beta), "theta": float(theta)}


def compute_asymmetry(
    window: np.ndarray,
    sampling_rate: int,
    left_channel: str = "AF7",
    right_channel: str = "AF8",
) -> float:
    """Frontal alpha asymmetry, normalized to [0, 1] with idle ~= 0.5.

    Standard FAA = log(alpha_right) - log(alpha_left). Positive values mean
    *less* left-frontal alpha (less alpha = more activity), historically
    associated with approach motivation / positive affect. We tanh-squash
    and shift so the output is bounded and easy to map into a synth knob.

    Returns 0.5 on degenerate input (no channels found, no data, etc.) so
    a missing-channel error doesn't push downstream synthesis to a knob
    extreme.
    """
    if window.size == 0:
        return 0.5
    lookup = {n: i for i, n in enumerate(config.EEG_CHANNEL_NAMES)}
    if left_channel not in lookup or right_channel not in lookup:
        return 0.5

    powers_l = compute_band_powers(window, sampling_rate, channels=(left_channel,))
    powers_r = compute_band_powers(window, sampling_rate, channels=(right_channel,))
    alpha_l = powers_l["alpha"]
    alpha_r = powers_r["alpha"]

    # Symmetric log ratio. The 1e-9 floor avoids log(0) on a dropped channel.
    faa = math.log10(alpha_r + 1e-9) - math.log10(alpha_l + 1e-9)

    # tanh keeps it bounded; the (+1)/2 shift puts the natural balance
    # point at 0.5. Typical FAA on this headset/fit lands in roughly
    # [-0.5, +0.5] which tanh maps to [-0.46, +0.46], i.e. [0.27, 0.73] --
    # a reasonable musical range without saturation.
    return 0.5 * (math.tanh(faa) + 1.0)


class _Refractory:
    """Helper: returns True at most once per `interval_s`."""

    def __init__(self, interval_s: float) -> None:
        self._interval = float(interval_s)
        self._last = 0.0

    def fire(self, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        if t - self._last < self._interval:
            return False
        self._last = t
        return True


class BlinkDetector:
    """Peak-to-peak amplitude spike on frontal channels (AF7/AF8) with refractory."""

    def __init__(
        self,
        threshold_uv: float = config.BLINK_THRESHOLD_UV,
        refractory_s: float = config.BLINK_REFRACTORY_S,
    ) -> None:
        self.threshold = float(threshold_uv)
        self._refractory = _Refractory(refractory_s)
        # Diagnostics: the most recent peak-to-peak value seen on frontal channels.
        self.last_ptp: float = 0.0

    def detect(self, window: np.ndarray) -> bool:
        idx = _channel_indices(config.FRONTAL_CHANNELS)
        if not idx or window.size == 0:
            return False
        frontal = window[idx, :]
        # Remove per-channel DC so a drifting baseline doesn't trigger.
        centered = frontal - np.mean(frontal, axis=1, keepdims=True)
        # Peak-to-peak across all frontal channels in this window.
        ptp = float(np.max(centered) - np.min(centered))
        self.last_ptp = ptp
        if ptp >= self.threshold:
            return self._refractory.fire()
        return False


class JawClenchDetector:
    """High-frequency, high-amplitude burst across all 4 channels.

    Measures the peak absolute value of the high-pass-filtered signal
    (averaged across channels). Peak is ~3-5x more sensitive to brief
    bursts (~150 ms jaw clenches) than window RMS.
    """

    def __init__(
        self,
        sampling_rate: int,
        threshold_uv: float = config.JAW_THRESHOLD_UV,
        hp_cutoff_hz: float = config.JAW_HP_CUTOFF_HZ,
        refractory_s: float = config.JAW_REFRACTORY_S,
    ) -> None:
        self.sampling_rate = int(sampling_rate)
        self.threshold = float(threshold_uv)
        self.hp_cutoff = float(hp_cutoff_hz)
        self._refractory = _Refractory(refractory_s)
        # Diagnostics: the most recent peak |HP| averaged across channels.
        self.last_rms: float = 0.0  # name kept for back-compat with the TUI

    def detect(self, window: np.ndarray) -> bool:
        idx = _channel_indices(config.ALL_CHANNELS)
        if not idx or window.size == 0:
            return False
        data = window[idx, :]
        # Simple high-pass: subtract a centered moving average (length ~= fs / hp_cutoff).
        n = max(2, int(self.sampling_rate / max(self.hp_cutoff, EPS)))
        kernel = np.ones(n) / n
        hp = np.empty_like(data)
        for ch in range(data.shape[0]):
            trend = np.convolve(data[ch], kernel, mode="same")
            hp[ch] = data[ch] - trend
        # Peak abs value per channel, then averaged. Captures burst activity.
        peak_per_ch = np.max(np.abs(hp), axis=1)
        peak = float(np.mean(peak_per_ch))
        self.last_rms = peak
        if peak >= self.threshold:
            return self._refractory.fire()
        return False


def _log_compress(x: float) -> float:
    """Log compression for band power / ratio features.

    Band powers span orders of magnitude and are roughly log-distributed,
    so we log-compress before smoothing + tanh normalization. This keeps the
    normalized [0, 1] output from saturating on small natural fluctuations.
    """
    return float(np.log10(max(float(x), 0.0) + 1e-6))


def compute_frame(
    window: np.ndarray,
    sampling_rate: int,
    blink: BlinkDetector,
    jaw: JawClenchDetector,
) -> FeatureFrame:
    """Full feature extraction for one window."""
    powers = compute_band_powers(window, sampling_rate)
    alpha_raw, beta_raw, theta_raw = powers["alpha"], powers["beta"], powers["theta"]
    focus_raw = beta_raw / (alpha_raw + theta_raw + EPS)
    calm_raw = alpha_raw / (beta_raw + EPS)

    # Log-compress so tanh normalization behaves musically.
    alpha = _log_compress(alpha_raw)
    beta = _log_compress(beta_raw)
    theta = _log_compress(theta_raw)
    focus = _log_compress(focus_raw)
    calm = _log_compress(calm_raw)

    return FeatureFrame(
        alpha=alpha,
        beta=beta,
        theta=theta,
        focus=float(focus),
        calm=float(calm),
        blink=blink.detect(window),
        jaw=jaw.detect(window),
    )
