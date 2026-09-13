# Example nodes

Four small ROS 2 nodes that talk to `mission_runner`, meant to be copied into
your own package. Each is one file of plain `rclpy` (no `mission_runner`
imports) in [`runner/mission_runner/examples/`](../runner/mission_runner/examples/),
with its parameters and the message formats in its top docstring.

They talk to a runner on the **nav2 backend** (`ros2 launch mission_runner
bringup.launch.py`); a `--sim` runner has no ROS interface. The outputs below
are from a run on ROS 2 Jazzy with a runner started by `bringup.launch.py` and
test missions (no Nav2 needed for those).

| Node | Shows |
|---|---|
| `example_start_mission` | start a mission with the `/missions/run` service, or follow it with the `/missions/run` action |
| `example_answer_requests` | answer `ros.request` questions |
| `example_ask_robot` | publish an event that starts a mission through its `ros.topic` trigger |
| `example_watch_missions` | follow `/mission/state` and `/mission/event`, stop a run with `/mission/stop` |

## example_start_mission

```bash
ros2 run mission_runner example_start_mission --ros-args -p mission:=go_to_point
ros2 run mission_runner example_start_mission --ros-args -p mission:=fetch_part -p inputs:='{"part": "B"}'
ros2 run mission_runner example_start_mission --ros-args -p mission:=go_to_point -p wait:=true
```

The service (`mission_msgs/srv/RunMission`) returns at once with the run id;
with `wait:=true` the action (`mission_msgs/action/RunMission`) prints a
feedback line per step and the result, and Ctrl+C cancels the run.

```
[example_start_mission]: started 'ask_station': run 20260913-215754-hp9z (starting)
[example_start_mission]: finished: succeeded  result={"ok": true, "status": "succeeded", "value": null, "error": "", "duration_s": 0.0}
[example_start_mission]: 'no_such_mission' was not started: unknown mission 'no_such_mission'
```

## example_answer_requests

```bash
ros2 run mission_runner example_answer_requests                                  # /iviz/request -> /iviz/answer
ros2 run mission_runner example_answer_requests --ros-args -p station:="Conveyor 1"  # /station/conveyor_1/...
ros2 run mission_runner example_answer_requests --ros-args -p request_topic:=/cell/req -p answer_topic:=/cell/ans
```

Receives `{"id", "text", "options", "default", "station", "data", ...}` and
publishes `{"id", "answer", "by"}`. Its `decide()` picks the last option when
the request's `data.ok` is `false`, else the default, else the first option;
replace it with your own logic. For answers that need no code (a fixed answer,
a sensor topic, a Trigger service, buttons) use the `station_answer` node
instead (docs/robot-startup.md, section 3).

```
[example_answer_requests]: answering requests on /iviz/request -> /iviz/answer
[example_answer_requests]: 'Part OK?' (ask_iviz) -> Reject
```

and in the mission's run log: `answer Reject by example_answer_requests`.

## example_ask_robot

```bash
ros2 run mission_runner example_ask_robot --ros-args -p part:=B
ros2 run mission_runner example_ask_robot --ros-args -p part:=B -p repeat_s:=30.0
```

Publishes `std_msgs/String` `B` on `/cell/part_ready` once somebody subscribes.
The mission in the node's docstring (`fetch_part`) has a trigger
`{"type": "ros.topic", "topic": "/cell/part_ready", "msg_type": "std_msgs/msg/String", "set": {"part": "payload.data"}}`,
so each message starts a run with `part = "B"`.

```
[example_ask_robot]: published 'B' on /cell/part_ready (1 subscribers)
```

and `GET /api/runs?mission=fetch_part` shows a run with
`"inputs": {"part": "B"}, "source": {"kind": "trigger", "id": "part_ready", "detail": "topic /cell/part_ready"}`.

## example_watch_missions

```bash
ros2 run mission_runner example_watch_missions
ros2 run mission_runner example_watch_missions --ros-args -p stop_after_s:=5.0
```

Prints state changes from `/mission/state` (transient local, so the current
state arrives at once) and run and step events from `/mission/event`. With
`stop_after_s` it calls `/mission/stop` (`std_srvs/srv/Trigger`) when a run has
been going for that long.

```
[example_watch_missions]: state idle
[example_watch_missions]: state running: long_wait is running
[example_watch_missions]: run 20260913-215801-sh0l of 'long_wait' started
[example_watch_missions]:   step wait (wait)
[example_watch_missions]: run is older than 3 s: calling /mission/stop
[example_watch_missions]: /mission/stop: stopping
[example_watch_missions]:   step w: canceled
[example_watch_missions]: state idle
[example_watch_missions]: run 20260913-215801-sh0l finished: canceled stopped via ROS
```

## Copying one into your package

Copy the file, change the node name, and register `main` as a console script
(`"my_node = my_package.my_node:main"` in `setup.py`).
`example_start_mission` also needs `<exec_depend>mission_msgs</exec_depend>`;
the others only use `std_msgs` and `std_srvs`.
