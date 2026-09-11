/**
 * Pure map geometry: the world<->screen view transform, pose resolution
 * (site / coordinates / expression / missing site), the placement of every
 * step that has a position on the floor, and the route legs between driving
 * steps. No DOM; shared by the map view, the steps list and the tests.
 *
 * World coordinates are metres, x right, y up (the map frame). Screen
 * coordinates are pixels, y down; the flip happens once in `viewMatrix`.
 */

import type { Mission, Path, Site, Step } from "../model/types";
import { isRecord } from "../model/types";
import { hasExpression } from "../model/expressions";
import { poseText, stepTitle } from "../model/blocks";
import { CONTAINER_KEYS } from "../model/ids";

// ---- view transform ------------------------------------------------------------------

export interface View {
  /** Pixels per metre. */
  scale: number;
  tx: number;
  ty: number;
}

/** A point in world coordinates (metres). */
export interface WorldPoint {
  x: number;
  y: number;
}

export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export const MIN_SCALE = 3;
export const MAX_SCALE = 600;

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

export function worldToScreen(v: View, x: number, y: number): [number, number] {
  return [v.tx + x * v.scale, v.ty - y * v.scale];
}

export function screenToWorld(v: View, sx: number, sy: number): [number, number] {
  return [(sx - v.tx) / v.scale, (v.ty - sy) / v.scale];
}

/** SVG transform of the world group: scale, flip y, translate. */
export function viewMatrix(v: View): string {
  return `matrix(${v.scale} 0 0 ${-v.scale} ${v.tx} ${v.ty})`;
}

export function fitView(b: Bounds, width: number, height: number, pad = 48): View {
  const bw = Math.max(b.maxX - b.minX, 0.5);
  const bh = Math.max(b.maxY - b.minY, 0.5);
  const scale = clamp(Math.min((width - 2 * pad) / bw, (height - 2 * pad) / bh), MIN_SCALE, MAX_SCALE);
  const cx = (b.minX + b.maxX) / 2;
  const cy = (b.minY + b.maxY) / 2;
  return { scale, tx: width / 2 - cx * scale, ty: height / 2 + cy * scale };
}

/** Zoom by `factor` keeping the world point under screen (sx, sy) fixed. */
export function zoomAt(v: View, sx: number, sy: number, factor: number): View {
  const scale = clamp(v.scale * factor, MIN_SCALE, MAX_SCALE);
  const f = scale / v.scale;
  return { scale, tx: sx - (sx - v.tx) * f, ty: sy - (sy - v.ty) * f };
}

export function centerOn(v: View, x: number, y: number, width: number, height: number): View {
  return { scale: v.scale, tx: width / 2 - x * v.scale, ty: height / 2 + y * v.scale };
}

export function boundsOf(points: readonly { x: number; y: number }[]): Bounds | null {
  if (!points.length) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, minY, maxX, maxY };
}

export function unionBounds(a: Bounds | null, b: Bounds | null): Bounds | null {
  if (!a) return b;
  if (!b) return a;
  return { minX: Math.min(a.minX, b.minX), minY: Math.min(a.minY, b.minY), maxX: Math.max(a.maxX, b.maxX), maxY: Math.max(a.maxY, b.maxY) };
}

export function expandBounds(b: Bounds, margin: number): Bounds {
  return { minX: b.minX - margin, minY: b.minY - margin, maxX: b.maxX + margin, maxY: b.maxY + margin };
}

// ---- numbers ---------------------------------------------------------------------------

/** Degrees normalized to (-180, 180]. */
export function normalizeYaw(deg: number): number {
  let d = ((((deg + 180) % 360) + 360) % 360) - 180;
  if (d === -180) d = 180;
  return d === 0 ? 0 : d; // avoid -0
}

export function round3(v: number): number {
  const r = Math.round(v * 1000) / 1000;
  return r === 0 ? 0 : r;
}

export function round1(v: number): number {
  const r = Math.round(v * 10) / 10;
  return r === 0 ? 0 : r;
}

/** Heading in degrees of the world vector (dx, dy), normalized. */
export function headingDeg(dx: number, dy: number): number {
  return normalizeYaw((Math.atan2(dy, dx) * 180) / Math.PI);
}

export function distance(ax: number, ay: number, bx: number, by: number): number {
  return Math.hypot(bx - ax, by - ay);
}

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

// ---- pose resolution ---------------------------------------------------------------------

export type PointKind = "coords" | "site" | "expr" | "missing";

export interface MapPoint {
  x: number;
  y: number;
  yaw: number | null;
  kind: PointKind;
  /** Referenced site name (site / missing / statically resolved expression). */
  site: string | null;
  /** Can be dragged on the map (coordinates and site references; not expressions). */
  editable: boolean;
  /** Short text of the raw pose (tooltips, the "not placeable" list). */
  text: string;
}

function fromSite(name: string, site: Site, yawOverride: number | null): MapPoint {
  const yaw = yawOverride !== null ? yawOverride : isNum(site.yaw_deg) ? normalizeYaw(site.yaw_deg) : null;
  return { x: site.x, y: site.y, yaw, kind: "site", site: name, editable: true, text: name };
}

/**
 * A `$name` expression that can be placed without running the mission: an
 * input of type site/pose (or a var) whose default is a site of the map or
 * plain coordinates.
 */
function staticExpr(expr: string, sites: Record<string, Site>, mission: Mission | null): MapPoint | null {
  const m = /^\$([A-Za-z_][A-Za-z0-9_]*)$/.exec(expr);
  if (!m || !mission) return null;
  const name = m[1]!;
  const def = mission.inputs?.[name];
  const value = def ? def.default : mission.vars?.[name];
  if (value === undefined || value === null) return null;
  if (def && def.type !== "site" && def.type !== "pose") return null;
  const inner = resolvePose(value, sites, null);
  if (!inner || inner.kind === "missing") return null;
  return { ...inner, kind: "expr", editable: false, text: expr };
}

/** Where a pose is on the map, or null when it cannot be placed (run-time expression). */
export function resolvePose(raw: unknown, sites: Record<string, Site>, mission: Mission | null): MapPoint | null {
  if (typeof raw === "string") {
    if (raw === "") return null;
    if (hasExpression(raw)) return staticExpr(raw, sites, mission);
    const site = sites[raw];
    if (site) return fromSite(raw, site, null);
    return { x: 0, y: 0, yaw: null, kind: "missing", site: raw, editable: true, text: raw };
  }
  if (Array.isArray(raw)) {
    const [x, y, yaw] = raw as unknown[];
    if (!isNum(x) || !isNum(y)) return null;
    return { x, y, yaw: isNum(yaw) ? normalizeYaw(yaw) : null, kind: "coords", site: null, editable: true, text: poseText(raw) };
  }
  if (!isRecord(raw)) return null;
  if (typeof raw.site === "string") {
    if (raw.site === "") return null;
    const yaw = isNum(raw.yaw_deg) ? normalizeYaw(raw.yaw_deg) : null;
    if (hasExpression(raw.site)) {
      const p = staticExpr(raw.site, sites, mission);
      return p ? { ...p, yaw: yaw ?? p.yaw, text: poseText(raw) } : null;
    }
    const site = sites[raw.site];
    if (site) return { ...fromSite(raw.site, site, yaw), text: poseText(raw) };
    return { x: 0, y: 0, yaw, kind: "missing", site: raw.site, editable: true, text: poseText(raw) };
  }
  if (isNum(raw.x) && isNum(raw.y)) {
    return { x: raw.x, y: raw.y, yaw: isNum(raw.yaw_deg) ? normalizeYaw(raw.yaw_deg) : null, kind: "coords", site: null, editable: true, text: poseText(raw) };
  }
  return null;
}

/**
 * The pose after a drag to world (x, y). A site reference stays a site
 * reference only when the drop snapped onto a site (`snap`); otherwise it
 * becomes coordinates (keeping `keepYaw`, the heading it had). Coordinate
 * forms keep their shape (`[x, y]` stays an array).
 */
export function movedPose(raw: unknown, x: number, y: number, snap: string | null, keepYaw: number | null): unknown {
  const rx = round3(x);
  const ry = round3(y);
  if (snap !== null) {
    if (isRecord(raw) && typeof raw.site === "string") return { ...raw, site: snap };
    return snap;
  }
  if (Array.isArray(raw)) return raw.length >= 3 && isNum(raw[2]) ? [rx, ry, raw[2]] : [rx, ry];
  if (isRecord(raw) && !("site" in raw)) return { ...raw, x: rx, y: ry };
  const out: Record<string, unknown> = { x: rx, y: ry };
  if (isRecord(raw) && raw.yaw_deg !== undefined) out.yaw_deg = raw.yaw_deg;
  else if (keepYaw !== null) out.yaw_deg = round1(keepYaw);
  return out;
}

/** The pose with its heading set to `yaw` degrees (site references get a yaw override). */
export function headedPose(raw: unknown, yaw: number): unknown {
  const y = round1(normalizeYaw(yaw));
  if (typeof raw === "string") return hasExpression(raw) ? raw : { site: raw, yaw_deg: y };
  if (Array.isArray(raw)) return [raw[0], raw[1], y];
  if (isRecord(raw)) return { ...raw, yaw_deg: y };
  return raw;
}

// ---- placement ------------------------------------------------------------------------------

export type PlaceGroup = "navigate" | "docking" | "plan";

export interface PlacedPoint extends MapPoint {
  /** `${stepId}:${param}:${index}` — stable across re-renders. */
  key: string;
  stepId: string;
  param: string;
  /** Index in a pose list, null for single poses. */
  index: number | null;
  /** Number of this stop along the route (driving stops only). */
  stopNo: number | null;
  /** Last point of its step (gets the label). */
  last: boolean;
}

export interface StepPlace {
  stepId: string;
  path: Path;
  type: string;
  title: string;
  group: PlaceGroup;
  /** The robot drives there: part of the route. */
  driving: boolean;
  /** In on_abort / before_retry: drawn muted, not on the route. */
  aside: boolean;
  points: PlacedPoint[];
  /** Poses of this step that cannot be placed (run-time expressions). */
  unplaced: string[];
}

export interface RouteLeg {
  key: string;
  from: PlacedPoint;
  to: PlacedPoint;
  /** Step of the leg's destination; the leg takes its run status. */
  stepId: string;
  kind: "leg" | "path" | "waypoints";
}

export interface MapModel {
  places: StepPlace[];
  byStep: Map<string, StepPlace>;
  byKey: Map<string, PlacedPoint>;
  legs: RouteLeg[];
  unplaced: { stepId: string; title: string; text: string }[];
  /** Number of numbered stops. */
  stops: number;
  bounds: Bounds | null;
}

const DRIVING: Record<string, PlaceGroup> = {
  "nav.go_to_pose": "navigate",
  "nav.go_through_poses": "navigate",
  "nav.follow_waypoints": "navigate",
  "nav.follow_path": "navigate",
  "nav.dock": "docking",
};
const PLANNING: Record<string, PlaceGroup> = {
  "nav.set_initial_pose": "plan",
  "nav.compute_path": "plan",
  "nav.compute_path_through_poses": "plan",
};

/** Parameters holding the poses drawn for a step type. */
export function poseParamsOf(type: string): { param: string; list: boolean }[] {
  switch (type) {
    case "nav.go_to_pose":
    case "nav.set_initial_pose":
      return [{ param: "pose", list: false }];
    case "nav.compute_path":
      return [{ param: "goal", list: false }];
    case "nav.dock":
      return [{ param: "dock_pose", list: false }];
    case "nav.go_through_poses":
    case "nav.follow_waypoints":
      return [{ param: "poses", list: true }];
    case "nav.follow_path":
      return [{ param: "points", list: true }];
    case "nav.compute_path_through_poses":
      return [{ param: "goals", list: true }];
    default:
      return [];
  }
}

export function hasMapPresence(type: string): boolean {
  return type in DRIVING || type in PLANNING;
}

function* walkForRoute(list: Step[], listPath: Path, aside: boolean): Generator<{ step: Step; path: Path; aside: boolean }> {
  for (let i = 0; i < list.length; i++) {
    const step = list[i]!;
    const path = [...listPath, i];
    yield { step, path, aside };
    for (const key of CONTAINER_KEYS[step.type] ?? []) {
      const nested = step[key];
      if (Array.isArray(nested)) yield* walkForRoute(nested as Step[], [...path, key], aside);
    }
    const before = isRecord(step.on_fail) && Array.isArray(step.on_fail.before_retry) ? (step.on_fail.before_retry as Step[]) : null;
    if (before) yield* walkForRoute(before, [...path, "on_fail", "before_retry"], true);
  }
}

export function emptyMapModel(): MapModel {
  return { places: [], byStep: new Map(), byKey: new Map(), legs: [], unplaced: [], stops: 0, bounds: null };
}

/** Places every step with a position and builds the route through the driving ones. */
export function buildMapModel(mission: Mission | null, sites: Record<string, Site>): MapModel {
  const model = emptyMapModel();
  if (!mission) return model;
  const dockSites = new Map<string, string>();
  for (const [name, s] of Object.entries(sites)) if (typeof s.dock_id === "string" && s.dock_id !== "") dockSites.set(s.dock_id, name);

  const visits = [...walkForRoute(mission.flow ?? [], ["flow"], false), ...walkForRoute(mission.on_abort ?? [], ["on_abort"], true)];
  const route: PlacedPoint[] = [];
  let stopNo = 0;
  const all: { x: number; y: number }[] = [];
  for (const v of visits) {
    const step = v.step;
    const type = step.type;
    const group = DRIVING[type] ?? PLANNING[type];
    if (!group) continue;
    const stepId = typeof step.id === "string" ? step.id : "";
    const driving = type in DRIVING;
    const place: StepPlace = { stepId, path: v.path, type, title: stepTitle(step), group, driving, aside: v.aside, points: [], unplaced: [] };
    const raws: { param: string; index: number | null; raw: unknown }[] = [];
    for (const { param, list } of poseParamsOf(type)) {
      const value = step[param];
      if (list) {
        if (Array.isArray(value)) value.forEach((raw, index) => raws.push({ param, index, raw }));
      } else if (value !== undefined) raws.push({ param, index: null, raw: value });
    }
    if (type === "nav.dock" && raws.length === 0 && typeof step.dock_id === "string") {
      const siteName = dockSites.get(step.dock_id);
      if (siteName) raws.push({ param: "dock_id", index: null, raw: siteName });
    }
    for (const r of raws) {
      const p = resolvePose(r.raw, sites, mission);
      if (!p) {
        place.unplaced.push(r.raw === undefined ? "" : poseText(r.raw));
        continue;
      }
      const pt: PlacedPoint = { ...p, key: `${stepId}:${r.param}:${r.index ?? ""}`, stepId, param: r.param, index: r.index, stopNo: null, last: false };
      if (r.param === "dock_id") pt.editable = false;
      place.points.push(pt);
    }
    if (place.points.length) place.points[place.points.length - 1]!.last = true;
    const numbered = driving && !v.aside;
    for (const pt of place.points) {
      if (numbered && (type !== "nav.follow_path" || pt.last)) pt.stopNo = ++stopNo;
      if (numbered) route.push(pt);
      model.byKey.set(pt.key, pt);
      all.push(pt);
    }
    if (place.points.length || place.unplaced.length) {
      model.places.push(place);
      if (stepId) model.byStep.set(stepId, place);
    }
    for (const text of place.unplaced) model.unplaced.push({ stepId, title: place.title, text });
  }
  for (let i = 1; i < route.length; i++) {
    const from = route[i - 1]!;
    const to = route[i]!;
    const place = model.byStep.get(to.stepId);
    const same = from.stepId === to.stepId;
    const kind: RouteLeg["kind"] = same && place?.type === "nav.follow_path" ? "path" : same && place?.type === "nav.follow_waypoints" ? "waypoints" : "leg";
    model.legs.push({ key: `${from.key}>${to.key}`, from, to, stepId: to.stepId, kind });
  }
  model.stops = stopNo;
  model.bounds = boundsOf(all);
  return model;
}

/** "3" or "1–4": the stop numbers of a step, or null when it has none. */
export function stopRange(place: StepPlace | undefined): string | null {
  if (!place) return null;
  const nos = place.points.map((p) => p.stopNo).filter((n): n is number => n !== null);
  if (!nos.length) return null;
  const lo = Math.min(...nos);
  const hi = Math.max(...nos);
  return lo === hi ? String(lo) : `${lo}–${hi}`;
}

/** Bounds of the mission's places and the map's sites (what "fit" shows). */
export function contentBounds(model: MapModel, sites: Record<string, Site>): Bounds | null {
  return unionBounds(model.bounds, boundsOf(Object.values(sites)));
}
