"""EEG acquisition + feature extraction.

The bottom half of the pipeline: BLE -> raw windows -> band powers + triggers
-> normalized 0-1 features. Used by both the legacy `run` (TUI diagnostics)
and the new `perform` orchestrator (Lyria + visualizer).
"""

from muse2_music_lab.eeg.board import Board, BoardInfo
from muse2_music_lab.eeg.features import (
    BlinkDetector,
    JawClenchDetector,
    FeatureFrame,
    compute_band_powers,
    compute_frame,
)
from muse2_music_lab.eeg.smoother import Baseline, Calibrator, EMA, Normalizer

__all__ = [
    "Board",
    "BoardInfo",
    "BlinkDetector",
    "JawClenchDetector",
    "FeatureFrame",
    "compute_band_powers",
    "compute_frame",
    "Baseline",
    "Calibrator",
    "EMA",
    "Normalizer",
]
