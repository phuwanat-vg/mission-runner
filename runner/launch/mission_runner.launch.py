"""ros2 launch mission_runner mission_runner.launch.py [home:=~/.mission] [port:=8080] [sim:=false]

Add it to your Nav2 bring-up so the runner starts with the robot.
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
                name="mission_runner",
                output="screen",
                arguments=["run", "--home", home, "--port", port, "--host", host, "--log-level", log_level],
                condition=UnlessCondition(sim),
            ),
            Node(
                package="mission_runner",
                executable="mission_runner",
                name="mission_runner",
                output="screen",
                arguments=["run", "--sim", "--home", home, "--port", port, "--host", host, "--log-level", log_level],
                condition=IfCondition(sim),
            ),
        ]
    )
