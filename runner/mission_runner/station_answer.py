"""``station_answer``: answers ``ros.request`` questions for one station without
writing code (docs/robot-startup.md, section 3).

``ros2 run mission_runner station_answer --ros-args -p station:="Conveyor 1" -p mode:=auto``

:class:`StationAnswerer` holds all the decisions (which requests to take, what
to answer, when, and only once per id) and needs no ROS, so it is tested on its
own. :class:`StationNode` only wires it to rclpy topics, a Trigger client or
gpiozero buttons.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any

MODES = ("auto", "topic", "service", "gpio")
SOURCE_TYPES = ("std_msgs/msg/Bool", "std_msgs/msg/String", "std_msgs/msg/Int32", "std_msgs/msg/Float32")
MAX_PENDING = 100


def slug(name: str) -> str:
    """Lowercase; anything but ``[a-z0-9_]`` becomes ``_``."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return bool(v)


@dataclass
class StationConfig:
    station: str = ""
    request_topic: str = ""
    answer_topic: str = ""
    match_station: bool = False
    mode: str = "auto"
    answer: str = ""
    delay_s: float = 0.0
    source_topic: str = ""
    source_type: str = "std_msgs/msg/Bool"
    true_answer: str = "OK"
    false_answer: str = "Reject"
    max_age_s: float = 0.0
    service: str = ""
    gpio_pins: list[int] = field(default_factory=list)
    gpio_answers: list[str] = field(default_factory=list)
    by: str = ""

    @classmethod
    def from_params(cls, params: Mapping[str, Any], node_name: str = "station_answer") -> StationConfig:
        """Defaults applied, types coerced; raises ``ValueError`` listing every problem."""
        errors: list[str] = []
        c = cls()
        for f in fields(cls):
            v = params.get(f.name)
            if v is None:
                continue
            try:
                if f.name in ("match_station",):
                    setattr(c, f.name, _as_bool(v))
                elif f.name in ("delay_s", "max_age_s"):
                    setattr(c, f.name, float(v))
                elif f.name == "gpio_pins":
                    setattr(c, f.name, [int(x) for x in v])
                elif f.name == "gpio_answers":
                    setattr(c, f.name, [str(x) for x in v])
                else:
                    setattr(c, f.name, str(v))
            except (TypeError, ValueError):
                errors.append(f"parameter {f.name}: invalid value {v!r}")
        c.station = c.station.strip()
        if c.station:
            c.request_topic = c.request_topic or f"/station/{slug(c.station)}/request"
            c.answer_topic = c.answer_topic or f"/station/{slug(c.station)}/answer"
        else:
            c.request_topic = c.request_topic or "/iviz/request"
            c.answer_topic = c.answer_topic or "/iviz/answer"
        c.by = c.by or node_name
        if c.source_type in ("Bool", "String", "Int32", "Float32"):
            c.source_type = f"std_msgs/msg/{c.source_type}"
        if c.mode not in MODES:
            errors.append(f"mode must be one of {', '.join(MODES)}, not '{c.mode}'")
        if c.delay_s < 0 or c.max_age_s < 0:
            errors.append("delay_s and max_age_s must not be negative")
        if c.mode == "topic":
            if not c.source_topic:
                errors.append("mode topic needs source_topic")
            if c.source_type not in SOURCE_TYPES:
                errors.append(f"source_type must be one of {', '.join(SOURCE_TYPES)}")
        if c.mode == "service" and not c.service:
            errors.append("mode service needs service (a std_srvs/srv/Trigger service name)")
        if c.mode == "gpio":
            if not c.gpio_pins:
                errors.append("mode gpio needs gpio_pins")
            elif len(c.gpio_answers) != len(c.gpio_pins):
                errors.append("gpio_answers needs one answer per pin in gpio_pins")
        if c.match_station and not c.station:
            errors.append("match_station needs station")
        if errors:
            raise ValueError("; ".join(errors))
        return c


@dataclass
class Request:
    id: str
    text: str = ""
    options: list[str] = field(default_factory=list)
    default: str | None = None
    station: str | None = None
    timeout_s: float | None = None
    received: float = 0.0


@dataclass
class Action:
    """What the node must do: ``publish`` ``message`` on the answer topic, or
    ``call`` the Trigger service for ``request_id``."""

    kind: str
    request_id: str
    message: dict[str, Any] | None = None


def parse_request(data: str, now: float = 0.0) -> Request | None:
    """A request JSON string, or None when it is not one or has no ``id``."""
    try:
        doc = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("id") in (None, ""):
        return None
    options = doc.get("options") if isinstance(doc.get("options"), list) else []
    timeout = doc.get("timeout_s")
    return Request(
        id=str(doc["id"]),
        text=str(doc.get("text") or ""),
        options=[str(o) for o in options],
        default=None if doc.get("default") is None else str(doc["default"]),
        station=None if doc.get("station") in (None, "") else str(doc["station"]),
        timeout_s=float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0 else None,
        received=now,
    )


class StationAnswerer:
    """Every decision of a station answer node, without ROS."""

    def __init__(self, config: StationConfig, clock: Callable[[], float] = time.monotonic):
        self.cfg = config
        self.clock = clock
        self.pending: OrderedDict[str, Request] = OrderedDict()
        self._done: deque[str] = deque(maxlen=1000)
        self._done_set: set[str] = set()
        self._due: dict[str, float] = {}
        self._retry: dict[str, float] = {}
        self._value: Any = None
        self._value_t: float | None = None

    # ----- choices -------------------------------------------------------------------

    def matches(self, req: Request) -> bool:
        if not self.cfg.match_station:
            return True
        return req.station is not None and (req.station == self.cfg.station or slug(req.station) == slug(self.cfg.station))

    def auto_answer(self, req: Request) -> str:
        """``answer``, else the request's default, else its first option, else ``OK``."""
        if self.cfg.answer:
            return self.cfg.answer
        if req.default is not None:
            return req.default
        return req.options[0] if req.options else "OK"

    def value_answer(self, value: Any) -> str | None:
        """Bool and numbers (non-zero = true) map to true/false_answer; a String passes through."""
        if isinstance(value, bool):
            return self.cfg.true_answer if value else self.cfg.false_answer
        if isinstance(value, (int, float)):
            return self.cfg.true_answer if value != 0 else self.cfg.false_answer
        if isinstance(value, str):
            return value if value != "" else None
        return None

    def fresh_answer(self) -> str | None:
        """The answer the last source value gives, or None when there is none or it is stale."""
        if self._value_t is None:
            return None
        if self.cfg.max_age_s > 0 and self.clock() - self._value_t > self.cfg.max_age_s:
            return None
        return self.value_answer(self._value)

    # ----- inputs ----------------------------------------------------------------------

    def on_request(self, data: str) -> list[Action]:
        req = parse_request(data, self.clock())
        if req is None or not self.matches(req) or req.id in self._done_set or req.id in self.pending:
            return []
        self.pending[req.id] = req
        while len(self.pending) > MAX_PENDING:
            old, _ = self.pending.popitem(last=False)
            self._forget(old)
        mode = self.cfg.mode
        if mode == "auto":
            if self.cfg.delay_s <= 0:
                return self._answer_one(req.id, self.auto_answer(req))
            self._due[req.id] = self.clock() + self.cfg.delay_s
            return []
        if mode == "topic":
            ans = self.fresh_answer()
            return self._answer_one(req.id, ans) if ans is not None else []
        if mode == "service":
            return [Action("call", req.id)]
        return []  # gpio: wait for a press

    def on_value(self, value: Any) -> list[Action]:
        """A source topic message arrived (its ``data``)."""
        if self.value_answer(value) is None:
            return []
        self._value, self._value_t = value, self.clock()
        if self.cfg.mode != "topic":
            return []
        ans = self.value_answer(value)
        return [a for rid in list(self.pending) for a in self._answer_one(rid, ans)]  # type: ignore[arg-type]

    def on_press(self, index: int) -> list[Action]:
        """Button ``index`` (position in gpio_pins) was pressed: answers what is waiting."""
        if self.cfg.mode != "gpio" or not 0 <= index < len(self.cfg.gpio_answers):
            return []
        ans = self.cfg.gpio_answers[index]
        return [a for rid in list(self.pending) for a in self._answer_one(rid, ans)]

    def on_service_result(self, request_id: str, success: bool | None) -> list[Action]:
        """Trigger result; None = the service was not reachable, try again in 1 s."""
        if request_id not in self.pending:
            return []
        if success is None:
            self._retry[request_id] = self.clock() + 1.0
            return []
        return self._answer_one(request_id, self.cfg.true_answer if success else self.cfg.false_answer)

    def on_timer(self) -> list[Action]:
        """Call about every 0.1 s: delayed auto answers, service retries, expired requests."""
        now = self.clock()
        out: list[Action] = []
        for rid, req in list(self.pending.items()):
            if req.timeout_s is not None and now - req.received > req.timeout_s + 1.0:
                self.pending.pop(rid, None)
                self._forget(rid)
                self._mark_done(rid)
        for rid, due in list(self._due.items()):
            if now >= due and rid in self.pending:
                out += self._answer_one(rid, self.auto_answer(self.pending[rid]))
        for rid, due in list(self._retry.items()):
            if now >= due and rid in self.pending:
                del self._retry[rid]
                out.append(Action("call", rid))
        return out

    # ----- answers -------------------------------------------------------------------------

    def _forget(self, rid: str) -> None:
        self._due.pop(rid, None)
        self._retry.pop(rid, None)

    def _mark_done(self, rid: str) -> None:
        if len(self._done) == self._done.maxlen:
            self._done_set.discard(self._done[0])
        self._done.append(rid)
        self._done_set.add(rid)

    def _answer_one(self, rid: str, answer: str) -> list[Action]:
        req = self.pending.pop(rid, None)
        if req is None or rid in self._done_set:
            return []
        self._forget(rid)
        self._mark_done(rid)
        msg: dict[str, Any] = {"id": rid, "answer": answer, "by": self.cfg.by}
        station = self.cfg.station or req.station
        if station:
            msg["station"] = station
        return [Action("publish", rid, msg)]


# ----- ROS node ----------------------------------------------------------------------------------


class StationNode:  # pragma: no cover - needs rclpy
    """Wires a StationAnswerer to rclpy (and gpiozero in gpio mode)."""

    def __init__(self, node: Any, cfg: StationConfig):
        from std_msgs.msg import String

        self.node = node
        self.cfg = cfg
        self.String = String
        self.logic = StationAnswerer(cfg)
        self.lock = threading.RLock()
        self.buttons: list[Any] = []
        self.client: Any = None
        self.pub = node.create_publisher(String, cfg.answer_topic, 10)
        node.create_subscription(String, cfg.request_topic, lambda m: self._run(self.logic.on_request, m.data), 10)
        node.create_timer(0.1, lambda: self._run(self.logic.on_timer))
        if cfg.mode == "topic":
            import std_msgs.msg as std

            cls = getattr(std, cfg.source_type.rsplit("/", 1)[-1])
            node.create_subscription(cls, cfg.source_topic, lambda m: self._run(self.logic.on_value, m.data), 10)
        elif cfg.mode == "service":
            from std_srvs.srv import Trigger

            self.Trigger = Trigger
            self.client = node.create_client(Trigger, cfg.service)
        elif cfg.mode == "gpio":
            from gpiozero import Button  # lazy: only gpio stations need it

            for i, pin in enumerate(cfg.gpio_pins):
                b = Button(pin, pull_up=True, bounce_time=0.05)
                b.when_pressed = lambda i=i: self._run(self.logic.on_press, i)
                self.buttons.append(b)
        source = {"auto": f"answer '{cfg.answer or '<default>'}' after {cfg.delay_s:g} s", "topic": f"{cfg.source_topic} ({cfg.source_type})", "service": cfg.service, "gpio": f"pins {cfg.gpio_pins} -> {cfg.gpio_answers}"}[cfg.mode]
        node.get_logger().info(f"station '{cfg.station or '-'}': {cfg.request_topic} -> {cfg.answer_topic}, mode {cfg.mode}: {source}")

    def _run(self, fn: Callable[..., list[Action]], *args: Any) -> None:
        with self.lock:
            for action in fn(*args):
                self._do(action)

    def _do(self, action: Action) -> None:
        if action.kind == "publish" and action.message is not None:
            self.pub.publish(self.String(data=json.dumps(action.message)))
            self.node.get_logger().info(f"answered {action.request_id}: {action.message['answer']}")
        elif action.kind == "call":
            if not self.client.service_is_ready():
                self.logic.on_service_result(action.request_id, None)
                return
            future = self.client.call_async(self.Trigger.Request())
            future.add_done_callback(lambda f, rid=action.request_id: self._run(self.logic.on_service_result, rid, self._success(f)))

    def _success(self, future: Any) -> bool | None:
        try:
            return bool(future.result().success)
        except Exception as e:  # noqa: BLE001
            self.node.get_logger().warning(f"{self.cfg.service} failed: {e}")
            return None


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs rclpy
    try:
        import rclpy
        from rcl_interfaces.msg import ParameterDescriptor
    except ImportError as e:
        print(f"station_answer needs ROS 2 (rclpy): {e}")
        return 1
    rclpy.init(args=sys.argv if argv is None else argv)
    node = rclpy.create_node("station_answer")
    dyn = ParameterDescriptor(dynamic_typing=True)
    values: dict[str, Any] = {}
    for f in fields(StationConfig):
        default = f.default_factory() if callable(f.default_factory) else f.default  # type: ignore[misc]
        node.declare_parameter(f.name, default, dyn)
        v = node.get_parameter(f.name).value
        values[f.name] = list(v) if isinstance(v, (list, tuple)) or type(v).__name__ == "array" else v
    rc = 0
    try:
        cfg = StationConfig.from_params(values, node.get_name())
        StationNode(node, cfg)
        rclpy.spin(node)
    except ValueError as e:
        node.get_logger().error(str(e))
        rc = 1
    except ImportError as e:
        node.get_logger().error(f"{e} (gpio mode needs gpiozero: sudo apt install python3-gpiozero)")
        rc = 1
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
