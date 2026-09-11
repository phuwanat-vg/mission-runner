"""Simulated robot. Runs without ROS: ``nav.*`` steps move a kinematic point
robot at a configurable speed and report realistic feedback; ``ros.*`` steps
are logged. Used by ``mission_runner --sim``, by the editor's dry run and by
the test-suite."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from ..types import RobotState, StepFailed, TaskCanceled
from .base import FeedbackCb, NavBackend, NavFeedback, Pose, path_from_points, path_length

log = logging.getLogger("mission.sim")


class SimBackend(NavBackend):
    name = "sim"

    def __init__(
        self,
        *,
        speed_mps: float = 0.6,
        turn_rate_dps: float = 90.0,
        time_scale: float = 1.0,
        tick_s: float = 0.05,
        start: Pose | None = None,
        battery: float = 0.85,
        startup_delay_s: float = 0.0,
    ):
        self.speed = speed_mps
        self.turn_rate = turn_rate_dps
        self.time_scale = max(0.01, time_scale)
        self.tick_s = tick_s
        self.startup_delay_s = startup_delay_s
        self._state = RobotState(x=start.x if start else 0.0, y=start.y if start else 0.0, yaw_deg=start.yaw_deg if start else 0.0, frame="map", battery=battery)
        self._cancel = asyncio.Event()
        self._busy = False
        self.docked = False
        self.current_map_file: str = ""
        self.lifecycle_state = "active"
        self.published: list[tuple[str, str, dict[str, Any]]] = []
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.action_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.params_set: list[tuple[str, dict[str, Any]]] = []
        #: Names of "fail zones": goals whose x/y match within 0.05 m fail (tests).
        self.fail_at: list[Pose] = []
        #: Set to fail the very next navigation task.
        self.fail_next: bool = False
        #: Simulated obstacle: navigation to any goal fails while set.
        self.blocked: bool = False
        #: Total simulated meters driven (for tests).
        self.odometer: float = 0.0

    # ----- state ------------------------------------------------------------

    def robot_state(self) -> RobotState:
        self._state.nav_active = self.lifecycle_state == "active"
        self._state.updated_mono = time.monotonic()
        return self._state

    def set_pose(self, pose: Pose) -> None:
        self._state.x, self._state.y, self._state.yaw_deg = pose.x, pose.y, pose.yaw_deg

    def pose(self) -> Pose:
        return Pose(self._state.x or 0.0, self._state.y or 0.0, self._state.yaw_deg or 0.0, self._state.frame)

    def capabilities(self) -> dict[str, dict[str, Any]]:
        return {t: {"available": True, "reason": "simulated"} for t in (
            "nav.wait_active", "nav.set_initial_pose", "nav.go_to_pose", "nav.go_through_poses", "nav.follow_waypoints", "nav.follow_path",
            "nav.compute_path", "nav.compute_path_through_poses", "nav.smooth_path", "nav.spin", "nav.backup", "nav.drive_on_heading",
            "nav.change_map", "nav.clear_costmap", "nav.dock", "nav.undock", "nav.lifecycle", "nav.cancel",
            "ros.publish", "ros.call_service", "ros.call_action", "ros.set_param",
        )}

    # ----- helpers ----------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        """Sleep in simulated time, aborting on cancel."""
        # Not asyncio.wait_for: on Windows its sub-resolution timeouts fire immediately.
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds / self.time_scale
        while True:
            if self._cancel.is_set():
                raise TaskCanceled()
            remaining = end - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.05))

    def _begin(self) -> None:
        if self._busy:
            raise StepFailed("another navigation task is already running")
        self._busy = True
        self._cancel.clear()

    def _end(self) -> None:
        self._busy = False

    def _check_fail(self, goal: Pose) -> None:
        if self.fail_next:
            self.fail_next = False
            raise StepFailed("simulated failure")
        if self.blocked:
            raise StepFailed("simulated obstacle: path blocked")
        for f in self.fail_at:
            if f.distance_to(goal) < 0.05:
                raise StepFailed(f"simulated failure at ({goal.x:.2f}, {goal.y:.2f})")

    async def _turn_to(self, yaw_deg: float) -> None:
        cur = self._state.yaw_deg or 0.0
        delta = (yaw_deg - cur + 180.0) % 360.0 - 180.0
        steps = max(1, int(abs(delta) / (self.turn_rate * self.tick_s)))
        for i in range(1, steps + 1):
            await self._sleep(self.tick_s)
            self._state.yaw_deg = (cur + delta * i / steps + 180.0) % 360.0 - 180.0

    async def _drive_to(self, goal: Pose, on_feedback: FeedbackCb, t0: float, remaining_after: float = 0.0, recoveries: int = 0) -> None:
        cur = self.pose()
        dist = cur.distance_to(goal)
        if dist > 1e-6:
            await self._turn_to(math.degrees(math.atan2(goal.y - cur.y, goal.x - cur.x)))
        travelled = 0.0
        last_fb = 0.0
        while travelled < dist:
            await self._sleep(self.tick_s)
            step = min(self.speed * self.tick_s, dist - travelled)
            travelled += step
            self.odometer += step
            t = travelled / dist if dist > 0 else 1.0
            self._state.x = cur.x + (goal.x - cur.x) * t
            self._state.y = cur.y + (goal.y - cur.y) * t
            if self._state.battery is not None:
                self._state.battery = max(0.0, self._state.battery - 0.0002 * step)
            now = time.monotonic()
            if now - last_fb >= 0.25:
                last_fb = now
                left = (dist - travelled) + remaining_after
                on_feedback(NavFeedback(distance_remaining=left, recoveries=recoveries, eta_s=left / self.speed, navigation_time_s=(now - t0) * self.time_scale))
        self._state.x, self._state.y = goal.x, goal.y

    # ----- navigation -------------------------------------------------------

    async def wait_active(self, timeout_s: float | None) -> None:
        if self.startup_delay_s > 0:
            await asyncio.sleep(min(self.startup_delay_s, timeout_s or self.startup_delay_s) / self.time_scale)
        if self.lifecycle_state != "active":
            raise StepFailed("Nav2 is not active (simulated lifecycle state: %s)" % self.lifecycle_state)

    async def set_initial_pose(self, pose: Pose) -> None:
        self.set_pose(pose)

    async def go_to_pose(self, pose: Pose, behavior_tree: str, on_feedback: FeedbackCb) -> dict[str, Any]:
        self._begin()
        t0 = time.monotonic()
        try:
            self._check_fail(pose)
            self.docked = False
            await self._drive_to(pose, on_feedback, t0)
            await self._turn_to(pose.yaw_deg)
            return {"reached": pose.as_dict(), "behavior_tree": behavior_tree, "navigation_time_s": (time.monotonic() - t0) * self.time_scale}
        finally:
            self._end()

    async def go_through_poses(self, poses: list[Pose], behavior_tree: str, on_feedback: FeedbackCb) -> dict[str, Any]:
        self._begin()
        t0 = time.monotonic()
        try:
            for p in poses:
                self._check_fail(p)
            self.docked = False
            for i, p in enumerate(poses):
                rest = sum(a.distance_to(b) for a, b in zip(poses[i:-1], poses[i + 1 :]))
                await self._drive_to(p, on_feedback, t0, remaining_after=rest)
            await self._turn_to(poses[-1].yaw_deg)
            return {"poses": len(poses), "behavior_tree": behavior_tree}
        finally:
            self._end()

    async def follow_waypoints(self, poses: list[Pose], on_feedback: FeedbackCb) -> dict[str, Any]:
        self._begin()
        t0 = time.monotonic()
        missed: list[int] = []
        try:
            self.docked = False
            for i, p in enumerate(poses):
                on_feedback(NavFeedback(current_waypoint=i, distance_remaining=self.pose().distance_to(p)))
                try:
                    self._check_fail(p)
                except StepFailed:
                    missed.append(i)
                    continue
                await self._drive_to(p, on_feedback, t0)
                await self._turn_to(p.yaw_deg)
            if len(missed) == len(poses) and poses:
                raise StepFailed("all waypoints were missed", value={"missed": missed})
            return {"missed": missed, "waypoints": len(poses)}
        finally:
            self._end()

    async def follow_path(self, path: dict[str, Any], controller_id: str, goal_checker_id: str, on_feedback: FeedbackCb) -> dict[str, Any]:
        self._begin()
        t0 = time.monotonic()
        try:
            poses = [Pose(float(p["x"]), float(p["y"]), float(p.get("yaw_deg", 0.0))) for p in path.get("poses") or []]
            if not poses:
                raise StepFailed("path is empty")
            if self.blocked:
                raise StepFailed("simulated obstacle: path blocked")
            self.docked = False
            total = path_length(path)
            done = 0.0
            cur = self.pose()
            # Follow the polyline without stopping to turn at every vertex.
            last_fb = 0.0
            for p in poses:
                seg = cur.distance_to(p)
                if seg > 1e-6:
                    self._state.yaw_deg = math.degrees(math.atan2(p.y - cur.y, p.x - cur.x))
                travelled = 0.0
                while travelled < seg:
                    await self._sleep(self.tick_s)
                    step = min(self.speed * self.tick_s, seg - travelled)
                    travelled += step
                    done += step
                    self.odometer += step
                    t = travelled / seg
                    self._state.x = cur.x + (p.x - cur.x) * t
                    self._state.y = cur.y + (p.y - cur.y) * t
                    now = time.monotonic()
                    if now - last_fb >= 0.25:
                        last_fb = now
                        on_feedback(NavFeedback(distance_remaining=max(0.0, total - done), eta_s=max(0.0, total - done) / self.speed, navigation_time_s=(now - t0) * self.time_scale))
                cur = Pose(p.x, p.y, self._state.yaw_deg or 0.0)
            self._state.x, self._state.y = poses[-1].x, poses[-1].y
            return {"length_m": round(total, 3), "controller_id": controller_id, "goal_checker_id": goal_checker_id}
        finally:
            self._end()

    async def compute_path(self, start: Pose | None, goal: Pose, planner_id: str, use_start: bool) -> dict[str, Any]:
        await self._sleep(0.05)
        s = start if (use_start and start) else self.pose()
        self._check_fail(goal)
        return path_from_points([s, goal], 0.1)

    async def compute_path_through_poses(self, start: Pose | None, goals: list[Pose], planner_id: str, use_start: bool) -> dict[str, Any]:
        await self._sleep(0.05)
        s = start if (use_start and start) else self.pose()
        return path_from_points([s, *goals], 0.1)

    async def smooth_path(self, path: dict[str, Any], smoother_id: str, max_duration_s: float, check_collision: bool) -> dict[str, Any]:
        await self._sleep(0.05)
        return {"frame": path.get("frame", "map"), "poses": list(path.get("poses") or []), "smoothed": True}

    async def spin(self, angle_rad: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]:
        self._begin()
        try:
            cur = self._state.yaw_deg or 0.0
            target = cur + math.degrees(angle_rad)
            steps = max(1, int(abs(math.degrees(angle_rad)) / (self.turn_rate * self.tick_s)))
            for i in range(1, steps + 1):
                await self._sleep(self.tick_s)
                self._state.yaw_deg = (cur + (target - cur) * i / steps + 180.0) % 360.0 - 180.0
                if i % 5 == 0:
                    on_feedback(NavFeedback(extra={"angular_distance_traveled": round(abs(target - cur) * i / steps, 1)}))
            return {"angle_deg": math.degrees(angle_rad)}
        finally:
            self._end()

    async def _straight(self, distance_m: float, speed_mps: float, sign: float, on_feedback: FeedbackCb) -> dict[str, Any]:
        self._begin()
        try:
            yaw = math.radians(self._state.yaw_deg or 0.0)
            spd = max(0.01, speed_mps)
            done = 0.0
            while done < distance_m:
                await self._sleep(self.tick_s)
                step = min(spd * self.tick_s, distance_m - done)
                done += step
                self.odometer += step
                self._state.x = (self._state.x or 0.0) + sign * step * math.cos(yaw)
                self._state.y = (self._state.y or 0.0) + sign * step * math.sin(yaw)
                on_feedback(NavFeedback(distance_remaining=distance_m - done))
            return {"distance_m": distance_m}
        finally:
            self._end()

    async def backup(self, distance_m: float, speed_mps: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]:
        return await self._straight(distance_m, speed_mps, -1.0, on_feedback)

    async def drive_on_heading(self, distance_m: float, speed_mps: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]:
        return await self._straight(distance_m, speed_mps, 1.0, on_feedback)

    async def change_map(self, map_file: str) -> None:
        await self._sleep(0.2)
        self.current_map_file = map_file

    async def clear_costmap(self, which: str) -> None:
        await self._sleep(0.05)
        self.blocked = False

    async def dock(self, dock_id: str | None, dock_pose: Pose | None, dock_type: str, navigate_to_staging: bool, on_feedback: FeedbackCb) -> dict[str, Any]:
        self._begin()
        try:
            for i in range(4):
                await self._sleep(0.5)
                on_feedback(NavFeedback(extra={"state": ["NAV_TO_STAGING", "INITIAL_PERCEPTION", "CONTROLLING", "WAIT_FOR_CHARGE"][i]}))
            self.docked = True
            if self._state.battery is not None:
                self._state.battery = min(1.0, self._state.battery + 0.05)
            return {"dock_id": dock_id, "dock_type": dock_type, "success": True}
        finally:
            self._end()

    async def undock(self, dock_type: str, on_feedback: FeedbackCb) -> dict[str, Any]:
        self._begin()
        try:
            await self._sleep(1.0)
            self.docked = False
            return {"success": True}
        finally:
            self._end()

    async def lifecycle(self, action: str) -> None:
        await self._sleep(0.5)
        self.lifecycle_state = "active" if action == "startup" else "inactive"

    async def cancel(self) -> None:
        self._cancel.set()

    # ----- generic ROS (logged only) -----------------------------------------

    async def publish(self, topic: str, msg_type: str, message: dict[str, Any]) -> None:
        log.info("[sim] publish %s (%s): %s", topic, msg_type, message)
        self.published.append((topic, msg_type, message))

    async def call_service(self, service: str, srv_type: str, request: dict[str, Any], timeout_s: float | None) -> dict[str, Any]:
        log.info("[sim] call service %s (%s): %s", service, srv_type, request)
        self.service_calls.append((service, srv_type, request))
        await self._sleep(0.05)
        return {"success": True, "message": "simulated", "request": request}

    async def call_action(self, action: str, action_type: str, goal: dict[str, Any], timeout_s: float | None, on_feedback: FeedbackCb) -> dict[str, Any]:
        log.info("[sim] call action %s (%s): %s", action, action_type, goal)
        self.action_calls.append((action, action_type, goal))
        self._begin()
        try:
            await self._sleep(0.5)
            return {"status": "succeeded", "goal": goal}
        finally:
            self._end()

    async def set_params(self, node: str, params: dict[str, Any]) -> None:
        log.info("[sim] set params on %s: %s", node, params)
        self.params_set.append((node, params))
