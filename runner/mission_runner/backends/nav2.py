"""Nav2 backend: ``nav2_simple_commander.BasicNavigator`` driven from asyncio.

Threading model
* ``BasicNavigator`` spins itself inside its blocking calls, so every call to it
  happens on a worker thread (``asyncio.to_thread``) under ``_nav_lock``.
* A separate helper node (TF, battery, generic publish/service/action, the
  ROS API of the runner) is spun by a ``MultiThreadedExecutor`` thread.
* Cancellation: :meth:`cancel` sets a flag; the worker thread polls it between
  ``isTaskComplete()`` calls and issues ``cancelTask()`` itself, so rclpy is
  never used from two threads at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from nav_msgs.msg import Path
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from rosidl_runtime_py.convert import message_to_ordereddict
from rosidl_runtime_py.set_message import set_message_fields
from rosidl_runtime_py.utilities import get_action, get_message, get_service

from ..config import RunnerConfig
from ..events import EventBus
from ..types import RobotState, StepFailed, StepTimeout, TaskCanceled
from .base import FeedbackCb, NavBackend, NavFeedback, Pose

log = logging.getLogger("mission.nav2")


def _duration_s(d: Any) -> float | None:
    try:
        return float(d.sec) + float(d.nanosec) * 1e-9
    except AttributeError:
        return None


def to_plain(value: Any) -> Any:
    """ROS message (or container of them) -> JSON-friendly Python."""
    if hasattr(value, "get_fields_and_field_types"):
        return json.loads(json.dumps(message_to_ordereddict(value), default=_json_default))
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    return json.loads(json.dumps(value, default=_json_default))


def _json_default(o: Any) -> Any:
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (bytes, bytearray)):
        return list(o)
    if hasattr(o, "get_fields_and_field_types"):
        return message_to_ordereddict(o)
    return str(o)


class Nav2Backend(NavBackend):
    name = "nav2"

    def __init__(self, config: RunnerConfig, events: EventBus):
        self.config = config
        self.events = events
        self.nav: BasicNavigator | None = None
        self.node: Node | None = None
        self._executor: MultiThreadedExecutor | None = None
        self._spin_thread: threading.Thread | None = None
        self._pose_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._nav_lock = threading.Lock()
        self._cancel_requested = threading.Event()
        self._state = RobotState(frame="map")
        self._tf_buffer: Any = None
        self._publishers: dict[tuple[str, str], Any] = {}
        self._initial_pose_pub: Any = None
        self._map_frame = "map"
        self.cb_group = ReentrantCallbackGroup()
        self._loop: asyncio.AbstractEventLoop | None = None
        nav2_cfg = getattr(config, "nav2", None) or {}
        self.wait_nodes: list[str] = list(nav2_cfg.get("wait_nodes", ["bt_navigator"]))
        self.localizer: str = str(nav2_cfg.get("localizer", "amcl"))
        self.robot_frame = config.robot_frame

    # ----- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not rclpy.ok():
            rclpy.init(args=None)
        ns = self.config.ros_namespace or ""
        self.node = rclpy.create_node(self.config.ros_node_name, namespace=ns or None)
        self.nav = BasicNavigator(node_name="mission_navigator", namespace=ns)

        from tf2_ros import Buffer, TransformListener

        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self.node, spin_thread=False)

        try:
            from sensor_msgs.msg import BatteryState

            self.node.create_subscription(BatteryState, self.config.battery_topic, self._on_battery, 10, callback_group=self.cb_group)
        except ImportError:
            pass

        self._executor = MultiThreadedExecutor(num_threads=4)
        self._executor.add_node(self.node)
        self._spin_thread = threading.Thread(target=self._spin, name="ros-spin", daemon=True)
        self._spin_thread.start()
        self._pose_thread = threading.Thread(target=self._pose_loop, name="ros-pose", daemon=True)
        self._pose_thread.start()
        log.info("nav2 backend started (node %s, namespace '%s')", self.config.ros_node_name, ns)

    def _spin(self) -> None:
        assert self._executor is not None
        try:
            self._executor.spin()
        except Exception:  # noqa: BLE001
            if not self._stop.is_set():
                log.exception("ROS executor stopped")

    async def stop(self) -> None:
        self._stop.set()
        self._cancel_requested.set()
        if self._executor is not None:
            self._executor.shutdown(timeout_sec=2.0)
        if self.nav is not None:
            try:
                self.nav.destroy_node()
            except Exception:  # noqa: BLE001
                pass
        if self.node is not None:
            try:
                self.node.destroy_node()
            except Exception:  # noqa: BLE001
                pass
        if rclpy.ok():
            rclpy.shutdown()

    # ----- robot state ------------------------------------------------------------

    def _on_battery(self, msg: Any) -> None:
        pct = float(msg.percentage)
        if pct > 1.0:
            pct /= 100.0
        if not math.isnan(pct):
            self._state.battery = max(0.0, min(1.0, pct))

    def _pose_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.2)
            if self._tf_buffer is None:
                continue
            try:
                t = self._tf_buffer.lookup_transform(self._map_frame, self.robot_frame, rclpy.time.Time())
            except Exception:  # noqa: BLE001 - TF not available yet
                continue
            q = t.transform.rotation
            self._state.x = float(t.transform.translation.x)
            self._state.y = float(t.transform.translation.y)
            self._state.yaw_deg = math.degrees(2.0 * math.atan2(q.z, q.w))
            self._state.frame = self._map_frame
            self._state.updated_mono = time.monotonic()

    def robot_state(self) -> RobotState:
        return self._state

    def capabilities(self) -> dict[str, dict[str, Any]]:
        nav = self.nav
        has = lambda *names: any(hasattr(nav, n) for n in names)  # noqa: E731
        caps = {t: {"available": True, "reason": ""} for t in (
            "nav.wait_active", "nav.set_initial_pose", "nav.go_to_pose", "nav.go_through_poses", "nav.follow_waypoints", "nav.follow_path",
            "nav.compute_path", "nav.compute_path_through_poses", "nav.smooth_path", "nav.spin", "nav.backup", "nav.drive_on_heading",
            "nav.change_map", "nav.clear_costmap", "nav.lifecycle", "nav.cancel", "ros.publish", "ros.call_service", "ros.call_action", "ros.set_param",
        )}
        dock_ok = has("dockRobot", "dockRobotByPose", "dockRobotByID")
        caps["nav.dock"] = {"available": dock_ok, "reason": "" if dock_ok else "nav2_simple_commander has no docking API (needs Nav2 Iron or newer)"}
        caps["nav.undock"] = {"available": has("undockRobot"), "reason": "" if has("undockRobot") else "no undockRobot() in nav2_simple_commander"}
        return caps

    # ----- conversions -------------------------------------------------------------

    def _pose_msg(self, pose: Pose) -> PoseStamped:
        assert self.nav is not None
        p = PoseStamped()
        p.header.frame_id = pose.frame or self._map_frame
        p.header.stamp = self.nav.get_clock().now().to_msg()
        p.pose.position.x = float(pose.x)
        p.pose.position.y = float(pose.y)
        yaw = math.radians(pose.yaw_deg)
        p.pose.orientation.z = math.sin(yaw / 2.0)
        p.pose.orientation.w = math.cos(yaw / 2.0)
        return p

    def _path_msg(self, path: dict[str, Any]) -> Path:
        assert self.nav is not None
        msg = Path()
        frame = str(path.get("frame") or self._map_frame)
        msg.header.frame_id = frame
        msg.header.stamp = self.nav.get_clock().now().to_msg()
        for p in path.get("poses") or []:
            msg.poses.append(self._pose_msg(Pose(float(p["x"]), float(p["y"]), float(p.get("yaw_deg", 0.0)), frame)))
        return msg

    @staticmethod
    def _path_dict(msg: Path) -> dict[str, Any]:
        poses = []
        for ps in msg.poses:
            q = ps.pose.orientation
            poses.append({"x": float(ps.pose.position.x), "y": float(ps.pose.position.y), "yaw_deg": math.degrees(2.0 * math.atan2(q.z, q.w))})
        return {"frame": msg.header.frame_id, "poses": poses}

    @staticmethod
    def _feedback(fb: Any) -> NavFeedback:
        out = NavFeedback()
        if hasattr(fb, "distance_remaining"):
            out.distance_remaining = float(fb.distance_remaining)
        if hasattr(fb, "number_of_recoveries"):
            out.recoveries = int(fb.number_of_recoveries)
        if hasattr(fb, "estimated_time_remaining"):
            out.eta_s = _duration_s(fb.estimated_time_remaining)
        if hasattr(fb, "navigation_time"):
            out.navigation_time_s = _duration_s(fb.navigation_time)
        if hasattr(fb, "current_waypoint"):
            out.current_waypoint = int(fb.current_waypoint)
        if hasattr(fb, "angular_distance_traveled"):
            out.extra["angular_distance_traveled"] = round(math.degrees(float(fb.angular_distance_traveled)), 1)
        if hasattr(fb, "distance_traveled"):
            out.extra["distance_traveled"] = round(float(fb.distance_traveled), 3)
        if hasattr(fb, "state"):
            out.extra["state"] = int(fb.state) if not isinstance(fb.state, str) else fb.state
        if hasattr(fb, "num_retries"):
            out.extra["retries"] = int(fb.num_retries)
        return out

    # ----- task runner -------------------------------------------------------------

    async def _task(self, start: Callable[[], Any], on_feedback: FeedbackCb, label: str, result_extra: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
        """Start a BasicNavigator task on the worker thread and wait for it."""
        nav = self.nav
        assert nav is not None
        loop = self._loop or asyncio.get_running_loop()

        def work() -> dict[str, Any]:
            with self._nav_lock:
                self._cancel_requested.clear()
                accepted = start()
                if accepted is False:
                    raise StepFailed(f"{label}: goal rejected by the action server")
                last_fb = 0.0
                while not nav.isTaskComplete():
                    if self._cancel_requested.is_set() or self._stop.is_set():
                        nav.cancelTask()
                        deadline = time.monotonic() + 5.0
                        while not nav.isTaskComplete() and time.monotonic() < deadline:
                            time.sleep(0.05)
                        raise TaskCanceled()
                    now = time.monotonic()
                    if now - last_fb >= 0.25:
                        last_fb = now
                        fb = nav.getFeedback()
                        if fb is not None:
                            loop.call_soon_threadsafe(on_feedback, self._feedback(fb))
                    time.sleep(0.1)
                result = nav.getResult()
                if result == TaskResult.SUCCEEDED:
                    out = {"result": "succeeded"}
                    if result_extra:
                        try:
                            out.update(result_extra())
                        except Exception:  # noqa: BLE001
                            pass
                    return out
                if result == TaskResult.CANCELED:
                    raise TaskCanceled()
                detail = self._error_detail()
                raise StepFailed(f"{label} failed ({getattr(result, 'name', result)}){detail}")

        try:
            return await asyncio.to_thread(work)
        except asyncio.CancelledError:
            self._cancel_requested.set()
            raise

    def _error_detail(self) -> str:
        nav = self.nav
        try:
            res = nav.result_future.result().result  # type: ignore[union-attr]
            code = getattr(res, "error_code", None)
            msg = getattr(res, "error_msg", "")
            if code:
                return f": error_code={code}" + (f" {msg}" if msg else "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _missed_waypoints(self) -> dict[str, Any]:
        res = self.nav.result_future.result().result  # type: ignore[union-attr]
        return {"missed": list(getattr(res, "missed_waypoints", []))}

    async def _blocking(self, fn: Callable[[], Any]) -> Any:
        def work() -> Any:
            with self._nav_lock:
                return fn()

        return await asyncio.to_thread(work)

    # ----- navigation API --------------------------------------------------------------

    def _wait_lifecycle(self, names: list[str], timeout_s: float, should_stop: Callable[[], bool]) -> None:
        """Block (worker thread) until every lifecycle node in ``names`` is ACTIVE."""
        node = self.node
        assert node is not None
        from lifecycle_msgs.srv import GetState

        deadline = time.monotonic() + timeout_s
        for name in names:
            client = node.create_client(GetState, f"{name}/get_state", callback_group=self.cb_group)
            try:
                while True:
                    if should_stop():
                        raise TaskCanceled()
                    if time.monotonic() > deadline:
                        raise StepTimeout(f"{name} is not active after {timeout_s:g} s")
                    if not client.wait_for_service(timeout_sec=1.0):
                        continue
                    fut = client.call_async(GetState.Request())
                    t0 = time.monotonic()
                    while not fut.done() and time.monotonic() - t0 < 2.0:
                        time.sleep(0.05)
                    if fut.done() and fut.result() is not None and fut.result().current_state.id == 3:  # ACTIVE
                        break
                    time.sleep(0.5)
            finally:
                node.destroy_client(client)

    def _localizer_nodes(self) -> list[str]:
        """Lifecycle nodes to wait for before localization ("" or robot_localization: none)."""
        return [self.localizer] if self.localizer and self.localizer != "robot_localization" else []

    async def wait_active(self, timeout_s: float | None) -> None:
        names = list(self.wait_nodes)
        for n in self._localizer_nodes():
            if n not in names:
                names.append(n)
        self._cancel_requested.clear()
        await asyncio.to_thread(self._wait_lifecycle, names, timeout_s or 3600.0, lambda: self._stop.is_set() or self._cancel_requested.is_set())
        self._state.nav_active = True

    # ----- localization ----------------------------------------------------------------

    def _tf_localized(self) -> bool:
        if self._tf_buffer is None:
            return False
        try:
            self._tf_buffer.lookup_transform(self._map_frame, self.robot_frame, rclpy.time.Time())
        except Exception:  # noqa: BLE001 - no map -> robot transform (yet)
            return False
        return True

    async def is_localized(self, grace_s: float = 0.0) -> bool:
        loop = asyncio.get_running_loop()
        end = loop.time() + grace_s
        while True:
            if self._tf_localized():
                return True
            if loop.time() >= end:
                return False
            await asyncio.sleep(0.2)

    async def wait_localizer(self, timeout_s: float) -> None:
        names = self._localizer_nodes()
        if names:
            await asyncio.to_thread(self._wait_lifecycle, names, timeout_s, self._stop.is_set)

    async def set_initial_pose(self, pose: Pose, *, localizer_timeout_s: float = 30.0, cancellable: bool = True) -> None:
        """Wait for the localizer, then publish ``/initialpose`` every second until
        AMCL answers on ``/amcl_pose`` or the map -> robot TF appears (30 s max).
        Without a localizer (FAST-LIO2, ...) the pose is published once."""
        from geometry_msgs.msg import PoseWithCovarianceStamped

        from ..localize import INITIAL_POSE_COVARIANCE, confirm_initial_pose

        node = self.node
        assert node is not None
        names = self._localizer_nodes()
        aborted = threading.Event()
        if cancellable:
            self._cancel_requested.clear()

        def should_stop() -> bool:
            return self._stop.is_set() or aborted.is_set() or (cancellable and self._cancel_requested.is_set())

        if self._initial_pose_pub is None:
            self._initial_pose_pub = node.create_publisher(PoseWithCovarianceStamped, "initialpose", QoSProfile(depth=10))
        pub = self._initial_pose_pub

        def publish() -> None:
            msg = PoseWithCovarianceStamped()
            msg.header.frame_id = pose.frame or self._map_frame
            msg.header.stamp = node.get_clock().now().to_msg()
            msg.pose.pose.position.x = float(pose.x)
            msg.pose.pose.position.y = float(pose.y)
            yaw = math.radians(pose.yaw_deg)
            msg.pose.pose.orientation.z = math.sin(yaw / 2.0)
            msg.pose.pose.orientation.w = math.cos(yaw / 2.0)
            cov = [0.0] * 36
            cov[0], cov[7], cov[35] = INITIAL_POSE_COVARIANCE
            msg.pose.covariance = cov
            pub.publish(msg)

        def work() -> None:
            if names:
                self._wait_lifecycle(names, localizer_timeout_s, should_stop)
            else:
                # One message only: give discovery a moment so it is not lost.
                t0 = time.monotonic()
                while pub.get_subscription_count() == 0 and time.monotonic() - t0 < 5.0 and not should_stop():
                    time.sleep(0.1)
                publish()
                log.info("initial pose published once on /initialpose (no localizer configured)")
                return
            last_amcl = [0.0]

            def on_amcl(_msg: Any) -> None:
                last_amcl[0] = time.monotonic()

            # Volatile on purpose: AMCL latches amcl_pose, and an old latched pose is no confirmation.
            sub = node.create_subscription(PoseWithCovarianceStamped, "amcl_pose", on_amcl, QoSProfile(depth=5), callback_group=self.cb_group)
            tf_before = self._tf_localized()
            try:
                n = confirm_initial_pose(
                    publish,
                    lambda t_first: last_amcl[0] > t_first or (not tf_before and self._tf_localized()),
                    should_stop=should_stop,
                )
            finally:
                node.destroy_subscription(sub)
            log.info("initial pose confirmed by %s after %d message(s)", self.localizer, n)

        try:
            await asyncio.to_thread(work)
        except asyncio.CancelledError:
            aborted.set()
            if cancellable:
                self._cancel_requested.set()
            raise

    async def go_to_pose(self, pose: Pose, behavior_tree: str, on_feedback: FeedbackCb) -> dict[str, Any]:
        msg = self._pose_msg(pose)
        return await self._task(lambda: self.nav.goToPose(msg, behavior_tree=behavior_tree), on_feedback, "go_to_pose")  # type: ignore[union-attr]

    async def go_through_poses(self, poses: list[Pose], behavior_tree: str, on_feedback: FeedbackCb) -> dict[str, Any]:
        msgs = [self._pose_msg(p) for p in poses]
        return await self._task(lambda: self.nav.goThroughPoses(msgs, behavior_tree=behavior_tree), on_feedback, "go_through_poses")  # type: ignore[union-attr]

    async def follow_waypoints(self, poses: list[Pose], on_feedback: FeedbackCb) -> dict[str, Any]:
        msgs = [self._pose_msg(p) for p in poses]
        return await self._task(lambda: self.nav.followWaypoints(msgs), on_feedback, "follow_waypoints", self._missed_waypoints)  # type: ignore[union-attr]

    async def follow_path(self, path: dict[str, Any], controller_id: str, goal_checker_id: str, on_feedback: FeedbackCb) -> dict[str, Any]:
        msg = self._path_msg(path)
        return await self._task(lambda: self.nav.followPath(msg, controller_id=controller_id, goal_checker_id=goal_checker_id), on_feedback, "follow_path")  # type: ignore[union-attr]

    async def compute_path(self, start: Pose | None, goal: Pose, planner_id: str, use_start: bool) -> dict[str, Any]:
        s = self._pose_msg(start) if start else self._pose_msg(Pose(0, 0, 0))
        g = self._pose_msg(goal)

        def work() -> dict[str, Any]:
            path = self.nav.getPath(s, g, planner_id=planner_id, use_start=use_start)  # type: ignore[union-attr]
            if path is None:
                raise StepFailed("compute_path failed" + self._error_detail())
            return self._path_dict(path)

        return await self._blocking(work)

    async def compute_path_through_poses(self, start: Pose | None, goals: list[Pose], planner_id: str, use_start: bool) -> dict[str, Any]:
        s = self._pose_msg(start) if start else self._pose_msg(Pose(0, 0, 0))
        gs = [self._pose_msg(g) for g in goals]

        def work() -> dict[str, Any]:
            path = self.nav.getPathThroughPoses(s, gs, planner_id=planner_id, use_start=use_start)  # type: ignore[union-attr]
            if path is None:
                raise StepFailed("compute_path_through_poses failed" + self._error_detail())
            return self._path_dict(path)

        return await self._blocking(work)

    async def smooth_path(self, path: dict[str, Any], smoother_id: str, max_duration_s: float, check_collision: bool) -> dict[str, Any]:
        msg = self._path_msg(path)

        def work() -> dict[str, Any]:
            out = self.nav.smoothPath(msg, smoother_id=smoother_id, max_duration=max_duration_s, check_for_collision=check_collision)  # type: ignore[union-attr]
            if out is None:
                raise StepFailed("smooth_path failed" + self._error_detail())
            return self._path_dict(out)

        return await self._blocking(work)

    async def spin(self, angle_rad: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]:
        return await self._task(lambda: self.nav.spin(spin_dist=angle_rad, time_allowance=int(time_allowance_s)), on_feedback, "spin")  # type: ignore[union-attr]

    async def backup(self, distance_m: float, speed_mps: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]:
        return await self._task(lambda: self.nav.backup(backup_dist=distance_m, backup_speed=speed_mps, time_allowance=int(time_allowance_s)), on_feedback, "backup")  # type: ignore[union-attr]

    async def drive_on_heading(self, distance_m: float, speed_mps: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]:
        return await self._task(lambda: self.nav.driveOnHeading(dist=distance_m, speed=speed_mps, time_allowance=int(time_allowance_s)), on_feedback, "drive_on_heading")  # type: ignore[union-attr]

    async def change_map(self, map_file: str) -> None:
        def work() -> None:
            r = self.nav.changeMap(map_file)  # type: ignore[union-attr]
            if r is False:
                raise StepFailed(f"change_map failed for {map_file}")

        await self._blocking(work)

    async def clear_costmap(self, which: str) -> None:
        nav = self.nav
        assert nav is not None
        fn = {"all": nav.clearAllCostmaps, "local": nav.clearLocalCostmap, "global": nav.clearGlobalCostmap}.get(which)
        if fn is None:
            raise StepFailed(f"unknown costmap '{which}'")
        await self._blocking(fn)

    async def dock(self, dock_id: str | None, dock_pose: Pose | None, dock_type: str, navigate_to_staging: bool, on_feedback: FeedbackCb) -> dict[str, Any]:
        nav = self.nav
        assert nav is not None

        def start() -> Any:
            if dock_id and hasattr(nav, "dockRobotByID"):
                try:
                    return nav.dockRobotByID(dock_id, nav_to_dock=navigate_to_staging)  # type: ignore[call-arg]
                except TypeError:
                    return nav.dockRobotByID(dock_id)
            if dock_pose is None:
                raise StepFailed("nav.dock needs dock_id or dock_pose")
            msg = self._pose_msg(dock_pose)
            fn = getattr(nav, "dockRobotByPose", None) or getattr(nav, "dockRobot", None)
            if fn is None:
                raise StepFailed("docking is not supported by this nav2_simple_commander")
            try:
                return fn(msg, dock_type, nav_to_dock=navigate_to_staging)  # type: ignore[misc]
            except TypeError:
                return fn(msg, dock_type)

        return await self._task(start, on_feedback, "dock")

    async def undock(self, dock_type: str, on_feedback: FeedbackCb) -> dict[str, Any]:
        nav = self.nav
        if not hasattr(nav, "undockRobot"):
            raise StepFailed("undocking is not supported by this nav2_simple_commander")
        return await self._task(lambda: nav.undockRobot(dock_type), on_feedback, "undock")  # type: ignore[union-attr]

    async def lifecycle(self, action: str) -> None:
        nav = self.nav
        assert nav is not None
        await self._blocking(nav.lifecycleStartup if action == "startup" else nav.lifecycleShutdown)
        self._state.nav_active = action == "startup"

    async def cancel(self) -> None:
        self._cancel_requested.set()

    # ----- generic ROS -------------------------------------------------------------------

    def _publisher(self, topic: str, msg_type: str) -> Any:
        key = (topic, msg_type)
        pub = self._publishers.get(key)
        if pub is None:
            cls = get_message(msg_type)
            qos = QoSProfile(depth=10)
            if topic.endswith(("/state", "/status")) or msg_type.endswith("OccupancyGrid"):
                qos = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.RELIABLE, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
            pub = self.node.create_publisher(cls, topic, qos)  # type: ignore[union-attr]
            self._publishers[key] = pub
        return pub

    async def publish(self, topic: str, msg_type: str, message: dict[str, Any]) -> None:
        def work() -> None:
            pub = self._publisher(topic, msg_type)
            msg = get_message(msg_type)()
            set_message_fields(msg, message or {})
            pub.publish(msg)

        await asyncio.to_thread(work)

    async def call_service(self, service: str, srv_type: str, request: dict[str, Any], timeout_s: float | None) -> dict[str, Any]:
        node = self.node
        assert node is not None

        def work() -> dict[str, Any]:
            cls = get_service(srv_type)
            client = node.create_client(cls, service, callback_group=self.cb_group)
            try:
                if not client.wait_for_service(timeout_sec=min(timeout_s or 10.0, 10.0)):
                    raise StepFailed(f"service {service} is not available")
                req = cls.Request()
                set_message_fields(req, request or {})
                fut = client.call_async(req)
                deadline = time.monotonic() + (timeout_s or 30.0)
                while not fut.done():
                    if time.monotonic() > deadline:
                        raise StepTimeout(f"service {service} timed out")
                    if self._cancel_requested.is_set():
                        raise TaskCanceled()
                    time.sleep(0.02)
                return to_plain(fut.result())
            finally:
                node.destroy_client(client)

        self._cancel_requested.clear()
        return await asyncio.to_thread(work)

    async def call_action(self, action: str, action_type: str, goal: dict[str, Any], timeout_s: float | None, on_feedback: FeedbackCb) -> dict[str, Any]:
        from rclpy.action import ActionClient

        node = self.node
        assert node is not None
        loop = self._loop or asyncio.get_running_loop()

        def work() -> dict[str, Any]:
            cls = get_action(action_type)
            client = ActionClient(node, cls, action, callback_group=self.cb_group)
            try:
                if not client.wait_for_server(timeout_sec=min(timeout_s or 10.0, 10.0)):
                    raise StepFailed(f"action {action} is not available")
                g = cls.Goal()
                set_message_fields(g, goal or {})

                def fb_cb(msg: Any) -> None:
                    fb = NavFeedback(extra=to_plain(msg.feedback))
                    loop.call_soon_threadsafe(on_feedback, fb)

                send = client.send_goal_async(g, feedback_callback=fb_cb)
                deadline = time.monotonic() + (timeout_s or 3600.0)
                while not send.done():
                    if time.monotonic() > deadline:
                        raise StepTimeout(f"action {action} did not accept the goal in time")
                    time.sleep(0.02)
                handle = send.result()
                if not handle.accepted:
                    raise StepFailed(f"action {action} rejected the goal")
                res_fut = handle.get_result_async()
                while not res_fut.done():
                    if self._cancel_requested.is_set() or self._stop.is_set():
                        handle.cancel_goal_async()
                        t0 = time.monotonic()
                        while not res_fut.done() and time.monotonic() - t0 < 5.0:
                            time.sleep(0.05)
                        raise TaskCanceled()
                    if time.monotonic() > deadline:
                        handle.cancel_goal_async()
                        raise StepTimeout(f"action {action} timed out")
                    time.sleep(0.05)
                wrapper = res_fut.result()
                status = int(wrapper.status)
                result = to_plain(wrapper.result)
                if status == 4:  # SUCCEEDED
                    return {"status": "succeeded", **(result if isinstance(result, dict) else {"result": result})}
                if status == 5:  # CANCELED
                    raise TaskCanceled()
                raise StepFailed(f"action {action} ended with status {status}", value=result)
            finally:
                client.destroy()

        self._cancel_requested.clear()
        try:
            return await asyncio.to_thread(work)
        except asyncio.CancelledError:
            self._cancel_requested.set()
            raise

    async def set_params(self, node_name: str, params: dict[str, Any]) -> None:
        from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
        from rcl_interfaces.srv import SetParameters

        def to_param(name: str, value: Any) -> Parameter:
            pv = ParameterValue()
            if isinstance(value, bool):
                pv.type, pv.bool_value = ParameterType.PARAMETER_BOOL, value
            elif isinstance(value, int):
                pv.type, pv.integer_value = ParameterType.PARAMETER_INTEGER, value
            elif isinstance(value, float):
                pv.type, pv.double_value = ParameterType.PARAMETER_DOUBLE, value
            elif isinstance(value, str):
                pv.type, pv.string_value = ParameterType.PARAMETER_STRING, value
            elif isinstance(value, list) and all(isinstance(v, bool) for v in value):
                pv.type, pv.bool_array_value = ParameterType.PARAMETER_BOOL_ARRAY, value
            elif isinstance(value, list) and all(isinstance(v, int) for v in value):
                pv.type, pv.integer_array_value = ParameterType.PARAMETER_INTEGER_ARRAY, value
            elif isinstance(value, list) and all(isinstance(v, (int, float)) for v in value):
                pv.type, pv.double_array_value = ParameterType.PARAMETER_DOUBLE_ARRAY, [float(v) for v in value]
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                pv.type, pv.string_array_value = ParameterType.PARAMETER_STRING_ARRAY, value
            else:
                raise StepFailed(f"unsupported parameter value for {name}: {value!r}")
            return Parameter(name=name, value=pv)

        req = SetParameters.Request(parameters=[to_param(k, v) for k, v in params.items()])
        node = self.node
        assert node is not None

        def work() -> None:
            client = node.create_client(SetParameters, f"{node_name}/set_parameters", callback_group=self.cb_group)
            try:
                if not client.wait_for_service(timeout_sec=5.0):
                    raise StepFailed(f"node {node_name} has no set_parameters service")
                fut = client.call_async(req)
                t0 = time.monotonic()
                while not fut.done() and time.monotonic() - t0 < 10.0:
                    time.sleep(0.02)
                if not fut.done():
                    raise StepTimeout(f"set_parameters on {node_name} timed out")
                for name, res in zip(params, fut.result().results):
                    if not res.successful:
                        raise StepFailed(f"parameter {name}: {res.reason}")
            finally:
                node.destroy_client(client)

        await asyncio.to_thread(work)
