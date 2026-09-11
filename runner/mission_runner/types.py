"""Plain data types shared by the runner components (no ROS, no asyncio)."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "RunStatus",
    "StepResult",
    "RunSource",
    "Run",
    "Prompt",
    "RobotState",
    "MissionError",
    "StepFailed",
    "StepTimeout",
    "TaskCanceled",
    "now_iso",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def finished(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED)

    @property
    def active(self) -> bool:
        return self in (RunStatus.RUNNING, RunStatus.PAUSED)


@dataclass(slots=True)
class StepResult:
    ok: bool
    status: str = "succeeded"  # succeeded | failed | canceled | timeout | skipped
    value: Any = None
    error: str = ""
    duration_s: float = 0.0

    @classmethod
    def success(cls, value: Any = None, duration_s: float = 0.0) -> StepResult:
        return cls(True, "succeeded", value, "", duration_s)

    @classmethod
    def failure(cls, error: str, status: str = "failed", value: Any = None, duration_s: float = 0.0) -> StepResult:
        return cls(False, status, value, error, duration_s)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "value": self.value, "error": self.error, "duration_s": round(self.duration_s, 3)}


@dataclass(slots=True)
class RunSource:
    kind: str  # trigger | manual | interrupt | resume | sub | boot
    id: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "detail": self.detail}


@dataclass
class Run:
    id: str
    mission: str
    inputs: dict[str, Any]
    source: RunSource
    priority: int = 50
    policy: str = "queue"
    status: RunStatus = RunStatus.QUEUED
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    step: dict[str, Any] | None = None
    error: str = ""
    feedback: dict[str, Any] | None = None
    result: Any = None
    parent_run_id: str | None = None
    # runtime-only (not serialized)
    created_mono: float = field(default_factory=time.monotonic, repr=False)
    resume_path: list[int] | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mission": self.mission,
            "inputs": self.inputs,
            "source": self.source.as_dict(),
            "priority": self.priority,
            "policy": self.policy,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "step": self.step,
            "error": self.error,
            "feedback": self.feedback,
            "result": self.result,
            "parent_run_id": self.parent_run_id,
        }


@dataclass(slots=True)
class Prompt:
    id: str
    run_id: str
    mission: str
    text: str
    options: list[str]
    default: str | None
    expires_at: str | None
    step_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "mission": self.mission,
            "step_id": self.step_id,
            "text": self.text,
            "options": self.options,
            "default": self.default,
            "expires_at": self.expires_at,
        }


@dataclass(slots=True)
class RobotState:
    x: float | None = None
    y: float | None = None
    yaw_deg: float | None = None
    frame: str = "map"
    battery: float | None = None
    nav_active: bool = False
    updated_mono: float = 0.0

    def pose_dict(self) -> dict[str, Any] | None:
        if self.x is None or self.y is None:
            return None
        return {"x": self.x, "y": self.y, "yaw_deg": self.yaw_deg or 0.0, "frame": self.frame}

    def as_dict(self) -> dict[str, Any] | None:
        p = self.pose_dict()
        if p is None and self.battery is None:
            return None
        d: dict[str, Any] = dict(p or {})
        d["battery"] = self.battery
        d["nav_active"] = self.nav_active
        return d


class MissionError(Exception):
    """A mission-level problem (unknown mission, bad inputs, ...)."""


class StepFailed(Exception):
    """Raised inside step implementations to fail the step with a message."""

    def __init__(self, message: str, value: Any = None):
        super().__init__(message)
        self.value = value


class StepTimeout(StepFailed):
    pass


class TaskCanceled(Exception):
    """The backend task was canceled (by pause, stop or preemption)."""
