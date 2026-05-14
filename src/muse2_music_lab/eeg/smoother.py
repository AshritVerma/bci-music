"""EMA smoothing, baseline calibration, and soft-clipping normalization."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from muse2_music_lab import config


@dataclass
class EMA:
    """Exponential moving average. `alpha` is the weight of each new sample."""

    alpha: float = config.SMOOTHING_ALPHA
    value: Optional[float] = None

    def update(self, x: float) -> float:
        if self.value is None:
            self.value = float(x)
        else:
            self.value = self.alpha * float(x) + (1.0 - self.alpha) * self.value
        return self.value

    def reset(self) -> None:
        self.value = None


@dataclass
class Baseline:
    """Per-feature baseline stats captured during calibration."""

    mean: float = 0.0
    std: float = 1.0

    def normalize(self, x: float) -> float:
        """Map x to [0, 1] with soft clipping via tanh on the z-score.

        The sigmoid `(tanh(z) + 1) / 2` lets values exceed the calibration
        range gracefully without hard-clipping.
        """
        std = self.std if self.std > 1e-9 else 1e-9
        z = (float(x) - self.mean) / std
        return 0.5 * (math.tanh(z) + 1.0)


class Normalizer:
    """Holds a `Baseline` per feature name and applies soft-clip normalization."""

    def __init__(self, baselines: Optional[Dict[str, Baseline]] = None) -> None:
        self.baselines: Dict[str, Baseline] = dict(baselines or {})

    def set_baseline(self, name: str, mean: float, std: float) -> None:
        self.baselines[name] = Baseline(mean=float(mean), std=float(std))

    def has(self, name: str) -> bool:
        return name in self.baselines

    def normalize(self, name: str, x: float) -> float:
        base = self.baselines.get(name)
        if base is None:
            return max(0.0, min(1.0, float(x)))
        return base.normalize(x)

    def normalize_dict(self, values: Dict[str, float]) -> Dict[str, float]:
        return {k: self.normalize(k, v) for k, v in values.items()}


@dataclass
class Calibrator:
    """Collect samples for a few seconds and compute per-feature mean/std."""

    names: Iterable[str]
    _samples: Dict[str, List[float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._samples = {n: [] for n in self.names}

    def add(self, values: Dict[str, float]) -> None:
        for k, v in values.items():
            if k in self._samples:
                self._samples[k].append(float(v))

    # Minimum std in (log-compressed) feature space. Prevents tanh from
    # saturating when a brief, very-still calibration captures a slice of
    # the noise floor that happens to have near-zero variance. Tuned for
    # log10(band power) which typically has std in [0.15, 0.5].
    MIN_STD: float = 0.15

    def finish(self) -> Dict[str, Baseline]:
        """Compute baseline stats. Empty channels fall back to mean=0, std=1."""
        out: Dict[str, Baseline] = {}
        for k, vs in self._samples.items():
            if not vs:
                out[k] = Baseline()
                continue
            n = len(vs)
            mean = sum(vs) / n
            var = sum((v - mean) ** 2 for v in vs) / max(n - 1, 1)
            std = math.sqrt(var) if var > 0 else 1.0
            std = max(std, self.MIN_STD)
            out[k] = Baseline(mean=mean, std=std)
        return out

    def reset(self) -> None:
        for k in self._samples:
            self._samples[k] = []
