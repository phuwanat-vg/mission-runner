"""Two ways to see what a mission will do before the robot moves.

* :func:`plan_route` asks the real planner for the path between every
  navigation step, so the editor can draw the route the robot would actually
  drive instead of straight lines, with distance and a time estimate.
* :class:`DryRun` replays the whole flow - branches, loops, retries and all -
  against a private simulated robot seeded at the current pose, and returns a
  timeline the editor can animate. Nothing reaches the real robot, the
  connectors or the run log.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .backends.base import Pose, path_length
from .backends.sim import SimBackend
from .connectors.base import Armed, Connector, ConnectorRegistry
from .events import EventBus
from .model import EventSource, Mission, Step
from .types import Run, RunSource, RunStatus, StepFailed

if TYPE_CHECKING:
    from .runner import Runner

log = logging.getLogger("mission.preview")

#: Steps that move the robot along a route, in the order the editor draws them.
ROUTE_STEPS = ("nav.go_to_pose", "nav.go_through_poses", "nav.follow_waypoints", "nav.follow_path")
#: Rough fixed costs, seconds, for steps that take time but cover no distance.
FIXED_COST_S = {"nav.dock": 25.0, "nav.undock": 8.0, "nav.spin": 4.0, "nav.change_map": 3.0, "nav.clear_costmap": 1.0, "ask_user": 0.0}


# ---------------------------------------------------------------------------
# route preview


@dataclass
class Leg:
    """One planned move between two poses."""

    step_id: str
    step_type: str
    index: int
    start: dict[str, Any]
    goal: dict[str, Any]
    path: dict[str, Any] | None = None
    length_m: float = 0.0
    planned: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "step_type": self.step_type,
            "index": self.index,
            "start": self.start,
            "goal": self.goal,
            "path": self.path,
            "length_m": round(self.length_m, 2),
            "planned": self.planned,
            "error": self.error,
        }


def static_route(mission: Mission, resolve: Any) -> list[tuple[Step, int, Pose]]:
    """Walk the flow the way the editor draws it: navigation steps in order,
    entering `then` branches and loop bodies once. Returns (step, index, pose)."""
    out: list[tuple[Step, int, Pose]] = []

    def walk(steps: list[Step]) -> None:
        for s in steps:
            if not s.enabled:
                continue
            if s.type == "if":
                walk(s.container("then"))
                continue
            if s.type == "loop":
                walk(s.container("body"))
                continue
            if s.type not in ROUTE_STEPS:
                continue
            for i, pose in enumerate(_poses_of(s, resolve)):
                out.append((s, i, pose))

    walk(mission.flow)
    return out


def _poses_of(step: Step, resolve: Any) -> list[Pose]:
    key = {"nav.go_to_pose": "pose", "nav.go_through_poses": "poses", "nav.follow_waypoints": "poses", "nav.follow_path": "points"}[step.type]
    raw = step.params.get(key)
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    poses: list[Pose] = []
    for v in values:
        try:
            poses.append(resolve(v))
        except Exception:  # noqa: BLE001 - an expression we cannot resolve statically
            continue
    return poses


async def plan_route(runner: Runner, mission: Mission, start: Pose | None = None, planner_id: str = "") -> dict[str, Any]:
    """Plan every leg of ``mission`` with the real planner. Falls back to
    straight lines when the planner is unavailable or the robot is busy."""

    scope = _static_scope(runner, mission)

    def resolve(raw: Any) -> Pose:
        return runner.interpreter.resolve_pose(raw, scope)

    stops = static_route(mission, resolve)
    rs = runner.backend.robot_state()
    cursor = start or (Pose(rs.x, rs.y, rs.yaw_deg or 0.0) if rs.x is not None and rs.y is not None else None)
    busy = runner.dispatcher.busy()
    legs: list[Leg] = []
    for n, (step, idx, goal) in enumerate(stops):
        leg = Leg(step.id, step.type, idx, cursor.as_dict() if cursor else {}, goal.as_dict())
        if cursor is not None:
            if busy:
                leg.error = "robot is busy; showing a straight line"
            else:
                try:
                    leg.path = await asyncio.wait_for(runner.backend.compute_path(cursor, goal, planner_id, True), timeout=10)
                    leg.length_m = path_length(leg.path)
                    leg.planned = True
                except (StepFailed, asyncio.TimeoutError) as e:
                    leg.error = str(e) or "planner did not answer"
                except Exception as e:  # noqa: BLE001
                    leg.error = f"{type(e).__name__}: {e}"
            if not leg.planned:
                leg.length_m = cursor.distance_to(goal)
                leg.path = {"frame": goal.frame, "poses": [cursor.as_dict(), goal.as_dict()]}
        cursor = goal
        legs.append(leg)
        _ = n

    distance = sum(x.length_m for x in legs)
    speed = _nominal_speed(runner)
    fixed = sum(FIXED_COST_S.get(s.type, 0.0) for s in mission.iter_steps(mission.flow) if s.enabled)
    waits = sum(_wait_seconds(s) for s in mission.iter_steps(mission.flow) if s.enabled)
    return {
        "mission": mission.name,
        "legs": [x.as_dict() for x in legs],
        "distance_m": round(distance, 2),
        "estimate_s": round(distance / max(0.05, speed) + fixed + waits, 1),
        "speed_mps": speed,
        "planned": all(x.planned for x in legs) and bool(legs),
        "planner": runner.backend.name,
        "note": "robot is busy; distances are straight-line" if busy else "",
    }


def _wait_seconds(step: Step) -> float:
    if step.type != "wait":
        return 0.0
    v = step.params.get("seconds")
    return float(v) if isinstance(v, (int, float)) else 0.0


def _nominal_speed(runner: Runner) -> float:
    b = runner.backend
    return float(getattr(b, "speed", 0.0)) or float((runner.config.sim or {}).get("speed_mps", 0.5)) or 0.5


def _static_scope(runner: Runner, mission: Mission) -> dict[str, Any]:
    """Enough scope to resolve poses that do not depend on run-time values."""
    scope: dict[str, Any] = {name: inp.default for name, inp in mission.inputs.items()}
    scope.update(mission.vars)
    scope["robot"] = runner.robot_scope()
    scope["current_map"] = runner.current_map
    return scope


# ---------------------------------------------------------------------------
# dry run


class _StubConnector(Connector):
    """Accepts everything a mission sends and records it."""

    type = "dryrun"
    source_types = ("mqtt.subscribe", "modbus.poll", "ros.topic", "gpio.input", "http.webhook", "timer.cron", "timer.interval", "timer.boot", "mission.done")
    singleton = True

    def __init__(self, sink: list[dict[str, Any]]):
        super().__init__("dryrun", {})
        self.sink = sink

    async def arm(self, source: EventSource, callback: Any) -> Armed:
        return Armed(lambda: None, source.summary())

    async def publish(self, topic: str, payload: Any, qos: int = 1, retain: bool = False) -> None:
        self.sink.append({"kind": "mqtt.publish", "topic": topic, "payload": payload})

    async def write(self, kind: str, address: int, value: Any) -> None:
        self.sink.append({"kind": f"{kind}.write", "address": address, "value": value})


class _StubRegistry(ConnectorRegistry):
    def __init__(self, stub: _StubConnector):
        super().__init__()
        self._stub = stub
        self.add(stub)

    def require(self, name: str) -> Connector:
        return self._stub

    def for_source(self, source: EventSource) -> Connector:
        return self._stub


class _StubTriggers:
    """`wait_event` is assumed to fire straight away, so a preview never hangs."""

    def __init__(self, notes: list[str]):
        self.notes = notes

    async def wait_event(self, source: EventSource, timeout_s: float | None) -> dict[str, Any]:
        self.notes.append(f"assumed '{source.summary()}' fires immediately")
        await asyncio.sleep(0)
        return {"dry_run": True}


class _StubPrompts:
    """`ask_user` answers with its default, or the first option."""

    def __init__(self, notes: list[str]):
        self.notes = notes
        self.current = None

    async def ask(self, *, run_id: str, mission: str, step_id: str, text: str, options: list[str], default: str | None, timeout_s: float | None) -> str:
        answer = default or (options[0] if options else "")
        self.notes.append(f"answered '{text}' with '{answer}'")
        await asyncio.sleep(0)
        return answer


@dataclass
class DryRunResult:
    ok: bool = True
    status: str = "succeeded"
    error: str = ""
    duration_s: float = 0.0
    distance_m: float = 0.0
    samples: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "duration_s": round(self.duration_s, 1),
            "distance_m": round(self.distance_m, 2),
            "samples": self.samples,
            "steps": self.steps,
            "notes": self.notes,
            "outputs": self.outputs,
            "truncated": self.truncated,
        }


class DryRun:
    """Runs a mission against a private simulated robot and records a timeline."""

    #: simulated seconds between timeline samples
    SAMPLE_S = 0.25

    def __init__(self, runner: Runner, mission: Mission, inputs: dict[str, Any] | None = None, *, time_scale: float = 60.0, max_wall_s: float = 20.0, max_sim_s: float = 3600.0):
        self.runner = runner
        self.mission = mission
        self.inputs = dict(inputs or {})
        self.time_scale = time_scale
        self.max_wall_s = max_wall_s
        self.max_sim_s = max_sim_s
        self.notes: list[str] = []
        self.outputs: list[dict[str, Any]] = []
        self.events = EventBus()
        rs = runner.backend.robot_state()
        start = Pose(rs.x or 0.0, rs.y or 0.0, rs.yaw_deg or 0.0)
        self.backend = SimBackend(speed_mps=_nominal_speed(runner), time_scale=time_scale, start=start, battery=rs.battery if rs.battery is not None else 0.85)
        self.registry = _StubRegistry(_StubConnector(self.outputs))
        self.triggers = _StubTriggers(self.notes)
        self.prompts = _StubPrompts(self.notes)
        self.store = runner.store
        self._map = runner.current_map
        self._step: dict[str, Any] | None = None
        self._samples: list[dict[str, Any]] = []
        self._steps: list[dict[str, Any]] = []
        self._t0 = 0.0

    # ----- Services -------------------------------------------------------------

    @property
    def current_map(self) -> str | None:
        return self._map

    def set_current_map(self, name: str | None) -> None:
        self._map = name

    def robot_scope(self) -> dict[str, Any]:
        return self.backend.robot_state().as_dict() or {}

    async def run_sub_mission(self, ctx: Any, name: str, inputs: dict[str, Any]) -> Any:
        from .types import StepResult

        sub = self.store.get(name)
        if sub is None:
            raise StepFailed(f"unknown mission '{name}'")
        self.notes.append(f"ran sub-mission '{name}'")
        run = Run(id=f"dry-{name}", mission=name, inputs=dict(inputs), source=RunSource("sub", "dryrun"))
        sub_ctx = self.interpreter.make_context(run, sub)
        sub_ctx.depth = getattr(ctx, "depth", 0) + 1
        outcome = await self.interpreter.run(sub_ctx)
        return StepResult(outcome.status == RunStatus.SUCCEEDED, outcome.status.value, outcome.result, outcome.error)

    async def http_request(self, method: str, url: str, headers: dict[str, str] | None, body: Any, timeout_s: float | None) -> dict[str, Any]:
        self.outputs.append({"kind": "http.request", "method": method, "url": url})
        self.notes.append(f"skipped {method} {url}")
        return {"status": 200, "body": {"dry_run": True}}

    # ----- execution ----------------------------------------------------------------

    async def execute(self) -> DryRunResult:
        from .interpreter import Interpreter

        self.interpreter = Interpreter(self)  # type: ignore[arg-type]
        self.events.subscribe(self._on_event)
        run = Run(id="dry-run", mission=self.mission.name, inputs=self.inputs, source=RunSource("manual", "dryrun"))
        try:
            ctx = self.interpreter.make_context(run, self.mission)
        except StepFailed as e:
            return DryRunResult(False, "failed", str(e), notes=self.notes)

        result = DryRunResult(notes=self.notes, outputs=self.outputs)
        self._t0 = time.monotonic()
        sampler = asyncio.create_task(self._sample_loop(), name="dryrun-sampler")
        try:
            outcome = await asyncio.wait_for(self.interpreter.run(ctx), timeout=self.max_wall_s)
            result.status = outcome.status.value
            result.ok = outcome.status == RunStatus.SUCCEEDED
            result.error = outcome.error
        except asyncio.TimeoutError:
            result.status = "timeout"
            result.ok = False
            result.truncated = True
            result.error = f"preview stopped after {self.max_wall_s:g} s of wall clock ({self.time_scale:g}x)"
            await self.backend.cancel()
        except Exception as e:  # noqa: BLE001
            result.status = "failed"
            result.ok = False
            result.error = f"{type(e).__name__}: {e}"
        finally:
            sampler.cancel()
            self._snapshot()
        result.samples = self._samples
        result.steps = self._steps
        result.duration_s = (time.monotonic() - self._t0) * self.time_scale
        result.distance_m = self.backend.odometer
        return result

    async def _sample_loop(self) -> None:
        interval = self.SAMPLE_S / self.time_scale
        while True:
            self._snapshot()
            await asyncio.sleep(max(0.005, interval))

    def _snapshot(self) -> None:
        if len(self._samples) >= 4000:
            return
        st = self.backend.robot_state()
        t = (time.monotonic() - self._t0) * self.time_scale
        last = self._samples[-1] if self._samples else None
        x, y, yaw = round(st.x or 0.0, 3), round(st.y or 0.0, 3), round(st.yaw_deg or 0.0, 1)
        step_id = (self._step or {}).get("id", "")
        if last and last["x"] == x and last["y"] == y and last["yaw_deg"] == yaw and last["step"] == step_id:
            return
        self._samples.append({"t": round(t, 2), "x": x, "y": y, "yaw_deg": yaw, "step": step_id})

    def _on_event(self, event: dict[str, Any]) -> None:
        t = round((time.monotonic() - self._t0) * self.time_scale, 2)
        kind = event.get("type", "")
        if kind == "step.started":
            self._step = {"id": event["step_id"], "name": event.get("name", ""), "type": event.get("step_type", ""), "t0": t, "path": event.get("path")}
            self._snapshot()
        elif kind == "step.finished":
            self._snapshot()
            cur = self._step or {"id": event["step_id"]}
            res = event.get("result") or {}
            self._steps.append({**cur, "t1": t, "status": res.get("status", ""), "error": res.get("error", ""), "duration_s": res.get("duration_s", 0)})
            self._step = None
        elif kind == "log":
            self.notes.append(f"{event.get('level', 'info')}: {event.get('text', '')}")

    # `time_scale` is a plain attribute set in __init__; the interpreter reads it
    # off the services object to compress `wait` steps by the same factor.
