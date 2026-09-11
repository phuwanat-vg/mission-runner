/**
 * The six map tools as small state machines: select (move a stop, drag its
 * heading, move a site, drag a zone or one of its vertices), goal, path,
 * waypoints, site and zone. The tools own the interaction; the map view owns
 * the pixels and implements `ToolHost`.
 *
 * Every mission edit goes through the store as a single `update`, so one drag
 * is one undo entry and the JSON, the steps list and validation follow along.
 * Site and zone edits go through the sites document instead (not undoable,
 * saved with "Save sites").
 */

import type { Store } from "../state/Store";
import type { AppActions } from "./actions";
import type { Mission, Site, Step, Zone } from "../model/types";
import type { PlacedPoint, WorldPoint } from "../map/geometry";
import { headedPose, headingDeg, movedPose, normalizeYaw, round1, round3 } from "../map/geometry";
import { toPolygon, zonePolygon } from "../map/zones";
import { newStep, stepTitle } from "../model/blocks";
import { allStepIds, findStep, genId } from "../model/ids";
import type { IconName } from "./icons";
import { toast, zoneForm } from "./dialogs";

export type { WorldPoint };

export type ToolName = "select" | "goal" | "path" | "waypoints" | "site" | "zone";

export interface ToolDef {
  name: ToolName;
  key: string;
  label: string;
  icon: IconName;
  hint: string;
}

export const TOOLS: readonly ToolDef[] = [
  { name: "select", key: "V", label: "Select", icon: "mousePointer", hint: "Click a stop or a site to select it. Drag it to move it, drag the arrow to set the heading." },
  { name: "goal", key: "G", label: "Goal", icon: "flag", hint: "Click the floor to add a “Go to pose” step. Drag before releasing to set the heading; hold over a site to use it." },
  { name: "path", key: "P", label: "Path", icon: "spline", hint: "Click to add points. Enter or double-click finishes, Backspace removes the last point, Esc cancels." },
  { name: "waypoints", key: "W", label: "Waypoints", icon: "waypoints", hint: "Click to add waypoints. Enter or double-click finishes, Backspace removes the last one, Esc cancels." },
  { name: "site", key: "S", label: "Site", icon: "mapPin", hint: "Click the floor to add a site to the current map." },
  { name: "zone", key: "Z", label: "Zone", icon: "zone", hint: "Click to draw a zone polygon. Enter or double-click finishes, Backspace removes the last point, Esc cancels." },
];

export const TOOL_BY_KEY = new Map<string, ToolName>(TOOLS.map((t) => [t.key, t.name]));

export type MapHit =
  | { kind: "point"; point: PlacedPoint }
  | { kind: "handle"; point: PlacedPoint }
  | { kind: "site"; name: string }
  | { kind: "zoneVertex"; name: string; index: number }
  | { kind: "zone"; name: string };

/** The polyline being drawn by the path / waypoints tools. */
export interface DraftShape {
  kind: "path" | "waypoints" | "zone";
  points: WorldPoint[];
  cursor: WorldPoint | null;
  snap: string | null;
}

/** A drag in progress, drawn by the view without touching the document. */
export interface PreviewState {
  /** Key of the placed point being moved, or null when a site is moved. */
  pointKey: string | null;
  siteName: string | null;
  x: number;
  y: number;
  yaw: number | null;
  snap: string | null;
  /** A new pose that does not exist in the document yet (goal tool). */
  ghost: boolean;
  /** A zone polygon being dragged (whole polygon or one vertex). */
  zone?: { name: string; points: WorldPoint[] };
}

/** What the tools need from the map view. */
export interface ToolHost {
  readonly store: Store;
  readonly actions: AppActions;
  /** Pixels per metre. */
  readonly scale: number;
  toWorld(sx: number, sy: number): WorldPoint;
  /** Name of the site within `radiusPx` of a world point, nearest first. */
  siteAt(x: number, y: number, radiusPx: number): string | null;
  hitAt(sx: number, sy: number): MapHit | null;
  setHint(text: string | null): void;
  setDraft(draft: DraftShape | null): void;
  setPreview(preview: PreviewState | null): void;
  /** Small inline name form at a screen position (site tool). */
  askName(sx: number, sy: number, value: string): Promise<string | null>;
}

type Drag =
  | { mode: "move"; point: PlacedPoint; raw: unknown; x: number; y: number; snap: string | null; moved: boolean }
  | { mode: "heading"; point: PlacedPoint; raw: unknown; yaw: number; moved: boolean }
  | { mode: "site"; name: string; x: number; y: number; moved: boolean }
  | { mode: "goal"; x: number; y: number; snap: string | null; yaw: number | null }
  | { mode: "zoneVertex"; name: string; index: number; base: WorldPoint[]; points: WorldPoint[]; moved: boolean }
  | { mode: "zonePoly"; name: string; base: WorldPoint[]; points: WorldPoint[]; from: WorldPoint; moved: boolean };

const DRAG_PX = 3;
const SNAP_PX = 20;

export class MapTools {
  #host: ToolHost;
  #tool: ToolName = "select";
  #drag: Drag | null = null;
  #draft: WorldPoint[] = [];
  #downAt: { sx: number; sy: number } | null = null;

  constructor(host: ToolHost) {
    this.#host = host;
  }

  get tool(): ToolName {
    return this.#tool;
  }

  get busy(): boolean {
    return this.#drag !== null || this.#draft.length > 0;
  }

  setTool(tool: ToolName): void {
    if (tool === this.#tool) return;
    this.cancel();
    this.#tool = tool;
    this.#host.setHint(TOOLS.find((t) => t.name === tool)?.hint ?? null);
  }

  /** Abandon anything in progress (Esc, tool change, mission change). */
  cancel(): void {
    this.#drag = null;
    this.#downAt = null;
    if (this.#draft.length) {
      this.#draft = [];
      this.#host.setDraft(null);
    }
    this.#host.setPreview(null);
  }

  // ---- pointer ---------------------------------------------------------------------------

  /** Returns true when the tool took the gesture (the view must not pan). */
  pointerDown(sx: number, sy: number): boolean {
    const s = this.#host.store;
    this.#downAt = { sx, sy };
    const w = this.#host.toWorld(sx, sy);
    if (this.#tool === "select") {
      const hit = this.#host.hitAt(sx, sy);
      if (!hit) return false;
      if (hit.kind === "site") {
        s.select({ kind: "site", name: hit.name });
        const site = s.activeSites[hit.name];
        if (!site) return true;
        this.#drag = { mode: "site", name: hit.name, x: site.x, y: site.y, moved: false };
        return true;
      }
      if (hit.kind === "zoneVertex") {
        const base = zonePolygon(s.activeZones[hit.name]);
        if (!base.length) return true;
        this.#drag = { mode: "zoneVertex", name: hit.name, index: hit.index, base, points: [...base], moved: false };
        return true;
      }
      if (hit.kind === "zone") {
        s.select({ kind: "zone", name: hit.name });
        const base = zonePolygon(s.activeZones[hit.name]);
        if (!base.length) return true;
        this.#drag = { mode: "zonePoly", name: hit.name, base, points: [...base], from: w, moved: false };
        return true;
      }
      const point = hit.point;
      if (point.stepId) s.select({ kind: "step", id: point.stepId });
      const raw = rawPoseOf(s.mission, point);
      if (!point.editable) {
        this.#host.setHint(point.kind === "expr" ? "This pose comes from an expression; edit it in the inspector." : "This pose cannot be moved on the map.");
        return true;
      }
      if (hit.kind === "handle") this.#drag = { mode: "heading", point, raw, yaw: point.yaw ?? 0, moved: false };
      else this.#drag = { mode: "move", point, raw, x: point.x, y: point.y, snap: point.kind === "site" ? point.site : null, moved: false };
      return true;
    }
    if (this.#tool === "goal") {
      const snap = this.#host.siteAt(w.x, w.y, SNAP_PX);
      this.#drag = { mode: "goal", x: w.x, y: w.y, snap, yaw: null };
      this.#host.setPreview({ pointKey: null, siteName: null, x: w.x, y: w.y, yaw: null, snap, ghost: true });
      return true;
    }
    if (this.#tool === "site") return true;
    return true; // path / waypoints / zone act on the click, in pointerUp
  }

  pointerMove(sx: number, sy: number): void {
    const host = this.#host;
    const w = host.toWorld(sx, sy);
    const drag = this.#drag;
    if (!drag) {
      if (this.#draft.length) {
        const snap = this.#tool === "waypoints" ? host.siteAt(w.x, w.y, SNAP_PX) : null;
        host.setDraft({ kind: this.#draftKind(), points: this.#draft, cursor: w, snap });
      }
      return;
    }
    const moved = this.#downAt !== null && Math.hypot(sx - this.#downAt.sx, sy - this.#downAt.sy) > DRAG_PX;
    switch (drag.mode) {
      case "move": {
        if (!moved) return;
        drag.moved = true;
        const snap = host.siteAt(w.x, w.y, SNAP_PX);
        drag.snap = snap;
        drag.x = snap ? host.store.activeSites[snap]?.x ?? w.x : w.x;
        drag.y = snap ? host.store.activeSites[snap]?.y ?? w.y : w.y;
        host.setPreview({ pointKey: drag.point.key, siteName: null, x: drag.x, y: drag.y, yaw: drag.point.yaw, snap, ghost: false });
        host.setHint(snap ? `Snapped to ${snap}` : `${round3(drag.x)}, ${round3(drag.y)} m`);
        return;
      }
      case "heading": {
        if (!moved) return;
        drag.moved = true;
        drag.yaw = headingDeg(w.x - drag.point.x, w.y - drag.point.y);
        host.setPreview({ pointKey: drag.point.key, siteName: null, x: drag.point.x, y: drag.point.y, yaw: drag.yaw, snap: null, ghost: false });
        host.setHint(`${round1(drag.yaw)}°`);
        return;
      }
      case "site": {
        if (!moved) return;
        drag.moved = true;
        drag.x = w.x;
        drag.y = w.y;
        host.setPreview({ pointKey: null, siteName: drag.name, x: w.x, y: w.y, yaw: null, snap: null, ghost: false });
        host.setHint(`${drag.name} · ${round3(w.x)}, ${round3(w.y)} m`);
        return;
      }
      case "zoneVertex": {
        if (!moved) return;
        drag.moved = true;
        drag.points = drag.base.map((p, i) => (i === drag.index ? { x: w.x, y: w.y } : p));
        host.setPreview({ pointKey: null, siteName: null, x: w.x, y: w.y, yaw: null, snap: null, ghost: false, zone: { name: drag.name, points: drag.points } });
        host.setHint(`${drag.name} · point ${drag.index + 1} · ${round3(w.x)}, ${round3(w.y)} m`);
        return;
      }
      case "zonePoly": {
        if (!moved) return;
        drag.moved = true;
        const dx = w.x - drag.from.x;
        const dy = w.y - drag.from.y;
        drag.points = drag.base.map((p) => ({ x: p.x + dx, y: p.y + dy }));
        host.setPreview({ pointKey: null, siteName: null, x: w.x, y: w.y, yaw: null, snap: null, ghost: false, zone: { name: drag.name, points: drag.points } });
        host.setHint(`${drag.name} moved by ${round3(dx)}, ${round3(dy)} m`);
        return;
      }
      case "goal": {
        const far = Math.hypot(w.x - drag.x, w.y - drag.y) * host.scale > 12;
        drag.yaw = far ? headingDeg(w.x - drag.x, w.y - drag.y) : null;
        host.setPreview({ pointKey: null, siteName: null, x: drag.x, y: drag.y, yaw: drag.yaw, snap: drag.snap, ghost: true });
        host.setHint(drag.snap ? `Snapped to ${drag.snap}${drag.yaw === null ? "" : ` · ${round1(drag.yaw)}°`}` : drag.yaw === null ? "Release to add the step; drag to set a heading." : `${round1(drag.yaw)}°`);
        return;
      }
    }
  }

  pointerUp(sx: number, sy: number): void {
    const host = this.#host;
    const w = host.toWorld(sx, sy);
    const drag = this.#drag;
    const clicked = this.#downAt !== null && Math.hypot(sx - this.#downAt.sx, sy - this.#downAt.sy) <= DRAG_PX;
    this.#downAt = null;
    this.#drag = null;
    host.setPreview(null);
    host.setHint(null);
    if (drag) {
      switch (drag.mode) {
        case "move":
          if (drag.moved) this.#commitMove(drag.point, drag.raw, drag.x, drag.y, drag.snap);
          return;
        case "heading":
          if (drag.moved) this.#commitHeading(drag.point, drag.raw, drag.yaw);
          return;
        case "site":
          if (drag.moved) this.#commitSiteMove(drag.name, drag.x, drag.y);
          return;
        case "goal":
          this.#addGoal(drag.x, drag.y, drag.snap, drag.yaw);
          return;
        case "zoneVertex":
        case "zonePoly":
          if (drag.moved) this.#commitZoneShape(drag.name, drag.points);
          return;
      }
    }
    if (!clicked) return;
    if (this.#tool === "select") {
      host.store.select(null);
      return;
    }
    if (this.#tool === "site") {
      void this.#addSite(sx, sy, w);
      return;
    }
    if (this.#drawing()) {
      this.#draft.push(w);
      host.setDraft({ kind: this.#draftKind(), points: this.#draft, cursor: null, snap: null });
      const missing = (this.#tool === "zone" ? 3 : 2) - this.#draft.length;
      host.setHint(`${this.#draft.length} point${this.#draft.length === 1 ? "" : "s"} · ${missing > 0 ? `${missing} more, then ` : ""}Enter finishes, Backspace removes the last, Esc cancels.`);
    }
  }

  #drawing(): boolean {
    return this.#tool === "path" || this.#tool === "waypoints" || this.#tool === "zone";
  }

  #draftKind(): DraftShape["kind"] {
    return this.#tool === "path" ? "path" : this.#tool === "zone" ? "zone" : "waypoints";
  }

  doubleClick(): void {
    if (this.#drawing()) this.finish();
  }

  /** Tool-specific keys. Returns true when the key was used. */
  key(e: KeyboardEvent): boolean {
    if (!this.#drawing()) return false;
    if (e.key === "Enter") {
      this.finish();
      return true;
    }
    if (e.key === "Backspace" && this.#draft.length) {
      this.#draft.pop();
      this.#host.setDraft(this.#draft.length ? { kind: this.#draftKind(), points: this.#draft, cursor: null, snap: null } : null);
      return true;
    }
    return false;
  }

  /** Turn the drafted polyline into a step, or the drafted polygon into a zone. */
  finish(): void {
    const points = this.#draft;
    const zone = this.#tool === "zone";
    this.#draft = [];
    this.#host.setDraft(null);
    this.#host.setHint(null);
    if (zone) {
      if (points.length >= 3) void this.#addZone(points);
      else if (points.length) toast("A zone needs at least three points.", "warn");
      return;
    }
    if (points.length < 2) return;
    const s = this.#host.store;
    const mission = s.mission;
    if (!mission) return;
    const path = this.#tool === "path";
    const step = newStep(path ? "nav.follow_path" : "nav.follow_waypoints", genId("s", allStepIds(mission)));
    if (path) {
      step.points = points.map((p) => [round3(p.x), round3(p.y)]);
    } else {
      step.poses = points.map((p) => {
        const site = this.#host.siteAt(p.x, p.y, SNAP_PX);
        return site ?? { x: round3(p.x), y: round3(p.y) };
      });
    }
    this.#host.actions.insertStep(step, s.insertionPoint());
    toast(`${path ? "Follow path" : "Follow waypoints"} with ${points.length} points added`, "ok", 2500);
  }

  // ---- commits ------------------------------------------------------------------------------

  #write(point: PlacedPoint, next: unknown): void {
    this.#host.store.update((doc) => {
      const found = findStep(doc, point.stepId);
      if (!found) return;
      const step = found.step;
      if (point.index === null) {
        step[point.param] = next;
        return;
      }
      const list = step[point.param];
      if (Array.isArray(list) && point.index < list.length) list[point.index] = next;
    });
  }

  #commitMove(point: PlacedPoint, raw: unknown, x: number, y: number, snap: string | null): void {
    const s = this.#host.store;
    const wasSite = point.kind === "site" ? point.site : null;
    this.#write(point, movedPose(raw, x, y, snap, point.yaw));
    if (wasSite && snap === null) {
      const title = s.mission ? stepTitle(findStep(s.mission, point.stepId)?.step ?? { type: "" }) : "The step";
      toast(`“${title}” no longer uses the site ${wasSite}; it now has coordinates.`, "warn", 9000, { label: "Undo", onClick: () => s.undo() });
    } else if (snap && snap !== wasSite) {
      toast(`Snapped to ${snap}`, "ok", 2000);
    }
  }

  #commitHeading(point: PlacedPoint, raw: unknown, yaw: number): void {
    this.#write(point, headedPose(raw, normalizeYaw(yaw)));
  }

  #commitSiteMove(name: string, x: number, y: number): void {
    const s = this.#host.store;
    const before = s.activeSites[name];
    if (!before) return;
    const prev: Site = { ...before };
    s.mutateSites((sites) => {
      const site = sites[name];
      if (site) {
        site.x = round3(x);
        site.y = round3(y);
      }
    });
    const actions = this.#host.actions;
    toast(`${name} moved to ${round3(x)}, ${round3(y)}. Sites are not saved on the robot yet.`, "warn", 9000, {
      label: s.connected ? "Save sites" : "Undo",
      onClick: () => {
        if (s.connected) actions.saveSites();
        else s.mutateSites((sites) => void (sites[name] = prev));
      },
    });
  }

  #addGoal(x: number, y: number, snap: string | null, yaw: number | null): void {
    const s = this.#host.store;
    const mission = s.mission;
    if (!mission) {
      toast("Open a mission first", "warn");
      return;
    }
    const step = newStep("nav.go_to_pose", genId("s", allStepIds(mission)));
    if (snap) {
      step.pose = yaw === null ? snap : { site: snap, yaw_deg: round1(yaw) };
      step.name = `Go to ${snap}`;
    } else {
      step.pose = yaw === null ? { x: round3(x), y: round3(y) } : { x: round3(x), y: round3(y), yaw_deg: round1(yaw) };
    }
    this.#host.actions.insertStep(step, s.insertionPoint());
  }

  #commitZoneShape(name: string, points: WorldPoint[]): void {
    const s = this.#host.store;
    const before = zonePolygon(s.activeZones[name]);
    s.mutateZones((zones) => {
      const zone = zones[name];
      if (zone) zone.polygon = toPolygon(points);
    });
    const actions = this.#host.actions;
    toast(`${name} reshaped. Zones are not saved on the robot yet.`, "warn", 9000, {
      label: s.connected ? "Save sites" : "Undo",
      onClick: () => {
        if (s.connected) actions.saveSites();
        else {
          s.mutateZones((zones) => {
            const zone = zones[name];
            if (zone) zone.polygon = toPolygon(before);
          });
        }
      },
    });
  }

  /** Finish the zone tool: ask for a name and a kind, then add it to the map. */
  async #addZone(points: WorldPoint[]): Promise<void> {
    const s = this.#host.store;
    const taken = new Set(Object.keys(s.activeZones));
    let n = taken.size + 1;
    while (taken.has(`Zone ${n}`)) n++;
    const value = await zoneForm("New zone", { name: `Zone ${n}`, kind: "keepout" }, taken);
    if (!value) return;
    const zone: Zone = { kind: value.kind, polygon: toPolygon(points) };
    if (value.speed_mps !== undefined) zone.speed_mps = value.speed_mps;
    if (value.notes) zone.notes = value.notes;
    s.mutateZones((zones) => {
      zones[value.name] = zone;
    });
    s.select({ kind: "zone", name: value.name });
    toast(`${value.name} added. Save sites to keep it on the robot.`, "ok", 6000, s.connected ? { label: "Save sites", onClick: () => this.#host.actions.saveSites() } : undefined);
  }

  async #addSite(sx: number, sy: number, w: WorldPoint): Promise<void> {
    const s = this.#host.store;
    const taken = new Set(Object.keys(s.activeSites));
    let n = taken.size + 1;
    while (taken.has(`Site ${n}`)) n++;
    const name = await this.#host.askName(sx, sy, `Site ${n}`);
    if (name === null) return;
    const clean = name.trim();
    if (clean === "") return;
    if (taken.has(clean)) {
      toast(`A site named “${clean}” already exists in this map.`, "warn");
      return;
    }
    s.mutateSites((sites) => {
      sites[clean] = { x: round3(w.x), y: round3(w.y), yaw_deg: 0, kind: "station" };
    });
    s.select({ kind: "site", name: clean });
    toast(`${clean} added. Save sites to keep it on the robot.`, "ok", 5000, s.connected ? { label: "Save sites", onClick: () => this.#host.actions.saveSites() } : undefined);
  }
}

/** The raw pose value a placed point was built from (what a drag rewrites). */
export function rawPoseOf(mission: Mission | null, point: PlacedPoint): unknown {
  if (!mission) return undefined;
  const found = findStep(mission, point.stepId);
  if (!found) return undefined;
  const value = (found.step as Step)[point.param];
  if (point.index === null) return value;
  return Array.isArray(value) ? value[point.index] : undefined;
}
