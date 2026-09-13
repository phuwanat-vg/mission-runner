# Mission

Build and run Nav2 robot missions without writing code.

A mission is a JSON file: *when* it starts (MQTT, a ROS topic, a PLC flag, a
schedule, a button, an HTTP call, another mission finishing), *what* it does
(every `nav2_simple_commander` call, loops, conditions, retries, waiting for
external events, asking a person), and *what may interrupt it* (low battery,
e-stop). `mission_runner` runs on the robot as an always-on service and needs
no operator and no internet. The editor is a web page served by the robot (or
run on a laptop) for building, deploying and watching missions; the same JSON
can be exported as a plain Python script.

```
┌──────────── Windows / laptop / phone ────────────┐      ┌────────────── robot (Raspberry Pi 5, ROS 2 Jazzy) ──────────────┐
│ editor (web page)  ──HTTP/WS──▶ mission_runner   │      │ mission_runner ──▶ Nav2 (BasicNavigator, BT XML per goal)        │
│ iViz / any ROS node ──services/actions/topics──▶ │      │   ▲ triggers: MQTT · ROS topics · Modbus · GPIO · cron · webhook │
│ PLC / Node-RED ──Modbus / HTTP──▶                │      │   ▼ state: /mission/state · MQTT · WebSocket · run log (SQLite)  │
└──────────────────────────────────────────────────┘      └───────────────────────────────────────────────────────────────────┘
```

## Repository

| Path | What |
|---|---|
| [`runner/`](runner/) | `mission_runner` ROS 2 (ament_python) package: interpreter, dispatcher, connectors, HTTP/WS API, web page. Runs without ROS in `--sim` mode. |
| [`mission_msgs/`](mission_msgs/) | `RunMission` service/action and `Answer` service (optional; the runner works without it). |
| [`editor/`](editor/) | Web mission editor (Vite + TypeScript), served by the runner. Still works, but **[Mission Builder](https://github.com/phuwanat-vg/mission-builder) is where missions are built now**. |
| [`examples/`](examples/) | Example missions (the four sample scripts as missions, plus PLC pickup, patrol, charging, e-stop) and `sites.json`. |
| [`docs/mission-format.md`](docs/mission-format.md) | The mission format: steps, triggers, policies, expressions. |
| [`docs/runner-api.md`](docs/runner-api.md) | HTTP, WebSocket and ROS interfaces of the runner. |
| **Mission Builder** ([phuwanat-vg/mission-builder](https://github.com/phuwanat-vg/mission-builder)) | The Windows desktop application for building missions: the mission tree, the map and the route graph, maps, deploy, run and export. See [`docs/mission-builder-app.md`](docs/mission-builder-app.md). |
| [`docs/iviz-route-mode.md`](docs/iviz-route-mode.md) | **Route mode in iViz** (parked: the button is greyed out in iViz 0.1.0): drawing the route graph, stops and their actions, over one foxglove_bridge connection. |
| [`docs/first-run-on-robot.md`](docs/first-run-on-robot.md) | **Checklist for the first run on real hardware**: install, start order, what to check at each step, what usually goes wrong. |
| [`docs/editor-spec.md`](docs/editor-spec.md) | Web editor design and behavior (legacy). |

## Quick start without a robot (any OS)

```bash
git clone https://github.com/phuwanat-vg/mission-runner.git && cd mission-runner
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r runner/requirements.txt
cd editor && npm install && npm run build && cd ..
cd runner && python -m mission_runner run --sim --port 8080
```

Open <http://localhost:8080>. The example missions are installed into
`~/.mission` on first start; press **Run** on `go_to_point` and watch the
simulated robot drive. The sim mode also exposes
`POST /api/sim/topic` (inject a ROS message, e.g. a low battery) and
`POST /api/sim/robot` (move the robot, set the battery, block the path).

## Install on the robot

Doing this for the first time? Follow
[`docs/first-run-on-robot.md`](docs/first-run-on-robot.md) instead — same steps,
plus what to verify at each stage and what usually goes wrong.

```bash
sudo apt install ros-$ROS_DISTRO-nav2-simple-commander python3-aiohttp python3-jsonschema python3-yaml python3-paho-mqtt python3-croniter
# optional: pip install pymodbus gpiozero
cd ~/ros2_ws/src && git clone https://github.com/phuwanat-vg/mission-runner.git
cd ~/ros2_ws && colcon build --packages-select mission_msgs mission_runner && source install/setup.bash
ros2 launch mission_runner mission_runner.launch.py
```

Then:

1. Copy `runner/config/runner.example.yaml` to `~/.mission/runner.yaml` and set `nav2.localizer` (`amcl`, or `""` with FAST-LIO2).
2. Copy `runner/config/connectors.example.yaml` to `~/.mission/connectors.yaml` for MQTT / Modbus; put secrets in `/etc/mission_runner.env`.
3. Install `runner/deploy/mission_runner.service` so the runner starts at boot and restarts on failure.
4. Open `http://<robot>:8080` from any browser on the LAN, teach sites with **Capture from robot**, build a mission, **Deploy**.

Build the editor once (`cd editor && npm run build`) before `colcon build` so
the web page is bundled; without it the robot serves a minimal status page
(STOP, run buttons, prompts) that still works from a phone.

## How it works

- **Mission = data.** `docs/mission-format.md` is the contract. The editor, the
  runner and the Python exporter all consume the same JSON.
- **Runner = the brain.** One active run at a time, a priority queue,
  suspended runs that resume where they stopped, and dispatch policies
  (`queue`, `preempt`, `preempt_latest`, `reject_if_busy`,
  `interrupt_and_resume`). Triggers are armed the moment a mission is deployed
  and stay armed after every reboot. Everything that happens is recorded in
  `runs.sqlite` and streamed over WebSocket, `/mission/event` and MQTT.
- **Nav2 stays Nav2.** Steps map one to one onto `BasicNavigator`; a
  per-step `behavior_tree` template is expanded to a Nav2 BT XML file, so
  recovery behavior can differ per goal without touching the global
  configuration.
- **Offline first.** Local MQTT broker, LAN web page, GPIO buttons, cron and
  PLC flags all work with no internet; cloud connectors reconnect on their
  own and never block a mission.

## Development

```bash
cd runner && python -m pytest          # 74 tests, sim backend, ~1.5 min
cd editor && npm test && npm run build
```

`mission_runner validate examples/*.json --sites examples/sites.json` checks
mission files from the command line; `mission_runner bt '{"template": "navigate_with_recovery"}'`
prints a behavior tree.

## Roadmap

- Graph view and a Nav2 behavior-tree editor with the `nav2_behavior_tree`
  node palette (the data model already carries per-step trees).
- Pose picking on the map inside iViz (the editor exposes a pose callback;
  iViz already has the click-and-drag goal tool).
- `parallel` step and per-waypoint task executor options for
  `nav.follow_waypoints`.
- Open-RMF task adapter.

## License

Apache-2.0
