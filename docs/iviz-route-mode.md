# Route mode in iViz

> **Status (first release).** Everything below is built except the parts marked
> *deferred*: zone drawing (`Z`), **Preview route**, **Dry run** and the live
> Nav2 layers. The layers menu therefore only switches points, lanes and the
> mission route. Two small deviations from this page are noted inline.

Missions are built in iViz now, not in a browser. iViz is already a desktop app
that draws the map, the robot and TF over a single `foxglove_bridge`
connection; Route mode adds the two things it was missing: **drawing the lanes
the robot may drive**, and **saying what happens at each stop**.

Nothing about the robot side changes. `mission_runner` keeps running headless
on the Pi as a systemd service, armed and executing whether or not iViz is
open. iViz is a window onto it.

```
Windows                                   Raspberry Pi 5
┌──────────────────────────┐   one WS     ┌──────────────────────────────────┐
│ iViz (Tauri + three.js)  │ ───────────▶ │ foxglove_bridge                  │
│  · map, TF, clouds       │              │   ├── /mission/api  (service)    │
│  · Route mode  ← new     │              │   ├── /mission/state /event      │
└──────────────────────────┘              │   └── Nav2, sensors              │
                                          │ mission_runner (headless service)│
                                          └──────────────────────────────────┘
```

## Transport

Everything goes through the connection iViz already has.

- **Requests**: the ROS service `/mission/api` (`mission_msgs/srv/Api`),
  `{method, path, body_json}` → `{ok, status, body_json, message}`. `path` and
  the bodies are exactly [`runner-api.md`](runner-api.md). A binary reply (the
  map image) arrives as `{"content_type": "image/png", "base64": "…"}`.
- **Live state**: subscribe `/mission/state` (latched JSON status, same shape as
  `GET /api/status`) and `/mission/event` (one JSON event per message, the same
  events the WebSocket streams).
- iViz must gain service-call support in `src/net/FoxgloveConnection.ts`
  (`@foxglove/ws-protocol` has `sendServiceCallRequest`, `advertiseServices`,
  `serviceCallResponse`, `serviceCallFailure`). Route mode is disabled with a
  clear reason when the bridge does not advertise the `services` capability or
  `/mission/api` is absent, and iViz keeps working as a viewer.

## The model the user sees

Two things live on the map, and they are different on purpose.

**The route graph** belongs to the *map*, not to a mission. It is the set of
points and lanes the robot is allowed to use, shared by every mission. Points
are sites (`sites.json`); lanes are the `edges` of that map. Drawn once, reused
forever.

**A mission** is an ordered list of **stops**. A stop is a point on the graph
plus the actions to perform on arrival. Saved as ordinary `mission/1` JSON: each
stop compiles to a `nav.follow_route` step followed by its action steps, so
everything already built (triggers, interrupts, retries, dry run, the Python
export) keeps working, and the raw step list stays available on a second tab
for anyone who wants it.

```
stop 1  Conveyor1     ──▶ nav.follow_route to Conveyor1
        ├ tell PLC coil 100          modbus.write
        └ wait for coil 101          wait_event
stop 2  Rack3         ──▶ nav.follow_route to Rack3
        └ publish robot/state        mqtt.publish
```

Because every drive is `nav.follow_route`, the robot only ever travels on the
lanes that were drawn. A stop whose point has no lane to the previous one is an
error the editor shows before it is ever deployed.

## Layout

Route mode replaces iViz's right sidebar; the 3D view stays where it is and
becomes the editing surface. 2D top-down is the default while editing.

```
┌ iViz top bar ─────────────────────────────────────── [Route] ──────────────┐
├ route panel 320px ─────────────────┬ view (three.js, the editing surface) ─┤
│ Delivery along the route  [ ⌄ ]    │                                        │
│ ● Nothing is running.              │        ①──────②                        │
│ [ ▶ Run ] [❚❚ Pause] [■ Stop]      │        │      │        ③ Conveyor1     │
│ ── The stops, in order ─────────   │      Home    B ────────┘               │
│  ⠿ ① Drive to the pick-up      🗑  │        │                               │
│       Drives to Conveyor1          │        ④ Charger                       │
│       · MQTT publish  robot/state  │                                        │
│       · Wait for the load          │   ┌──────────────────────────────┐     │
│  ⠿ ② Drive to the drop         🗑  │   │ [▣ Select V][📍Point N][🔗Link L] │
│  + Add a stop     ( from a list ▾) │   │ Drag from one point to another…│   │
│ ── Stop 1: Drive to the pick-up ─  │   └──────────────────────────────┘     │
│  Drives to / Called / actions      │                                        │
│ ── Save to the robot ───────────   │                                        │
│  What is unsaved, in a sentence    │                                        │
│  [ ⇧ Save both ] [ ↺ ] [ ↻ ]       │                                        │
└────────────────────────────────────┴────────────────────────────────────────┘
```

The panel is ordered the way the job is done — **what is running**, **the
stops**, **the selected thing**, **saving** — and a section with nothing in it
is not drawn at all. In its place comes one card that says what to do next:

- **an empty map**: the three numbered steps ("Draw the stops the robot should
  visit", "Connect them so the robot only drives your lanes", "Put the stops in
  order and add what happens at each one"), each with its key and a button that
  arms the matching tool;
- **points but no mission open**: what a mission is, and the list to open one
  from (or, when the robot has none, how to make one);
- **a mission with no stops**: one button that arms the pick-on-map, and the
  list of points as the second way in.

The header carries the mission's title, one sentence about what the robot is
doing (with a coloured dot), and **Run** as the one prominent button, with
Pause and Stop beside it. Everything else — which mission, which map, what the
view draws, reload from the robot — is behind the small **⌄** menu next to the
title.

Each stop is a card: its number, its name, the point it drives to, and its
actions indented underneath with a readable one-line summary. Clicking a stop,
a point or a lane on the map opens its editor further down the panel and
scrolls it into view.

Saving is **one** button. The route graph and the mission are two documents on
the robot, but from here it is one act: the button says what it will do —
**Save mission**, **Save map** or **Save both** — a sentence above it says in
words what is unsaved, and Undo and Redo sit beside it as icon buttons. When
nothing is unsaved the section is not there at all. Nothing is ever reported as
a status code or a blob of JSON: a failure is a sentence saying what happened,
with the technical text folded away behind **Details**.

## Tools

The tool bar floats at the bottom of the view. Each tool is an icon, a word and
its key on a 34px-high button, the armed one is filled in blue, and one muted
line underneath says what that tool does right now — including, while the Link
tool is dragging, what it is about to connect ("Home ↔ Conveyor1 — let go to
draw it") and whether Shift has made it one-way. That line replaced the hint
box that used to sit over the middle of the map.

| Key | Tool | Behaviour |
|---|---|---|
| `V` | Select | Click a point, lane, zone or stop to select. Drag a point to move it; drag its arrow to set the heading. Drag a zone vertex or its middle to reshape or move it. `Del` deletes the selection (with a confirmation when something references it). |
| `N` | Point | Click on the floor to drop a point, drag before releasing to set the heading, then type its name inline. A point dropped on a lane splits that lane in two. |
| `L` | Link | Drag from one point to another to draw a lane. Shift-drag makes it one-way. Clicking an existing lane selects it. |
| `Z` | Zone | *Deferred.* Click to lay out a polygon, `Enter` or double click to close it, then choose the kind (keep-out, speed limit, work) and a name. |

`Esc` returns to Select. Everything is undoable with the usual `Ctrl+Z` /
`Ctrl+Y`, and one drag is one undo step.

A route tool takes only the left mouse button, so middle-drag pan and
right-drag orbit stay live while editing; that is what makes the tools work in
3D as well as in the 2D top-down view. Points and lanes lie on the ground
plane, so a click is resolved by intersecting the pointer ray with that plane
once and then hit-testing in world coordinates — exact in both views.

Points, lanes and zones are map data: they are saved to the robot with
`PUT /api/sites` and take effect for every mission. The panel does not make the
user think about that split — the one **Save to the robot** button sends
whatever is unsaved, the map data, the mission, or both, and says which.

## What the map shows

- **Points**: a filled circle with the name beside it, and a heading arrow when
  a yaw is set. Icons distinguish home, dock/charger, station and plain
  waypoints. A point that is part of the open mission also carries its stop
  number.
- **Lanes**: a line between two points, with one arrowhead when one-way and two
  when bidirectional. A lane with a speed cap is drawn thinner with its limit
  written along it; a blocked lane is dashed red.
- **Zones**: *deferred.* A translucent polygon, red-ish for keep-out, amber for
  speed limits, neutral for work areas, with the name in the middle.
- **The mission**: the planned route through the stops is highlighted on top of
  the lanes, with the stop numbers.
- **The robot**: iViz already draws it. While a run is active, the lane being
  driven animates, finished stops turn green, a failed stop turns red.
- A layers menu switches points, lanes and the mission route on and off; zones
  and the live Nav2 layers (costmap, scan, plan, footprint) are deferred. Nav2
  topics the bridge already publishes stay available through iViz's ordinary
  layer list, which Route mode does not remove — turning Route mode off brings
  it back.

Labels keep a constant size on screen and the ones that would collide at the
current zoom are hidden, most important first (a stop number beats a station
name beats a plain waypoint).

Points and lanes are click targets before they are decoration: the picking
tolerance is 18 screen pixels, a point never draws smaller than 11 pixels
whatever the zoom, hovering one lights it and turns the cursor into a pointer,
and the selected point or lane wears a wide, opaque white ring or ribbon that
is unmistakable at a glance.

## Actions on a stop

Selecting a stop shows its actions and an **Add action** button that opens a
grouped picker. The groups are named for what the user wants to do, not for the
step type underneath:

A stop's actions are the steps that follow its `nav.follow_route` in the flat
`flow`. **Deviation:** an `if` or a `loop` added from the Logic group ends the
stop when the mission is read back, so it appears under "Other steps" rather
than nested under the stop. That keeps the round trip exact — nothing is ever
dropped or reordered — and the picker says so on those two entries.

| Group | Actions | Compiles to |
|---|---|---|
| Send a signal | MQTT message, Modbus coil/register, GPIO output, ROS topic | `mqtt.publish`, `modbus.write`, `gpio.write`, `ros.publish` |
| Wait for something | MQTT message, Modbus coil, GPIO input, ROS topic, web call, a delay | `wait_event`, `wait` |
| Ask a person | A question with buttons and a default | `ask_user` |
| Robot | Dock, undock, spin, back up, clear costmaps, change map | `nav.*` |
| Call another system | HTTP request, ROS service, ROS action, set a ROS parameter | `http.request`, `ros.call_service`, `ros.call_action`, `ros.set_param` |
| Logic | Set a value, if, loop, log, run another mission, end | `set`, `if`, `loop`, `log`, `run_mission`, `end` |

Each action gets a small form generated from the same registry the web editor
used (`editor/src/model/blocks.ts` ports over). Connector names come from
`GET /api/connectors`, so an MQTT action offers the brokers the robot actually
has. Actions reorder by drag, and each one shows a one-line summary.

## Running

The panel header has Run (prominent), Pause and Stop, plus one sentence about
what the robot is doing, fed by `/mission/state`. Deploy is gone as a separate
idea: saving the mission to the robot is the Save button in the Save section. Two preview actions are *deferred*; they will use the
endpoints the runner already provides:

- **Preview route** (`POST /api/preview/route`) draws the path the planner
  would really take through the stops, with total distance and an estimate.
- **Dry run** (`POST /api/preview/dryrun`) animates a ghost robot through the
  whole flow on the map, with a scrubber, and lists what it assumed.

Live layers (costmap, scan, plan, footprint) are *deferred*; they will be
requested from the runner over `/mission/api` and drawn by iViz's existing
layer machinery where possible.

## What to reuse

The web editor already contains the pieces this needs, in TypeScript with no
runtime dependencies. Port rather than rewrite:

| From `Mission/editor/src` | Use for |
|---|---|
| `model/types.ts`, `model/blocks.ts` | the step and trigger registries, the action forms |
| `model/validate.ts` | the same errors and warnings, before deploy |
| `model/ids.ts` | id generation and step-tree walking |
| `map/geometry.ts` | pose extraction from steps, route building, world maths |
| `codegen/python.ts`, `codegen/bt.ts` | export a mission as a script or a behavior tree (*deferred*) |
| `api/RunnerClient.ts` | the shape of every request; swap `fetch` for a service call |

What actually landed in iViz, under `src/mission/`: `types.ts`, `blocks.ts`,
`validate.ts`, `ids.ts` and `expressions.ts` ported as they were, plus
`geometry.ts` trimmed to the pose maths (the SVG view transform and the
step-based `buildMapModel` are meaningless with three.js) and extended with the
lane-graph routing the web editor never had. `MissionApi.ts` replaces
`RunnerClient.ts`, and `stops.ts` compiles a flow to stops and back.

Two things the ports were missing and gained here, because the schema already
had them but the web editor never caught up: `nav.follow_route` in `STEP_TYPES`
and the block registry, and `edges` on a map in `SitesDoc`.

The web editor stays in the repository and still works over HTTP, but iViz is
where missions are built from now on. The runner's small phone page stays as
the operator's remote control.

## Without iViz

Nothing above is required for the robot to work. `mission_runner` starts at
boot, arms every trigger, runs missions on schedule, on a PLC signal, on MQTT,
on a button, and answers `ask_user` prompts with their defaults when nobody is
connected. iViz, the phone page and the HTTP API are three optional windows
onto the same running service.
