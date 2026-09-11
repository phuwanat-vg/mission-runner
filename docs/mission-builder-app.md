# Mission Builder

A Windows desktop application for building and running Nav2 missions without
writing code. It is a separate program from iViz: iViz is the viewer, Mission
Builder is the authoring tool. Both talk to the robot the same way, over one
`foxglove_bridge` WebSocket.

Nothing on the robot changes. `mission_runner` already executes missions
headless as a systemd service, armed and running whether or not this
application is open; see [`runner-api.md`](runner-api.md) and
[`mission-format.md`](mission-format.md). Mission Builder is a window onto it.

```
Windows                                   Raspberry Pi 5
┌──────────────────────────┐   one WS     ┌──────────────────────────────────┐
│ Mission Builder          │ ───────────▶ │ foxglove_bridge                  │
│  map · route · tree      │              │   ├── /mission/api  (service)    │
└──────────────────────────┘              │   ├── /mission/state /event      │
┌──────────────────────────┐              │   ├── /map, /tf, sensors         │
│ iViz (viewer)            │ ───────────▶ │   └── Nav2                       │
└──────────────────────────┘              │ mission_runner (headless service)│
                                          └──────────────────────────────────┘
```

## Stack

Tauri 2 + TypeScript 5 (strict) + Vite 7 + three.js, vanilla DOM, no UI
framework, no runtime dependencies. Same conventions as iViz: `h()` DOM helper,
`#private` fields, inline SVG icons, one CSS file with variables, dark palette,
13px base. Built and released the same way (NSIS installer, `tauri-plugin-updater`,
its own signing key and GitHub repository).

## What it must do

1. **Show the map.** The robot's occupancy grid, with the robot's live pose and
   TF, pan and zoom, 2D top-down by default.
2. **Draw the route graph.** Create points by clicking, drag to move, drag the
   arrow to set heading, name them. Connect points into lanes and configure each
   lane: which way traffic may flow (one way, or both ways), whether the return
   direction is allowed, a speed cap, blocked, and a cost. Missions that use
   `nav.follow_route` may only drive on these lanes.
3. **Cover the Nav2 API.** Every step type in the mission schema is available:
   point to point (`nav.go_to_pose`), through poses, follow waypoints, follow
   path, compute and smooth path, follow route, spin, back up, drive on heading,
   dock and undock, change map, clear costmap, set initial pose, lifecycle,
   cancel.
4. **Set up missions in a tree.** See below.
5. **Task sequences.** A mission is an ordered sequence of tasks; branches
   (`if`), loops and retries nest inside it as child levels of the tree.
6. **Triggers and connectors.** Anything the runner supports: ROS topic, ROS
   service and action, MQTT, Modbus, GPIO, HTTP webhook, cron and interval
   timers, boot, and another mission finishing.
7. **Change and load maps.** List the maps the robot knows, switch the active
   one, register a new map file, and edit each map's own points and lanes.
8. **Generate and run offline.** Deploying a mission writes it to the robot,
   where it runs on triggers with no GUI attached. The application also exports
   a mission as a standalone `nav2_simple_commander` Python script and as Nav2
   behavior-tree XML.

## Layout

```
┌ top bar ────────────────────────────────────────────────────────────────────┐
│ Mission Builder   ws://robot:8765  [Connect]  ● connected   idle            │
│                          … spacer …   ▶ Run   ‖ Pause   ■ STOP   ⬆ Deploy   │
├ tree 300px ─────────────┬ map (fills) ──────────────────┬ properties 320px ─┤
│ ▾ Missions              │                               │ (selected node)   │
│   ▸ patrol              │      ①──────②                 │                   │
│   ▾ pickup_job          │      │      │   ③ Conveyor1   │  name, x, y, yaw  │
│     ▾ Starts when       │    Home    B ──────┘          │  kind             │
│         PLC coil 100    │      │                        │  lanes here       │
│         POST /hooks/…   │      ④ Charger                │                   │
│     ▾ While running     │                               │                   │
│         battery < 20%   │  [Select] [Point] [Lane] [Fit]│                   │
│     ▾ Tasks             │                               │                   │
│       ▾ 1 Go to Conveyor1                               │                   │
│           Tell the PLC  │                               │                   │
│           Wait for load │                               │                   │
│       ▾ 2 If answer = No│                               │                   │
│         ▾ then          │                               │                   │
│             Stop        │                               │                   │
│       ▸ 3 Go to Rack3   │                               │                   │
│     ▸ When it fails     │                               │                   │
├ bottom bar (28px, expands while a run is live) ─────────────────────────────┤
│ ˄ Activity   ✓ patrol · 13 s                                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

## The tree

The left column is the whole mission as a collapsible tree, in the shape a
Windows file explorer uses: a disclosure triangle, an icon, a label, and one
muted detail line. It is the primary way to build a mission.

Levels:

```
Missions
└── pickup_job                     the mission
    ├── Starts when                its triggers
    │   ├── PLC coil 100 rising
    │   └── POST /hooks/pickup
    ├── While running              its interrupts
    │   └── battery < 20% → go_charge
    ├── Settings                   name, title, policy, priority, inputs, variables
    ├── Tasks                      the flow, in order
    │   ├── 1  Go to Conveyor1
    │   │   ├── Tell the PLC it arrived
    │   │   └── Wait for the load
    │   ├── 2  If the answer is No
    │   │   ├── then
    │   │   │   └── Stop the mission
    │   │   └── else
    │   └── 3  Go to Rack3
    │       └── When it fails      the on_fail steps of that task
    │           └── Clear the costmaps
    └── When it fails              the mission's on_abort steps
```

Behaviour:

- **Expand and collapse** each node; the state is remembered per mission.
- **Select** a node to edit it in the properties column. Selecting a task that
  has a place on the map highlights it there, and the reverse.
- **Add** with a `+` on any container node, or right-click → Add. The picker is
  grouped by what the user wants to do, not by step type: *Drive somewhere*,
  *Send a signal*, *Wait for something*, *Ask a person*, *Robot behaviour*,
  *Call another system*, *Logic*. Every step type in the schema appears in
  exactly one group.
- **Reorder** by dragging within a level, and move between levels by dragging
  onto another container. Alt+↑/↓ moves the selection.
- **Duplicate**, **Delete**, **Turn off** (`enabled: false`) from the context
  menu, with keyboard equivalents.
- Nodes carry a status glyph while a run is live: waiting, running, done,
  failed, and the time each task took.
- Nodes with a problem carry a warning or error badge; hovering explains it in
  a sentence.

The tree is a view over the ordinary `mission/1` JSON. Anything the tree cannot
express is shown read-only rather than dropped, and step ids and unknown fields
round-trip untouched.

## The map

Same drawing rules as [`iviz-route-mode.md`](iviz-route-mode.md): points as
discs with names and heading arrows, lanes as lines with one arrowhead when one
way and two when both ways, a speed cap written along the lane, blocked lanes
dashed red, the mission's planned route highlighted on top, the live robot, and
labels that keep a constant size and hide when they would collide.

Tools: **Select** (`V`), **Point** (`N`), **Lane** (`L`), **Fit** (`F`).
`Esc` returns to Select. Undo and redo cover every map and tree edit, one drag
being one step.

Lane configuration, in the properties column when a lane is selected:

| Field | Meaning |
|---|---|
| Direction | Both ways · One way A→B · One way B→A |
| Blocked | Temporarily closed; the planner routes around it |
| Speed limit | Cap in m/s while driving this lane |
| Cost | Multiplies the length when choosing a route (1 = neutral) |

## Maps

A **Maps** section lists what the robot knows from `sites.json`: name, file
path on the robot, how many points and lanes it has, and which one is active.
From there: switch the active map, register a new one by giving its name and
the path of its `.yaml` on the robot, rename, and delete. Each map owns its own
points and lanes, so a mission written as "go to Inbound" moves between
buildings without being rewritten.

## Running and deploying

- **Deploy** validates and writes the mission to the robot (`PUT /api/missions/{name}`),
  and saves map data (`PUT /api/sites`) in the same action. The button says
  what it is about to save.
- **Run**, **Pause**, **Stop** drive the runner; status comes from
  `/mission/state` and per-step progress from `/mission/event`.
- The **Activity** bar at the bottom shows the last run's result and expands
  into the live event list and the run history while something is running.
- **Export** a mission as a Python script or as behavior-tree XML from the
  mission's context menu.

## Reuse

Most of this is already written and tested; port rather than rewrite. From
[iViz](https://github.com/phuwanat-vg/iviz):

| Source | Use |
|---|---|
| `src/net/FoxgloveConnection.ts` | the bridge connection, subscriptions, publishing, service calls |
| `src/ros/*` | TF tree, message decoding, schemas |
| `src/viz/Viewer.ts`, `src/viz/layers/*` | the three.js scene, occupancy grid, TF and pose layers, tool mechanism |
| `src/mission/*` | `MissionApi`, `RouteStore`, `blocks`, `validate`, `stops`, `geometry`, `ids`, `types` |
| `src/viz/layers/RouteLayer.ts`, `src/ui/RouteTools.ts` | map drawing and the point/lane tools |
| `src/ui/ActionForm.ts` | the generated per-step forms |
| `src/updater.ts`, `.github/workflows/release.yml`, `tools/set-version.mjs` | releases and in-app updates |

New work is the tree column, the Maps section, the lane configuration editor,
the application shell, and the export actions.
