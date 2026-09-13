"""Composes everything into the always-on service: store, backend, connectors,
triggers, dispatcher, HTTP/WS server and (with the nav2 backend) the ROS
interface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from . import __version__
from .backends.base import NavBackend
from .config import RunnerConfig
from .connectors.base import ConnectorRegistry
from .connectors.gpio import GpioConnector
from .connectors.internal import InternalConnector, SimTopicConnector
from .connectors.modbus import ModbusConnector
from .connectors.mqtt import MqttConnector
from .connectors.timer import TimerConnector
from .dispatcher import Dispatcher
from .events import EventBus
from .expressions import ExpressionError, evaluate
from .interpreter import Interpreter, RunContext, Suspended
from .model import Finding, Mission, MissionValidationError, TriggerSpec, check_mission_cycles, load_mission, validate_mission
from .prompts import PromptManager
from .store import MissionStore, RunLog
from .triggers import TriggerManager
from .types import Run, RunSource, RunStatus, StepFailed, StepResult, TaskCanceled

log = logging.getLogger("mission")

CONNECTOR_TYPES = {"mqtt": MqttConnector, "modbus_tcp": ModbusConnector, "modbus": ModbusConnector}


def find_examples_dir() -> Path | None:
    candidates = [Path(__file__).resolve().parents[2] / "examples", Path(__file__).resolve().parent / "examples"]
    try:
        from ament_index_python.packages import get_package_share_directory

        candidates.append(Path(get_package_share_directory("mission_runner")) / "examples")
    except Exception:  # noqa: BLE001 - not a ROS install
        pass
    for c in candidates:
        if c.is_dir() and any(c.glob("*.json")):
            return c
    return None


class Runner:
    """Implements :class:`mission_runner.interpreter.Services`."""

    def __init__(self, config: RunnerConfig):
        self.config = config
        self.events = EventBus()
        self.store = MissionStore(config.home)
        self.run_log = RunLog(config.home / "runs.sqlite", keep_runs=config.keep_runs)
        self.registry = ConnectorRegistry()
        self.internal = InternalConnector()
        self.timer = TimerConnector()
        self.sim_topics: SimTopicConnector | None = None
        self.backend: NavBackend = self._make_backend()
        self.triggers = TriggerManager(self.registry, self.events, self.trigger_scope)
        self.prompts = PromptManager(self.events)
        self.interpreter = Interpreter(self)
        self.dispatcher = Dispatcher(self.interpreter, self.store.get, self.run_log)
        self.dispatcher.on_run_started = self._on_run_started
        self.dispatcher.on_run_finished = self._on_run_finished
        self._current_map: str | None = None
        self.started_mono = time.monotonic()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._http_session: Any = None
        self.server: Any = None
        self.ros: Any = None
        self.live: Any = None
        self.trigger_problems: dict[str, list[str]] = {}
        from .autostart import AutostartManager

        self.autostart = AutostartManager(config.home, config.autostart)

    # ----- construction ---------------------------------------------------------------

    def _make_backend(self) -> NavBackend:
        if self.config.backend == "sim":
            from .backends.base import Pose
            from .backends.sim import SimBackend

            s = self.config.sim or {}
            start = s.get("start") or {}
            return SimBackend(
                speed_mps=float(s.get("speed_mps", 0.6)),
                turn_rate_dps=float(s.get("turn_rate_dps", 90.0)),
                time_scale=float(s.get("time_scale", 1.0)),
                start=Pose(float(start.get("x", 0)), float(start.get("y", 0)), float(start.get("yaw_deg", 0))),
                battery=float(s.get("battery", 0.85)),
                startup_delay_s=float(s.get("startup_delay_s", 0.0)),
            )
        try:
            from .backends.nav2 import Nav2Backend
        except ImportError as e:
            raise SystemExit(f"The nav2 backend needs ROS 2 (rclpy, nav2_simple_commander): {e}\nUse --sim to run without a robot.") from None
        return Nav2Backend(self.config, self.events)

    def _make_connectors(self) -> None:
        self.registry.add(self.timer)
        self.registry.add(self.internal)
        self.registry.add(GpioConnector("gpio", self.config.gpio))
        if self.config.backend == "sim":
            self.sim_topics = SimTopicConnector()
            self.registry.add(self.sim_topics)
        elif self.ros is not None:
            self.registry.add(self.ros)
        for name, cfg in self.store.load_connectors_config().items():
            ctype = str(cfg.get("type", "")).lower()
            cls = CONNECTOR_TYPES.get(ctype)
            if cls is None:
                log.warning("connectors.yaml: '%s' has unknown type '%s'", name, ctype)
                continue
            self.registry.add(cls(name, cfg))

    def _make_live_relay(self) -> None:
        """Costmap / scan / plan / footprint for the editor's map, on demand."""
        from .live import RosLiveRelay, SimLiveRelay

        if self.config.backend == "sim":
            self.live = SimLiveRelay(self.events, self.backend)  # type: ignore[arg-type]
            return
        b = self.backend
        node = getattr(b, "node", None)
        if node is None:
            return
        self.live = RosLiveRelay(
            self.events,
            node,
            getattr(b, "cb_group", None),
            getattr(b, "_tf_buffer", None),
            (getattr(self.config, "live_topics", None) or None),
            getattr(b, "_map_frame", "map"),
        )

    # ----- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.events.bind_loop(loop)
        self.triggers.bind_loop(loop)
        self.events.subscribe(self._log_listener)

        self.store.load_all()
        if self.config.install_examples and not self.store.missions and not any(self.store.missions_dir.glob("*.json")):
            self.install_examples()
        self._current_map = self.store.sites.default_map

        await self.backend.start()
        if self.config.backend != "sim":
            from .connectors.ros import RosConnector

            self.ros = RosConnector(self)
        self._make_connectors()
        await self.registry.start_all()
        self._make_live_relay()

        # Render behavior trees for all missions so bt_navigator can load them.
        for m in list(self.store.missions.values()):
            self._render_trees(m.mission)

        from .server import HttpServer

        self.server = HttpServer(self)
        await self.server.start(self.config.http_host, self.config.http_port)

        await self.dispatcher.start()
        for m in list(self.store.missions.values()):
            await self.arm_mission(m.mission)
        self._spawn(self._robot_ticker(), "robot-ticker")
        self.events.log("info", f"mission_runner {__version__} ready: backend={self.backend.name} home={self.config.home} http={self.config.http_host}:{self.config.http_port} missions={len(self.store.missions)}")

    async def stop(self) -> None:
        log.info("stopping")
        for t in list(self._tasks):
            t.cancel()
        await self.dispatcher.stop()
        if self.live is not None:
            self.live.close()
        await self.triggers.disarm_all()
        if self.server is not None:
            await self.server.stop()
        await self.registry.stop_all()
        await self.backend.stop()
        if self._http_session is not None:
            await self._http_session.close()
        self.run_log.close()

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def install_examples(self) -> list[str]:
        src = find_examples_dir()
        if src is None:
            log.warning("no examples directory found")
            return []
        names: list[str] = []
        for f in sorted(src.glob("*.json")):
            doc = json.loads(f.read_text("utf-8"))
            if doc.get("schema") == "sites/1":
                if not self.store.sites_path.exists():
                    shutil.copy(f, self.store.sites_path)
                continue
            if doc.get("schema") == "mission/1":
                shutil.copy(f, self.store.missions_dir / f"{doc['name']}.json")
                names.append(str(doc["name"]))
        self.store.load_all()
        log.info("installed %d example missions into %s", len(names), self.store.missions_dir)
        return names

    # ----- Services (used by the interpreter) --------------------------------------------

    @property
    def current_map(self) -> str | None:
        return self._current_map

    def set_current_map(self, name: str | None) -> None:
        self._current_map = name

    def robot_scope(self) -> dict[str, Any]:
        return self.backend.robot_state().as_dict() or {}

    def trigger_scope(self) -> dict[str, Any]:
        cur = self.dispatcher.current
        return {"robot": self.robot_scope(), "current_map": self._current_map, "state": self.dispatcher.state, "mission": cur.mission if cur else None}

    async def run_sub_mission(self, ctx: RunContext, name: str, inputs: dict[str, Any]) -> StepResult:
        outcome = await self.dispatcher.run_sub_mission(ctx, name, inputs)
        if outcome.status == RunStatus.SUSPENDED:
            raise Suspended(ctx.current_step.path if ctx.current_step else ("flow", 0))
        if outcome.status == RunStatus.CANCELED:
            raise TaskCanceled()
        return StepResult(outcome.status == RunStatus.SUCCEEDED, outcome.status.value, outcome.result, outcome.error)

    async def call_api(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """Call this runner's own HTTP API in-process. Used by the ROS `/mission/api`
        service so a desktop GUI on foxglove_bridge reaches every endpoint over
        the one connection it already has. Binary replies come back base64."""
        import base64

        import aiohttp

        if self._http_session is None:
            self._http_session = aiohttp.ClientSession()
        host = "127.0.0.1" if self.config.http_host in ("0.0.0.0", "::") else self.config.http_host
        url = f"http://{host}:{self.config.http_port}{path if path.startswith('/') else '/' + path}"
        kwargs: dict[str, Any] = {"timeout": aiohttp.ClientTimeout(total=60)}
        if body is not None and method.upper() != "GET":
            kwargs["json"] = body
        async with self._http_session.request(method.upper(), url, **kwargs) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, await resp.json()
            data = await resp.read()
            if ctype.startswith("text/"):
                return resp.status, {"content_type": ctype, "text": data.decode("utf-8", "replace")}
            return resp.status, {"content_type": ctype, "base64": base64.b64encode(data).decode("ascii")}

    async def http_request(self, method: str, url: str, headers: dict[str, str] | None, body: Any, timeout_s: float | None) -> dict[str, Any]:
        import aiohttp

        if self._http_session is None:
            self._http_session = aiohttp.ClientSession()
        kwargs: dict[str, Any] = {"headers": headers or {}, "timeout": aiohttp.ClientTimeout(total=timeout_s or 30)}
        if body is not None and method.upper() != "GET":
            if isinstance(body, (dict, list)):
                kwargs["json"] = body
            else:
                kwargs["data"] = str(body)
        try:
            async with self._http_session.request(method.upper(), url, **kwargs) as resp:
                text = await resp.text()
                try:
                    parsed: Any = json.loads(text)
                except ValueError:
                    parsed = text
                if resp.status >= 400:
                    raise StepFailed(f"HTTP {resp.status} from {url}", value={"status": resp.status, "body": parsed})
                return {"status": resp.status, "body": parsed}
        except aiohttp.ClientError as e:
            raise StepFailed(f"HTTP request failed: {e}") from None
        except asyncio.TimeoutError:
            raise StepFailed("HTTP request timed out") from None

    # ----- missions ------------------------------------------------------------------------

    def _connector_names(self) -> set[str]:
        return set(self.registry.names())

    def connector_status(self) -> dict[str, dict[str, Any]]:
        """Configured connectors, plus any name a deployed mission refers to.

        A mission is usually written before the broker or the PLC exists, so the
        editor still needs those names to offer; they come back with
        ``configured: false`` instead of disappearing."""
        out = {name: {**status, "configured": True} for name, status in self.registry.status().items()}
        for stored in self.store.missions.values():
            m = stored.mission
            referenced = {str(s.params["connector"]) for s in m.iter_steps() if s.params.get("connector")}
            for t in (*m.triggers, *m.interrupts):
                if t.connector:
                    referenced.add(t.connector)
            for s in m.iter_steps():
                src = s.params.get("source")
                if isinstance(src, dict) and src.get("connector"):
                    referenced.add(str(src["connector"]))
            for name in referenced:
                out.setdefault(name, {"type": "", "connected": False, "available": False, "reason": "not configured on this robot", "config": {}, "configured": False})
        return out

    def validate(self, doc: dict[str, Any]) -> tuple[list[Finding], list[Finding], Mission | None]:
        names = self.store.names()
        if isinstance(doc, dict) and isinstance(doc.get("name"), str):
            names = names | {doc["name"]}
        errors, warnings, m = validate_mission(
            doc,
            sites=self.store.sites,
            missions=names,
            connectors=self._connector_names(),
            capabilities=self.backend.capabilities(),
            trigger_capabilities=self.registry.trigger_capabilities(),
        )
        if m is not None:
            others = {n: s.mission for n, s in self.store.missions.items() if n != m.name}
            others[m.name] = m
            errors.extend(check_mission_cycles(others))
        return errors, warnings, m

    async def deploy_mission(self, doc: dict[str, Any]) -> tuple[Mission, list[Finding]]:
        errors, warnings, m = self.validate(doc)
        if errors or m is None:
            raise MissionValidationError(errors)
        saved = self.store.save(doc)
        self._render_trees(saved)
        await self.arm_mission(saved)
        cur = self.dispatcher.current
        if cur and cur.mission == saved.name:
            warnings.append(Finding("warning", [], "a run of this mission is active; changes apply to the next run"))
        self.events.emit("missions.changed", names=[saved.name])
        self.events.log("info", f"mission '{saved.name}' deployed (v{saved.version})")
        return saved, warnings

    def export_project(self, name: str = "") -> dict[str, Any]:
        from .project import export_project

        return export_project(
            self.store,
            name or self.config.home.name,
            {"request_topic": self.config.request_topic, "answer_topic": self.config.answer_topic},
        )

    async def import_project(self, doc: Any, replace: bool = False) -> tuple[bool, dict[str, Any]]:
        """Validate a whole project, then write it and re-arm triggers. Nothing is
        written when any part is invalid."""
        from .project import check_project, write_project

        errors, warnings, book, _missions = check_project(
            doc,
            connectors=self._connector_names(),
            capabilities=self.backend.capabilities(),
            trigger_capabilities=self.registry.trigger_capabilities(),
        )
        if errors or book is None:
            return False, {"errors": [e.as_dict() for e in errors], "warnings": [w.as_dict() for w in warnings]}
        cur = self.dispatcher.current
        result = write_project(self.store, doc, book, replace=replace, protect={cur.mission} if cur else set())
        if self._current_map not in book.maps:
            self.set_current_map(book.default_map)
        for name in result["deleted"]:
            await self.triggers.disarm(name)
            await self.triggers.disarm(f"{name}:interrupts")
            self.trigger_problems.pop(name, None)
        for name in result["saved"]:
            m = self.store.get(name)
            if m is not None:
                self._render_trees(m)
                await self.arm_mission(m)
        self.events.emit("sites.changed")
        self.events.emit("missions.changed", names=sorted({*result["saved"], *result["deleted"]}))
        self.events.log("info", f"project imported: {len(result['saved'])} saved, {len(result['deleted'])} deleted")
        return True, {**result, "warnings": [w.as_dict() for w in warnings]}

    async def delete_mission(self, name: str) -> bool:
        await self.triggers.disarm(name)
        await self.triggers.disarm(f"{name}:interrupts")
        ok = self.store.delete(name)
        if ok:
            self.events.emit("missions.changed", names=[name])
        return ok

    def _render_trees(self, mission: Mission) -> None:
        from .bt import bt_file_name, render_behavior_tree

        for step in mission.iter_steps():
            spec = step.params.get("behavior_tree")
            if isinstance(spec, dict):
                try:
                    self.store.write_bt(bt_file_name(mission.name, step.id), render_behavior_tree(spec))
                except ValueError as e:
                    log.warning("%s/%s: %s", mission.name, step.id, e)

    async def arm_mission(self, mission: Mission) -> None:
        if mission.is_global:
            problems = await self.triggers.arm_specs("global:interrupts", mission.interrupts, self._on_global_interrupt)
        else:
            problems = await self.triggers.arm_specs(mission.name, mission.triggers, lambda spec, payload: self._on_trigger(mission.name, spec, payload))
        if problems:
            self.trigger_problems[mission.name] = problems
        else:
            self.trigger_problems.pop(mission.name, None)

    async def _on_trigger(self, mission_name: str, spec: TriggerSpec, payload: dict[str, Any]) -> None:
        inputs = self._inputs_from(spec, payload)
        if inputs is None:
            return
        target = spec.run or mission_name
        kind = "interrupt" if spec.run else "trigger"
        accepted, run, reason = await self.dispatcher.submit(target, inputs, RunSource(kind, spec.id, spec.summary()), policy=spec.policy, priority=spec.priority)
        self.events.log("info" if accepted else "warn", f"{kind} '{spec.id}' -> {target}: {reason}" + (f" (run {run.id})" if run else ""))

    async def _on_global_interrupt(self, spec: TriggerSpec, payload: dict[str, Any]) -> None:
        await self._on_trigger("global", spec, payload)

    def _inputs_from(self, spec: TriggerSpec, payload: dict[str, Any]) -> dict[str, Any] | None:
        scope = {**self.trigger_scope(), "payload": payload}
        inputs: dict[str, Any] = {}
        for k, expr in spec.set.items():
            try:
                inputs[k] = evaluate(expr, scope)
            except ExpressionError as e:
                self.events.log("warn", f"trigger '{spec.id}': cannot compute input '{k}': {e}")
                return None
        return inputs

    async def _on_run_started(self, run: Run, ctx: RunContext | None) -> None:
        if ctx is None or run.parent_run_id:
            return
        m = self.store.get(run.mission)
        if m and m.interrupts:
            await self.triggers.arm_specs(f"{run.mission}:interrupts", m.interrupts, lambda spec, payload: self._on_trigger(run.mission, spec, payload))

    async def _on_run_finished(self, run: Run, ctx: RunContext | None) -> None:
        await self.triggers.disarm(f"{run.mission}:interrupts")
        if run.status.finished:
            self.internal.mission_done(run.mission, run.id, "success" if run.status == RunStatus.SUCCEEDED else "failed")

    async def run_mission(self, name: str, inputs: dict[str, Any] | None, source: RunSource, policy: str | None = None, priority: int | None = None) -> tuple[bool, Run | None, str]:
        return await self.dispatcher.submit(name, inputs, source, policy, priority)

    # ----- status ----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        d = self.dispatcher.status()
        return {
            "runner": {
                "version": __version__,
                "uptime_s": round(time.monotonic() - self.started_mono, 1),
                "backend": self.backend.name,
                "home": str(self.config.home),
                "trigger_problems": self.trigger_problems,
            },
            **d,
            "robot": self.backend.robot_state().as_dict(),
            "current_map": self._current_map,
            "connectors": self.connector_status(),
            "prompt": self.prompts.current.as_dict() if self.prompts.current else None,
            "armed": self.triggers.armed_summary(),
            "live": {"available": self.live.available() if self.live else {}, "layers": sorted(self.live.layers) if self.live else []},
        }

    def capabilities(self) -> dict[str, Any]:
        steps = self.backend.capabilities()
        for t, c in (("mqtt.publish", "mqtt"), ("modbus.write", "modbus_tcp")):
            has = [x for x in self.registry.all() if x.type in (c, "modbus")] if c != "mqtt" else [x for x in self.registry.all() if x.type == "mqtt"]
            steps[t] = {"available": bool(has) and any(x.available for x in has), "reason": "" if has else f"no {c} connector configured"}
        gpio = self.registry.get("gpio")
        steps["gpio.write"] = {"available": bool(gpio and gpio.available), "reason": gpio.unavailable_reason if gpio else "no gpio connector"}
        for t in ("set", "if", "loop", "break", "wait", "wait_event", "ask_user", "log", "run_mission", "end", "http.request"):
            steps[t] = {"available": True, "reason": ""}
        return {
            "backend": self.backend.name,
            "version": __version__,
            "ros_distro": os.environ.get("ROS_DISTRO", ""),
            "steps": steps,
            "triggers": self.registry.trigger_capabilities(),
            "connectors": self.connector_status(),
            "bt_templates": ["navigate_with_recovery", "navigate_through_poses_with_recovery"],
            "live_layers": self.live.available() if self.live else {},
            "preview": {"route": True, "dry_run": True},
        }

    def examples(self) -> list[dict[str, Any]]:
        src = find_examples_dir()
        if src is None:
            return []
        out = []
        for f in sorted(src.glob("*.json")):
            try:
                doc = json.loads(f.read_text("utf-8"))
            except ValueError:
                continue
            if doc.get("schema") == "mission/1":
                out.append(doc)
        return out

    # ----- events → run log --------------------------------------------------------------------

    def _log_listener(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        run_id = event.get("run_id") or (event.get("run") or {}).get("id")
        if not run_id:
            return
        if t.startswith(("step.", "run.")) or t in ("log", "prompt", "prompt.answered"):
            try:
                self.run_log.add_event(str(run_id), event)
            except Exception:  # noqa: BLE001
                log.exception("run log failed")
        if t == "run.finished":
            try:
                self.run_log.prune()
            except Exception:  # noqa: BLE001
                pass

    async def _robot_ticker(self) -> None:
        last: dict[str, Any] | None = None
        last_emit = 0.0
        while True:
            await asyncio.sleep(0.5)
            try:
                st = self.backend.robot_state().as_dict()
            except Exception:  # noqa: BLE001
                continue
            now = time.monotonic()
            if st is not None and (st != last or now - last_emit > 5.0):
                last = st
                last_emit = now
                self.events.emit("robot", **st)
            tick = getattr(self.live, "tick", None)
            if tick is not None:
                try:
                    tick()
                except Exception:  # noqa: BLE001
                    log.exception("live relay tick failed")


def load_mission_file(path: str | os.PathLike[str]) -> Mission:
    return load_mission(json.loads(Path(path).read_text("utf-8")))
