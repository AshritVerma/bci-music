"""Thin wrapper around syphon-python for publishing RGBA frames.

Kept isolated so the render loop doesn't have to know about Syphon's
Metal-texture bookkeeping.
"""

from __future__ import annotations

import numpy as np


class SyphonPublisher:
    def __init__(self, name: str, width: int, height: int) -> None:
        # Deferred import so the rest of the sidecar runs on non-Darwin hosts.
        import syphon  # type: ignore
        from syphon.utils.numpy import copy_image_to_mtl_texture  # type: ignore
        from syphon.utils.raw import create_mtl_texture  # type: ignore

        self._syphon = syphon
        self._copy = copy_image_to_mtl_texture
        self._server = syphon.SyphonMetalServer(name)
        self._texture = create_mtl_texture(self._server.device, width, height)
        self._name = name
        self._width = width
        self._height = height
        self._last_shape: tuple[int, int, int] | None = None

    @property
    def name(self) -> str:
        return self._name

    def publish(self, rgba: np.ndarray) -> None:
        if rgba.ndim != 3 or rgba.shape[2] != 4:
            raise ValueError(
                f"Expected RGBA array (H, W, 4); got shape {rgba.shape}"
            )
        if rgba.dtype != np.uint8:
            rgba = rgba.astype(np.uint8, copy=False)
        if rgba.shape[0] != self._height or rgba.shape[1] != self._width:
            raise ValueError(
                f"Frame size mismatch: expected {self._width}x{self._height}, "
                f"got {rgba.shape[1]}x{rgba.shape[0]}"
            )
        self._copy(rgba, self._texture)
        self._server.publish_frame_texture(self._texture)

    def close(self) -> None:
        try:
            self._server.stop()
        except Exception:
            pass
