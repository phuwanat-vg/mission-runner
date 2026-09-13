# Mission Builder 2: points, routes, requests and project files

This page changes Mission Builder in four ways the user asked for. Everything
in [`mission-builder-app.md`](mission-builder-app.md) still holds unless it is
overridden here. The robot-side contracts it relies on are defined in this page
and implemented in `mission_runner`.

1. Points are placed on the map **or typed in as coordinates**, joined into a
   route graph whose lanes are **one-way or two-way**, and every drive follows
   that graph: the robot is only ever given waypoints that lie on the lanes.
2. Every point can carry **actions**, including a **request** that waits for an
   answer on configurable topics, using the same exchange iViz already answers,
   so iViz, a PLC adapter or any node written later can take part.
3. The whole setup is a **project file**: New, Open, Save, Save As, with no
   robot connected.
4. It still runs **offline**: the robot executes missions headless, and a
   project can be put on a robot without the application, from a USB stick or
   `scp`.

---

## 1. Points and routes

### Placing a point

- **On the map**: the Point tool (`N`), click, drag before releasing to set the
  heading, type the name.
- **By coordinates**: an **Add point** button opens a small form: name, x (m),
  y (m), heading (degrees), kind (station, dock, waypoint, home). **Robot
  pose** fills x, y and heading from the robot's current position when
  connected. The point appears on the map and is selected.
- The properties of a selected point always show **x, y and heading as number
  fields**; typing moves the point live, with the same undo as dragging.
- Coordinates are in the map frame, metres, heading in degrees with 0 along +x
  and counter-clockwise positive, rounded to 3 decimals (heading to 1).

### Joining points

- The Lane tool (`L`) drags from one point to another. The lane is **two-way**
  by default; holding Shift makes it one-way in the drag direction.
- A selected lane shows **Direction** as three large choices with arrows:
  `A ⇄ B` two-way, `A → B` one-way, `B → A` one-way. Plus blocked, speed limit
  and cost as today.
- **Connect in order**: selecting several points (Ctrl-click) and pressing
  **Connect** draws two-way lanes between them in selection order.
- On the map, a two-way lane has arrowheads both ways and a one-way lane one;
  the direction is also readable in the lane list of a selected point
  (`→ Rack3`, `← Charger`, `⇄ B`).

### Driving follows the graph

- A task that drives somewhere is added as **Follow route** (`nav.follow_route`)
  by default. Plain `go_to_pose` stays available under *Drive somewhere → Direct
  (ignores the route)* for the rare case that needs it.
- Before anything is sent to Nav2 the runner plans on the graph (Dijkstra over
  lane length x cost, honouring one-way and blocked lanes), turns the planned
  chain of points into waypoints, and sends those with `NavigateThroughPoses`.
  The robot never receives a waypoint that is not on a lane.
- `nav.follow_route` gains an optional **`through`** list: sites to pass in
  order before `to`, each leg planned on the graph. Use it for a route with
  several points and no actions between them.
- The application shows the **planned route** for the selected task (and for
  the whole mission, in order) on the map before running, computed with the same
  rules, and marks a task red with a sentence when there is no route (for
  example because a lane is one-way the wrong way).

Schema (`mission.schema.json`):

```jsonc
{ "type": "nav.follow_route",
  "to": "Rack3",                      // site, or expression
  "through": ["B", "Conveyor1"],      // optional, visited in order first
  "from": "Home",                     // optional; default: the point nearest the robot
  "on_no_route": "fail",              // or "direct"
  "apply_speed_limits": false,
  "waypoint_spacing_m": 0.75,         // extra waypoints along lanes; 0 = lane nodes only
  "controller_id": "",                // FollowPath controller for strict lanes (optional)
  "goal_checker_id": "" }             // FollowPath goal checker for strict lanes (optional)
```

### Strict lanes

Between two waypoints the Nav2 global planner is free, so on a long lane the
robot can cut a corner or swing off the drawn line. Two ways to keep it there:

- **Dense waypoints** (default): `nav.follow_route` adds a waypoint every
  `waypoint_spacing_m` (0.75 m) along each lane, facing along the lane. The
  planner stays close to the lane but can still go around an obstacle.
- **Strict lane** (`"strict": true` on the edge): consecutive strict lanes are
  driven with `FollowPath` along the drawn line itself (a point every 0.05 m).
  There is no planner, so the robot follows the line exactly and **stops rather
  than detouring** when something blocks it. If the route starts with a strict
  lane and the robot is more than 0.3 m from its start, the runner first drives
  there with `NavigateToPose`.

The step result lists what was sent, in order:
`"segments": [{"mode": "through_poses" | "follow_path" | "go_to_pose", "sites": [...], "poses": N}]`.

---

## 2. Actions at a point, and requests

A point's properties list **the tasks that happen when a mission arrives
there**. Because a mission is a sequence, "actions at a point" are the tasks
that follow a *Follow route* to that point in the tree; selecting a point shows
them per mission (`patrol: 2 actions`, `pickup_job: 3 actions`) with **Add
action here**, which inserts after that drive in the open mission.

The action picker is unchanged, plus one new group entry, **Ask for an
answer**, which is the `ros.request` step.

### `ros.request`

Publishes a request and waits for its answer, over two `std_msgs/msg/String`
topics carrying JSON. This is exactly the exchange iViz's Dashboard answers
(iViz 0.4.0 and later), so iViz is an answerer out of the box and any node can
be one.

```jsonc
{ "type": "ros.request",
  "text": "Is the part in place?",
  "options": ["OK", "Reject"],        // empty or absent: free-form answer
  "default": "OK",                    // used on timeout when on_timeout is "default"
  "timeout_s": 120,
  "on_timeout": "default",            // "default" | "fail"
  "request_topic": "/iviz/request",   // optional; else the station's topic, else the runner default
  "answer_topic": "/iviz/answer",     // optional; else the station's topic, else the runner default
  "station": "Conveyor1",             // optional; defaults to the site of the last Follow route
  "data": { "order": "$order_id" },   // optional extra JSON for the answering node
  "out": "check" }
```

Request message (published on `request_topic`):

```json
{ "id": "a1b2c3d4", "text": "Is the part in place?", "options": ["OK", "Reject"],
  "default": "OK", "timeout_s": 120, "station": "Conveyor1",
  "source": "mission_runner", "mission": "pickup_job", "run_id": "...", "step_id": "check",
  "data": { "order": "42" } }
```

Answer message (expected on `answer_topic`):

```json
{ "id": "a1b2c3d4", "answer": "OK", "by": "iviz" }
```

- The runner accepts the first answer whose `id` matches; answers for other ids
  are ignored, so many robots and requests can share the two topics.
- An answer outside `options` is accepted but logged as a warning.
- The step's value is `{"answer": "OK", "by": "iviz", "id": "a1b2c3d4",
  "timed_out": false}`. With `out: "check"`, a later `if` reads
  `check.value.answer == 'Reject'`.
- On timeout: `on_timeout: "default"` with a `default` gives
  `{"answer": <default>, "by": "timeout", "timed_out": true}`; otherwise the
  step fails with a timeout, and `on_fail` applies as for any step.
- Runner defaults for the topics live in `runner.yaml`:
  `request_topic: /iviz/request`, `answer_topic: /iviz/answer`.

#### Topics per station

When each station has its own screen or node, give the point its own topics so
it only receives its own questions:

```json
"Conveyor1": { "x": 3.0, "y": 0.0, "kind": "station",
               "request_topic": "/station/conveyor1/request",
               "answer_topic": "/station/conveyor1/answer" }
```

Each topic is chosen on its own, first match wins: the step's
`request_topic` / `answer_topic` → the topics on the site named by the step's
`station` (or, without one, the last Follow route's destination) → the runner
default. In Mission Builder the point's panel has **Questions at this point**
with the two fields; the request form leaves its topic fields empty and shows
which topic applies and where it comes from. On the station, set iViz's
Dashboard **Requests** / **Answers** topics to that pair, or subscribe your node
to them.

Writing a new answering node is the mirror of iViz's asking example: subscribe
`request_topic`, publish `{"id", "answer"}` on `answer_topic`.

---

## 3. Project files

A project is one JSON file holding everything that was set up: the maps with
their points and lanes, the missions, and the project settings. It opens and
saves with no robot connected.

```jsonc
{
  "schema": "project/1",
  "name": "Line 3 delivery",
  "created_at": "2026-09-13T09:00:00+07:00",
  "updated_at": "2026-09-13T11:20:00+07:00",
  "settings": {
    "robot_url": "ws://192.168.1.40:8765",
    "request_topic": "/iviz/request",
    "answer_topic": "/iviz/answer"
  },
  "sites": { "schema": "sites/1", "default_map": "line3", "maps": { } },
  "missions": [ { "schema": "mission/1", "name": "pickup_job", "flow": [ ] } ]
}
```

The machine-readable contract is
`runner/mission_runner/schema/project.schema.json`. File extension:
**`.mproj`** (it is JSON).

In the application:

- **File** menu: New project, Open…, Open recent, Save, Save as…, Close.
  Ctrl+N / Ctrl+O / Ctrl+S / Ctrl+Shift+S.
- The title bar shows the project name and a dot when there are unsaved
  changes; closing the window or opening another project asks first.
- **New project** starts with one empty map and no missions, and the guided
  empty state.
- **Import from robot** fills the open project with the robot's sites and
  missions (replace or merge, asked). **Deploy project to robot** sends the
  sites and every mission, validating first; **Replace missions on the robot**
  also removes robot missions that are not in the project (asked, with the
  list).
- Project settings (request/answer topics, robot URL, project name) are edited
  from **File → Project settings**. New `ros.request` steps take the project's
  topics.
- The last project reopens on start. Autosave to a sibling `.mproj.autosave`
  every 30 s while there are unsaved changes; offered back after a crash.

Runner side:

| Interface | What |
|---|---|
| `GET /api/project` | the robot's sites and missions as a `project/1` document |
| `PUT /api/project?replace=false` | import a `project/1` document: validates everything first and writes nothing if any mission is invalid; saves sites, saves missions, re-arms triggers; with `replace=true` also deletes robot missions not in the project. Returns `{ok, saved, deleted, warnings}` or `{ok:false, errors}` |
| `mission_runner project export <file>` | write the robot home's sites and missions to a project file |
| `mission_runner project import <file> [--replace]` | validate and write a project into the home directory, with no network and no GUI |

Both HTTP endpoints are reachable through `/mission/api` like every other.

---

## 4. Offline

Nothing about execution needs the application:

- `mission_runner` starts at boot, arms every trigger, and runs missions on
  schedule, on topics, on MQTT, on PLC signals, and on requests, with no GUI
  connected. `ros.request` waits for whatever node answers.
- A project reaches a robot three ways: **Deploy** over the bridge; copy the
  `.mproj` file by USB or `scp` and run `mission_runner project import` on the
  robot, then `sudo systemctl restart mission_runner`; or `PUT /api/project`
  from any script.
- The application itself works with no robot: open, edit, validate, save,
  export Python and BT XML.
