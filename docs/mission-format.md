# Mission format (`mission/1`)

A mission is a JSON document. It is the single source of truth: the editor
edits it, `mission_runner` executes it on the robot, and the exporter turns it
into a `nav2_simple_commander` Python script or Nav2 behavior-tree XML.

The machine-readable contract is
[`runner/mission_runner/schema/mission.schema.json`](../runner/mission_runner/schema/mission.schema.json)
(JSON Schema 2020-12). This page explains the semantics.

```jsonc
{
  "schema": "mission/1",
  "name": "pickup_job",            // ^[a-z][a-z0-9_-]{0,63}$ ; also the file name and service name
  "title": "Pickup job",
  "description": "...",
  "version": 3,
  "policy": "queue",               // policy for manual starts (service/action/HTTP/editor)
  "priority": 50,                  // 0..100, higher wins
  "inputs":     { ... },           // declared parameters
  "vars":       { ... },           // initial variables
  "triggers":   [ ... ],           // external events that start this mission
  "interrupts": [ ... ],           // armed while running; start another mission
  "flow":       [ ... ],           // steps
  "on_abort":   [ ... ]            // cleanup steps when the run fails or is canceled
}
```

## Values, variables and expressions

Every mission run has a variable scope:

| Name | Meaning |
|---|---|
| `zones` | the current map's zones, `{name: {kind, speed_mps}}` |
| declared `inputs` | filled by the trigger's `set`, by the caller, or by `default` |
| `vars` | initial values, then whatever `set` steps write |
| `last` | result of the previous step: `{ok, status, value, error, duration_s}` |
| `<step.out>` | a step's result stored under a name of your choice |
| `payload` | inside trigger `when`/`set` and `wait_event`: the event payload |
| `robot` | `{x, y, yaw_deg, frame, battery}` when known |
| `mission`, `run_id`, `current_map` | metadata |

Expressions use a Python-like syntax evaluated by a small safe interpreter (no
imports, no attribute access on Python objects, no assignment):

```
battery < 0.2 and not charging
payload.data == 'start'
distance(robot, sites.Charger) > 5
answer.value == 'No'
```

Functions: `abs min max len round int float str bool lower upper now distance hypot sqrt get`,
plus two that read the current map's zones: `in_zone('name')` (or
`in_zone('name', x, y)`) and `zone_of()` / `zone_of(x, y)`, which returns the
names of every zone containing the point. Without coordinates both use the
robot's current position.
Dots index into dicts (`payload.pose.position.x`); `[i]` indexes lists.
`$name` and `$.field` are accepted as aliases of `name` and `payload.field`.

Where a step parameter takes a *value*, the runner resolves it like this:

| Written as | Resolves to |
|---|---|
| `12`, `true`, `{"x": 1}` | the literal |
| `"$goal.x"` | the value of expression `goal.x` (any type) |
| `"${rounds + 1}"` | the value of the expression (any type) |
| `"Round ${rounds} done"` | text with `${...}` interpolated |
| any other string | the literal string |

Fields documented as *expression* (`when`, `condition`, `while`) are always
evaluated and need no `$`/`${}` wrapper.

## Poses and sites

A pose can be written as coordinates, a site reference, or a value:

```jsonc
{ "x": 1.0, "y": 0.5, "yaw_deg": -90 }           // frame defaults to "map"
{ "site": "Rack3" }                                 // site in the current map
{ "site": "Rack3", "yaw_deg": 0 }                   // override heading
"Rack3"                                             // shorthand for a site
"$pickup"                                           // input of type site or pose
```

### Zones

Alongside sites, each map may define **zones**: named polygons drawn in the
editor and stored in the same `sites.json`.

| `kind` | Effect |
|---|---|
| `keepout` | exported as a Nav2 keep-out filter mask, so the planner will not route through it |
| `speed_limit` | exported as a Nav2 speed filter mask with `speed_mps` inside |
| `preferred`, `work` | not enforced; a named area for the operator and for expressions |

`POST /api/maps/{name}/filters` writes the masks (`.pgm` + `.yaml`) into
`<home>/filters/`; point your `costmap_filter_info_server` at them to make
keep-outs and speed limits take effect. Missions can read zones at run time
with `in_zone(...)` and `zone_of(...)`, for example to slow down or to refuse a
goal:

```jsonc
{ "type": "if", "condition": "in_zone('cold_store')",
  "then": [{ "type": "ros.set_param", "node": "/controller_server",
             "params": { "FollowPath.max_vel_x": 0.2 } }] }
```

Sites live in `sites.json` on the robot, grouped by map (see
[`sites.schema.json`](../runner/mission_runner/schema/sites.schema.json)).
`nav.change_map` switches both the Nav2 map and the active site set, so a
mission written as "go to Inbound" works in every building that defines
`Inbound`. Referencing a site that does not exist in the current map is a
validation error at deploy time and a step failure at run time.

### Initial pose (Home)

A map may name the site the robot starts at:

```jsonc
"maps": {
  "demo_room": {
    "file": "/home/pi/maps/demo_room.yaml",
    "initial_pose": { "site": "Home", "on_start": true },   // on_start defaults to true
    "sites": { "Home": { "x": 0, "y": 0, "yaw_deg": 0, "kind": "home" } }
  }
}
```

`site` must be a site of the same map (otherwise saving `sites.json` or
importing the project fails). With `on_start`, when mission_runner starts on the
current map and the robot is **not localized** (no map -> `robot_frame` TF within
about 3 s), it waits for the localizer to become active and sets the initial pose
at that site, repeating `/initialpose` until AMCL confirms it. A robot that is
already localized is left alone, so restarting only the mission service never
resets its pose. `POST /api/robot/initial_pose` and the `nav.set_initial_pose`
step use the same confirmed method; each emits `robot.initial_pose`. See
[robot-startup.md](robot-startup.md#home-as-initial-pose).

### Lanes (route graph)

`edges` in a map are the lanes `nav.follow_route` may drive:

```jsonc
"edges": [
  { "from": "Home", "to": "A" },                                   // two-way
  { "from": "A", "to": "B", "bidirectional": false },              // one-way, A -> B
  { "from": "B", "to": "Conveyor1", "speed_mps": 0.3 },            // cap with apply_speed_limits
  { "from": "Conveyor1", "to": "Rack3", "strict": true },          // drive exactly along the line
  { "from": "D", "to": "Home", "blocked": true, "cost": 2, "notes": "" }
]
```

| Field | Default | Meaning |
|---|---|---|
| `bidirectional` | `true` | `false` = one-way, `from` -> `to` |
| `speed_mps` | none | speed cap applied when the step sets `apply_speed_limits` |
| `blocked` | `false` | closed; routes go around it |
| `cost` | `1` | multiplies the length when choosing a route |
| `strict` | `false` | follow the drawn line with Nav2 `FollowPath` instead of planning; the robot stops rather than detouring when the lane is blocked |

Waypoint lists (`poses`, `points`, `goals`) accept the same forms; `points`
additionally accepts `[x, y]` / `[x, y, yaw_deg]` arrays.

## Steps

Common fields on every step:

| Field | Meaning |
|---|---|
| `id` | unique within the mission; the editor generates one |
| `type` | one of the types below |
| `name` | label shown in the UI and logs |
| `enabled` | `false` skips the step |
| `out` | variable name that receives the step result |
| `timeout_s` | fail the step after this long |
| `on_fail` | `{retry, retry_delay_s, before_retry: [steps], then: "abort" \| "continue"}` |

A step result is `{ok, status, value, error, duration_s}` with `status` in
`succeeded`, `failed`, `canceled`, `timeout`. A failed step (after retries)
aborts the run unless `on_fail.then` is `continue`.

### Navigation (`nav.*`)

These wrap `nav2_simple_commander.BasicNavigator` one to one.

| Type | Parameters | Notes |
|---|---|---|
| `nav.wait_active` | `timeout_s` | `waitUntilNav2Active()` |
| `nav.set_initial_pose` | `pose` | waits for the localizer, publishes `/initialpose` every second until AMCL confirms it (30 s max); once without a localizer |
| `nav.follow_route` | `to`, `through[]`, `from`, `on_no_route`, `apply_speed_limits`, `waypoint_spacing_m` (default `0.75`), `controller_id`, `goal_checker_id`, `behavior_tree` | plans on the map's route graph (one-way and blocked lanes honoured). Runs of ordinary lanes go to `goThroughPoses` with a waypoint every `waypoint_spacing_m` along each lane (`0` = lane nodes only), facing the direction of travel (a node faces the next lane; the last pose keeps the destination's `yaw_deg`); a single pose uses `goToPose`. Runs of `strict` lanes go to `followPath` along the drawn lines (points every 0.05 m, `controller_id` / `goal_checker_id`, empty = Nav2 defaults); when the first lane is strict and the robot is more than 0.3 m from its start, a `goToPose` to that start comes first (`lead_in`). With `apply_speed_limits` each lane's `speed_mps` is set before its lanes and restored after. `value` = `{route, legs, length_m, from, to, through, direct, waypoint_spacing_m, segments: [{mode: through_poses \| follow_path \| go_to_pose, sites, poses}], lead_in?, result}` |
| `nav.go_to_pose` | `pose`, `behavior_tree` | `goToPose()`; `value` = final feedback |
| `nav.go_through_poses` | `poses[]`, `behavior_tree` | `goThroughPoses()` |
| `nav.follow_waypoints` | `poses[]` | `followWaypoints()`; `value.missed` = indexes that failed |
| `nav.follow_path` | `points[]` or `path`, `from_robot`, `spacing_m`, `controller_id`, `goal_checker_id` | `points` builds a straight-segment `nav_msgs/Path` like `follow_path.py`; `path` takes a value from `nav.compute_path` / `nav.smooth_path` |
| `nav.compute_path` | `goal`, `start`, `planner_id`, `use_start` | `getPath()`; `value` = the path |
| `nav.compute_path_through_poses` | `goals[]`, `start`, `planner_id`, `use_start` | `getPathThroughPoses()` |
| `nav.smooth_path` | `path`, `smoother_id`, `max_duration_s`, `check_collision` | `smoothPath()` |
| `nav.spin` | `angle_deg`, `time_allowance_s` | `spin()` |
| `nav.backup` | `distance_m`, `speed_mps`, `time_allowance_s` | `backup()` |
| `nav.drive_on_heading` | `distance_m`, `speed_mps`, `time_allowance_s` | `driveOnHeading()` |
| `nav.change_map` | `map` | `changeMap()` with the map's yaml; also switches the active site set |
| `nav.clear_costmap` | `which`: `all` \| `local` \| `global` | |
| `nav.dock` | `dock_id` or `dock_pose`, `dock_type`, `navigate_to_staging` | `dockRobot()` (Nav2 docking server) |
| `nav.undock` | `dock_type` | `undockRobot()` |
| `nav.lifecycle` | `action`: `startup` \| `shutdown` | |
| `nav.cancel` | | cancels any running Nav2 task |

`behavior_tree` is either a path to an XML file on the robot or a template
the runner expands at deploy time:

```jsonc
"behavior_tree": { "template": "navigate_with_recovery", "retries": 4,
                   "recoveries": ["clear_costmap", "spin", "backup"] }
```

The generated file is written to `<home>/bt/<mission>__<step>.xml` and its
path is sent with the goal, so each step can have its own recovery behavior
without touching the global Nav2 configuration.

### ROS (`ros.*`)

| Type | Parameters |
|---|---|
| `ros.publish` | `topic`, `msg_type`, `message` |
| `ros.call_service` | `service`, `srv_type`, `request`; `value` = response |
| `ros.call_action` | `action`, `action_type`, `goal`; `value` = result. Covers any Nav2 action not wrapped above (route server, coverage, ...). |
| `ros.set_param` | `node`, `params` (name → value) |
| `ros.request` | `text`, `options`, `default`, `timeout_s`, `on_timeout` (`default` \| `fail`), `request_topic`, `answer_topic`, `station`, `data`; publishes a JSON request on a `std_msgs/String` topic and waits for `{id, answer, by}` with the same id. `value` = `{id, answer, by, timed_out}`. `station` defaults to the last `nav.follow_route` target; each topic comes from the step, else the station site's `request_topic` / `answer_topic` in `sites.json`, else `/iviz/request`, `/iviz/answer` (runner.yaml). The exchange is the one iViz answers; any node can answer too. Details in [`mission-builder-v2.md`](mission-builder-v2.md#ros-request) |

Message fields are plain JSON matching the ROS type; values may use
expressions.

### Connectors

| Type | Parameters |
|---|---|
| `mqtt.publish` | `connector`, `topic`, `payload`, `qos`, `retain` |
| `http.request` | `method`, `url`, `headers`, `body`; `value` = `{status, body}` |
| `modbus.write` | `connector`, `kind`: `coil` \| `register`, `address`, `value` |
| `gpio.write` | `pin`, `value` |

Connectors are configured on the robot (`connectors.yaml`), missions only
reference them by name. Secrets never appear in mission files.

### Logic

| Type | Parameters | Notes |
|---|---|---|
| `set` | `var`, `value` | |
| `if` | `condition`, `then[]`, `else[]` | |
| `loop` | `count` or `while`, `body[]` | neither → forever; `break` exits |
| `break` | | exits the innermost loop |
| `wait` | `seconds` | |
| `wait_event` | `source` (an event source, see triggers), `timeout_s`, `on_timeout` | `value` = payload |
| `ask_user` | `text`, `options[]`, `timeout_s`, `default` | shown on every connected UI; `value` = chosen option; without `timeout_s` waits forever |
| `log` | `text`, `level` | |
| `run_mission` | `mission`, `inputs` | runs another mission inline; `value` = its result |
| `end` | `result`, `message` | ends the run early |

## Triggers

A trigger is an event source plus dispatch settings. When it fires, the
runner computes inputs from `set` and submits a run.

```jsonc
{
  "id": "plc_start",
  "type": "modbus.poll", "connector": "plc1", "kind": "coil", "address": 100,
  "when": "payload.value", "edge": "rising", "debounce_s": 0,
  "set": { "pickup": "'Conveyor1'" },
  "policy": "queue", "priority": 50, "enabled": true
}
```

Event sources and their payloads:

| Type | Parameters | Payload |
|---|---|---|
| `ros.topic` | `topic`, `msg_type` (optional) | the message as JSON |
| `mqtt.subscribe` | `connector`, `topic` (wildcards ok) | parsed JSON, or `{text}` when not JSON; plus `topic` |
| `http.webhook` | `path` | `POST /hooks/<path>` JSON body |
| `timer.cron` | `cron` (5 fields, local time) | `{time}` |
| `timer.interval` | `seconds` | `{time, count}` |
| `timer.boot` | `delay_s` | `{time}` |
| `gpio.input` | `pin`, `gpio_edge`, `pull`, `bounce_s` | `{pin, value}` |
| `modbus.poll` | `connector`, `kind`, `address`, `poll_s` | `{value}` |
| `mission.done` | `mission`, `result` | `{mission, run_id, result}` |

`when` filters events; `edge: rising` fires only when `when` turns from false
to true (use it for battery thresholds and PLC flags). `debounce_s` ignores
repeats within the window.

Manual starts are always available and need no trigger entry:

- ROS service `/missions/<name>/run` (`std_srvs/srv/Trigger`) and
  `/missions/run` (`mission_msgs/srv/RunMission` with JSON inputs)
- ROS action `/missions/run` (`mission_msgs/action/RunMission`)
- `POST /api/missions/<name>/run` with `{"inputs": {...}}`
- the editor and the robot's web page

They use the mission-level `policy` and `priority`.

### Dispatch policies

Only one run is active at a time. When a trigger fires while something is
running:

| Policy | Behavior |
|---|---|
| `queue` | append to the queue; the queue is ordered by priority, then arrival |
| `preempt` | if `priority >= current.priority`, cancel the current run and start now; otherwise queue |
| `preempt_latest` | like `preempt`, and drop queued runs of the same mission ("latest goal wins") |
| `reject_if_busy` | drop the event when anything is running or queued |
| `interrupt_and_resume` | suspend the current run (its navigation is canceled), run this mission, then resume the suspended run from the step it was in |

### Interrupts

`interrupts` use the same shape as triggers plus `run: "<mission>"` and are
armed only while the mission is running. Put interrupts that must always be
armed (e-stop) in the special mission `global` (`examples/global.json`); its
flow is ignored.

## Validation

The runner validates on deploy (`PUT /api/missions/<name>`) and on load:

1. JSON Schema (structure, types, enums).
2. Semantic checks: unique step ids, referenced missions exist, sites exist in
   the default map, connectors exist, `break` only inside `loop`, `run_mission`
   has no cycles, expressions parse.
3. Capability warnings: step or trigger types the current backend cannot run
   (for example `nav.dock` when the docking server is absent) are reported as
   warnings, not errors, so a mission can be authored before the hardware is
   ready.
