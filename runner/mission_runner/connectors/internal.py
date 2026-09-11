"""Internal event sources fed by the runner itself: ``mission.done`` and
``http.webhook`` (the HTTP server pushes into :meth:`InternalConnector.push`)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..model import EventSource
from ..types import StepFailed
from .base import Armed, Connector, EventCallback


class InternalConnector(Connector):
    type = "internal"
    source_types = ("mission.done", "http.webhook")
    singleton = True

    def __init__(self, name: str = "internal", config: dict[str, Any] | None = None):
        super().__init__(name, config)
        self._done: dict[str, list[tuple[str, EventCallback]]] = defaultdict(list)
        self._hooks: dict[str, list[EventCallback]] = defaultdict(list)

    async def arm(self, source: EventSource, callback: EventCallback) -> Armed:
        p = source.params
        if source.type == "mission.done":
            mission = str(p.get("mission", ""))
            want = str(p.get("result", "any"))
            entry = (want, callback)
            self._done[mission].append(entry)

            def disarm() -> None:
                try:
                    self._done[mission].remove(entry)
                except ValueError:
                    pass

            return Armed(disarm, source.summary())
        if source.type == "http.webhook":
            path = str(p.get("path", "")).strip("/")
            if not path:
                raise StepFailed("http.webhook needs a path")
            self._hooks[path].append(callback)

            def disarm_hook() -> None:
                try:
                    self._hooks[path].remove(callback)
                except ValueError:
                    pass

            return Armed(disarm_hook, source.summary())
        raise StepFailed(f"internal connector cannot arm '{source.type}'")

    # ----- fed by the runner --------------------------------------------------

    def mission_done(self, mission: str, run_id: str, result: str) -> None:
        for want, cb in list(self._done.get(mission, ())):
            if want == "any" or want == result:
                cb({"mission": mission, "run_id": run_id, "result": result})

    def webhook_paths(self) -> list[str]:
        return [p for p, cbs in self._hooks.items() if cbs]

    def push_webhook(self, path: str, payload: dict[str, Any]) -> int:
        """Deliver a webhook. Returns the number of listeners."""
        cbs = list(self._hooks.get(path.strip("/"), ()))
        for cb in cbs:
            cb(payload)
        return len(cbs)


class SimTopicConnector(Connector):
    """Stands in for ROS topics in ``--sim`` mode: ``ros.topic`` triggers are
    armed here and messages are injected with ``POST /api/sim/topic``."""

    type = "sim_topics"
    source_types = ("ros.topic",)
    singleton = True

    def __init__(self, name: str = "ros", config: dict[str, Any] | None = None):
        super().__init__(name, config)
        self._subs: dict[str, list[EventCallback]] = defaultdict(list)
        self.last: dict[str, dict[str, Any]] = {}

    async def arm(self, source: EventSource, callback: EventCallback) -> Armed:
        topic = str(source.params.get("topic", ""))
        if not topic:
            raise StepFailed("ros.topic needs a topic")
        self._subs[topic].append(callback)

        def disarm() -> None:
            try:
                self._subs[topic].remove(callback)
            except ValueError:
                pass

        return Armed(disarm, source.summary())

    def push(self, topic: str, payload: dict[str, Any]) -> int:
        self.last[topic] = payload
        cbs = list(self._subs.get(topic, ()))
        for cb in cbs:
            cb(dict(payload))
        return len(cbs)

    def topics(self) -> list[str]:
        return [t for t, cbs in self._subs.items() if cbs]
