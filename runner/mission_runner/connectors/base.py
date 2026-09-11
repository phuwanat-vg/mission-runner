"""Connector interface and registry."""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Awaitable, Callable
from typing import Any

from ..model import EventSource
from ..types import StepFailed

log = logging.getLogger("mission.connectors")

#: Called (from any thread) with the event payload.
EventCallback = Callable[[dict[str, Any]], None]


class Armed:
    """Handle returned by :meth:`Connector.arm`; call :meth:`disarm` to stop."""

    def __init__(self, disarm: Callable[[], Awaitable[None] | None], description: str = ""):
        self._disarm = disarm
        self.description = description
        self._done = False

    async def disarm(self) -> None:
        if self._done:
            return
        self._done = True
        r = self._disarm()
        if r is not None:
            await r


class Connector(ABC):
    """A named integration. Subclasses declare which event-source types they
    can arm and implement the sinks they support."""

    #: connector type as written in connectors.yaml
    type: str = "abstract"
    #: event source types this connector can arm (e.g. ("mqtt.subscribe",))
    source_types: tuple[str, ...] = ()
    #: True for singletons that are looked up by source type (timer, ros, gpio)
    singleton: bool = False

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = dict(config or {})

    async def start(self) -> None:  # noqa: B027
        pass

    async def stop(self) -> None:  # noqa: B027
        pass

    @property
    def connected(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        """False when a required library or device is missing."""
        return True

    @property
    def unavailable_reason(self) -> str:
        return ""

    def public_config(self) -> dict[str, Any]:
        """Config with secrets removed."""
        return {k: v for k, v in self.config.items() if not any(s in k.lower() for s in ("pass", "secret", "token", "key"))}

    def status(self) -> dict[str, Any]:
        return {"type": self.type, "connected": self.connected, "available": self.available, "reason": self.unavailable_reason, "config": self.public_config()}

    async def arm(self, source: EventSource, callback: EventCallback) -> Armed:
        raise StepFailed(f"connector '{self.name}' cannot arm '{source.type}'")

    # ----- sinks (optional) ---------------------------------------------------

    async def publish(self, topic: str, payload: Any, qos: int = 1, retain: bool = False) -> None:
        raise StepFailed(f"connector '{self.name}' ({self.type}) cannot publish")

    async def write(self, kind: str, address: int, value: Any) -> None:
        raise StepFailed(f"connector '{self.name}' ({self.type}) cannot write")


class ConnectorRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, Connector] = {}
        self._by_source_type: dict[str, Connector] = {}

    def add(self, connector: Connector) -> None:
        self._by_name[connector.name] = connector
        if connector.singleton:
            for t in connector.source_types:
                self._by_source_type[t] = connector

    def get(self, name: str) -> Connector | None:
        return self._by_name.get(name)

    def require(self, name: str) -> Connector:
        c = self._by_name.get(name)
        if c is None:
            raise StepFailed(f"connector '{name}' is not configured")
        if not c.available:
            raise StepFailed(f"connector '{name}' is unavailable: {c.unavailable_reason}")
        return c

    def for_source(self, source: EventSource) -> Connector:
        name = source.connector
        if name:
            return self.require(name)
        c = self._by_source_type.get(source.type)
        if c is None:
            raise StepFailed(f"no connector can arm '{source.type}'")
        if not c.available:
            raise StepFailed(f"'{source.type}' is unavailable: {c.unavailable_reason}")
        return c

    def names(self) -> list[str]:
        return [n for n, c in self._by_name.items() if not c.singleton]

    def all(self) -> list[Connector]:
        return list(self._by_name.values())

    def status(self) -> dict[str, dict[str, Any]]:
        return {n: c.status() for n, c in self._by_name.items() if not c.singleton}

    def trigger_capabilities(self) -> dict[str, dict[str, Any]]:
        caps: dict[str, dict[str, Any]] = {}
        for c in self._by_name.values():
            for t in c.source_types:
                if c.singleton or t not in caps:
                    caps[t] = {"available": c.available, "reason": c.unavailable_reason}
        return caps

    async def start_all(self) -> None:
        for c in self._by_name.values():
            try:
                await c.start()
            except Exception as e:  # noqa: BLE001 - one bad connector must not stop the runner
                log.warning("connector %s failed to start: %s", c.name, e)

    async def stop_all(self) -> None:
        for c in reversed(list(self._by_name.values())):
            try:
                await c.stop()
            except Exception:  # noqa: BLE001
                log.exception("connector %s failed to stop", c.name)
