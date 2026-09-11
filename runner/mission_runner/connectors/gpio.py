"""Raspberry Pi GPIO (gpiozero): buttons and e-stop inputs as event sources,
outputs as a sink. Unavailable (but harmless) on other machines."""

from __future__ import annotations

import logging
from typing import Any

from ..model import EventSource
from ..types import StepFailed
from .base import Armed, Connector, EventCallback

log = logging.getLogger("mission.gpio")

try:
    from gpiozero import Button, OutputDevice
    from gpiozero.exc import BadPinFactory, GPIOZeroError

    _IMPORT_ERROR = ""
except Exception as _e:  # noqa: BLE001 - gpiozero raises on non-Pi hosts at import time in some versions
    Button = None  # type: ignore[assignment, misc]
    OutputDevice = None  # type: ignore[assignment, misc]
    BadPinFactory = GPIOZeroError = Exception  # type: ignore[assignment, misc]
    _IMPORT_ERROR = str(_e) or "gpiozero is not installed"


class GpioConnector(Connector):
    type = "gpio"
    source_types = ("gpio.input",)
    singleton = True

    def __init__(self, name: str = "gpio", config: dict[str, Any] | None = None):
        super().__init__(name, config)
        self._inputs: dict[int, Any] = {}
        self._outputs: dict[int, Any] = {}
        self._reason = _IMPORT_ERROR
        if Button is not None:
            try:  # Probe the pin factory once so status shows the truth on non-Pi hosts.
                from gpiozero.devices import Device

                Device.pin_factory  # noqa: B018 - property access triggers factory creation
                if Device.pin_factory is None:
                    self._reason = "no GPIO pin factory (not a Raspberry Pi?)"
            except Exception as e:  # noqa: BLE001
                self._reason = f"no GPIO pin factory: {e}"

    @property
    def available(self) -> bool:
        return Button is not None and not self._reason

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    async def stop(self) -> None:
        for d in [*self._inputs.values(), *self._outputs.values()]:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass
        self._inputs.clear()
        self._outputs.clear()

    async def arm(self, source: EventSource, callback: EventCallback) -> Armed:
        if not self.available:
            raise StepFailed(f"gpio unavailable: {self._reason}")
        p = source.params
        pin = int(p.get("pin", -1))
        if pin < 0:
            raise StepFailed("gpio.input needs a pin")
        edge = str(p.get("gpio_edge", "falling"))
        pull = str(p.get("pull", "up"))
        bounce = float(p.get("bounce_s", 0.05) or 0)
        try:
            btn = self._inputs.get(pin)
            if btn is None:
                btn = Button(pin, pull_up=(pull == "up"), bounce_time=bounce or None)  # type: ignore[misc]
                self._inputs[pin] = btn
        except (GPIOZeroError, Exception) as e:  # noqa: BLE001
            raise StepFailed(f"gpio pin {pin}: {e}") from None

        def pressed() -> None:  # active (pulled to the "pressed" level)
            callback({"pin": pin, "value": 0 if pull == "up" else 1, "edge": "falling" if pull == "up" else "rising"})

        def released() -> None:
            callback({"pin": pin, "value": 1 if pull == "up" else 0, "edge": "rising" if pull == "up" else "falling"})

        # With pull-up, "pressed" is the falling edge; with pull-down it is rising.
        want_pressed = (edge == "falling") == (pull == "up")
        handlers: list[tuple[str, Any]] = []
        if edge == "both" or want_pressed:
            handlers.append(("when_pressed", pressed))
        if edge == "both" or not want_pressed:
            handlers.append(("when_released", released))
        for attr, fn in handlers:
            _add_handler(btn, attr, fn)

        def disarm() -> None:
            for attr, fn in handlers:
                _remove_handler(btn, attr, fn)

        return Armed(disarm, source.summary())

    async def write(self, kind: str, address: int, value: Any) -> None:
        if not self.available:
            raise StepFailed(f"gpio unavailable: {self._reason}")
        dev = self._outputs.get(address)
        if dev is None:
            try:
                dev = OutputDevice(address)  # type: ignore[misc]
            except Exception as e:  # noqa: BLE001
                raise StepFailed(f"gpio pin {address}: {e}") from None
            self._outputs[address] = dev
        if value in (True, 1, "1", "on", "high", "true"):
            dev.on()
        else:
            dev.off()


# gpiozero exposes a single callback per event; multiplex so several triggers can share a pin.
def _add_handler(btn: Any, attr: str, fn: Any) -> None:
    lst: list[Any] = getattr(btn, f"_mr_{attr}", None) or []
    lst.append(fn)
    setattr(btn, f"_mr_{attr}", lst)

    def fanout() -> None:
        for f in list(lst):
            try:
                f()
            except Exception:  # noqa: BLE001
                log.exception("gpio handler failed")

    setattr(btn, attr, fanout)


def _remove_handler(btn: Any, attr: str, fn: Any) -> None:
    lst: list[Any] = getattr(btn, f"_mr_{attr}", None) or []
    if fn in lst:
        lst.remove(fn)
    if not lst:
        setattr(btn, attr, None)
