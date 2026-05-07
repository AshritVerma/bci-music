"""BrainFlow connection to the Muse 2. Pulls raw EEG windows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from brainflow.board_shim import BoardShim, BrainFlowInputParams

from muse2_music_lab import config


@dataclass
class BoardInfo:
    """Static info about the board. Useful so callers don't re-query BrainFlow."""

    board_id: int
    sampling_rate: int
    eeg_channels: list[int]
    channel_names: tuple[str, ...]


class Board:
    """Thin wrapper around BrainFlow's BoardShim for Muse 2."""

    def __init__(
        self,
        board_id: int = config.BOARD_ID,
        mac_address: Optional[str] = None,
        serial_number: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.board_id = int(board_id)
        self._params = BrainFlowInputParams()
        if mac_address:
            self._params.mac_address = mac_address
        if serial_number:
            self._params.serial_number = serial_number
        if timeout is not None:
            self._params.timeout = int(timeout)
        self._shim: Optional[BoardShim] = None
        self._started = False

    @property
    def info(self) -> BoardInfo:
        return BoardInfo(
            board_id=self.board_id,
            sampling_rate=BoardShim.get_sampling_rate(self.board_id),
            eeg_channels=list(BoardShim.get_eeg_channels(self.board_id)),
            channel_names=config.EEG_CHANNEL_NAMES,
        )

    def start(self) -> BoardInfo:
        """Connect to the headset and begin streaming. Returns static board info."""
        shim = BoardShim(self.board_id, self._params)
        shim.prepare_session()
        shim.start_stream()
        self._shim = shim
        self._started = True
        return self.info

    def stop(self) -> None:
        if self._shim is None:
            return
        try:
            if self._started:
                self._shim.stop_stream()
        finally:
            try:
                self._shim.release_session()
            finally:
                self._shim = None
                self._started = False

    def get_window(self, n_samples: int = config.WINDOW_SIZE) -> np.ndarray:
        """Return the most recent `n_samples` of EEG as an (n_channels, n_samples) array.

        Uses `get_current_board_data` so it does not drain the internal ring buffer.
        Returns an array with shape `(len(EEG_CHANNEL_NAMES), n_samples)`; if fewer
        samples are available yet, returns what's there (possibly empty along axis 1).
        """
        if self._shim is None:
            raise RuntimeError("Board is not started. Call start() first.")
        raw = self._shim.get_current_board_data(int(n_samples))
        eeg_rows = BoardShim.get_eeg_channels(self.board_id)
        return np.asarray(raw[eeg_rows, :], dtype=np.float64)

    def drain(self) -> np.ndarray:
        """Drain and return all buffered samples. Shape: (n_channels, n)."""
        if self._shim is None:
            raise RuntimeError("Board is not started. Call start() first.")
        raw = self._shim.get_board_data()
        eeg_rows = BoardShim.get_eeg_channels(self.board_id)
        return np.asarray(raw[eeg_rows, :], dtype=np.float64)

    def __enter__(self) -> "Board":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
