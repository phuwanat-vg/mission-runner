# Changelog

## Unreleased

- **`ros.request` step**: publish a JSON request and wait for the answer with the
  same id over two `std_msgs/String` topics (default `/iviz/request` and
  `/iviz/answer`, set in `runner.yaml` or per step). It is the exchange iViz's
  Dashboard answers, and any node can answer too. Options, default, timeout
  that falls back to the default or fails, a `station` that defaults to the last
  route target, and free `data`. Events `request` and `request.answered`
- **`nav.follow_route.through`**: pass several sites in order before `to`, every
  leg planned on the route graph, still sent to Nav2 as on-lane waypoints only
- **Project files (`project/1`, `.mproj`)**: `GET /api/project` exports the
  robot's sites and missions; `PUT /api/project` validates a whole project and
  writes nothing if any mission is invalid, then re-arms triggers
  (`replace=true` also removes missions not in the project). Offline, with no
  network or GUI: `mission_runner project import|export <file>`
- **Topics per station**: a site may carry `request_topic` / `answer_topic`.
  A `ros.request` asked at that station (its `station`, or the last route
  target) uses them unless the step names its own, so each station's screen or
  node only receives its own questions
- Example `inspect_route`: a round on the lanes with a request at each stop
- Fix: `nav.follow_route` without `from` starts at the site nearest the robot
  even when no lane leads away from it. It used to skip such a site (e.g. the
  end of a one-way lane) and start at a farther one, sending the robot across
  the floor off the lanes; now there is no route and the step fails
- **Route graph**: sites can be connected by `edges` (one-way, speed cap, blocked,
  cost). New `nav.follow_route` step plans over the graph and drives only the
  lanes that were drawn, with `on_no_route` and optional per-lane speed limits.
- **Zones**: keep-out, speed-limit and named areas per map, exported as Nav2
  costmap filter masks (`POST /api/maps/{name}/filters`), readable from
  expressions with `in_zone()` and `zone_of()`.
- **Preview**: `POST /api/preview/route` plans the real path through a mission
  with distance and time estimate; `POST /api/preview/dryrun` replays the whole
  flow against a private simulated robot and returns an animatable timeline.
- **Live map layers**: costmap, laser scan, Nav2 plan and footprint streamed on
  demand over the WebSocket, converted to map coordinates and rate limited.
- **Map images**: `GET /api/maps/{name}` and `/image` serve the robot's map as a
  PNG, with a plain room drawn around the sites when no map file exists.
- **One ROS service for GUIs**: `/mission/api` (`mission_msgs/srv/Api`) tunnels
  the whole HTTP API, so a desktop app on foxglove_bridge needs no second
  connection. Mission building moves into iViz's Route mode.


## 0.1.0 — 2026-09-10

First version.

- `mission/1` format: navigation steps for the whole `BasicNavigator` API, logic
  (set/if/loop/break/end), wait, wait_event, ask_user, run_mission, ROS
  publish/service/action/params, MQTT/HTTP/Modbus/GPIO sinks
- Triggers: ROS topic, MQTT, webhook, cron/interval/boot, GPIO, Modbus poll,
  mission.done; interrupts with `interrupt_and_resume`; global e-stop interrupts
- Dispatcher with priorities, queue, preempt, preempt_latest, reject_if_busy,
  pause/resume, STOP
- `mission_runner` ROS 2 package with `--sim` mode, HTTP/WebSocket API, run log,
  ROS services/action, behavior-tree templates, systemd unit and launch file
- `mission_msgs` interfaces
- Web editor served by the runner: steps list, inspector, triggers, sites,
  live run view, run history, JSON tab, Python and BT XML export
- Examples reproducing p2p.py, waypoint.py, follow_path.py and p2p_mqtt.py
