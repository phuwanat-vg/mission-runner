"""Executes a mission's flow: steps, control flow, retries, timeouts, pause,
suspend/resume and cancellation. Talks to the robot only through
:class:`~mission_runner.backends.base.NavBackend` and to the outside world
through connectors, so it runs unchanged in sim mode."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .backends.base import NavBackend, NavFeedback, Pose, path_from_points, pose_from_any
from .bt import bt_file_name, render_behavior_tree
from .connectors.base import ConnectorRegistry
from .events import EventBus
from .expressions import ExpressionError, evaluate, resolve
from .model import CONTAINER_KEYS, EventSource, Mission, SitesBook, Step
from .prompts import PromptManager
from .store import MissionStore
from .triggers import TriggerManager
from .types import Run, RunStatus, StepFailed, StepResult, StepTimeout, TaskCanceled

log = logging.getLogger("mission.interpreter")

FEEDBACK_MIN_INTERVAL = 0.5


class Services(Protocol):
    """What the interpreter needs from the runner."""

    backend: NavBackend
    registry: ConnectorRegistry
    triggers: TriggerManager
    prompts: PromptManager
    events: EventBus
    store: MissionStore

    @property
    def current_map(self) -> str | None: ...

    def set_current_map(self, name: str | None) -> None: ...

    def robot_scope(self) -> dict[str, Any]: ...

    async def run_sub_mission(self, ctx: RunContext, name: str, inputs: dict[str, Any]) -> StepResult: ...

    async def http_request(self, method: str, url: str, headers: dict[str, str] | None, body: Any, timeout_s: float | None) -> dict[str, Any]: ...


class BreakSignal(Exception):
    pass


class EndSignal(Exception):
    def __init__(self, result: str, message: str):
        super().__init__(message)
        self.result = result
        self.message = message


class Suspended(Exception):
    """Raised out of the flow when the dispatcher suspends the run."""

    def __init__(self, path: tuple[Any, ...]):
        super().__init__("suspended")
        self.path = path


@dataclass
class RunOutcome:
    status: RunStatus
    error: str = ""
    result: Any = None
    resume_path: tuple[Any, ...] | None = None


@dataclass
class RunContext:
    run: Run
    mission: Mission
    vars: dict[str, Any]
    #: None | "pause" | "cancel" | "suspend" (set by the dispatcher, then it cancels step_task)
    control: str | None = None
    control_reason: str = ""
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    step_task: asyncio.Task[Any] | None = None
    current_step: Step | None = None
    #: path of the step to resume at (set when resuming a suspended run)
    resume_path: tuple[Any, ...] | None = None
    loop_state: dict[str, int] = field(default_factory=dict)
    depth: int = 0
    last_feedback_mono: float = 0.0
    #: site the last nav.follow_route drove to (the default station of ros.request)
    last_site: str | None = None

    def scope(self, services: Services) -> dict[str, Any]:
        sites = services.store.sites
        m = sites.map(services.current_map) if sites else None
        robot = services.robot_scope()

        def _point(args: tuple[Any, ...]) -> tuple[float, float] | None:
            """in_zone('a') uses the robot; in_zone('a', x, y) uses a point."""
            if len(args) >= 2:
                return float(args[0]), float(args[1])
            if robot.get("x") is None:
                return None
            return float(robot["x"]), float(robot["y"])

        def in_zone(name: str, *rest: Any) -> bool:
            zone = m.zones.get(str(name)) if m else None
            p = _point(rest)
            return bool(zone and p and zone.contains(*p))

        def zone_of(*args: Any) -> list[str]:
            p = _point(tuple(args))
            return [z.name for z in m.zones.values() if p and z.contains(*p)] if m else []

        return {
            **self.vars,
            "robot": robot,
            "mission": self.mission.name,
            "run_id": self.run.id,
            "current_map": services.current_map,
            "sites": {s.name: s.as_pose() for s in m.sites.values()} if m else {},
            "zones": {z.name: {"kind": z.kind, "speed_mps": z.speed_mps} for z in m.zones.values()} if m else {},
            "in_zone": in_zone,
            "zone_of": zone_of,
        }


class Interpreter:
    def __init__(self, services: Services):
        self.s = services

    # ----- run ----------------------------------------------------------------

    def make_context(self, run: Run, mission: Mission) -> RunContext:
        vars_: dict[str, Any] = {}
        for name, inp in mission.inputs.items():
            vars_[name] = inp.default
        vars_.update(mission.vars)
        for k, v in run.inputs.items():
            vars_[k] = v
        missing = [n for n, i in mission.inputs.items() if i.required and vars_.get(n) is None]
        if missing:
            raise StepFailed(f"missing required inputs: {', '.join(missing)}")
        vars_.setdefault("last", None)
        return RunContext(run=run, mission=mission, vars=vars_)

    async def run(self, ctx: RunContext) -> RunOutcome:
        run, mission = ctx.run, ctx.mission
        outcome: RunOutcome
        try:
            try:
                await self._run_steps(mission.flow, ctx)
                outcome = RunOutcome(RunStatus.SUCCEEDED, result=ctx.vars.get("last"))
            except EndSignal as e:
                outcome = RunOutcome(RunStatus.SUCCEEDED if e.result == "success" else RunStatus.FAILED, e.message, ctx.vars.get("last"))
            except BreakSignal:
                outcome = RunOutcome(RunStatus.SUCCEEDED, result=ctx.vars.get("last"))
            except StepFailed as e:
                outcome = RunOutcome(RunStatus.FAILED, str(e), getattr(e, "value", None))
            except Suspended as e:
                ctx.control = None
                return RunOutcome(RunStatus.SUSPENDED, resume_path=e.path)
            except TaskCanceled:
                outcome = RunOutcome(RunStatus.CANCELED, ctx.control_reason or "canceled")
            except asyncio.CancelledError:
                # The whole run task was canceled from outside (runner shutdown).
                await self._safe_cancel_backend()
                raise
            except Exception as e:  # noqa: BLE001 - never let a bug in a step kill the runner
                log.exception("run %s crashed", run.id)
                outcome = RunOutcome(RunStatus.FAILED, f"internal error: {e}")
        finally:
            ctx.current_step = None
        if outcome.status in (RunStatus.FAILED, RunStatus.CANCELED) and mission.on_abort:
            await self._run_on_abort(ctx, outcome)
        return outcome

    async def run_on_abort(self, ctx: RunContext, outcome: RunOutcome) -> None:
        """Run the mission's on_abort steps (used for suspended runs that get canceled)."""
        await self._run_on_abort(ctx, outcome)

    async def _run_on_abort(self, ctx: RunContext, outcome: RunOutcome) -> None:
        ev = self.s.events
        ev.log("info", f"running on_abort of '{ctx.mission.name}'", run_id=ctx.run.id)
        await self._safe_cancel_backend()
        ctx.control = None
        ctx.vars["abort_reason"] = outcome.error
        try:
            await asyncio.wait_for(asyncio.shield(self._run_steps(ctx.mission.on_abort, ctx, abort_mode=True)), timeout=120)
        except (StepFailed, TaskCanceled, EndSignal, BreakSignal, Suspended) as e:
            ev.log("warn", f"on_abort stopped: {e}", run_id=ctx.run.id)
        except asyncio.TimeoutError:
            ev.log("warn", "on_abort timed out after 120 s", run_id=ctx.run.id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            ev.log("error", f"on_abort crashed: {e}", run_id=ctx.run.id)

    async def _safe_cancel_backend(self) -> None:
        try:
            await self.s.backend.cancel()
        except Exception:  # noqa: BLE001
            log.exception("backend cancel failed")

    # ----- step lists -----------------------------------------------------------

    async def _run_steps(self, steps: list[Step], ctx: RunContext, abort_mode: bool = False) -> None:
        for step in steps:
            if not step.enabled:
                continue
            if ctx.resume_path is not None and not self._on_resume_path(step, ctx):
                continue
            await self._run_step(step, ctx, abort_mode=abort_mode)

    def _on_resume_path(self, step: Step, ctx: RunContext) -> bool:
        """While resuming, skip steps that come before the interrupted one."""
        rp = ctx.resume_path
        if rp is None:
            return True
        sp = step.path
        if len(sp) > len(rp):
            return True
        if rp[: len(sp)] == sp:
            return True  # this step contains (or is) the resume target
        # Same parent list? compare the index at this level.
        if sp[:-1] == rp[: len(sp) - 1] and isinstance(sp[-1], int) and isinstance(rp[len(sp) - 1], int):
            return sp[-1] > rp[len(sp) - 1]
        return True

    # ----- one step ---------------------------------------------------------------

    async def _run_step(self, step: Step, ctx: RunContext, abort_mode: bool = False) -> StepResult:
        ev = self.s.events
        run = ctx.run
        resuming = ctx.resume_path is not None and step.path == ctx.resume_path
        if resuming:
            ctx.resume_path = None
        await self._check_control(ctx)
        ctx.current_step = step
        run.step = {"id": step.id, "name": step.label, "type": step.type, "path": list(step.path), "started_at": _iso_now()}
        run.feedback = None
        ev.emit("step.started", run_id=run.id, step_id=step.id, path=list(step.path), name=step.label, step_type=step.type, resumed=resuming)
        t0 = time.monotonic()
        attempts = 0
        retry = step.on_fail.retry if step.on_fail else 0
        result: StepResult
        while True:
            result = await self._attempt(step, ctx)
            if result.ok or result.status == "canceled":
                break
            if attempts < retry and not abort_mode:
                attempts += 1
                ev.emit("step.retry", run_id=run.id, step_id=step.id, attempt=attempts, of=retry, error=result.error)
                ev.log("warn", f"step '{step.label}' failed ({result.error}); retry {attempts}/{retry}", run_id=run.id, step_id=step.id)
                if step.on_fail and step.on_fail.before_retry:
                    await self._run_steps(step.on_fail.before_retry, ctx)
                if step.on_fail and step.on_fail.retry_delay_s > 0:
                    await self._sleep_controlled(step.on_fail.retry_delay_s, ctx)
                continue
            break
        result.duration_s = time.monotonic() - t0
        ctx.vars["last"] = result.as_dict()
        if step.out:
            ctx.vars[step.out] = result.as_dict()
        ev.emit("step.finished", run_id=run.id, step_id=step.id, path=list(step.path), result=result.as_dict(), attempts=attempts)
        if result.status == "canceled":
            raise TaskCanceled()
        if not result.ok:
            then = step.on_fail.then if step.on_fail else "abort"
            if then == "continue" or abort_mode:
                ev.log("warn", f"step '{step.label}' failed: {result.error} (continuing)", run_id=run.id, step_id=step.id)
            else:
                raise StepFailed(f"step '{step.label}' failed: {result.error}", value=result.value)
        return result

    async def _attempt(self, step: Step, ctx: RunContext) -> StepResult:
        """One attempt of a step with timeout and control handling."""
        while True:
            task = asyncio.create_task(self._exec(step, ctx), name=f"step:{step.id}")
            ctx.step_task = task
            t0 = time.monotonic()
            # wait_event / ask_user apply timeout_s themselves (with a better message).
            timeout = None if step.type in ("wait_event", "ask_user", "ros.request") else step.timeout_s
            try:
                done, _ = await asyncio.wait({task}, timeout=timeout)
            except asyncio.CancelledError:
                task.cancel()
                await _reap(task)
                await self._safe_cancel_backend()
                raise
            finally:
                ctx.step_task = None
            if not done:
                task.cancel()
                await _reap(task)
                await self._safe_cancel_backend()
                return StepResult.failure(f"timeout after {step.timeout_s:g} s", "timeout", duration_s=time.monotonic() - t0)
            if task.cancelled():
                # The dispatcher canceled the step: pause, suspend or cancel.
                control = ctx.control
                await self._safe_cancel_backend()
                if control == "pause":
                    ctx.control = None
                    await self._wait_resume(ctx)
                    if ctx.control in ("cancel", "suspend"):
                        return await self._control_result(step, ctx)
                    continue  # re-run the step
                return await self._control_result(step, ctx)
            try:
                value = task.result()
            except (BreakSignal, EndSignal, Suspended):
                raise
            except StepTimeout as e:
                return StepResult.failure(str(e), "timeout", getattr(e, "value", None), time.monotonic() - t0)
            except StepFailed as e:
                return StepResult.failure(str(e), "failed", e.value, time.monotonic() - t0)
            except TaskCanceled:
                return await self._control_result(step, ctx)
            except ExpressionError as e:
                return StepResult.failure(f"expression error: {e}", "failed", None, time.monotonic() - t0)
            except Exception as e:  # noqa: BLE001
                log.exception("step %s crashed", step.id)
                return StepResult.failure(f"{type(e).__name__}: {e}", "failed", None, time.monotonic() - t0)
            return StepResult.success(value, time.monotonic() - t0)

    async def _control_result(self, step: Step, ctx: RunContext) -> StepResult:
        control = ctx.control
        if control == "suspend":
            ctx.control = None
            raise Suspended(step.path)
        ctx.control = None
        return StepResult.failure(ctx.control_reason or "canceled", "canceled")

    async def _check_control(self, ctx: RunContext) -> None:
        if ctx.control == "pause":
            ctx.control = None
            await self._wait_resume(ctx)
        if ctx.control == "cancel":
            raise TaskCanceled()
        if ctx.control == "suspend":
            ctx.control = None
            raise Suspended(ctx.current_step.path if ctx.current_step else ("flow", 0))

    async def _wait_resume(self, ctx: RunContext) -> None:
        run = ctx.run
        run.status = RunStatus.PAUSED
        self.s.events.emit("run.paused", run=run.as_dict())
        ctx.resume_event.clear()
        await ctx.resume_event.wait()
        if ctx.control is None:
            run.status = RunStatus.RUNNING
            self.s.events.emit("run.resumed", run=run.as_dict())

    async def _sleep_controlled(self, seconds: float, ctx: RunContext) -> None:
        # Dry runs set time_scale so a "wait 5 minutes" step previews instantly.
        end = time.monotonic() + seconds / max(0.01, float(getattr(self.s, "time_scale", 1.0)))
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(left, 0.25))
            if ctx.control:
                await self._check_control(ctx)

    # ----- step implementations ---------------------------------------------------

    def _feedback_cb(self, ctx: RunContext, step: Step) -> Any:
        def cb(fb: NavFeedback) -> None:
            now = time.monotonic()
            d = fb.as_dict()
            ctx.run.feedback = d
            if now - ctx.last_feedback_mono >= FEEDBACK_MIN_INTERVAL:
                ctx.last_feedback_mono = now
                self.s.events.emit("feedback", run_id=ctx.run.id, step_id=step.id, feedback=d)

        return cb

    async def _nav(self, coro: Awaitable[Any]) -> Any:
        """Await a backend call; make sure the robot stops if we get canceled."""
        try:
            return await coro
        except asyncio.CancelledError:
            await self._safe_cancel_backend()
            raise

    def resolve_pose(self, value: Any, scope: dict[str, Any]) -> Pose:
        """Public form used by the route preview (no run context needed)."""
        return self._resolve_pose(value, None, scope)

    def _resolve_pose(self, value: Any, ctx: RunContext | None, scope: dict[str, Any]) -> Pose:
        v = resolve(value, scope)
        sites: SitesBook = self.s.store.sites
        frame = "map"
        m = sites.map(self.s.current_map)
        if m:
            frame = m.frame
        if isinstance(v, str):
            site = sites.lookup(self.s.current_map, v)
            if site is None:
                raise StepFailed(f"site '{v}' not found in map '{self.s.current_map or '?'}'")
            return Pose(site.x, site.y, site.yaw_deg, frame)
        if isinstance(v, dict) and "site" in v:
            name = v["site"]
            site = sites.lookup(self.s.current_map, str(name))
            if site is None:
                raise StepFailed(f"site '{name}' not found in map '{self.s.current_map or '?'}'")
            yaw = v.get("yaw_deg")
            return Pose(site.x, site.y, float(yaw) if yaw is not None else site.yaw_deg, frame)
        return pose_from_any(v, frame)

    def _resolve_poses(self, values: Any, ctx: RunContext, scope: dict[str, Any]) -> list[Pose]:
        if not isinstance(values, list):
            raise StepFailed("expected a list of poses")
        return [self._resolve_pose(v, ctx, scope) for v in values]

    def _behavior_tree(self, spec: Any, ctx: RunContext, step: Step) -> str:
        if not spec:
            return ""
        if isinstance(spec, str):
            return spec
        if isinstance(spec, dict):
            xml = render_behavior_tree(spec)
            path = self.s.store.write_bt(bt_file_name(ctx.mission.name, step.id), xml)
            return str(path)
        raise StepFailed("behavior_tree must be a path or a template object")

    async def _exec(self, step: Step, ctx: RunContext) -> Any:  # noqa: C901, PLR0911, PLR0912, PLR0915 - dispatch table by design
        s = self.s
        b = s.backend
        scope = ctx.scope(s)
        p = step.params
        t = step.type
        fb = self._feedback_cb(ctx, step)

        def num(key: str, default: float | None = None) -> float:
            v = resolve(p.get(key, default), scope)
            if v is None:
                raise StepFailed(f"'{key}' is required")
            try:
                return float(v)
            except (TypeError, ValueError):
                raise StepFailed(f"'{key}' must be a number, got {v!r}") from None

        def text(key: str, default: str = "") -> str:
            v = resolve(p.get(key, default), scope)
            return "" if v is None else str(v)

        # ---- navigation
        if t == "nav.wait_active":
            await self._nav(b.wait_active(step.timeout_s or float(p.get("timeout_s") or 0) or None))
            return None
        if t == "nav.set_initial_pose":
            pose = self._resolve_pose(p.get("pose"), ctx, scope)
            setter = getattr(s, "set_initial_pose", None)  # the runner logs and emits robot.initial_pose
            if setter is None:
                await self._nav(b.set_initial_pose(pose))
            else:
                raw = resolve(p.get("pose"), scope)
                site = raw if isinstance(raw, str) else raw.get("site") if isinstance(raw, dict) else None
                await self._nav(setter(pose, site=str(site) if site else None, source="step", cancellable=True))
            return None
        if t == "nav.go_to_pose":
            pose = self._resolve_pose(p.get("pose"), ctx, scope)
            return await self._nav(b.go_to_pose(pose, self._behavior_tree(p.get("behavior_tree"), ctx, step), fb))
        if t == "nav.go_through_poses":
            poses = self._resolve_poses(p.get("poses"), ctx, scope)
            return await self._nav(b.go_through_poses(poses, self._behavior_tree(p.get("behavior_tree"), ctx, step), fb))
        if t == "nav.follow_route":
            return await self._follow_route(step, ctx, scope, fb)
        if t == "nav.follow_waypoints":
            poses = self._resolve_poses(p.get("poses"), ctx, scope)
            return await self._nav(b.follow_waypoints(poses, fb))
        if t == "nav.follow_path":
            if p.get("path") is not None:
                path = resolve(p["path"], scope)
                if not isinstance(path, dict) or "poses" not in path:
                    raise StepFailed("'path' must be a path value from nav.compute_path or nav.smooth_path")
            else:
                pts = self._resolve_poses(p.get("points"), ctx, scope)
                if bool(p.get("from_robot", True)):
                    rs = b.robot_state()
                    if rs.x is None or rs.y is None:
                        raise StepFailed("robot pose unknown; cannot start the path from the robot")
                    pts = [Pose(rs.x, rs.y, rs.yaw_deg or 0.0), *pts]
                path = path_from_points(pts, float(p.get("spacing_m", 0.05) or 0.05), pts[0].frame if pts else "map")
            return await self._nav(b.follow_path(path, text("controller_id"), text("goal_checker_id"), fb))
        if t == "nav.compute_path":
            goal = self._resolve_pose(p.get("goal"), ctx, scope)
            start = self._resolve_pose(p["start"], ctx, scope) if p.get("start") is not None else None
            return await self._nav(b.compute_path(start, goal, text("planner_id"), bool(p.get("use_start", False))))
        if t == "nav.compute_path_through_poses":
            goals = self._resolve_poses(p.get("goals"), ctx, scope)
            start = self._resolve_pose(p["start"], ctx, scope) if p.get("start") is not None else None
            return await self._nav(b.compute_path_through_poses(start, goals, text("planner_id"), bool(p.get("use_start", False))))
        if t == "nav.smooth_path":
            path = resolve(p.get("path"), scope)
            if not isinstance(path, dict) or "poses" not in path:
                raise StepFailed("'path' must be a path value")
            return await self._nav(b.smooth_path(path, text("smoother_id"), num("max_duration_s", 2.0), bool(p.get("check_collision", False))))
        if t == "nav.spin":
            return await self._nav(b.spin(math.radians(num("angle_deg")), num("time_allowance_s", 10.0), fb))
        if t == "nav.backup":
            return await self._nav(b.backup(num("distance_m"), num("speed_mps", 0.15), num("time_allowance_s", 10.0), fb))
        if t == "nav.drive_on_heading":
            return await self._nav(b.drive_on_heading(num("distance_m"), num("speed_mps", 0.15), num("time_allowance_s", 10.0), fb))
        if t == "nav.change_map":
            name = text("map")
            if not name:
                raise StepFailed("'map' is required")
            sites = s.store.sites
            await self._nav(b.change_map(sites.map_file(name)))
            s.set_current_map(name if name in sites.maps else name)
            s.events.emit("map.changed", map=name)
            return {"map": name}
        if t == "nav.clear_costmap":
            which = text("which", "all") or "all"
            await self._nav(b.clear_costmap(which))
            return {"which": which}
        if t == "nav.dock":
            dock_pose = self._resolve_pose(p["dock_pose"], ctx, scope) if p.get("dock_pose") is not None else None
            dock_id = text("dock_id") or None
            dock_type = text("dock_type")
            if dock_id and dock_pose is None:
                site = s.store.sites.lookup(s.current_map, dock_id)
                if site and site.kind == "dock":
                    dock_pose = Pose(site.x, site.y, site.yaw_deg)
                    dock_type = dock_type or (site.dock_type or "")
                    dock_id = site.dock_id or dock_id
            return await self._nav(b.dock(dock_id, dock_pose, dock_type, bool(p.get("navigate_to_staging", True)), fb))
        if t == "nav.undock":
            return await self._nav(b.undock(text("dock_type"), fb))
        if t == "nav.lifecycle":
            action = text("action")
            if action not in ("startup", "shutdown"):
                raise StepFailed("action must be 'startup' or 'shutdown'")
            await self._nav(b.lifecycle(action))
            return {"action": action}
        if t == "nav.cancel":
            await b.cancel()
            return None

        # ---- ROS
        if t == "ros.publish":
            await b.publish(text("topic"), text("msg_type"), resolve(p.get("message") or {}, scope))
            return None
        if t == "ros.call_service":
            return await self._nav(b.call_service(text("service"), text("srv_type"), resolve(p.get("request") or {}, scope), step.timeout_s))
        if t == "ros.call_action":
            return await self._nav(b.call_action(text("action"), text("action_type"), resolve(p.get("goal") or {}, scope), step.timeout_s, fb))
        if t == "ros.set_param":
            await b.set_params(text("node"), resolve(p.get("params") or {}, scope))
            return None

        # ---- connectors
        if t == "mqtt.publish":
            c = s.registry.require(text("connector"))
            payload = resolve(p.get("payload"), scope)
            await c.publish(text("topic"), payload, int(p.get("qos", 1)), bool(p.get("retain", False)))
            return None
        if t == "http.request":
            return await s.http_request(text("method", "POST") or "POST", text("url"), resolve(p.get("headers"), scope), resolve(p.get("body"), scope), step.timeout_s)
        if t == "modbus.write":
            c = s.registry.require(text("connector"))
            await c.write(text("kind", "coil") or "coil", int(num("address")), resolve(p.get("value"), scope))
            return None
        if t == "gpio.write":
            c = s.registry.require("gpio")
            await c.write("pin", int(num("pin")), resolve(p.get("value"), scope))
            return None

        # ---- logic
        if t == "set":
            name = str(p.get("var", ""))
            value = resolve(p.get("value"), scope)
            ctx.vars[name] = value
            return value
        if t == "if":
            cond = bool(evaluate(str(p.get("condition", "")), scope))
            branch = step.container("then" if cond else "else")
            await self._run_steps(branch, ctx)
            return cond
        if t == "loop":
            return await self._loop(step, ctx, scope)
        if t == "break":
            raise BreakSignal()
        if t == "wait":
            seconds = num("seconds", 0.0)
            await self._sleep_controlled(seconds, ctx)
            return seconds
        if t == "wait_event":
            src = p.get("source") or {}
            source = EventSource(
                type=str(src.get("type", "")),
                params={k: resolve(v, scope) for k, v in src.items() if k not in ("type", "when", "edge", "debounce_s")},
                when=src.get("when"),
                edge=str(src.get("edge", "any")),
                debounce_s=float(src.get("debounce_s", 0) or 0),
            )
            try:
                return await s.triggers.wait_event(source, step.timeout_s or (float(p["timeout_s"]) if p.get("timeout_s") else None))
            except StepTimeout:
                if str(p.get("on_timeout", "abort")) == "continue":
                    return {"timeout": True}
                raise
        if t == "ros.request":
            return await self._ros_request(step, ctx, scope)
        if t == "ask_user":
            options = [str(o) for o in (resolve(p.get("options"), scope) or ["Continue", "Stop"])]
            default = resolve(p.get("default"), scope)
            return await s.prompts.ask(
                run_id=ctx.run.id,
                mission=ctx.mission.name,
                step_id=step.id,
                text=text("text"),
                options=options,
                default=str(default) if default is not None else None,
                timeout_s=step.timeout_s or (float(p["timeout_s"]) if p.get("timeout_s") else None),
            )
        if t == "log":
            s.events.log(str(p.get("level", "info")), text("text"), run_id=ctx.run.id, step_id=step.id)
            return None
        if t == "run_mission":
            name = text("mission")
            inputs = resolve(p.get("inputs") or {}, scope)
            if ctx.depth >= 8:
                raise StepFailed("run_mission nested too deep")
            result = await s.run_sub_mission(ctx, name, inputs if isinstance(inputs, dict) else {})
            if result.status == "canceled":
                raise TaskCanceled()
            if not result.ok:
                raise StepFailed(result.error or f"mission '{name}' failed", value=result.value)
            return result.value
        if t == "end":
            raise EndSignal(str(p.get("result", "success")), text("message"))
        raise StepFailed(f"unknown step type '{t}'")

    async def _follow_route(self, step: Step, ctx: RunContext, scope: dict[str, Any], fb: Any) -> dict[str, Any]:
        """Drive to a site along the route graph, so the robot stays on the lanes
        that were drawn instead of cutting across the floor."""
        s = self.s
        p = step.params
        sites = s.store.sites
        mapdef = sites.map(s.current_map)
        if mapdef is None:
            raise StepFailed(f"no map '{s.current_map}' in sites.json")
        goal_name = str(resolve(p.get("to"), scope) or "")
        if goal_name not in mapdef.sites:
            raise StepFailed(f"site '{goal_name}' not found in map '{mapdef.name}'")
        goal_site = mapdef.sites[goal_name]

        start_name = str(resolve(p.get("from"), scope) or "") if p.get("from") else ""
        if start_name and start_name not in mapdef.sites:
            raise StepFailed(f"site '{start_name}' not found in map '{mapdef.name}'")
        if not start_name:
            rs = s.backend.robot_state()
            if rs.x is None or rs.y is None:
                raise StepFailed("robot pose unknown; cannot find the nearest route node")
            # Nearest site of any kind: skipping a dead end (e.g. the robot parked
            # at the end of a one-way lane) would send it across the floor off the lanes.
            near = mapdef.nearest_site(rs.x, rs.y)
            if near is None:
                raise StepFailed(f"map '{mapdef.name}' has no sites to start from")
            start_name = near.name

        through_raw = resolve(p.get("through"), scope) if p.get("through") is not None else []
        if not isinstance(through_raw, list):
            raise StepFailed("'through' must be a list of sites")
        through = [str(x) for x in through_raw]
        for name in through:
            if name not in mapdef.sites:
                raise StepFailed(f"site '{name}' not found in map '{mapdef.name}'")

        # Plan every leg on the graph: start -> through... -> to.
        frame = mapdef.frame
        stops = [start_name, *through, goal_name]
        legs = []
        direct_hops: list[str] = []
        for a, b in zip(stops[:-1], stops[1:]):
            leg = mapdef.plan_route(a, b)
            if leg is None:
                if str(p.get("on_no_route", "fail")) != "direct":
                    raise StepFailed(f"no route from '{a}' to '{b}' on this map's graph")
                s.events.log("warn", f"no route from '{a}' to '{b}'; driving direct", run_id=ctx.run.id, step_id=step.id)
                direct_hops.append(b)
                from .model import RouteLeg

                sa, sb = mapdef.sites[a], mapdef.sites[b]
                leg = [RouteLeg(a, b, math.hypot(sb.x - sa.x, sb.y - sa.y))]
            legs.extend(leg)
        route = [start_name, *[leg.to for leg in legs]]
        ctx.last_site = goal_name
        bt = self._behavior_tree(p.get("behavior_tree"), ctx, step)
        poses = [Pose(mapdef.sites[n].x, mapdef.sites[n].y, mapdef.sites[n].yaw_deg, frame) for n in route[1:]] or [
            Pose(goal_site.x, goal_site.y, goal_site.yaw_deg, frame)
        ]
        poses[-1] = Pose(goal_site.x, goal_site.y, goal_site.yaw_deg, frame)
        result: dict[str, Any] = {
            "route": route,
            "legs": [leg.as_dict() for leg in legs],
            "length_m": round(sum(leg.length_m for leg in legs), 2),
            "from": start_name,
            "to": goal_name,
            "through": through,
            "direct": direct_hops,
        }

        if bool(p.get("apply_speed_limits", False)) and any(leg.speed_mps for leg in legs):
            node, param = _speed_param(s)
            restored = False
            try:
                for leg, pose in zip(legs, poses):
                    if leg.speed_mps:
                        await self._nav(s.backend.set_params(node, {param: float(leg.speed_mps)}))
                        restored = True
                    elif restored:
                        await self._nav(s.backend.set_params(node, {param: _default_speed(s)}))
                        restored = False
                    await self._nav(s.backend.go_to_pose(pose, bt, fb))
            finally:
                if restored:
                    await self._nav(s.backend.set_params(node, {param: _default_speed(s)}))
            return result

        if len(poses) == 1:
            out = await self._nav(s.backend.go_to_pose(poses[0], bt, fb))
        else:
            out = await self._nav(s.backend.go_through_poses(poses, bt, fb))
        result["result"] = out
        return result

    async def _ros_request(self, step: Step, ctx: RunContext, scope: dict[str, Any]) -> dict[str, Any]:
        """Publish a JSON request and wait for the answer with the same id, over
        two std_msgs/String topics. iViz's Dashboard answers this exchange, and
        so can any node: it only has to echo the id back with an answer."""
        import json
        import uuid

        s = self.s
        p = step.params
        cfg = getattr(s, "config", None)
        station = resolve(p.get("station"), scope) if p.get("station") else ctx.last_site
        # Topics, each on its own: the step's, then the station site's, then the runner default.
        mapdef = s.store.sites.map(s.current_map) if station else None
        site = mapdef.sites.get(str(station)) if mapdef is not None else None
        request_topic = str(
            resolve(p.get("request_topic"), scope) or (site.request_topic if site else "") or getattr(cfg, "request_topic", "/iviz/request")
        )
        answer_topic = str(resolve(p.get("answer_topic"), scope) or (site.answer_topic if site else "") or getattr(cfg, "answer_topic", "/iviz/answer"))
        options = [str(o) for o in (resolve(p.get("options"), scope) or [])]
        default = resolve(p.get("default"), scope)
        default = None if default is None else str(default)
        timeout_s = step.timeout_s or (float(p["timeout_s"]) if p.get("timeout_s") else None)
        on_timeout = str(p.get("on_timeout", "default"))
        rid = uuid.uuid4().hex[:8]
        body: dict[str, Any] = {
            "id": rid,
            "text": str(resolve(p.get("text"), scope) or ""),
            "options": options,
            "source": "mission_runner",
            "mission": ctx.mission.name,
            "run_id": ctx.run.id,
            "step_id": step.id,
        }
        if default is not None:
            body["default"] = default
        if timeout_s:
            body["timeout_s"] = timeout_s
        if station:
            body["station"] = str(station)
        if p.get("data") is not None:
            body["data"] = resolve(p.get("data"), scope)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()

        def on_answer(payload: dict[str, Any]) -> None:
            raw = payload.get("data", payload)
            if isinstance(raw, str):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    return
            elif isinstance(raw, dict):
                msg = raw
            else:
                return
            if not isinstance(msg, dict) or str(msg.get("id")) != rid or fut.done():
                return
            fut.set_result(msg)

        # Listen before publishing, so a fast answerer cannot beat us.
        source = EventSource(type="ros.topic", params={"topic": answer_topic, "msg_type": "std_msgs/msg/String"})
        armed = await s.triggers.arm(source, on_answer, label=f"ros.request/{step.id}")
        try:
            await s.backend.publish(request_topic, "std_msgs/msg/String", {"data": json.dumps(body, default=str)})
            s.events.emit("request", run_id=ctx.run.id, step_id=step.id, request=body, request_topic=request_topic, answer_topic=answer_topic)
            try:
                msg = await (asyncio.wait_for(asyncio.shield(fut), timeout=timeout_s) if timeout_s else fut)
            except asyncio.TimeoutError:
                if on_timeout == "default" and default is not None:
                    s.events.log("warn", f"no answer to '{body['text']}' within {timeout_s:g} s; using '{default}'", run_id=ctx.run.id, step_id=step.id)
                    return {"id": rid, "answer": default, "by": "timeout", "timed_out": True}
                raise StepTimeout(f"no answer on {answer_topic} within {timeout_s:g} s") from None
        finally:
            await armed.disarm()
        answer = msg.get("answer")
        if options and str(answer) not in options:
            s.events.log("warn", f"answer '{answer}' is not one of {options}", run_id=ctx.run.id, step_id=step.id)
        result = {"id": rid, "answer": answer, "by": msg.get("by", ""), "timed_out": False}
        s.events.emit("request.answered", run_id=ctx.run.id, step_id=step.id, id=rid, answer=answer, by=result["by"])
        return result

    async def _loop(self, step: Step, ctx: RunContext, scope: dict[str, Any]) -> int:
        p = step.params
        body = step.container("body")
        count: int | None = None
        if p.get("count") is not None:
            c = resolve(p["count"], scope)
            try:
                count = int(c)
            except (TypeError, ValueError):
                raise StepFailed(f"loop count must be a number, got {c!r}") from None
        cond = str(p["while"]) if p.get("while") else None
        # Resume inside this loop continues at the stored iteration.
        resuming_inside = ctx.resume_path is not None and ctx.resume_path[: len(step.path)] == step.path and len(ctx.resume_path) > len(step.path)
        i = ctx.loop_state.get(step.id, 0) if resuming_inside else 0
        ctx.loop_state[step.id] = i
        try:
            while True:
                if count is not None and i >= count:
                    break
                if cond is not None and not bool(evaluate(cond, ctx.scope(self.s))):
                    break
                ctx.vars["loop_index"] = i
                ctx.loop_state[step.id] = i
                try:
                    await self._run_steps(body, ctx)
                except BreakSignal:
                    break
                i += 1
                if ctx.control:
                    await self._check_control(ctx)
                if count is None and cond is None and not body:
                    await asyncio.sleep(0.1)  # forever-loop with no steps: don't spin
        finally:
            ctx.loop_state.pop(step.id, None)
        return i


def _speed_param(services: Services) -> tuple[str, str]:
    """Which node and parameter caps the controller's speed, from runner.yaml."""
    cfg = getattr(getattr(services, "config", None), "nav2", None) or {}
    return str(cfg.get("speed_node", "/controller_server")), str(cfg.get("speed_param", "FollowPath.max_vel_x"))


def _default_speed(services: Services) -> float:
    cfg = getattr(getattr(services, "config", None), "nav2", None) or {}
    return float(cfg.get("default_speed_mps", 0.5))


async def _reap(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except BaseException:  # noqa: BLE001 - result is irrelevant, we only need the task to finish
        pass


def _iso_now() -> str:
    from .types import now_iso

    return now_iso()
