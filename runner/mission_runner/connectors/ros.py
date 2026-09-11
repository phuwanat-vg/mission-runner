"""ROS side of the runner (nav2 backend only):

* ``ros.topic`` event sources (type auto-detected, dynamic subscriptions)
* the runner's ROS API: ``/mission/state`` + ``/mission/event`` topics,
  ``/missions/<name>/run`` and control services, and (when ``mission_msgs`` is
  built) ``/missions/run`` service + action with JSON inputs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from rosidl_runtime_py.utilities import get_message
from std_msgs.msg import String
from std_srvs.srv import Trigger

from ..backends.nav2 import to_plain
from ..model import EventSource
from ..types import RunSource, StepFailed
from .base import Armed, Connector, EventCallback

if TYPE_CHECKING:
    from ..runner import Runner

log = logging.getLogger("mission.ros")

try:
    from mission_msgs.action import RunMission as RunMissionAction
    from mission_msgs.srv import Answer as AnswerSrv
    from mission_msgs.srv import Api as ApiSrv
    from mission_msgs.srv import RunMission as RunMissionSrv

    HAVE_MISSION_MSGS = True
except ImportError:
    RunMissionAction = RunMissionSrv = AnswerSrv = ApiSrv = None  # type: ignore[assignment, misc]
    HAVE_MISSION_MSGS = False


class RosConnector(Connector):
    type = "ros"
    source_types = ("ros.topic",)
    singleton = True

    def __init__(self, runner: Runner, name: str = "ros"):
        super().__init__(name, {})
        self.runner = runner
        self.backend = runner.backend
        self.node = self.backend.node  # type: ignore[attr-defined]
        self.loop = asyncio.get_event_loop()
        self._subs: dict[str, dict[str, Any]] = {}  # topic -> {sub, type, callbacks}
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._retry_task: asyncio.Task[None] | None = None
        self._services: dict[str, Any] = {}
        self._mission_services: dict[str, Any] = {}
        self._action_server: Any = None
        self._state_pub: Any = None
        self._event_pub: Any = None
        self._unsubscribe = runner.events.subscribe(self._on_event)

    # ----- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        node = self.node
        latched = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.RELIABLE, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        self._state_pub = node.create_publisher(String, "/mission/state", latched)
        self._event_pub = node.create_publisher(String, "/mission/event", 50)
        cbg = self.backend.cb_group  # type: ignore[attr-defined]
        self._services["cancel"] = node.create_service(Trigger, "/mission/cancel", self._srv_cancel, callback_group=cbg)
        self._services["pause"] = node.create_service(Trigger, "/mission/pause", self._srv_pause, callback_group=cbg)
        self._services["resume"] = node.create_service(Trigger, "/mission/resume", self._srv_resume, callback_group=cbg)
        self._services["stop"] = node.create_service(Trigger, "/mission/stop", self._srv_stop, callback_group=cbg)
        if HAVE_MISSION_MSGS:
            from rclpy.action import ActionServer, CancelResponse, GoalResponse

            self._services["run"] = node.create_service(RunMissionSrv, "/missions/run", self._srv_run, callback_group=cbg)
            self._services["answer"] = node.create_service(AnswerSrv, "/mission/answer", self._srv_answer, callback_group=cbg)
            self._services["api"] = node.create_service(ApiSrv, "/mission/api", self._srv_api, callback_group=cbg)
            self._action_server = ActionServer(
                node,
                RunMissionAction,
                "/missions/run",
                execute_callback=self._action_execute,
                goal_callback=lambda _req: GoalResponse.ACCEPT,
                cancel_callback=lambda _handle: CancelResponse.ACCEPT,
                callback_group=cbg,
            )
        else:
            log.warning("mission_msgs is not built: only std_srvs services and String topics are available")
        self.sync_missions(sorted(self.runner.store.names()))
        self._retry_task = asyncio.create_task(self._retry_pending(), name="ros-subscribe-retry")
        self._publish_state()

    async def stop(self) -> None:
        self._unsubscribe()
        if self._retry_task:
            self._retry_task.cancel()
        with self._lock:
            for entry in self._subs.values():
                try:
                    self.node.destroy_subscription(entry["sub"])
                except Exception:  # noqa: BLE001
                    pass
            self._subs.clear()
        for srv in [*self._services.values(), *self._mission_services.values()]:
            try:
                self.node.destroy_service(srv)
            except Exception:  # noqa: BLE001
                pass
        if self._action_server is not None:
            try:
                self._action_server.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ----- per-mission services ------------------------------------------------------------

    def sync_missions(self, names: list[str]) -> None:
        """Create /missions/<name>/run for every mission, drop stale ones."""
        cbg = self.backend.cb_group  # type: ignore[attr-defined]
        wanted = {n for n in names if n != "global"}
        for n in list(self._mission_services):
            if n not in wanted:
                try:
                    self.node.destroy_service(self._mission_services.pop(n))
                except Exception:  # noqa: BLE001
                    pass
        for n in wanted:
            if n not in self._mission_services:

                def make(name: str) -> Any:
                    return lambda req, resp: self._srv_run_named(name, req, resp)

                try:
                    self._mission_services[n] = self.node.create_service(Trigger, f"/missions/{n}/run", make(n), callback_group=cbg)
                except Exception as e:  # noqa: BLE001
                    log.warning("cannot create service for mission %s: %s", n, e)

    # ----- helpers ----------------------------------------------------------------------------

    def _call(self, coro: Any, timeout: float = 10.0) -> Any:
        """Run a coroutine on the asyncio loop from an executor thread."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def _submit(self, name: str, inputs: dict[str, Any], detail: str) -> tuple[bool, str, str]:
        accepted, run, reason = self._call(self.runner.run_mission(name, inputs, RunSource("manual", "ros", detail)))
        return accepted, run.id if run else "", reason

    # ----- service callbacks (executor threads) ---------------------------------------------------

    def _srv_run_named(self, name: str, req: Any, resp: Any) -> Any:
        accepted, run_id, reason = self._submit(name, {}, f"service /missions/{name}/run")
        resp.success = accepted
        resp.message = f"{reason} {run_id}".strip()
        return resp

    def _srv_run(self, req: Any, resp: Any) -> Any:
        try:
            inputs = json.loads(req.inputs_json) if req.inputs_json else {}
        except ValueError as e:
            resp.accepted, resp.run_id, resp.message = False, "", f"inputs_json: {e}"
            return resp
        accepted, run_id, reason = self._submit(req.name, inputs if isinstance(inputs, dict) else {}, "service /missions/run")
        resp.accepted, resp.run_id, resp.message = accepted, run_id, reason
        return resp

    def _srv_api(self, req: Any, resp: Any) -> Any:
        """The whole HTTP API over one ROS service, so a GUI connected through
        foxglove_bridge needs no second connection to the robot."""
        try:
            body = json.loads(req.body_json) if req.body_json.strip() else None
        except ValueError as e:
            resp.ok, resp.status, resp.body_json, resp.message = False, 400, "", f"body_json: {e}"
            return resp
        try:
            status, payload = self._call(self.runner.call_api(req.method or "GET", req.path, body), timeout=90.0)
        except Exception as e:  # noqa: BLE001 - never let a GUI request kill the node
            resp.ok, resp.status, resp.body_json, resp.message = False, 500, "", f"{type(e).__name__}: {e}"
            return resp
        resp.ok = 200 <= status < 300
        resp.status = int(status)
        resp.body_json = json.dumps(payload, default=str)
        resp.message = "" if resp.ok else str((payload or {}).get("error", ""))
        return resp

    def _srv_answer(self, req: Any, resp: Any) -> Any:
        ok, msg = self.runner.prompts.answer(req.prompt_id, req.answer)
        resp.ok, resp.message = ok, msg
        return resp

    def _srv_cancel(self, req: Any, resp: Any) -> Any:
        cur = self.runner.dispatcher.current
        if cur is None:
            resp.success, resp.message = False, "nothing is running"
            return resp
        ok, msg = self._call(self.runner.dispatcher.cancel(cur.id, "canceled via ROS"))
        resp.success, resp.message = ok, msg
        return resp

    def _srv_pause(self, req: Any, resp: Any) -> Any:
        ok, msg = self._call(self.runner.dispatcher.pause())
        resp.success, resp.message = ok, msg
        return resp

    def _srv_resume(self, req: Any, resp: Any) -> Any:
        ok, msg = self._call(self.runner.dispatcher.resume())
        resp.success, resp.message = ok, msg
        return resp

    def _srv_stop(self, req: Any, resp: Any) -> Any:
        self._call(self.runner.backend.cancel())
        asyncio.run_coroutine_threadsafe(self.runner.dispatcher.stop_all("stopped via ROS"), self.loop)
        resp.success, resp.message = True, "stopping"
        return resp

    def _action_execute(self, goal_handle: Any) -> Any:
        req = goal_handle.request
        result = RunMissionAction.Result()
        try:
            inputs = json.loads(req.inputs_json) if req.inputs_json else {}
        except ValueError as e:
            result.success, result.status, result.message = False, "rejected", f"inputs_json: {e}"
            goal_handle.abort()
            return result
        accepted, run_id, reason = self._submit(req.name, inputs if isinstance(inputs, dict) else {}, "action /missions/run")
        if not accepted:
            result.success, result.status, result.message = False, "rejected", reason
            goal_handle.abort()
            return result
        last_step = None
        while True:
            time.sleep(0.2)
            if goal_handle.is_cancel_requested:
                self._call(self.runner.dispatcher.cancel(run_id, "canceled via action"))
            run = self.runner.dispatcher.find(run_id)
            if run is None:
                rec = self.runner.run_log.get_run(run_id) or {}
                status = str(rec.get("status", "unknown"))
                result.success = status == "succeeded"
                result.status = status
                result.message = str(rec.get("error") or "")
                result.result_json = json.dumps(rec.get("result"), default=str)
                result.run_id = run_id
                if status == "canceled" and goal_handle.is_cancel_requested:
                    goal_handle.canceled()
                elif result.success:
                    goal_handle.succeed()
                else:
                    goal_handle.abort()
                return result
            step = run.step
            fb = RunMissionAction.Feedback()
            fb.run_id = run_id
            fb.status = run.status.value
            fb.step_id = str(step.get("id", "")) if step else ""
            fb.step_name = str(step.get("name", "")) if step else ""
            fb.json = json.dumps({"feedback": run.feedback, "path": step.get("path") if step else None}, default=str)
            if step != last_step or run.feedback:
                last_step = step
                goal_handle.publish_feedback(fb)

    # ----- events -> topics -----------------------------------------------------------------------

    def _on_event(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if self._event_pub is not None and t not in ("robot", "feedback"):
            try:
                self._event_pub.publish(String(data=json.dumps(event, default=str)))
            except Exception:  # noqa: BLE001
                pass
        if t in ("run.queued", "run.started", "run.finished", "run.suspended", "run.resumed", "run.paused", "prompt", "prompt.answered", "map.changed", "step.started"):
            self._publish_state()
        if t == "missions.changed":
            self.sync_missions(sorted(self.runner.store.names()))

    def _publish_state(self) -> None:
        if self._state_pub is None:
            return
        try:
            self._state_pub.publish(String(data=json.dumps(self.runner.status(), default=str)))
        except Exception:  # noqa: BLE001
            pass

    # ----- ros.topic sources ------------------------------------------------------------------------

    async def arm(self, source: EventSource, callback: EventCallback) -> Armed:
        topic = str(source.params.get("topic", "")).strip()
        if not topic:
            raise StepFailed("ros.topic needs a topic")
        msg_type = str(source.params.get("msg_type") or "").strip()
        with self._lock:
            entry = self._subs.get(topic)
            if entry is None:
                entry = {"sub": None, "type": msg_type, "callbacks": []}
                self._subs[topic] = entry
            entry["callbacks"].append(callback)
        self._ensure_subscription(topic)

        def disarm() -> None:
            with self._lock:
                e = self._subs.get(topic)
                if not e:
                    return
                if callback in e["callbacks"]:
                    e["callbacks"].remove(callback)
                if not e["callbacks"]:
                    self._subs.pop(topic, None)
                    self._pending.discard(topic)
                    if e["sub"] is not None:
                        try:
                            self.node.destroy_subscription(e["sub"])
                        except Exception:  # noqa: BLE001
                            pass

        return Armed(disarm, source.summary())

    def _ensure_subscription(self, topic: str) -> None:
        with self._lock:
            entry = self._subs.get(topic)
            if entry is None or entry["sub"] is not None:
                return
            msg_type = entry["type"] or self._detect_type(topic)
            if not msg_type:
                self._pending.add(topic)
                return
            try:
                cls = get_message(msg_type)
            except Exception as e:  # noqa: BLE001
                log.warning("ros.topic %s: unknown type %s (%s)", topic, msg_type, e)
                self._pending.add(topic)
                return

            def cb(msg: Any, _topic: str = topic) -> None:
                payload = to_plain(msg)
                if not isinstance(payload, dict):
                    payload = {"value": payload}
                payload["topic"] = _topic
                with self._lock:
                    cbs = list(self._subs.get(_topic, {}).get("callbacks", []))
                for c in cbs:
                    c(dict(payload))

            entry["type"] = msg_type
            entry["sub"] = self.node.create_subscription(cls, topic, cb, 10, callback_group=self.backend.cb_group)  # type: ignore[attr-defined]
            self._pending.discard(topic)
            log.info("subscribed %s (%s)", topic, msg_type)

    def _detect_type(self, topic: str) -> str:
        for name, types in self.node.get_topic_names_and_types():
            if name == topic and types:
                return str(types[0])
        return ""

    async def _retry_pending(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            for topic in list(self._pending):
                self._ensure_subscription(topic)
