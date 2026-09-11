"""mission_runner: executes declarative missions (mission/1 JSON) against Nav2.

The package is importable without ROS 2. Only ``backends.nav2`` and
``connectors.ros`` import ``rclpy``; everything else (interpreter, dispatcher,
HTTP API, MQTT, timers, sim backend) runs anywhere Python 3.10+ runs.
"""

__version__ = "0.1.0"
