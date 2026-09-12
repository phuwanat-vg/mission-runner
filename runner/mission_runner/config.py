"""Runner configuration: command line + ``<home>/runner.yaml`` + environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


@dataclass
class RunnerConfig:
    home: Path = field(default_factory=lambda: Path(os.environ.get("MISSION_HOME", "~/.mission")).expanduser())
    backend: str = "nav2"  # nav2 | sim
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    log_level: str = "INFO"
    keep_runs: int = 2000
    install_examples: bool = True
    ros_namespace: str = ""
    ros_node_name: str = "mission_runner"
    battery_topic: str = "/battery_state"
    robot_frame: str = "base_link"
    # Default topics for the ros.request step: the JSON request/answer exchange
    # iViz's Dashboard answers. A step can override either.
    request_topic: str = "/iviz/request"
    answer_topic: str = "/iviz/answer"
    nav2: dict[str, Any] = field(default_factory=lambda: {"wait_nodes": ["bt_navigator"], "localizer": "amcl", "speed_node": "/controller_server", "speed_param": "FollowPath.max_vel_x", "default_speed_mps": 0.5})
    live_topics: dict[str, Any] = field(default_factory=dict)
    sim: dict[str, Any] = field(default_factory=lambda: {"speed_mps": 0.6, "turn_rate_dps": 90.0, "time_scale": 1.0, "start": {"x": 0.0, "y": 0.0, "yaw_deg": 0.0}, "battery": 0.85})
    gpio: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, home: str | os.PathLike[str] | None = None, **overrides: Any) -> RunnerConfig:
        cfg = cls()
        if home:
            cfg.home = Path(home).expanduser()
        path = cfg.home / "runner.yaml"
        if path.exists() and yaml is not None:
            try:
                doc = yaml.safe_load(path.read_text("utf-8")) or {}
            except Exception as e:  # noqa: BLE001
                raise SystemExit(f"{path}: {e}") from None
            if isinstance(doc, dict):
                for k, v in doc.items():
                    if hasattr(cfg, k) and v is not None:
                        setattr(cfg, k, v if not isinstance(getattr(cfg, k), dict) else {**getattr(cfg, k), **v})
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        cfg.home = Path(cfg.home).expanduser()
        return cfg

    def as_dict(self) -> dict[str, Any]:
        return {
            "home": str(self.home),
            "backend": self.backend,
            "http_host": self.http_host,
            "http_port": self.http_port,
            "log_level": self.log_level,
            "keep_runs": self.keep_runs,
            "ros_namespace": self.ros_namespace,
            "battery_topic": self.battery_topic,
            "robot_frame": self.robot_frame,
            "sim": self.sim,
        }
