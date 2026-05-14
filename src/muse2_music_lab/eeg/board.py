"""BrainFlow connection to the Muse 2. Pulls raw EEG windows."""

from __future__ import annotations

import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np
from brainflow.board_shim import BoardShim, BrainFlowInputParams, LogLevels
from brainflow.exit_codes import BrainFlowError

from muse2_music_lab import config


@contextmanager
def _sigint_shielded() -> Iterator[None]:
    """Ignore Ctrl-C inside the block so a critical cleanup path can finish.

    The Muse 2 leaves its BLE peripheral in a stuck "still connected" state
    if ``release_session`` is interrupted partway through, which is exactly
    what happens when a user mashes Ctrl-C during shutdown. Ignoring SIGINT
    while we tear the session down avoids that.
    """
    try:
        prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, AttributeError):
        prev = None
    try:
        yield
    finally:
        if prev is not None:
            try:
                signal.signal(signal.SIGINT, prev)
            except (ValueError, AttributeError):
                pass


# Set MUSE2_DEBUG_BOARD=1 to dump BrainFlow's verbose BLE/GATT logs to stderr.
# Useful when the Muse 2 silently drops its BLE link right after `start_stream`.
if os.environ.get("MUSE2_DEBUG_BOARD"):
    BoardShim.set_log_level(LogLevels.LEVEL_TRACE.value)
    BoardShim.enable_dev_board_logger()


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
        max_attempts: int = 3,
    ) -> None:
        self.board_id = int(board_id)
        self._params = BrainFlowInputParams()
        if mac_address:
            self._params.mac_address = mac_address
        if serial_number:
            self._params.serial_number = serial_number
        # BrainFlow's default BLE discovery timeout is 6s, which is short for
        # the Muse 2 when macOS has stale BLE state. Honor an explicit override
        # if provided, otherwise fall back to the project default in config.
        eff_timeout = timeout if timeout is not None else config.BOARD_PREPARE_TIMEOUT_S
        self._params.timeout = max(1, int(eff_timeout))
        self._shim: Optional[BoardShim] = None
        self._started = False
        self._max_attempts = max(1, int(max_attempts))

    @property
    def info(self) -> BoardInfo:
        return BoardInfo(
            board_id=self.board_id,
            sampling_rate=BoardShim.get_sampling_rate(self.board_id),
            eeg_channels=list(BoardShim.get_eeg_channels(self.board_id)),
            channel_names=config.EEG_CHANNEL_NAMES,
        )

    def start(self) -> BoardInfo:
        """Connect to the headset and begin streaming. Returns static board info.

        The Muse 2 BLE driver in BrainFlow occasionally fails on the first
        ``prepare_session`` even when the device is discovered, especially when
        macOS has stale BLE state. We retry a few times with a short backoff
        and make sure each failed shim is fully released before retrying.
        """
        last_err: Optional[BaseException] = None
        for attempt in range(1, self._max_attempts + 1):
            shim = BoardShim(self.board_id, self._params)
            try:
                shim.prepare_session()
                shim.start_stream()
            except BrainFlowError as e:
                last_err = e
                # Make sure no half-initialized native session lingers and
                # holds the BLE peripheral before the next attempt.
                try:
                    shim.release_session()
                except Exception:
                    pass
                if attempt < self._max_attempts:
                    time.sleep(2.0)
                    continue
                raise
            else:
                self._shim = shim
                self._started = True
                # BrainFlow's Muse driver returns from start_stream as soon as
                # GATT subscription is requested, but the data thread may need
                # a couple hundred ms to actually start receiving notifications.
                # Polling get_current_board_data too early occasionally causes
                # the underlying session to be torn down with BOARD_NOT_CREATED.
                # A short, blocking sleep here is much cheaper than a failed
                # session.
                time.sleep(0.5)
                return self.info
        # Should be unreachable: loop either returns or re-raises.
        assert last_err is not None
        raise last_err

    def stop(self) -> None:
        """Tear the session down cleanly so the *next* connect doesn't need a power-cycle.

        Tolerates an already-dead native session: if the BLE link has dropped
        between ``start()`` and ``stop()``, BrainFlow resets itself to
        ``BOARD_NOT_CREATED`` and ``stop_stream``/``release_session`` raise.
        We swallow both so the caller still sees the ORIGINAL failure (e.g.
        the data-read error that triggered cleanup) instead of a confusing
        cascade.

        Two pacing sleeps prevent the post-teardown "stale peripheral" symptom
        where the next ``prepare_session`` gets handed a half-disconnected
        CoreBluetooth handle and shows the slow-connect-then-drop pattern:

          1. ``BOARD_TEARDOWN_HALT_PAUSE_S`` between stop_stream and release:
             gives the headset firmware time to actually process the halt
             before we yank the BLE link.
          2. ``BOARD_TEARDOWN_FLUSH_S`` after release_session returns: lets
             CoreBluetooth's async ``didDisconnectPeripheral`` notification
             flush. Without this, the next ``prepare_session`` (in this
             process or a freshly spawned one) sees a peripheral that's still
             "connected" from CoreBluetooth's perspective even though
             SimpleBLE has dropped its handle.

        Both sleeps are inside the SIGINT shield: a Ctrl-C during teardown
        must not interrupt them, otherwise we re-introduce the very dirty
        state we're trying to avoid. The cost of paying these sleeps on
        every shutdown (~2.3s) is much smaller than the cost of ever needing
        to power-cycle the headset to recover.
        """
        if self._shim is None:
            return
        shim = self._shim
        self._shim = None
        started = self._started
        self._started = False
        with _sigint_shielded():
            try:
                if started:
                    shim.stop_stream()
            except BrainFlowError:
                pass
            # Let the headset settle on the halt before we tear the BLE link.
            time.sleep(config.BOARD_TEARDOWN_HALT_PAUSE_S)
            try:
                shim.release_session()
            except BrainFlowError:
                pass
            # Flush CoreBluetooth's async disconnect notification.
            time.sleep(config.BOARD_TEARDOWN_FLUSH_S)

    def get_window(self, n_samples: int = config.WINDOW_SIZE) -> np.ndarray:
        """Return the most recent `n_samples` of EEG as an (n_channels, n_samples) array.

        Uses `get_current_board_data` so it does not drain the internal ring buffer.
        Returns an array with shape `(len(EEG_CHANNEL_NAMES), n_samples)`; if fewer
        samples are available yet, returns what's there (possibly empty along axis 1).

        Raises ``ConnectionError`` (instead of leaking a raw BrainFlow exit code)
        if the underlying BLE session has been torn down — typical for the
        Muse 2 when battery is low or the headset slips off mid-session.
        """
        if self._shim is None:
            raise RuntimeError("Board is not started. Call start() first.")
        try:
            raw = self._shim.get_current_board_data(int(n_samples))
        except BrainFlowError as e:
            raise ConnectionError(
                "Lost connection to the Muse 2 (BrainFlow session torn down). "
                "Common causes: low headset battery, headset slipped off, "
                "or macOS Bluetooth dropped the BLE link."
            ) from e
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
