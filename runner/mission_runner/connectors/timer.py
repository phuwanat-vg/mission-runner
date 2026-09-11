"""Timer event sources: cron, interval, boot."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from ..model import EventSource
from ..types import StepFailed
from .base import Armed, Connector, EventCallback

try:
    from croniter import croniter
except ImportError:  # pragma: no cover
    croniter = None  # type: ignore[assignment]


class TimerConnector(Connector):
    type = "timer"
    source_types = ("timer.cron", "timer.interval", "timer.boot")
    singleton = True

    def __init__(self, name: str = "timer", config: dict[str, Any] | None = None):
        super().__init__(name, config)
        self._tasks: set[asyncio.Task[None]] = set()
        self.boot_mono = time.monotonic()

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()

    async def arm(self, source: EventSource, callback: EventCallback) -> Armed:
        p = source.params
        if source.type == "timer.cron":
            if croniter is None:
                raise StepFailed("timer.cron needs the 'croniter' package")
            expr = str(p.get("cron", "")).strip()
            if not croniter.is_valid(expr):
                raise StepFailed(f"invalid cron expression '{expr}'")
            coro = self._cron_loop(expr, callback)
        elif source.type == "timer.interval":
            seconds = float(p.get("seconds", 0))
            if seconds <= 0:
                raise StepFailed("timer.interval needs seconds > 0")
            coro = self._interval_loop(seconds, callback)
        elif source.type == "timer.boot":
            coro = self._boot(float(p.get("delay_s", 0) or 0), callback)
        else:
            raise StepFailed(f"timer cannot arm '{source.type}'")
        task = asyncio.create_task(coro, name=f"timer:{source.type}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        def disarm() -> None:
            task.cancel()

        return Armed(disarm, source.summary())

    async def _cron_loop(self, expr: str, callback: EventCallback) -> None:
        while True:
            now = datetime.now()
            nxt = croniter(expr, now).get_next(datetime)
            await asyncio.sleep(max(0.0, (nxt - now).total_seconds()))
            callback({"time": datetime.now().isoformat(timespec="seconds"), "cron": expr})

    async def _interval_loop(self, seconds: float, callback: EventCallback) -> None:
        count = 0
        while True:
            await asyncio.sleep(seconds)
            count += 1
            callback({"time": datetime.now().isoformat(timespec="seconds"), "count": count})

    async def _boot(self, delay_s: float, callback: EventCallback) -> None:
        # "Boot" is relative to runner start, so a mission deployed later does not fire.
        elapsed = time.monotonic() - self.boot_mono
        remaining = delay_s - elapsed
        if remaining < -5.0:
            return
        await asyncio.sleep(max(0.0, remaining))
        callback({"time": datetime.now().isoformat(timespec="seconds"), "delay_s": delay_s})
