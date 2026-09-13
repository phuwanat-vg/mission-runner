"""ros2 launch mission_runner bringup.launch.py [project:=FILE] [bridge:=true] [stations:=FILE] [sim:=false]

The mission layer of a robot in one launch file: mission_runner, foxglove_bridge
and one station_answer node per entry of a stations YAML. Nav2 and the drivers
stay in your own robot launch file. See docs/robot-startup.md.
"""

import os

import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, LogInfo, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

ARGS = [
    ("project", "", "Project file (.mproj) imported before the runner starts; an invalid project stops the runner"),
    ("project_replace", "false", "Also delete missions on the robot that are not in the project"),
    ("bridge", "true", "Start foxglove_bridge"),
    ("bridge_port", "8765", "foxglove_bridge port"),
    ("include_hidden", "true", "foxglove_bridge include_hidden (iViz needs it for Nav2 actions)"),
    ("stations", "", "YAML file listing station_answer nodes"),
    ("home", "~/.mission", "Data directory (missions, sites, run log)"),
    ("port", "8080", "HTTP/WebSocket port"),
    ("host", "0.0.0.0", "HTTP bind address"),
    ("sim", "false", "Simulated robot instead of Nav2"),
    ("log_level", "INFO", ""),
]

STATION_FLOATS = ("delay_s", "max_age_s")


def _true(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes", "on")


def slug(name: str) -> str:
    out = "".join(c if c.isascii() and (c.isalnum() or c == "_") else "_" for c in name.lower())
    return out or "station"


def station_nodes(path: str) -> list:
    """One station_answer node per entry of ``stations:``."""
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    entries = doc.get("stations") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError(f"{path}: expected a 'stations:' list")
    nodes = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"{path}: stations[{i}] must be a mapping")
        params = {}
        for k, v in entry.items():
            if k in STATION_FLOATS and isinstance(v, (int, float)) and not isinstance(v, bool):
                v = float(v)
            elif isinstance(v, list):
                v = [str(x) for x in v] if k == "gpio_answers" else [int(x) for x in v] if k == "gpio_pins" else v
            elif not isinstance(v, (str, int, float, bool)):
                v = str(v)
            params[str(k)] = v
        name = f"station_answer_{slug(str(entry.get('station', '')) or str(i))}"
        if name in seen:
            raise RuntimeError(f"{path}: two stations are both named '{name}'")
        seen.add(name)
        nodes.append(Node(package="mission_runner", executable="station_answer", name=name, output="screen", parameters=[params]))
    return nodes


def _setup(context, *_args, **_kwargs):
    v = {name: LaunchConfiguration(name).perform(context) for name, _d, _h in ARGS}
    run_args = ["run", "--home", v["home"], "--port", v["port"], "--host", v["host"], "--log-level", v["log_level"]]
    if _true(v["sim"]):
        run_args.append("--sim")
    if v["project"].strip():
        run_args += ["--project", os.path.expanduser(v["project"].strip())]
        if _true(v["project_replace"]):
            run_args.append("--project-replace")
    # No name=: it would remap every node in the process, BasicNavigator's too,
    # into two nodes called mission_runner. The runner names its own node.
    # respawn: ros2 launch exits 0 when a node dies, so systemd's Restart=on-failure
    # would never bring a crashed runner back; launch does it instead.
    actions = [Node(package="mission_runner", executable="mission_runner", output="screen", arguments=run_args, respawn=True, respawn_delay=5.0)]

    if _true(v["bridge"]):
        try:
            from ament_index_python.packages import PackageNotFoundError, get_package_share_directory

            get_package_share_directory("foxglove_bridge")
            actions.append(
                Node(
                    package="foxglove_bridge",
                    executable="foxglove_bridge",
                    name="foxglove_bridge",
                    output="screen",
                    parameters=[{"port": int(v["bridge_port"]), "include_hidden": _true(v["include_hidden"])}],
                )
            )
        except PackageNotFoundError:
            actions.append(LogInfo(msg="foxglove_bridge is not installed (sudo apt install ros-$ROS_DISTRO-foxglove-bridge); starting without it"))

    if v["stations"].strip():
        actions += station_nodes(v["stations"].strip())
    return actions


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription([*(DeclareLaunchArgument(n, default_value=d, description=h) for n, d, h in ARGS), OpaqueFunction(function=_setup)])
