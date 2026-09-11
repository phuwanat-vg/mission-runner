"""In-process event bus. Everything observable (run/step lifecycle, logs,
prompts, robot state) flows through here; the HTTP/WS server, the ROS
publisher and the run log are just listeners."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("mission")

Listener = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def emit(self, type_: str, **data: Any) -> dict[str, Any]:
        """Emit from the event loop thread. Returns the event dict."""
        event = {"type": type_, "t": time.time(), **data}
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001
                log.exception("event listener failed on %s", type_)
        return event

    def emit_threadsafe(self, type_: str, **data: Any) -> None:
        """Emit from any thread (connector callbacks)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            self.emit(type_, **data)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.emit(type_, **data)
        else:
            loop.call_soon_threadsafe(self.emit, type_, **data)

    def log(self, level: str, text: str, **extra: Any) -> None:
        getattr(log, level if level != "warn" else "warning", log.info)(text)
        self.emit("log", level=level, text=text, **extra)
