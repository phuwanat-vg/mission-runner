"""Start a mission from your own node.

Two ways, both named /missions/run (they need the mission_msgs package):

* the **service** mission_msgs/srv/RunMission starts the mission and answers at
  once with a run id;
* the **action** mission_msgs/action/RunMission starts it and follows it: one
  feedback message per step, the result at the end. Ctrl+C cancels the run.

Run:
    ros2 run mission_runner example_start_mission --ros-args -p mission:=go_to_point
    ros2 run mission_runner example_start_mission --ros-args -p mission:=fetch_part -p inputs:='{"part": "B"}'
    ros2 run mission_runner example_start_mission --ros-args -p mission:=go_to_point -p wait:=true
"""

import json

import rclpy
from mission_msgs.action import RunMission as RunMissionAction
from mission_msgs.srv import RunMission
from rclpy.action import ActionClient
from rclpy.node import Node


def start(node: Node, name: str, inputs: str) -> int:
    """Service: start and return."""
    client = node.create_client(RunMission, "/missions/run")
    if not client.wait_for_service(timeout_sec=10.0):
        node.get_logger().error("/missions/run is not available: is mission_runner running (and mission_msgs built)?")
        return 1
    future = client.call_async(RunMission.Request(name=name, inputs_json=inputs))
    rclpy.spin_until_future_complete(node, future, timeout_sec=30.0)
    reply = future.result()
    if reply is None:
        node.get_logger().error("no reply from /missions/run")
        return 1
    if reply.accepted:
        node.get_logger().info(f"started '{name}': run {reply.run_id} ({reply.message})")
        return 0
    node.get_logger().error(f"'{name}' was not started: {reply.message}")
    return 1


def follow(node: Node, name: str, inputs: str) -> int:
    """Action: start, print feedback, wait for the result."""
    client = ActionClient(node, RunMissionAction, "/missions/run")
    if not client.wait_for_server(timeout_sec=10.0):
        node.get_logger().error("the /missions/run action is not available")
        return 1

    def on_feedback(msg) -> None:
        fb = msg.feedback
        node.get_logger().info(f"[{fb.run_id}] {fb.status}: step {fb.step_name or fb.step_id}")

    goal_future = client.send_goal_async(RunMissionAction.Goal(name=name, inputs_json=inputs), feedback_callback=on_feedback)
    rclpy.spin_until_future_complete(node, goal_future)
    handle = goal_future.result()
    if handle is None or not handle.accepted:
        node.get_logger().error("goal rejected")
        return 1
    result_future = handle.get_result_async()
    try:
        rclpy.spin_until_future_complete(node, result_future)
    except KeyboardInterrupt:
        node.get_logger().info("canceling the run")
        rclpy.spin_until_future_complete(node, handle.cancel_goal_async(), timeout_sec=5.0)
        return 130
    result = result_future.result().result
    node.get_logger().info(f"finished: {result.status} {result.message} result={result.result_json}")
    return 0 if result.success else 1


def main(args=None) -> int:
    rclpy.init(args=args)
    node = Node("example_start_mission")
    name = node.declare_parameter("mission", "go_to_point").value
    inputs = node.declare_parameter("inputs", "{}").value
    wait = node.declare_parameter("wait", False).value
    json.loads(inputs)  # fail early on a typo in the inputs JSON
    try:
        rc = follow(node, name, inputs) if wait else start(node, name, inputs)
    except KeyboardInterrupt:
        rc = 130
    node.destroy_node()
    rclpy.try_shutdown()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
