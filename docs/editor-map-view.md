# Map view (editor design pass 3)

The first two versions of the editor were lists and forms: a mission that says
"go to Rack3, then Conveyor1" told the user nothing about *where* that is. This
pass makes the map the main surface, so a mission can be read and built by
looking at the floor plan instead of reading text.

Everything here replaces the corresponding parts of
[`editor-spec.md`](editor-spec.md); the mission format, the runner API and the
validation rules do not change.

## Layout

```
┌ top bar (unchanged) ────────────────────────────────────────────────────────┐
├ left 200 ──┬ MAP (fills the rest) ─────────────────────┬ right 360 ─────────┤
│ Missions   │ ┌ tools ──────────────────┐  map ▾  ⤢ ⟳  │ Starts when  …  +   │
│ ● Patrol   │ │ ⬚ Select  ⚑ Goal  ⤳ Path │              ├─────────────────────┤
│   Pickup   │ │ ⚐ Waypoints  ⌖ Site      │              │ Steps  |  JSON      │
│   Charge   │ └──────────────────────────┘              │  1 ✓ Wait for Nav2  │
│            │                                            │  2 ▶ Loop           │
│            │        ① ──── ② ──── ③                     │    │ 1 Follow …     │
│            │         Conveyor1   Rack3                  │  3   Go to Home     │
│            │            ▲ robot                         │  + Add step         │
│            │                                            ├─────────────────────┤
│ + New      │                                            │ ▾ Go to pose        │
│            │                                            │   (inspector)       │
├────────────┴────────────────────────────────────────────┴─────────────────────┤
│ ˄ Run log  ✓ patrol · 13 s                                                    │
└───────────────────────────────────────────────────────────────────────────────┘
```

- The map is the largest area and never collapses. The right column holds the
  steps list (top, scrolls) and the inspector for the selection (bottom,
  resizable by dragging the divider, remembered in `localStorage`).
- The sites list moves onto the map: sites *are* the markers. The old
  `Missions | Sites` segmented control is gone; the sidebar lists missions only.
  Site editing happens on the map and in the inspector.
- `Ctrl+B` hides the sidebar, `Ctrl+I` hides the inspector, for a nearly
  full-screen map.

## The map

Rendered as a single SVG with one `<g>` holding a world transform, so panning
and zooming are one matrix update and every element stays in world
coordinates (metres, x right, y **up**; the SVG y axis is flipped once at the
transform).

Layers, bottom to top:

1. **Floor** — the PNG from `GET /api/maps/{name}/image` placed with the
   `bounds` from `GET /api/maps/{name}`, drawn with
   `image-rendering: pixelated`, dimmed to about 55% so markers stay readable.
   Unknown/occupied greys come from the map itself; do not recolour it.
2. **Grid** — 1 m lines, plus a 5 m emphasis line, in a very low-contrast
   colour; hidden below a zoom where it would alias. Origin cross at (0, 0).
3. **Route** — the mission's navigation steps in order, as a polyline through
   their poses with an arrowhead at each leg. Legs the runner has completed in
   the current run are solid green; the leg in progress is the accent colour
   and animated (`stroke-dasharray` march); later legs are muted and dashed.
   `nav.follow_path` draws its own densified polyline, `nav.follow_waypoints`
   draws a dashed line through its waypoints.
4. **Stops** — one marker per navigation step: a circle with the step's index,
   a heading triangle when a yaw is set, and the step name below at zoom levels
   where it fits. Colour follows the step's group (navigate = accent blue,
   behaviour = teal, docking = purple). Selected = filled with a halo; hovered
   = enlarged.
5. **Sites** — small diamonds with their name; a site used by the open mission
   is drawn brighter and connected to its stop marker. Charger sites get a
   battery glyph, docks a dock glyph.
6. **Robot** — a triangle at the live pose from the `robot` event, with a
   fading trail of the last 200 poses while a run is active, a translucent
   circle for the footprint radius (0.3 m default), and a thin line to the
   current goal.

### Interaction

- **Pan/zoom**: drag with the middle button or space, wheel to zoom at the
  cursor, `F` fits the mission and sites, `R` resets. A "follow robot" toggle in
  the map toolbar keeps the robot centred while a run is active.
- **Select tool** (default): click a stop marker or a site to select it (the
  steps list scrolls to it and the inspector opens). Drag a stop marker to move
  that step's pose; drag its heading handle to set the yaw; both are undoable
  and update the JSON immediately. Drag a site to move the site (asks to save
  to the robot on drop, or marks sites dirty with a "Save sites" button).
- **Goal tool**: click on the floor to append a `nav.go_to_pose` at that point;
  drag before releasing to set the heading. Holding it over an existing site
  snaps to that site and creates a site-referenced step instead of coordinates
  (a small "snapped to Conveyor1" hint appears).
- **Path tool**: click repeatedly to build a `nav.follow_path` polyline, double
  click or `Enter` to finish, `Esc` to cancel, `Backspace` removes the last
  point.
- **Waypoints tool**: same, but produces `nav.follow_waypoints`.
- **Site tool**: click to create a site; a small inline form asks for the name
  (default `Site N`), then the site is added to the current map.
- Steps are inserted after the current selection, exactly like the block
  picker, so map building and list building agree.
- **Two-way highlight**: hovering a step row highlights its marker and its leg;
  hovering a marker highlights the row. Selection is shared state in the store.
- Steps that have no place on the map (logic, waits, connectors) are listed
  normally and highlight nothing; their rows carry a small map-pin-off glyph so
  it is obvious why nothing lights up.

### Empty and error states

- No map on the robot yet → the floor layer is the synthetic room the runner
  returns; a one-line note says "No saved map; showing a plain room" with a
  link to the docs.
- Runner offline → the last map image and sites are used from
  `localStorage`; the map shows a muted "offline" watermark and dragging still
  edits the mission (it just cannot save sites).
- A step whose site is missing from the current map draws a dashed red marker
  at the last known position (or at the origin) with the site name struck
  through, matching the validation error in the list.

## Steps list changes

The list stays, narrower, in the right column. Each row is: index, run glyph,
name, one muted summary line. A row that has a map presence also shows its stop
number so the eye can jump between list and map. Nested containers keep the
single left rule. Drag and drop, keyboard moves and the block picker are
unchanged.

## Run view

While a run is active the map is the status display: the robot moves, the
current leg animates, completed stops turn green, a failed stop turns red with
the error in a tooltip, and the feedback line (distance remaining, recoveries)
is drawn in the map's bottom-left corner instead of only in the step row. The
`ask_user` prompt banner floats over the map's top edge.

## Performance

The SVG is rebuilt only when the mission, sites or map change; live updates
(robot pose, run marks) mutate existing nodes. Robot updates arrive at most
2 Hz from the runner, so no throttling beyond `requestAnimationFrame` batching
is needed. A mission with 200 steps must still pan smoothly.
