"""Answer the questions a mission asks (the ros.request step) from your own node.

A ros.request step publishes a JSON request as std_msgs/msg/String:

    {"id": "3f2a9c1e", "text": "Part OK?", "options": ["OK", "Reject"],
     "default": "OK", "station": "Conveyor 1", "mission": "inspect", "data": {...}}

and waits for an answer with the same id on the answer topic:

    {"id": "3f2a9c1e", "answer": "OK", "by": "example_answer_requests"}

Change decide() to your own logic: read a sensor, ask a PLC, check a camera.

Run:
    ros2 run mission_runner example_answer_requests                      # /iviz/request -> /iviz/answer
    ros2 run mission_runner example_answer_requests --ros-args -p station:="Conveyor 1"
        # /station/conveyor_1/request -> /station/conveyor_1/answer
    ros2 run mission_runner example_answer_requests --ros-args -p request_topic:=/cell/req -p answer_topic:=/cell/ans
"""

import json
import re

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class AnswerRequests(Node):
    def __init__(self):
        super().__init__("example_answer_requests")
        station = self.declare_parameter("station", "").value
        slug = re.sub(r"[^a-z0-9_]", "_", station.lower())
        default_request = f"/station/{slug}/request" if station else "/iviz/request"
        default_answer = f"/station/{slug}/answer" if station else "/iviz/answer"
        request_topic = self.declare_parameter("request_topic", default_request).value
        answer_topic = self.declare_parameter("answer_topic", default_answer).value
        self.answered: set[str] = set()
        self.pub = self.create_publisher(String, answer_topic, 10)
        self.create_subscription(String, request_topic, self.on_request, 10)
        self.get_logger().info(f"answering requests on {request_topic} -> {answer_topic}")

    def decide(self, request: dict) -> str:
        """The answer to one request. Here: data.ok == false picks the last
        option (e.g. "Reject"); otherwise the default, else the first option."""
        options = request.get("options") or []
        data = request.get("data")
        if isinstance(data, dict) and data.get("ok") is False and options:
            return str(options[-1])
        if request.get("default") is not None:
            return str(request["default"])
        return str(options[0]) if options else "OK"

    def on_request(self, msg: String) -> None:
        try:
            request = json.loads(msg.data)
        except ValueError:
            return
        request_id = request.get("id") if isinstance(request, dict) else None
        if not request_id or request_id in self.answered:
            return  # not a request, or answered already
        answer = self.decide(request)
        self.answered.add(request_id)
        self.pub.publish(String(data=json.dumps({"id": request_id, "answer": answer, "by": self.get_name()})))
        self.get_logger().info(f"'{request.get('text', '')}' ({request.get('mission', '?')}) -> {answer}")


def main(args=None) -> int:
    rclpy.init(args=args)
    node = AnswerRequests()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
