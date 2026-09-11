# Mission editor specification

The editor is a single-page web app that lets a Nav2 user build missions
without writing code. It is served by `mission_runner` at `GET /` (so it works
from any browser on the LAN, offline) and also runs standalone with `vite dev`.

Read first: [`mission-format.md`](mission-format.md), [`runner-api.md`](runner-api.md),
the JSON schema in `runner/mission_runner/schema/`, and the examples in
`examples/`. Those are the contract; the editor must load every example
unchanged and round-trip it byte-for-byte (modulo key order and generated ids).

## Stack and conventions

- Vite 7 + TypeScript 5 (`strict`, `noUnusedLocals`, `noUnusedParameters`), vanilla DOM. No UI framework, no CSS framework, no runtime dependencies. Dev dependencies: `vite`, `typescript`, `vitest`.
- Build output goes to `../runner/mission_runner/webui` (`build.outDir`, `emptyOutDir: true`, `base: "./"`) so the runner can serve it. `vite dev` proxies `/api`, `/hooks` and the WebSocket `/api/events` to `http://localhost:8080`.
- Code style follows [iViz](https://github.com/phuwanat-vg/iviz): a tiny `h(tag, props, ...children)` helper, private class fields, inline SVG icons (`icons.ts` with Lucide-style 24×24 paths), one CSS file with variables. Reuse this palette:

```css
:root {
  --bg: #15181d; --panel: #1d2128; --panel-2: #242933; --border: #2a303b;
  --text: #dfe4ec; --muted: #8b95a5; --accent: #3da5ff; --accent-2: #2b7bd1;
  --ok: #3ccf7a; --warn: #f2b134; --err: #ff5a6a;
  --font: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, Consolas, "Cascadia Mono", monospace;
}
```

- 13px base font, 8px row padding, `user-select: none` on chrome, selectable text in inputs and the JSON tab.
- Quiet by default: low-contrast borders, no uppercase or letter-spaced headers (sentence case, 12px, muted), `--panel-2` only on inputs, buttons and hover states (no nested panel backgrounds), one accent color (blue) plus red STOP and green/amber/red run status, at most one icon per list row (hover actions excepted).
- All user-facing strings in English, sentence case.

## Files

```
editor/
  package.json  index.html  vite.config.ts  tsconfig.json  README.md
  src/
    main.ts                 bootstrap, HMR dispose
    style.css
    model/types.ts          TS types mirroring mission.schema.json and sites.schema.json
    model/blocks.ts         step + trigger registries (see below)
    model/validate.ts       structural + semantic validation -> {errors, warnings}
    model/ids.ts            id generation, deep clone, path helpers (find step by path)
    model/templates.ts      built-in "new mission" templates (mirror examples/)
    api/RunnerClient.ts     REST + WebSocket client with reconnect and typed events
    state/Store.ts          app state, undo/redo (snapshot stack, 50 deep), dirty tracking, sha256 of deployed copy
    codegen/python.ts       mission -> nav2_simple_commander script
    codegen/bt.ts           behavior_tree template -> Nav2 BT XML
    ui/App.ts               layout, sidebar tabs, wiring
    ui/TopBar.ts  ui/MissionsPanel.ts  ui/SitesPanel.ts
    ui/StepsList.ts  ui/TriggerBar.ts  ui/Inspector.ts  ui/JsonTab.ts
    ui/RunLog.ts  ui/PromptBanner.ts  ui/dialogs.ts (modal, confirm, inputs form, block picker)  ui/icons.ts  ui/dom.ts (h helper)
  test/validate.test.ts  test/codegen.test.ts  test/roundtrip.test.ts   (vitest; use ../examples/*.json)
```

## Registries (`model/blocks.ts`)

Every step type has an entry:

```ts
interface ParamDef {
  key: string; label: string;
  kind: "number" | "integer" | "string" | "text" | "boolean" | "select" | "expression" | "value"
      | "pose" | "poses" | "points" | "site" | "map" | "connector" | "json" | "steps" | "behavior_tree" | "event_source" | "options";
  options?: string[]; default?: unknown; required?: boolean; help?: string; advanced?: boolean;
  min?: number; max?: number; step?: number; placeholder?: string;
}
interface BlockDef {
  type: string; label: string; group: "Navigate" | "Behaviors" | "Map & costmap" | "Logic" | "Wait & ask" | "ROS" | "Connectors";
  icon: IconName; help: string; params: ParamDef[];
  summary(step: Step, ctx: SummaryCtx): string;   // one-line subtitle, e.g. "go_to_pose · Rack3 · retry 2"
  containers?: string[];                          // keys holding nested steps: ["then","else"], ["body"]
  capability?: string;                            // key into /api/capabilities.steps
}
```

Groups and members (all step types in the schema must be present):

- Navigate: `nav.go_to_pose`, `nav.go_through_poses`, `nav.follow_waypoints`, `nav.follow_path`, `nav.compute_path`, `nav.compute_path_through_poses`, `nav.smooth_path`, `nav.wait_active`, `nav.set_initial_pose`, `nav.cancel`
- Behaviors: `nav.spin`, `nav.backup`, `nav.drive_on_heading`, `nav.dock`, `nav.undock`
- Map & costmap: `nav.change_map`, `nav.clear_costmap`, `nav.lifecycle`
- Logic: `set`, `if`, `loop`, `break`, `end`, `log`, `run_mission`
- Wait & ask: `wait`, `wait_event`, `ask_user`
- ROS: `ros.publish`, `ros.call_service`, `ros.call_action`, `ros.set_param`
- Connectors: `mqtt.publish`, `http.request`, `modbus.write`, `gpio.write`

Trigger/event-source registry (`TriggerDef`) with the same shape for:
`ros.topic`, `mqtt.subscribe`, `http.webhook`, `timer.cron`, `timer.interval`, `timer.boot`, `gpio.input`, `modbus.poll`, `mission.done`.
Each has a `summary(spec)` such as `PLC coil 100 ↑`, `MQTT robot/goal`, `22:00 daily` (render cron in words for the common `m h * * *` and `m h * * d` forms, otherwise show the raw string).

Common step fields (`name`, `enabled`, `out`, `timeout_s`, `on_fail`) are edited by a shared section in the inspector, not repeated per block.

## Layout

Desktop-first, minimum useful width 1000px; below 900px the left column and inspector become slide-over panels toggled from the top bar. One thing at a time: the sidebar shows a single list, the trigger bar is one line, the run log is a 28px bar until a run starts.

```
┌ top bar ───────────────────────────────────────────────────────────────────┐
│ ● Mission  [mission title ▾]  [idle]  [up to date]                          │
│                       … spacer …  ↶ ↷  [▶ Run | ‖ Pause]  STOP  Deploy  ⋯    │
├ left 240px ────┬ center (flex) ──────────────────────────┬ inspector 300px ─┤
│ [Missions|Sites]│ Starts when  [Manual] [22:00 daily] +  + interrupt │ (selected item) │
│ Emergency stop │ ┌ tabs: Steps | JSON ┐                  │ Show help        │
│ Patrol A-B-C-D │  1 ✓ Wait for Nav2                 0.8 s│                  │
│ Pickup job  ●  │  ────────────────────────────────────── │                  │
│                │  2 ▶ Loop                               │                  │
│                │      x $rounds                          │                  │
│                │    │ body                                │                  │
│                │    │  1   Follow waypoints               │                  │
│                │    │      A, B, C, D · retry 1           │                  │
│                │    │  + Add step                         │                  │
│ + New mission  │  + Add step                             │                  │
├ bottom bar (28px; expands to 180px while a run is live) ────────────────────┤
│ ˄ Run log  ✓ patrol · 13 s                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

- **Top bar**: brand · mission title dropdown · status chip (idle / running · step / paused; a red "offline" chip only when the runner is unreachable) · deploy chip ("up to date", "modified", "not on robot", with the error/warning count appended) … undo/redo · Run and Pause grouped · red STOP · Deploy · ⋯ menu. The runner URL lives in the ⋯ menu ("Runner URL… (host:port)").
- **Sidebar**: a segmented control `Missions | Sites` at the top shows one list at a time (the choice is remembered in `localStorage`). There is no block palette; steps are added through the block picker.
- **Trigger bar**: one quiet line, `Starts when [Manual] [chip…] +`; a second line `While running [chip…] +` only when the mission has interrupts, otherwise a muted `+ interrupt` link ends the first line.
- **Steps list**: flat rows separated by hairlines (no card borders); nested bodies indented behind a single 2px left rule with a small inline label (`then`, `else`, `body`, `before retry`).
- **Run log drawer**: collapsed by default to a 28px bar with the last run's result; expands when a run starts.

A prompt banner (ask_user) appears above the steps list when `/api/status.prompt` is set: text, one button per option, countdown to `expires_at`, default marked.

STOP is red, always visible, never covered by dialogs, and calls `POST /api/stop` immediately (no confirmation).

## Behavior

### Missions panel (sidebar tab "Missions")
- Lists runner missions (`GET /api/missions`) as title-only rows with a dot only while the mission is active: blue = running, amber = queued/suspended; plus local drafts not yet deployed (italic). No subtitle lines and no warning icons in the list: the tooltip carries name, version, trigger list and any problems, and runner trigger problems are shown as a dot on the affected chip in the trigger bar.
- New (footer button or ⋯ → New mission…) → template gallery (built-ins + `GET /api/examples`), each with title and description; picking one creates a draft with a fresh unique name.
- Open, rename (validates the pattern), duplicate, delete (confirm), import JSON file, export JSON.
- Switching missions with unsaved changes asks to keep or discard.
- Drafts autosave to `localStorage` (`mission-editor.drafts.v1`); runner copies are the source of truth. When the runner copy changed since the draft was taken (sha256 differs from the one recorded at open/deploy), show a banner "Robot copy changed" with "Load robot copy" / "Keep mine".

### Steps list
- Vertical list of flat rows separated by hairlines; containers (`if.then`/`else`, `loop.body`, `on_fail.before_retry`) render as indented nested lists behind a 2px left rule with a small muted inline label ("then", "else", "body", "before retry"). The selected row gets a soft accent background (no outline).
- Row: muted index, status glyph for the current run (✓ ✗ ▶; blank when idle), title (`name` or block label) on one line, a single muted summary line without the raw type name ("Rack3 · retry 2", not "go_to_pose · Rack3 · retry 2"; the type shows in the inspector header; `→ out` is appended), text badges ("warning", "2 errors", "off"), duration when finished, and — only on hover — the drag handle and actions (enable, duplicate, delete). Containers have a fold chevron.
- Click selects (inspector shows it); double-click on title edits the name inline.
- Add: "+ Add step" at the end of every list, a `+` between rows on hover, and the `+` on containers all open the **block picker**: a dialog with a search field (focused on open), the blocks grouped as in the registry with label and help, arrow keys to move the highlight, Enter to insert, and entries that stay draggable into any position of the list. Blocks whose capability is unavailable on the robot are dimmed with the reason in the tooltip. The picker inserts at the position it was opened from.
- Reorder: HTML5 drag and drop within and across lists with a clear drop indicator; keyboard Alt+↑/↓ moves the selected step.
- Delete key deletes the selection (confirm only when it contains nested steps); Ctrl+D duplicates; Ctrl+Z/Ctrl+Y undo/redo; Esc clears selection.
- Live: on `step.started` scroll the step into view and mark ▶; on `step.finished` mark ✓/✗ with duration; `feedback` shows "4.2 m left · 0 recoveries" under the running step; `run.finished` keeps the marks until the next run or an edit.

### Trigger bar
- Line "Starts when": fixed chip "Manual" (click → mission settings), one chip per trigger showing only its summary (or `name`), disabled ones dimmed, "+" opens a picker of trigger types. Policy, priority, type and any problems are in the chip tooltip; a small red/amber dot on a chip marks validation findings or runner trigger problems (`trigger_problems` entries of the form `id: message` are matched to the chip by trigger id; unmatched ones get a dot after the "+").
- Line "While running": one chip per interrupt (`summary → run`), "+" adds. Shown only when the mission has interrupts; otherwise the first line ends with a muted `+ interrupt` link.
- Clicking a chip selects it; the inspector edits it (type-specific fields, `when`, `edge`, `debounce_s`, `policy`, `priority`, `set` as a key/expression table, `enabled`, and `run` for interrupts).

### Inspector
- Section headers are sentence case, 12px, muted; fields are separated by spacing, not boxes. Per-field help text is hidden by default and available as the label's tooltip; a "Show help" toggle in the header reveals the help lines under every field (remembered in `localStorage`).
- Nothing selected → mission settings: name, title, description, policy, priority, inputs table (name, type, default, required, label), vars table (name, JSON value). Empty tables render as a single line ("No inputs · Add").
- Step selected → header (icon, label, type, "Show help", delete), the block help, type-specific form from the registry, then "On failure" (retry, delay, before-retry container is edited in the list, then abort/continue), then "Advanced" (id read-only with copy, out, timeout_s, enabled).
- Field widgets:
  - `pose`: mode toggle Site / Coordinates / Expression; site select lists sites of the active map (plus "not found" entries in red); coordinates x, y, yaw_deg, frame; "Use robot pose" fills coordinates from `GET /api/robot/pose`.
  - `poses`/`points`: ordered rows of the pose widget with add/remove/move; `points` also accepts pasting `x, y` lines.
  - `behavior_tree`: None / File path / Template (template select and its fields).
  - `expression`: monospaced input with a hint line listing available variables (inputs, vars, outs, `last`, `payload`, `robot`).
  - `value`: text input; strings beginning with `$` or containing `${` are shown with an "expr" badge.
  - `json`: textarea with parse error inline.
  - `select`, `number`, `boolean`, `string`, `text` as expected; `connector` and `map` are selects fed by `/api/connectors` and `/api/sites` with free text fallback.
- Every change goes through the store (undoable), re-validates, and updates the list summary immediately.

### JSON tab
- Read-only pretty JSON of the current mission with a copy button, and an "Edit" mode: textarea, "Apply" parses and validates; errors listed with paths; invalid JSON never replaces the model.

### Run controls
- Run: if the mission declares inputs, open a dialog prefilled with defaults (site inputs as selects); then `POST /api/missions/{name}/run`. If the mission is modified vs the robot copy, ask "Deploy and run" / "Run robot copy" / "Cancel".
- Pause/Resume toggle, STOP, Deploy (validate → `PUT`; show server warnings as toasts, errors in a dialog and as badges).
- Status chip: idle / running · step name / paused / offline. Deploy chip: "up to date", "modified", "not on robot".
- Menu (⋯): New mission…, Import JSON…, Export JSON, Export Python script, Export BT XML (for the selected nav step with a template), Runner URL… (shows the current host:port), Sites (switches the sidebar), About.

### Run log drawer
- Collapsed by default to a 28px bar: chevron, "Run log" and the most recent run's result ("✓ patrol · 13 s", "✗ pickup_job · 2m 10s · failed", or "▶ patrol · running"). Clicking the bar toggles it; it expands automatically on `run.started`; the state is remembered in `localStorage` (`mission-editor.runlog.open`).
- Expanded (180px): history from `GET /api/runs?limit=50` newest first (mission, source, started, duration, result, error) with click → detail panel listing step events; live events appended from the WebSocket; filter by mission; "Clear view".

### Sites panel (sidebar tab "Sites")
- Map selector (maps from sites.json; the runner's `current_map` is marked). Site rows: kind icon, name, muted `x, y · yaw°` (replaced by the hover actions: insert "Go to", capture, edit, delete). Add, edit inline, delete, "Capture" from the robot pose, drag a site onto the steps list to create `nav.go_to_pose`. Save → `PUT /api/sites`. Missing-site warnings in the mission update immediately when sites change.

### Connection
- Runner URL defaults to `window.location.origin` when served by the runner, else `http://localhost:8080`; editable in the menu; stored in `localStorage`.
- The WebSocket reconnects with backoff; the status chip shows "offline" and every remote action is disabled with a tooltip; editing, validation, export and drafts keep working.

## Validation (`model/validate.ts`)

Errors (block deploy): schema-shape problems for known types (required params missing, wrong kinds), duplicate step ids, `break` outside `loop`, `loop` with both `count` and `while`, unknown step or trigger type, `run_mission` referencing an unknown mission (when the missions list is known), missing `run` on interrupts, invalid mission name, `if` without `then`.
Warnings: site not found in the active map, connector not found, empty flow, step disabled, capability unavailable on the robot (`/api/capabilities`), `ask_user` without timeout in a mission that has non-manual triggers, expression that does not parse (use a light tokenizer: balanced brackets and quotes).

Each finding: `{level, path: (string|number)[], stepId?, message}`.

## Code generation

### Python (`codegen/python.ts`)

Produces a self-contained `nav2_simple_commander` script in the style of the user's own scripts:

```python
#!/usr/bin/env python3
"""patrol — generated by Mission editor from patrol.json (version 3). Edit freely."""
import json, math, os, sys, time
import rclpy
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult

SITES = { "A": (1.0, 1.0, 0.0), ... }          # from the active map

def make_pose(nav, x, y, yaw_deg): ...
def site(nav, name): ...
def make_path(nav, points, spacing=0.05): ...
def wait_task(nav):                            # returns True on SUCCEEDED
def ev(expr, ctx):                             # evaluates a mission expression with ctx as variables
def val(v, ctx):                               # resolves "$x", "${...}", "text ${x}" like the runner

def main():
    rclpy.init()
    nav = BasicNavigator()
    ctx = {"rounds": 1}                        # inputs defaults + vars
    # --- step "wait": nav.wait_active
    nav.waitUntilNav2Active()
    # --- step "rounds": loop x $rounds
    for _i in range(int(val("$rounds", ctx))):
        # --- step "lap": nav.follow_waypoints
        ...
```

Rules: each step becomes a commented block; `if`/`loop` become Python blocks; `on_fail.retry` becomes a `for attempt in range(n + 1)` with `before_retry` inlined; `end` → `return`; `break` → `break`; `set` assigns into `ctx`; `log` → `print`; `wait` → `time.sleep`; `ask_user` → `input()` with the options listed; `mqtt.publish` adds a paho client set up from environment variables (`MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`) once at the top; `ros.publish`/`ros.call_service` use `rosidl_runtime_py` `get_message`/`set_message_fields`; `wait_event`, `modbus.*`, `gpio.*`, `http.request`, `run_mission` emit a clearly marked `# TODO` block with the JSON of the step. Expressions are evaluated at run time by `ev()` (Python `eval` with a restricted namespace); pure references like `$goal.x` are emitted as direct `ctx` lookups.

### Behavior tree (`codegen/bt.ts`)

`renderBehaviorTree(template)` returns Nav2-compatible XML for `navigate_with_recovery` and `navigate_through_poses_with_recovery`, based on Nav2's default `navigate_to_pose_w_replanning_and_recovery.xml`: a `RecoveryNode` with `number_of_retries`, a `PipelineSequence` with `RateController` at `replan_rate_hz` around `ComputePathToPose` and `FollowPath`, and a `ReactiveFallback`/`RoundRobin` of the selected recoveries (`ClearEntireCostmap` for both costmaps, `Spin spin_dist` in radians, `Wait wait_duration`, `BackUp backup_dist`). Include `GoalUpdated` guards as in the Nav2 default. The runner has the same generator in Python; tests compare output against `test/fixtures/*.xml`.

## Tests

- `roundtrip.test.ts`: every `examples/*.json` loads through `validate` with zero errors, and `JSON.parse(JSON.stringify(model))` equals the input.
- `validate.test.ts`: crafted invalid missions produce the expected error paths.
- `codegen.test.ts`: `go_to_point`, `patrol`, `follow_line` produce scripts containing the expected calls (`goToPose`, `followWaypoints`, `followPath`, `range(`), and BT XML matches fixtures.

`npm run build` (tsc + vite) and `npm test` must pass.
