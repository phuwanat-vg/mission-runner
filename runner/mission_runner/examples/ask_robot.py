"""Ask the robot to do something by publishing an event a mission listens for.

A mission starts itself when a message arrives on a topic (a ros.topic
trigger). Your node only publishes; it needs to know nothing about missions.
Deploy this mission on the robot (Mission Builder, or ~/.mission/missions/fetch_part.json):

    {
      "schema": "mission/1",
      "name": "fetch_part",
      "inputs": { "part": { "type": "string", "default": "A" } },
      "triggers": [
        { "id": "part_ready", "type": "ros.topic", "topic": "/cell/part_ready",
          "msg_type": "std_msgs/msg/String", "set": { "part": "payload.data" } }
      ],
      "flow": [
        { "id": "say", "type": "log", "text": "fetching part ${part}" },
        { "id": "go", "type": "nav.follow_route", "to": "${part}" }
      ]
    }

"set" turns the message into mission inputs: payload is the message as JSON, so
payload.data is the String's text.

Run:
    ros2 run mission_runner example_ask_robot --ros-args -p part:=B
    ros2 run mission_runner example_ask_robot --ros-args -p part:=B -p repeat_s:=30.0   # every 30 s
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class AskRobot(Node):
    def __init__(self):
        super().__init__("example_ask_robot")
        self.topic = self.declare_parameter("topic", "/cell/part_ready").value
        self.part = self.declare_parameter("part", "A").value
        self.repeat_s = float(self.declare_parameter("repeat_s", 0.0).value)
        self.pub = self.create_publisher(String, self.topic, 10)
        self.sent = False
        self.waited = 0.0
        self.timer = self.create_timer(0.2, self.tick)

    def tick(self) -> None:
        # A message published before the runner has discovered this node is
        # lost, so wait until somebody subscribes (at most 10 s).
        if self.pub.get_subscription_count() == 0 and self.waited < 10.0:
            self.waited += 0.2
            return
        self.pub.publish(String(data=self.part))
        self.get_logger().info(f"published '{self.part}' on {self.topic} ({self.pub.get_subscription_count()} subscribers)")
        self.sent = True
        if self.repeat_s > 0:
            self.timer.cancel()
            self.timer = self.create_timer(self.repeat_s, self.tick)
        else:
            self.timer.cancel()


def main(args=None) -> int:
    rclpy.init(args=args)
    node = AskRobot()
    try:
        while rclpy.ok() and (node.repeat_s > 0 or not node.sent):
            rclpy.spin_once(node, timeout_sec=0.2)
        rclpy.spin_once(node, timeout_sec=0.5)  # let the message go out
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
