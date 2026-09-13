# Changelog

## Unreleased

- **Routes stay on the lanes**: `nav.follow_route` no longer lets the planner
  cut corners between distant lane nodes
  - `waypoint_spacing_m` (default 0.75, `0` = lane nodes only, as before): a
    waypoint every that many metres along each lane, facing along it; nodes face
    the next lane, the last pose keeps the destination's yaw
  - `sites/1` edges take `"strict": true`: consecutive strict lanes are driven
    with `FollowPath` along the drawn line (0.05 m points, optional step
    `controller_id` / `goal_checker_id`); a strict first lane more than 0.3 m
    from the robot gets a `goToPose` to its start first (`lead_in`)
  - the route is sent as consecutive segments; `value.segments` =
    `[{mode: through_poses|follow_path|go_to_pose, sites, poses}]`, plus
    `waypoint_spacing_m`; `legs[]` carry `strict`
  - `apply_speed_limits` sets each lane's cap before its segment (segments split
    where the cap changes) instead of driving lane by lane with `goToPose`

- **Home as initial pose**: `sites/1` maps take
  `"initial_pose": {"site": "Home", "on_start": true}` (the site must exist in that
  map). On start, when the robot is not localized (no map -> robot TF within ~3 s),
  mission_runner waits for the localizer (up to 10 min, in the background) and sets
  the initial pose at that site; a robot that is already localized is left alone.
  Replaces AMCL `set_initial_pose` and fixes the global costmap timing out at
  bring-up for want of map -> odom (docs/robot-startup.md)
  - nav2 `set_initial_pose` (also the `nav.set_initial_pose` step) waits for the
    localizer lifecycle node, then republishes `/initialpose` every second until
    `/amcl_pose` answers or the map -> robot TF appears, 30 s max; once with
    `localizer: ""`
  - `POST /api/robot/initial_pose` `{site}` or `{x, y, yaw_deg}` (400 / 409 while
    a mission runs / 504), event `robot.initial_pose {site, x, y, yaw_deg, source, ok, message?}`

- **Robot startup** (docs/robot-startup.md): the robot side of Mission Builder's
  *Robot startup* tool
  - `bringup.launch.py`: mission_runner + foxglove_bridge (`include_hidden`) +
    one `station_answer` node per entry of a stations YAML; `project:=` imports a
    project before the runner starts
  - `mission_runner run --project FILE [--project-replace]`: an invalid project
    prints the errors, writes nothing and the runner does not start
  - `station_answer` node: answers `ros.request` for one station, by a fixed
    answer (`auto`), a Bool/String/Int32/Float32 topic, a `std_srvs/Trigger`
    service or Raspberry Pi buttons (`gpio`, needs gpiozero)
  - **Autostart services**: systemd user units that launch a robot launch file
    or a package launch file at boot, with ordering (`after`), ROS_DOMAIN_ID and
    RMW. `GET/PUT/DELETE /api/autostart...` (browse the robot's files for launch
    files, start/stop/restart, journal log, linger), event `autostart.changed`,
    and offline `mission_runner autostart list|add|start|stop|restart|log|remove|linger`.
    Every name, path, argument and value is validated and shell-quoted;
    `autostart.enabled: false` in `runner.yaml` makes the API read-only
- `install.sh`: one command installs or updates the runner on a Jazzy robot
  (clone/pull, rosdep, build, linger, `mission` autostart service)
- Example nodes to copy: `example_start_mission`, `example_answer_requests`,
  `example_ask_robot`, `example_watch_missions` (docs/example-nodes.md)
- Fix: `colcon build` failed on current setuptools ("'data_files' must be
  relative") because the example missions were listed with absolute paths

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
