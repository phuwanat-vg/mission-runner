"""Watch what the runner does, and stop it from your own node.

The runner publishes two std_msgs/msg/String topics with JSON inside:

* /mission/state  (transient local: you get the latest at once) - the whole
  status: {"state": "idle" | "running" | ..., "run": {"id", "mission", "status", "step"}, ...}
* /mission/event  - one message per event: run.started, step.started,
  step.finished, run.finished, log, request, ...

and has std_srvs/srv/Trigger services /mission/stop, /mission/cancel,
/mission/pause and /mission/resume.

Run:
    ros2 run mission_runner example_watch_missions
    ros2 run mission_runner example_watch_missions --ros-args -p stop_after_s:=5.0   # stop any run after 5 s
"""

import json
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger


class WatchMissions(Node):
    def __init__(self):
        super().__init__("example_watch_missions")
        self.stop_after_s = float(self.declare_parameter("stop_after_s", 0.0).value)
        latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(String, "/mission/state", self.on_state, latched)
        self.create_subscription(String, "/mission/event", self.on_event, 50)
        self.stop_client = self.create_client(Trigger, "/mission/stop")
        self.run_started: float | None = None
        self.last_state = None
        self.create_timer(0.5, self.check_stop)

    def on_state(self, msg: String) -> None:
        status = json.loads(msg.data)
        run = status.get("run") or {}
        summary = (status.get("state"), run.get("mission"), run.get("status"))
        if summary != self.last_state:  # the state is republished often; print changes only
            self.last_state = summary
            self.get_logger().info(f"state {summary[0]}" + (f": {summary[1]} is {summary[2]}" if run else ""))

    def on_event(self, msg: String) -> None:
        event = json.loads(msg.data)
        kind = event.get("type", "")
        if kind == "run.started":
            self.run_started = time.monotonic()
            self.get_logger().info(f"run {event['run']['id']} of '{event['run']['mission']}' started")
        elif kind == "run.finished":
            self.run_started = None
            run = event["run"]
            self.get_logger().info(f"run {run['id']} finished: {run['status']} {run.get('error') or ''}".rstrip())
        elif kind == "step.started":
            self.get_logger().info(f"  step {event.get('name') or event['step_id']} ({event.get('step_type')})")
        elif kind == "step.finished":
            self.get_logger().info(f"  step {event['step_id']}: {event['result']['status']}")
        elif kind == "log":
            self.get_logger().info(f"  log {event.get('level')}: {event.get('text')}")

    def check_stop(self) -> None:
        if self.stop_after_s <= 0 or self.run_started is None:
            return
        if time.monotonic() - self.run_started < self.stop_after_s:
            return
        self.run_started = None
        if not self.stop_client.service_is_ready():
            self.get_logger().warning("/mission/stop is not available")
            return
        self.get_logger().info(f"run is older than {self.stop_after_s:g} s: calling /mission/stop")
        future = self.stop_client.call_async(Trigger.Request())
        future.add_done_callback(lambda f: self.get_logger().info(f"/mission/stop: {f.result().message}"))


def main(args=None) -> int:
    rclpy.init(args=args)
    node = WatchMissions()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
