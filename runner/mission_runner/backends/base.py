"""Backend interface. The interpreter only talks to this; ``nav2.py`` maps it
onto ``nav2_simple_commander.BasicNavigator`` and ``sim.py`` fakes a robot.

Conventions
* Poses are :class:`Pose` (x, y, yaw in degrees, frame).
* Paths are JSON-friendly dicts ``{"frame": "map", "poses": [{"x","y","yaw_deg"}, ...]}``
  so they can be stored in step results and mission variables.
* Long-running calls accept ``on_feedback`` and must raise
  :class:`~mission_runner.types.TaskCanceled` when :meth:`NavBackend.cancel`
  is called while they run.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..types import RobotState, StepFailed

FeedbackCb = Callable[["NavFeedback"], None]


@dataclass(slots=True)
class Pose:
    x: float
    y: float
    yaw_deg: float = 0.0
    frame: str = "map"

    @property
    def yaw(self) -> float:
        return math.radians(self.yaw_deg)

    def as_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "yaw_deg": self.yaw_deg, "frame": self.frame}

    def distance_to(self, other: Pose) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass(slots=True)
class NavFeedback:
    distance_remaining: float | None = None
    recoveries: int | None = None
    eta_s: float | None = None
    navigation_time_s: float | None = None
    current_waypoint: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.distance_remaining is not None:
            d["distance_remaining"] = round(self.distance_remaining, 2)
        if self.recoveries is not None:
            d["recoveries"] = self.recoveries
        if self.eta_s is not None:
            d["eta_s"] = round(self.eta_s, 1)
        if self.navigation_time_s is not None:
            d["navigation_time_s"] = round(self.navigation_time_s, 1)
        if self.current_waypoint is not None:
            d["current_waypoint"] = self.current_waypoint
        d.update(self.extra)
        return d


def pose_from_any(value: Any, frame: str = "map") -> Pose:
    """Accept ``{x,y,yaw_deg,frame}``, ``[x, y]``, ``[x, y, yaw]``, or a
    ROS-like ``{position:{x,y}, orientation:{z,w}}`` / ``{pose:{...}}`` dict."""
    if isinstance(value, Pose):
        return value
    if isinstance(value, Mapping):
        if "x" in value and "y" in value:
            return Pose(float(value["x"]), float(value["y"]), float(value.get("yaw_deg", 0.0) or 0.0), str(value.get("frame") or frame))
        inner = value.get("pose", value)
        pos = inner.get("position") if isinstance(inner, Mapping) else None
        if isinstance(pos, Mapping):
            ori = inner.get("orientation") or {}
            z = float(ori.get("z", 0.0))
            w = float(ori.get("w", 1.0))
            yaw = math.degrees(2.0 * math.atan2(z, w))
            hdr = value.get("header") or {}
            return Pose(float(pos["x"]), float(pos["y"]), yaw, str(hdr.get("frame_id") or frame))
    if isinstance(value, Sequence) and not isinstance(value, str) and 2 <= len(value) <= 3:
        return Pose(float(value[0]), float(value[1]), float(value[2]) if len(value) == 3 else 0.0, frame)
    raise StepFailed(f"not a pose: {value!r}")


def path_from_points(points: Sequence[Pose], spacing_m: float = 0.05, frame: str = "map") -> dict[str, Any]:
    """Straight segments between points, densified every ``spacing_m`` with the
    heading of each segment (like the user's ``follow_path.py``)."""
    poses: list[dict[str, Any]] = []
    if not points:
        return {"frame": frame, "poses": poses}
    spacing = max(0.005, float(spacing_m))
    for a, b in zip(points[:-1], points[1:]):
        dist = a.distance_to(b)
        yaw = math.degrees(math.atan2(b.y - a.y, b.x - a.x)) if dist > 1e-9 else a.yaw_deg
        n = max(1, int(dist / spacing))
        for i in range(n):
            t = i / n
            poses.append({"x": a.x + t * (b.x - a.x), "y": a.y + t * (b.y - a.y), "yaw_deg": yaw})
    last = points[-1]
    poses.append({"x": last.x, "y": last.y, "yaw_deg": last.yaw_deg})
    return {"frame": frame, "poses": poses}


def path_length(path: Mapping[str, Any]) -> float:
    poses = path.get("poses") or []
    total = 0.0
    for a, b in zip(poses[:-1], poses[1:]):
        total += math.hypot(float(b["x"]) - float(a["x"]), float(b["y"]) - float(a["y"]))
    return total


class NavBackend(ABC):
    """What the interpreter needs from the robot."""

    name = "abstract"

    async def start(self) -> None:  # noqa: B027 - optional hook
        pass

    async def stop(self) -> None:  # noqa: B027
        pass

    @abstractmethod
    def robot_state(self) -> RobotState: ...

    def capabilities(self) -> dict[str, dict[str, Any]]:
        """``step type -> {available, reason}`` for the nav.* and ros.* steps."""
        return {}

    # ----- navigation -------------------------------------------------------

    @abstractmethod
    async def wait_active(self, timeout_s: float | None) -> None: ...

    @abstractmethod
    async def set_initial_pose(self, pose: Pose) -> None: ...

    @abstractmethod
    async def go_to_pose(self, pose: Pose, behavior_tree: str, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def go_through_poses(self, poses: list[Pose], behavior_tree: str, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def follow_waypoints(self, poses: list[Pose], on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def follow_path(self, path: dict[str, Any], controller_id: str, goal_checker_id: str, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def compute_path(self, start: Pose | None, goal: Pose, planner_id: str, use_start: bool) -> dict[str, Any]: ...

    @abstractmethod
    async def compute_path_through_poses(self, start: Pose | None, goals: list[Pose], planner_id: str, use_start: bool) -> dict[str, Any]: ...

    @abstractmethod
    async def smooth_path(self, path: dict[str, Any], smoother_id: str, max_duration_s: float, check_collision: bool) -> dict[str, Any]: ...

    @abstractmethod
    async def spin(self, angle_rad: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def backup(self, distance_m: float, speed_mps: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def drive_on_heading(self, distance_m: float, speed_mps: float, time_allowance_s: float, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def change_map(self, map_file: str) -> None: ...

    @abstractmethod
    async def clear_costmap(self, which: str) -> None: ...

    @abstractmethod
    async def dock(self, dock_id: str | None, dock_pose: Pose | None, dock_type: str, navigate_to_staging: bool, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def undock(self, dock_type: str, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def lifecycle(self, action: str) -> None: ...

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel the running task, if any. Must be safe to call at any time."""

    # ----- generic ROS ------------------------------------------------------

    @abstractmethod
    async def publish(self, topic: str, msg_type: str, message: dict[str, Any]) -> None: ...

    @abstractmethod
    async def call_service(self, service: str, srv_type: str, request: dict[str, Any], timeout_s: float | None) -> dict[str, Any]: ...

    @abstractmethod
    async def call_action(self, action: str, action_type: str, goal: dict[str, Any], timeout_s: float | None, on_feedback: FeedbackCb) -> dict[str, Any]: ...

    @abstractmethod
    async def set_params(self, node: str, params: dict[str, Any]) -> None: ...
