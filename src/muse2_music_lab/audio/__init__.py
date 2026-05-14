"""Audio analysis subpackage (Phase 6).

Exposes the FFT-based perceptual feature extractor that taps Lyria's
PCM stream and writes `rms` / `centroid` / `onset` into AppState.
"""

from muse2_music_lab.audio.fft import run_audio_analysis_loop

__all__ = ["run_audio_analysis_loop"]
