"""OSC listener. Translates `/viz/*` traffic into `VizState` mutations."""

from __future__ import annotations

import threading
from typing import Any

from pythonosc import dispatcher, osc_server

from .state import VizState


def _param_name_from_address(address: str) -> str:
    # "/viz/params/intensity" -> "intensity"
    return address.rsplit("/", 1)[-1]


class VizOscServer:
    """Threaded OSC server writing into shared `VizState`."""

    def __init__(self, host: str, port: int, state: VizState) -> None:
        self.host = host
        self.port = int(port)
        self.state = state
        self._server: osc_server.BlockingOSCUDPServer | None = None
        self._thread: threading.Thread | None = None
        self._disp = self._build_dispatcher()

    def _build_dispatcher(self) -> dispatcher.Dispatcher:
        d = dispatcher.Dispatcher()
        d.map("/viz/params/*", self._on_param)
        d.map("/viz/trigger/*", self._on_trigger)
        d.map("/viz/prompt/base", self._on_prompt_base)
        d.map("/viz/prompt/style", self._on_prompt_style)
        d.map("/viz/prompt/source", self._on_prompt_source)
        d.set_default_handler(self._on_unknown)
        return d

    def _on_param(self, address: str, *args: Any) -> None:
        if not args:
            return
        name = _param_name_from_address(address)
        try:
            value = float(args[0])
        except (TypeError, ValueError):
            return
        self.state.set_param(name, value)

    def _on_trigger(self, address: str, *args: Any) -> None:
        if not args:
            return
        try:
            v = float(args[0])
        except (TypeError, ValueError):
            return
        if v <= 0.0:
            return
        name = _param_name_from_address(address)
        self.state.mark_trigger(name)

    def _on_prompt_base(self, _address: str, *args: Any) -> None:
        if not args:
            return
        self.state.set_base_prompt(str(args[0]))

    def _on_prompt_style(self, _address: str, *args: Any) -> None:
        if not args:
            return
        self.state.set_style(str(args[0]))

    def _on_prompt_source(self, _address: str, *args: Any) -> None:
        if not args:
            return
        self.state.set_source(str(args[0]))

    def _on_unknown(self, address: str, *_args: Any) -> None:
        # Silent by default; uncomment for debugging.
        # print(f"[osc] unhandled {address} {_args}")
        pass

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = osc_server.BlockingOSCUDPServer(
            (self.host, self.port), self._disp
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="viz-osc",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None
