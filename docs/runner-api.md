# mission_runner API

The runner is the always-on brain on the robot. Everything else (the editor,
the robot's web page, iViz, PLCs, other ROS nodes) is a client of one of the
interfaces below. All of them work on the LAN without internet.

## HTTP (default `http://<robot>:8080`)

JSON everywhere. Errors return `{"error": "...", "errors": [{"path", "message"}]}`
with 4xx/5xx.

### Status

`GET /api/status`

```jsonc
{
  "runner": { "version": "0.1.0", "uptime_s": 1234, "backend": "nav2" | "sim", "home": "/home/pi/.mission" },
  "state": "idle" | "running" | "paused",
  "run": Run | null,                 // active run
  "queue": [Run],                    // waiting, in dispatch order
  "suspended": [Run],                // interrupted runs waiting to resume
  "robot": { "x": 1.2, "y": 0.4, "yaw_deg": 90, "frame": "map", "battery": 0.78, "nav_active": true } | null,
  "current_map": "demo_room",
  "connectors": { "broker": { "type": "mqtt", "connected": true }, "plc1": { "type": "modbus_tcp", "connected": false } },
  "prompt": Prompt | null
}
```

`Run`:

```jsonc
{
  "id": "20260910-091203-3f2a",
  "mission": "pickup_job",
  "inputs": { "pickup": "Conveyor1", "drop": "Rack3" },
  "source": { "kind": "trigger" | "manual" | "interrupt" | "resume" | "sub", "id": "plc_start", "detail": "modbus.poll coil 100" },
  "priority": 50,
  "status": "queued" | "running" | "paused" | "suspended" | "succeeded" | "failed" | "canceled",
  "started_at": "2026-09-10T09:12:03+07:00", "finished_at": null,
  "step": { "id": "to_drop", "name": "...", "path": ["again", "to_drop"], "started_at": "..." } | null,   // null between steps, too

  "error": "" ,
  "feedback": { "distance_remaining": 4.2, "recoveries": 0, "eta_s": 12 } | null
}
```

`Prompt`: `{ "id", "run_id", "mission", "text", "options": [], "default", "expires_at" | null }`

### Missions

| Method | Path | Body / result |
|---|---|---|
| `GET` | `/api/missions` | `[{name, title, description, version, updated_at, triggers: ["cron 0 22 * * *", ...], interrupts, steps, sha256, state: "idle" \| "running" \| "queued" \| "suspended", trigger_problems: [], errors?}]` |
| `GET` | `/api/missions/{name}` | the mission JSON |
| `PUT` | `/api/missions/{name}` | mission JSON → `{ok: true, name, version, sha256, warnings: []}` or 400 `{ok: false, errors}`; re-arms triggers and rewrites behavior-tree files |
| `DELETE` | `/api/missions/{name}` | |
| `POST` | `/api/missions/validate` | mission JSON → `{ok, errors, warnings}` (no save) |
| `POST` | `/api/missions/{name}/run` | `{inputs?, policy?, priority?}` → `{accepted, run_id, reason}` |

### Runs

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/runs/{id}/cancel` | |
| `POST` | `/api/runs/{id}/pause` | navigation is canceled; the step re-runs on resume |
| `POST` | `/api/runs/{id}/resume` | |
| `POST` | `/api/stop` | cancel the active run, the queue and suspended runs (the STOP button) |
| `GET` | `/api/runs?limit=50&mission=` | history, newest first |
| `GET` | `/api/runs/{id}` | `Run` plus `events: [{t, type, step_id, path, data}]` |

### Prompts (`ask_user`)

| Method | Path | Body |
|---|---|---|
| `GET` | `/api/prompt` | current prompt or `null` |
| `POST` | `/api/prompt/{id}/answer` | `{"answer": "Yes"}` |

### Sites, connectors, capabilities

| Method | Path | Result |
|---|---|---|
| `GET` / `PUT` | `/api/sites` | `sites.json` |
| `GET` | `/api/maps/{name}` | `{name, frame, file, width, height, resolution, origin: {x, y, yaw_deg}, bounds: [min_x, min_y, max_x, max_y], source, image_url}` |
| `GET` | `/api/maps/{name}/image` | the map as a greyscale PNG, row 0 at `max_y` (image convention) |

The map comes from the `file` of that map in `sites.json` (a ROS `map.yaml`
plus its `.pgm`/`.png`). When the file is missing or unreadable the runner
draws a plain room around that map's sites instead and reports
`"source": "synthetic"`, so the editor always has a floor to place things on.

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/maps/{name}/filters` | writes Nav2 costmap filter masks for this map's zones into `<home>/filters/`; body `{resolution?, max_speed_mps?}` → `{ok, masks: [{kind, yaml, image, zones}], directory}` |
| `GET` | `/api/project?name=` | the robot's sites and missions as a `project/1` document (see [`mission-builder-v2.md`](mission-builder-v2.md)) |
| `PUT` | `/api/project?replace=false` | import a `project/1` document. Everything is validated first and nothing is written if any mission is invalid (`400 {ok:false, errors}`, paths prefixed `missions/<name>`). Saves sites and missions and re-arms triggers; `replace=true` also deletes robot missions not in the project, except a running one. `200 {ok, saved, deleted, kept, warnings}` |
| `GET` | `/api/robot/pose` | `{x, y, yaw_deg, frame}` (for "capture site from robot") |
| `GET` | `/api/connectors` | `{name: {type, connected, available, reason, config, configured}}` with secrets redacted. Names referenced by a deployed mission but absent from `connectors.yaml` are listed with `configured: false`, so a mission can be written before the broker or the PLC exists. |
| `GET` | `/api/capabilities` | `{backend, steps: {type: {available, reason}}, triggers: {...}, connectors: [...], ros_distro}` |
| `GET` | `/api/schema` | mission JSON schema |
| `GET` | `/api/examples` | bundled example missions (full documents) |
| `POST` | `/hooks/{path}` | webhook trigger; body is the payload; 404 when no mission listens |
| `POST` | `/api/sim/topic` | sim only: `{topic, payload}` injects a ROS message into `ros.topic` triggers |
| `POST` | `/api/sim/robot` | sim only: `{x, y, yaw_deg, battery, fail_next, blocked}` moves or breaks the fake robot |

### Preview

Both endpoints take either `{"name": "<saved mission>"}` or
`{"mission": {…}}` for a document that has not been deployed yet, so the editor
can preview unsaved edits.

| Method | Path | Body / result |
|---|---|---|
| `POST` | `/api/preview/route` | `{name \| mission, start?, planner_id?}` → `{ok, legs: [{step_id, index, start, goal, path, length_m, planned, error}], distance_m, estimate_s, speed_mps, planned, note}`. Each leg is planned by the real planner; when the robot is busy or the planner does not answer, the leg falls back to a straight line with `planned: false` and an `error`. |
| `POST` | `/api/preview/dryrun` | `{name \| mission, inputs?, time_scale?, max_wall_s?}` → `{ok, status, error, duration_s, distance_m, samples: [{t, x, y, yaw_deg, step}], steps: [{id, name, type, t0, t1, status, error}], notes, outputs, truncated}` |

A dry run replays the whole flow — branches, loops, retries, `run_mission` —
against a private simulated robot seeded at the current pose. The real robot
never moves, connector writes are recorded in `outputs` instead of sent,
`ask_user` answers with its default, `wait_event` is assumed to fire at once,
and `wait` steps are compressed by `time_scale` (default 60x, capped by
`max_wall_s`, default 20 s of real time). Everything assumed is listed in
`notes`.

### Events (WebSocket `GET /api/events`)

On connect the server sends `{"type": "status", ...}` (same as `GET /api/status`),
then one JSON object per event:

| `type` | Fields |
|---|---|
| `status` | full status (sent on every state change; cheap, ~1 KB) |
| `run.queued` / `run.started` / `run.finished` / `run.suspended` / `run.resumed` | `run` |
| `step.started` | `run_id, step_id, path, name, step_type, resumed` |
| `step.finished` | `run_id, step_id, path, result` |
| `feedback` | `run_id, step_id, feedback` (≤ 2 Hz) |
| `log` | `t, level, text, run_id?, step_id?` |
| `prompt` / `prompt.answered` | `prompt`, `answer` |
| `request` / `request.answered` | a `ros.request` step published `request` on `request_topic` / the answer `{id, answer, by}` arrived |
| `robot` | `x, y, yaw_deg, frame, battery` (≤ 2 Hz) |
| `missions.changed` | `names: []` |
| `sites.changed` | |

Clients may send `{"type": "ping"}`; the server answers `{"type": "pong"}`.

### Live map layers

Send `{"type": "live", "layers": ["costmap", "scan", "plan", "footprint"]}` to
subscribe; the server answers `{"type": "live.layers", layers, available}` and
then streams the layers you asked for. The runner subscribes to the matching
ROS topics only while at least one client wants them, and drops them again when
the last one disconnects, so an idle robot pays nothing.

| Event | Fields | Rate |
|---|---|---|
| `live.costmap` | `png` (base64 greyscale, cost 0-100 scaled to 0-255, unknown = 0), `width`, `height`, `resolution`, `origin`, `bounds` | ≤ 1 Hz |
| `live.scan` | `points: [[x, y], …]` in the map frame (≤ 360), `origin` | ≤ 2 Hz |
| `live.plan` | `points: [[x, y], …]` in the map frame (≤ 300) | ≤ 2 Hz |
| `live.footprint` | `points: [[x, y], …]` in the map frame | ≤ 2 Hz |

Topics default to `/local_costmap/costmap`, `/scan`, `/plan` and
`/local_costmap/published_footprint`; override them under `live_topics` in
`runner.yaml`. With `--sim` only `plan` and `footprint` are available;
`GET /api/status` reports what this robot can serve under `live.available`.

### Web page

`GET /` serves the editor bundle if it was built into
`mission_runner/webui/`, otherwise a minimal status page with STOP, run buttons
and the prompt dialog. Both work from a phone on the LAN.

## ROS 2

Node name `mission_runner`.

| Kind | Name | Type |
|---|---|---|
| topic (pub, transient local) | `/mission/state` | `std_msgs/msg/String` JSON = status |
| topic (pub) | `/mission/event` | `std_msgs/msg/String` JSON = event |
| service | `/mission/api` | `mission_msgs/srv/Api` `{method, path, body_json}` → `{ok, status, body_json, message}` — **the whole HTTP API over one ROS service** |
| service | `/missions/<name>/run` | `std_srvs/srv/Trigger` |
| service | `/missions/run` | `mission_msgs/srv/RunMission` `{name, inputs_json}` → `{accepted, run_id, message}` |
| service | `/mission/cancel`, `/mission/pause`, `/mission/resume`, `/mission/stop` | `std_srvs/srv/Trigger` |
| service | `/mission/answer` | `mission_msgs/srv/Answer` `{prompt_id, answer}` |
| action | `/missions/run` | `mission_msgs/action/RunMission` goal `{name, inputs_json}`, feedback `{step_id, step_name, status, json}`, result `{success, result_json, message}`; cancel cancels the run |

When `mission_msgs` is not built, the `std_srvs` services and the String
topics still work and the runner logs a warning once.

### One connection for a desktop GUI

`/mission/api` exists so a desktop application that already talks to
`foxglove_bridge` needs no second connection to the robot: `path` and
`body_json` are exactly the HTTP API above, the reply is the same JSON, and
binary replies (the map image) come back as
`{"content_type": "image/png", "base64": "…"}`. Live state arrives on
`/mission/state` and `/mission/event` as it always does.

That is how iViz's Route mode talks to the runner. The runner does not need a
GUI: it runs headless as a systemd service, keeps its triggers armed and its
missions running whether or not anybody is connected. The HTTP server stays up
for the phone page and for anything that prefers REST.

## Files on the robot (`--home`, default `~/.mission`)

```
missions/*.json      one file per mission, name == file stem
sites.json
connectors.yaml      connector configuration; secrets via env vars
bt/*.xml             behavior trees generated from templates
runs.sqlite          run history and events
```

## Sim mode

`mission_runner --sim` runs without ROS: a fake robot moves at a configurable
speed, `nav.*` steps succeed after a realistic delay, `ros.*` steps are logged,
and every other interface (HTTP, WS, MQTT, timers, webhooks) is real. The
editor's "Run" against a sim runner is the dry-run.
