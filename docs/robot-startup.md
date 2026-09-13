# Robot startup: bringup package, station answers, autostart at boot

Status: sections 2-4 (the robot side) are implemented in `mission_runner`
(see "Implementation notes" at the end of section 4); section 5 (Mission
Builder) is in progress. Everything here is part of the existing ROS 2
package `mission_runner` (ament_python), so it installs with
`colcon build --packages-select mission_msgs mission_runner` and
`rosdep install --from-paths src -y --ignore-src`.

## 1. What the robot runs

Two layers, each its own systemd service, so Nav2 keeps running in the
background when the mission layer restarts (and the other way round):

| Service | Launches | Written by |
|---|---|---|
| `robot` (any name) | the user's own robot launch file: drivers, localization, **Nav2** | the user; Mission Builder only points at it |
| `mission` | `mission_runner bringup.launch.py`: mission_runner + foxglove_bridge + station answer nodes | this package |

`mission` starts **after** `robot` (`After=` + `Wants=`) and `mission_runner`
already waits for Nav2 to become active (`nav.wait_active`), so the order at boot
is safe even when Nav2 takes a minute. A user may also put everything into one
launch file (their robot launch including `bringup.launch.py`); then there is only
one service.

## 2. `bringup.launch.py`

```bash
ros2 launch mission_runner bringup.launch.py \
  project:=~/line2.mproj          # optional: import this project before the runner starts
  bridge:=true bridge_port:=8765 include_hidden:=true \
  stations:=~/.mission/stations.yaml \
  home:=~/.mission port:=8080 sim:=false log_level:=INFO
```

| Argument | Default | Meaning |
|---|---|---|
| `project` | `""` | `.mproj` / project JSON file. Imported (replace=false) before the runner starts; if it is invalid the runner does not start and the errors are printed. |
| `project_replace` | `false` | also delete missions on the robot that are not in the project |
| `bridge` | `true` | start `foxglove_bridge` |
| `bridge_port` | `8765` | |
| `include_hidden` | `true` | needed by iViz for Nav2 actions |
| `stations` | `""` | YAML file listing station answer nodes to start (below) |
| `home`, `port`, `host`, `sim`, `log_level` | as `mission_runner.launch.py` | |

`mission_runner run --project FILE [--project-replace]` does the import in the
runner process itself (same checks as `mission_runner project import`), so the
launch file only passes the argument.

## 3. `station_answer` node

`ros2 run mission_runner station_answer --ros-args -p station:="Conveyor 1" -p mode:=auto`

Answers `ros.request` questions for one station without writing code.

| Parameter | Default | Meaning |
|---|---|---|
| `station` | `""` | station name; used for the default topics and, with `match_station`, to ignore other stations' requests |
| `request_topic` / `answer_topic` | `/station/<slug>/request` / `/answer` (slug: lowercase, non `[a-z0-9_]` → `_`); `/iviz/request` / `/iviz/answer` when `station` is empty | |
| `match_station` | `false` | ignore requests whose `station` differs (for shared topics) |
| `mode` | `auto` | `auto` \| `topic` \| `service` \| `gpio` |
| `answer` | `""` | `auto`: the answer; empty = the request's `default`, else its first option, else `OK` |
| `delay_s` | `0.0` | `auto`: wait before answering |
| `source_topic`, `source_type` | `""`, `std_msgs/msg/Bool` | `topic`: Bool, String, Int32 or Float32 |
| `true_answer` / `false_answer` | `OK` / `Reject` | Bool (and numbers: non-zero = true) map to these; String is passed through |
| `max_age_s` | `0.0` | `topic`: a value older than this is stale → wait for a fresh one (0 = any value) |
| `service` | `""` | `service`: `std_srvs/srv/Trigger`; success → `true_answer`, else `false_answer` |
| `gpio_pins` / `gpio_answers` | `[]` / `[]` | `gpio`: BCM pins and the answer each button gives (needs `gpiozero`) |
| `by` | node name | the `by` field of the answer |

Every mode answers each request once, with its `id`; the answer includes
`"station"` when known. Requests without an `id` are ignored.

`stations.yaml` for `bringup.launch.py`:

```yaml
stations:
  - station: Conveyor 1
    mode: topic
    source_topic: /conveyor1/part_present
    source_type: std_msgs/msg/Bool
  - station: Rack3
    mode: gpio
    gpio_pins: [17, 27]
    gpio_answers: [Done, Retry]
```

Each entry becomes one `station_answer` node named `station_answer_<slug>`.

## 4. Autostart services

Managed by `mission_runner` itself as **systemd user services** of the user the
runner runs as: no root needed to add, change or remove one. For them to start
at boot without anyone logging in, the user needs *linger* once:
`sudo loginctl enable-linger $USER` (the API tries `loginctl enable-linger`
without sudo first and reports the command when that is not allowed).

Files, for a service named `robot`:

- `~/.config/systemd/user/mission-autostart-robot.service`
- `~/.mission/autostart/robot.sh`: the wrapper the unit executes
- `~/.mission/autostart/robot.json`: what was asked for (the source of truth for listing)

Wrapper (values quoted with `shlex.quote`; no user text reaches a shell unquoted):

```bash
#!/bin/bash
# Written by mission_runner autostart. Edit through Mission Builder or `mission_runner autostart`.
set -e
source /opt/ros/jazzy/setup.bash
source /home/pi/robot_ws/install/setup.bash      # each workspace in order
export ROS_DOMAIN_ID=7                           # only when set
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp     # only when set
exec ros2 launch /home/pi/robot_ws/src/my_robot/launch/robot.launch.py use_sim_time:=false
```

(`exec ros2 launch <package> <file> args...` for the package form.)

Unit:

```ini
[Unit]
Description=<description> (mission_runner autostart)
After=network-online.target mission-autostart-<after>.service
Wants=network-online.target mission-autostart-<after>.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/home/pi/.mission/autostart/robot.sh
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

Rules:

- **Name**: `^[a-z][a-z0-9-]{0,31}$`.
- **Launch target**, one of:
  - `{"file": "/abs/path/x.launch.py"}`: must exist, be under an allowed root, and end in `.launch.py`, `.launch.xml`, `.launch.yaml`, or be `.py/.xml/.yaml` inside a directory named `launch`;
  - `{"package": "mission_runner", "file": "bringup.launch.py"}`: package and file names only (`^[A-Za-z0-9_.-]+$`).
- **Args**: each `name:=value`, name `^[A-Za-z_][A-Za-z0-9_]*$`, value without newlines.
- **Workspaces**: `setup.bash` files that exist; default for a file target: every ancestor directory's `install/setup.bash`, nearest last; ROS distro from `$ROS_DISTRO` or the only directory in `/opt/ros`.
- **Allowed roots** (browsing and file targets): `autostart.roots` in `runner.yaml`, default `[$HOME, /opt/ros]`; symlinks resolved before the check.
- `autostart.enabled: false` in `runner.yaml` turns every endpoint into 403 (read-only listing still allowed).
- Not Linux / no `systemctl --user` → `supported: false` with the reason; listing works, changes return 409.
- Calls use `systemctl --user` with `XDG_RUNTIME_DIR=/run/user/<uid>` set when missing (a runner started by a system unit has none).
- Removing or stopping **the service the runner itself runs in** (detected by `INVOCATION_ID` / cgroup) answers first, then acts after 1 s; the response says `"self": true`.

### HTTP API (also over `/mission/api`)

| Method & path | Body / query | Result |
|---|---|---|
| `GET /api/autostart` | | `{supported, reason?, enabled, user, linger, ros_distro, roots, self?, services:[Service]}` |
| `GET /api/autostart/browse` | `?path=` (default: first root) | `{path, parent|null, roots, entries:[{name, path, kind: "dir"\|"file", launch: bool}]}`; dirs first, hidden entries skipped |
| `PUT /api/autostart/{name}` | `{description?, launch:{file}\|{package,file}, args?:[], workspaces?:[], ros_domain_id?:int, rmw?:str, after?:[name], start_now?:bool}` | `200 Service` / `400 {errors:[str]}`; writes files, `daemon-reload`, `enable`, and `restart` when `start_now` |
| `POST /api/autostart/{name}/start\|stop\|restart` | | `200 Service` |
| `DELETE /api/autostart/{name}` | | `200 {removed: name, self: bool}`: stop, disable, delete the three files, `daemon-reload` |
| `GET /api/autostart/{name}/log` | `?lines=200` | `{lines:[str]}` from `journalctl --user -u <unit> -n N --no-pager -o short-iso` |
| `POST /api/autostart/linger` | | `{linger: bool, command?: "sudo loginctl enable-linger pi"}` |

`Service`: `{name, unit, description, launch, args, workspaces, ros_domain_id, rmw, after, enabled: bool, active: "active"|"activating"|"inactive"|"failed"|..., sub_state, since: iso|null, restarts: int, main_pid: int|null, self: bool}`
(from `systemctl --user show <unit> -p ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,NRestarts,MainPID`).

Events: `autostart.changed {name}` on every change.

### CLI (on the robot, no network needed; the first install has no runner running yet)

```bash
mission_runner autostart list
mission_runner autostart add robot --launch ~/robot_ws/src/my_robot/launch/robot.launch.py --arg use_sim_time:=false
mission_runner autostart add mission --package mission_runner --launch bringup.launch.py --after robot --arg project:=/home/pi/line2.mproj --start
mission_runner autostart start|stop|restart|remove|log <name>
mission_runner autostart linger
```

`add` on an existing service changes only the options given (`--arg` replaces
an argument of the same name; `--clear-args` drops the old ones first).
`--domain N` sets ROS_DOMAIN_ID, `--rmw` the RMW. The CLI works whatever
`autostart.enabled` says.

### Implementation notes (details the tables above leave open)

- Every error response is `{error: str, errors: [str]}` (`error` = the first
  message), so a client may read either; `PUT` validation errors are `400`.
- Status codes: unknown service `404`; browse outside the roots `403`, a
  non-directory or relative path `400`; a failing `systemctl` call `500`.
- `after` must name services that already exist; `browse` and `linger` are
  not allowed as service names; unknown body keys are ignored, so a `Service`
  object can be sent back as a `PUT` body.
- File targets and workspaces are stored with symlinks resolved. Omitted or
  empty `workspaces` are detected: for a file, the ancestors' `install/setup.bash`;
  for a package, the workspaces the runner was started from (`COLCON_PREFIX_PATH`).
- `restart` of the runner's own service (and `PUT` with `start_now`) is deferred
  like `stop`; removing it disables and deletes the files first, then stops it.
- `ros2 launch` exits 0 when a node dies, so `Restart=on-failure` only covers
  the launch process itself: `bringup.launch.py` starts mission_runner with
  `respawn` (5 s delay), so a crashed runner comes back inside the service
  (an invalid `project` is then reported again every 5 s until it is fixed).
- Only the nav2 backend talks ROS: a `--sim` runner publishes `ros.request`
  into its simulated topics, so `station_answer` nodes answer a real runner only.

## 5. Mission Builder: Robot startup

**… menu → Robot startup** (enabled when connected; a runner without
`/api/autostart` shows "Update mission_runner on the robot").

- A list of services: status dot + state ("Running since 08:02", "Failed, restarted 3 times", "Stopped"), launch target, "starts after robot", buttons **Start / Stop / Restart / Log / Edit / Remove**. Remove asks for confirmation; for the runner's own service it says the connection will drop.
- **Add service…** dialog: Name, Description, Launch file (**Browse…** opens a file browser *on the robot*: roots, breadcrumbs, folders, launch files highlighted, others greyed out) or Package + file, launch arguments as rows (`name` `:=` `value`), Workspaces (filled from the chosen file, editable), ROS_DOMAIN_ID, RMW, Start after (other services), Start now.
- Quick action **Add the mission layer**: package `mission_runner`, file `bringup.launch.py`, after the first other service, `project` argument offered with the robot path the last deploy used (editable).
- A banner when linger is off: "Services will only start at boot after `sudo loginctl enable-linger pi` is run once on the robot." with **Try now** (POST linger) and a copy button.
- A banner when `supported` is false, with the reason.
- **Log** shows the last 200 journal lines, monospace, with Refresh.
