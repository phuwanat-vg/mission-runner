"""ros2 launch mission_runner mission_runner.launch.py [home:=~/.mission] [port:=8080] [sim:=false]

Starts the runner alone. bringup.launch.py adds foxglove_bridge, station answer
nodes and a project import (docs/robot-startup.md).
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    home = LaunchConfiguration("home")
    port = LaunchConfiguration("port")
    host = LaunchConfiguration("host")
    sim = LaunchConfiguration("sim")
    log_level = LaunchConfiguration("log_level")
    # No name= on the nodes: a remapped node name applies to every node in the
    # process (BasicNavigator's too). The runner names its own node.
    return LaunchDescription(
        [
            DeclareLaunchArgument("home", default_value="~/.mission", description="Data directory (missions, sites, run log)"),
            DeclareLaunchArgument("port", default_value="8080", description="HTTP/WebSocket port"),
            DeclareLaunchArgument("host", default_value="0.0.0.0", description="HTTP bind address"),
            DeclareLaunchArgument("sim", default_value="false", description="Simulated robot instead of Nav2"),
            DeclareLaunchArgument("log_level", default_value="INFO"),
            Node(
                package="mission_runner",
                executable="mission_runner",
                output="screen",
                arguments=["run", "--home", home, "--port", port, "--host", host, "--log-level", log_level],
                condition=UnlessCondition(sim),
            ),
            Node(
                package="mission_runner",
                executable="mission_runner",
                output="screen",
                arguments=["run", "--sim", "--home", home, "--port", port, "--host", host, "--log-level", log_level],
                condition=IfCondition(sim),
            ),
        ]
    )
