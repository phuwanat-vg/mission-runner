"""Arms event sources through connectors and applies the ``when`` / ``edge`` /
``debounce_s`` filters. Used for mission triggers, interrupts and the
``wait_event`` step."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .connectors.base import Armed, ConnectorRegistry
from .events import EventBus
from .expressions import ExpressionError, evaluate
from .model import EventSource, TriggerSpec
from .types import StepFailed, StepTimeout

log = logging.getLogger("mission.triggers")

FireCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class TriggerManager:
    def __init__(self, registry: ConnectorRegistry, events: EventBus, scope_provider: Callable[[], dict[str, Any]]):
        self.registry = registry
        self.events = events
        self.scope_provider = scope_provider
        self.loop: asyncio.AbstractEventLoop | None = None
        self._armed: dict[str, list[tuple[TriggerSpec, Armed]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # ----- low level ------------------------------------------------------------

    async def arm(self, source: EventSource, callback: FireCallback, *, label: str = "") -> Armed:
        connector = self.registry.for_source(source)
        state = {"last": False, "last_fire": -1e9}
        loop = self.loop or asyncio.get_running_loop()

        def on_event(payload: dict[str, Any]) -> None:
            # Connectors may call from their own threads.
            loop.call_soon_threadsafe(self._handle, source, state, payload, callback, label)

        armed = await connector.arm(source, on_event)
        log.info("armed %s%s", source.summary(), f" [{label}]" if label else "")
        return armed

    def _handle(self, source: EventSource, state: dict[str, Any], payload: dict[str, Any], callback: FireCallback, label: str) -> None:
        if not isinstance(payload, dict):
            payload = {"value": payload}
        ok = True
        if source.when:
            scope = {**self.scope_provider(), "payload": payload}
            try:
                ok = bool(evaluate(source.when, scope))
            except ExpressionError as e:
                self.events.log("warn", f"trigger '{label or source.summary()}': {e}")
                ok = False
        if source.edge == "rising":
            fire = ok and not state["last"]
            state["last"] = ok
        else:
            fire = ok
        if not fire:
            return
        now = time.monotonic()
        if source.debounce_s > 0 and now - state["last_fire"] < source.debounce_s:
            return
        state["last_fire"] = now
        try:
            r = callback(payload)
        except Exception:  # noqa: BLE001
            log.exception("trigger callback failed (%s)", label)
            return
        if inspect.isawaitable(r):
            task = asyncio.ensure_future(r)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ----- mission level --------------------------------------------------------

    async def arm_specs(self, key: str, specs: list[TriggerSpec], on_fire: Callable[[TriggerSpec, dict[str, Any]], Awaitable[None] | None]) -> list[str]:
        """Arm a list of triggers under ``key`` (mission name or "<name>:interrupts").
        Returns human-readable problems for specs that could not be armed."""
        await self.disarm(key)
        problems: list[str] = []
        armed: list[tuple[TriggerSpec, Armed]] = []
        for spec in specs:
            if not spec.enabled:
                continue

            def make_cb(s: TriggerSpec) -> FireCallback:
                return lambda payload: on_fire(s, payload)

            try:
                a = await self.arm(spec.as_source(), make_cb(spec), label=f"{key}/{spec.id}")
                armed.append((spec, a))
            except StepFailed as e:
                problems.append(f"{spec.id}: {e}")
                self.events.log("warn", f"{key}: trigger '{spec.id}' not armed: {e}")
            except Exception as e:  # noqa: BLE001
                problems.append(f"{spec.id}: {e}")
                log.exception("arming %s/%s failed", key, spec.id)
        self._armed[key] = armed
        return problems

    async def disarm(self, key: str) -> None:
        for _spec, a in self._armed.pop(key, []):
            try:
                await a.disarm()
            except Exception:  # noqa: BLE001
                log.exception("disarm failed for %s", key)

    async def disarm_all(self) -> None:
        for key in list(self._armed):
            await self.disarm(key)

    def armed_summary(self) -> dict[str, list[str]]:
        return {k: [s.summary() for s, _ in v] for k, v in self._armed.items()}

    # ----- wait_event -----------------------------------------------------------

    async def wait_event(self, source: EventSource, timeout_s: float | None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()

        def fire(payload: dict[str, Any]) -> None:
            if not fut.done():
                fut.set_result(payload)

        armed = await self.arm(source, fire, label="wait_event")
        try:
            if timeout_s:
                try:
                    return await asyncio.wait_for(fut, timeout=timeout_s)
                except asyncio.TimeoutError:
                    raise StepTimeout(f"no event from {source.summary()} within {timeout_s:g} s") from None
            return await fut
        finally:
            await armed.disarm()
