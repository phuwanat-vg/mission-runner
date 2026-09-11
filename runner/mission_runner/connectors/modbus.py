"""Modbus TCP connector (pymodbus 3.x): polled coils/discrete inputs/registers
as event sources, coil/register writes as a sink. Typical use: a PLC flag
starts a job, the robot sets a flag when done."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from ..model import EventSource
from ..types import StepFailed
from .base import Armed, Connector, EventCallback

log = logging.getLogger("mission.modbus")

try:
    from pymodbus.client import AsyncModbusTcpClient
except ImportError:  # pragma: no cover
    AsyncModbusTcpClient = None  # type: ignore[assignment, misc]


class ModbusConnector(Connector):
    type = "modbus_tcp"
    source_types = ("modbus.poll",)

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        super().__init__(name, config)
        self._client: Any = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return AsyncModbusTcpClient is not None

    @property
    def unavailable_reason(self) -> str:
        return "" if AsyncModbusTcpClient is not None else "python package 'pymodbus' is not installed"

    @property
    def connected(self) -> bool:
        return bool(self._client is not None and getattr(self._client, "connected", False))

    async def start(self) -> None:
        if AsyncModbusTcpClient is None:
            log.warning("connector %s: pymodbus not installed", self.name)
            return
        self._client = AsyncModbusTcpClient(str(self.config.get("host", "127.0.0.1")), port=int(self.config.get("port", 502)), timeout=float(self.config.get("timeout_s", 2.0)))
        try:
            await self._client.connect()
        except Exception as e:  # noqa: BLE001
            log.warning("connector %s: connect failed: %s", self.name, e)

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        if self._client is not None:
            try:
                r = self._client.close()
                if inspect.isawaitable(r):
                    await r
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _ensure(self) -> Any:
        if self._client is None:
            raise StepFailed(f"connector '{self.name}' is not started")
        if not getattr(self._client, "connected", False):
            try:
                await self._client.connect()
            except Exception as e:  # noqa: BLE001
                raise StepFailed(f"modbus '{self.name}': {e}") from None
            if not getattr(self._client, "connected", False):
                raise StepFailed(f"modbus '{self.name}' is not connected")
        return self._client

    def _unit(self) -> int:
        return int(self.config.get("unit", self.config.get("slave", 1)))

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a pymodbus client method, tolerating the slave=/unit= rename."""
        client = await self._ensure()
        fn = getattr(client, name)
        async with self._lock:
            for kw in ("slave", "unit", "device_id"):
                try:
                    rr = await fn(*args, **{kw: self._unit()}, **kwargs)
                    break
                except TypeError:
                    continue
            else:
                rr = await fn(*args, **kwargs)
        if hasattr(rr, "isError") and rr.isError():
            raise StepFailed(f"modbus '{self.name}' {name} failed: {rr}")
        return rr

    async def read(self, kind: str, address: int) -> Any:
        if kind == "coil":
            rr = await self._call("read_coils", address, count=1)
            return bool(rr.bits[0])
        if kind == "discrete":
            rr = await self._call("read_discrete_inputs", address, count=1)
            return bool(rr.bits[0])
        if kind == "register":
            rr = await self._call("read_holding_registers", address, count=1)
            return int(rr.registers[0])
        raise StepFailed(f"unknown modbus kind '{kind}'")

    async def write(self, kind: str, address: int, value: Any) -> None:
        if kind == "coil":
            await self._call("write_coil", address, bool(value))
            return
        if kind == "register":
            await self._call("write_register", address, int(value) & 0xFFFF)
            return
        raise StepFailed(f"cannot write modbus kind '{kind}'")

    async def arm(self, source: EventSource, callback: EventCallback) -> Armed:
        if source.type != "modbus.poll":
            raise StepFailed(f"modbus cannot arm '{source.type}'")
        p = source.params
        kind = str(p.get("kind", "coil"))
        address = int(p.get("address", 0))
        poll_s = max(0.05, float(p.get("poll_s", 0.5) or 0.5))
        task = asyncio.create_task(self._poll(kind, address, poll_s, callback), name=f"modbus:{self.name}:{kind}:{address}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return Armed(task.cancel, source.summary())

    async def _poll(self, kind: str, address: int, poll_s: float, callback: EventCallback) -> None:
        errors = 0
        while True:
            try:
                value = await self.read(kind, address)
                errors = 0
                callback({"value": value, "kind": kind, "address": address})
            except StepFailed as e:
                errors += 1
                if errors in (1, 10, 100):
                    log.warning("modbus %s poll %s %s: %s", self.name, kind, address, e)
            await asyncio.sleep(poll_s if errors == 0 else min(10.0, poll_s * (1 + errors)))
