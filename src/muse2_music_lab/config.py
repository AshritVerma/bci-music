"""All tunable constants live here. Edit freely during performance tuning.

Nothing in this file does any work on import except looking up BrainFlow's
board-id enum, so it is cheap to import from anywhere.
"""

from __future__ import annotations

from brainflow.board_shim import BoardIds


# ---------------------------------------------------------------------------
# Board / acquisition
# ---------------------------------------------------------------------------

BOARD_ID: int = BoardIds.MUSE_2_BOARD.value

WINDOW_SIZE: int = 256              # samples per feature computation (~1 s at 256 Hz)
SEND_RATE_HZ: float = 30.0          # output messages per second
CALIBRATION_DURATION: float = 8.0   # seconds of baseline on startup

BOARD_PREPARE_TIMEOUT_S: float = 15.0


# ---------------------------------------------------------------------------
# Smoothing / normalization
# ---------------------------------------------------------------------------

SMOOTHING_ALPHA: float = 0.2        # EMA weight (lower = smoother, more lag)


# ---------------------------------------------------------------------------
# Output backend
# ---------------------------------------------------------------------------

# One of: "midi", "osc", "both".
OUTPUT_BACKEND: str = "midi"

MIDI_PORT_NAME: str = "IAC Driver Bus 1"
MIDI_CHANNEL_DEFAULT: int = 1       # 1-based channel

OSC_HOST: str = "127.0.0.1"
OSC_PORT: int = 9000


# ---------------------------------------------------------------------------
# Visual layer (/viz/* bus to TouchDesigner + diffusion sidecar)
# ---------------------------------------------------------------------------

# Enable by default? CLI --viz overrides. When False, no viz OSC is sent.
VIZ_ENABLED: bool = False

# Separate from the DAW OSC port so the two buses can't collide.
VIZ_HOST: str = "127.0.0.1"
VIZ_PORT: int = 9100

# Prompt source toggle for the sidecar. Accepts: "auto" | "manual" | "mix".
#   auto   - brain-only; sidecar interpolates between prompt banks
#   manual - uses /viz/prompt/base text, brain modulates diffusion only
#   mix    - bank interpolation with user text appended as style suffix
VIZ_PROMPT_SOURCE_DEFAULT: str = "auto"


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

# Order matches BrainFlow's Muse 2 EEG channel ordering.
EEG_CHANNEL_NAMES: tuple[str, ...] = ("TP9", "AF7", "AF8", "TP10")

# Channels used for band power (focus/calm/alpha/beta/theta).
FRONTAL_CHANNELS: tuple[str, ...] = ("AF7", "AF8")

# Channels used for whole-head triggers (jaw clench).
ALL_CHANNELS: tuple[str, ...] = EEG_CHANNEL_NAMES


# ---------------------------------------------------------------------------
# Band definitions (Hz)
# ---------------------------------------------------------------------------

BAND_THETA: tuple[float, float] = (4.0, 8.0)
BAND_ALPHA: tuple[float, float] = (8.0, 12.0)
BAND_BETA:  tuple[float, float] = (13.0, 30.0)


# ---------------------------------------------------------------------------
# Discrete triggers (tune these to your headset fit and signal quality)
# ---------------------------------------------------------------------------

BLINK_THRESHOLD_UV: float = 150.0
BLINK_REFRACTORY_S: float = 0.3

JAW_HP_CUTOFF_HZ: float = 20.0
# Threshold is peak |HP| averaged across channels.
# Observed on this headset/fit: rest ~40μV, strong clench 100+μV.
# 80μV gives ~40μV margin above idle + ~20μV below typical clench peaks.
JAW_THRESHOLD_UV: float = 80.0
JAW_REFRACTORY_S: float = 0.3
