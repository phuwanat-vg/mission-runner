/**
 * Pure zone geometry: the polygons a map defines in `sites.json` under
 * `zones`, their bounds, hit tests and the per-kind look. No DOM, so the map
 * view, the inspector and the tests all agree on what a zone is.
 *
 * World coordinates are metres (x right, y up), exactly like the rest of the
 * map model.
 */

import type { Zone, ZoneKind } from "../model/types";
import { ZONE_KINDS, isRecord } from "../model/types";
import type { Bounds, WorldPoint } from "./geometry";
import { boundsOf, round3 } from "./geometry";

export const DEFAULT_ZONE_KIND: ZoneKind = "work";

export interface ZoneStyle {
  label: string;
  /** One line for the inspector and the tooltips. */
  help: string;
  /** Base colour; the fill and the hatch are derived from it in CSS. */
  color: string;
}

export const ZONE_STYLES: Record<ZoneKind, ZoneStyle> = {
  keepout: { label: "Keep-out", help: "Exported as a Nav2 keep-out mask: the planner will not route through it.", color: "#ff5a6a" },
  speed_limit: { label: "Speed limit", help: "Exported as a Nav2 speed mask: the controller is capped at the speed below inside it.", color: "#f2b134" },
  preferred: { label: "Preferred", help: "Drawn only. A named area for the operator and for in_zone() expressions.", color: "#3ccf7a" },
  work: { label: "Work area", help: "Drawn only. A named area missions can test with in_zone('name').", color: "#8b95a5" },
};

export function zoneKindOf(zone: Zone | undefined): ZoneKind {
  const k = zone?.kind;
  return k !== undefined && (ZONE_KINDS as readonly string[]).includes(k) ? k : DEFAULT_ZONE_KIND;
}

/** The polygon as world points, dropping anything that is not a finite pair. */
export function zonePolygon(zone: Zone | undefined): WorldPoint[] {
  if (!zone || !Array.isArray(zone.polygon)) return [];
  const out: WorldPoint[] = [];
  for (const p of zone.polygon) {
    if (!Array.isArray(p)) continue;
    const [x, y] = p as unknown[];
    if (typeof x !== "number" || typeof y !== "number" || !Number.isFinite(x) || !Number.isFinite(y)) continue;
    out.push({ x, y });
  }
  return out;
}

/** World points back into the stored `[[x, y], …]` form (rounded to mm). */
export function toPolygon(points: readonly WorldPoint[]): [number, number][] {
  return points.map((p) => [round3(p.x), round3(p.y)] as [number, number]);
}

/** An SVG path for a closed polygon, or "" when there is nothing to draw. */
export function polygonPath(points: readonly WorldPoint[]): string {
  if (points.length < 2) return "";
  return `${points.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(3)} ${p.y.toFixed(3)}`).join("")}Z`;
}

/** Signed area doubled; positive when the ring is counter-clockwise. */
function area2(points: readonly WorldPoint[]): number {
  let a = 0;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    a += points[j]!.x * points[i]!.y - points[i]!.x * points[j]!.y;
  }
  return a;
}

export function polygonArea(points: readonly WorldPoint[]): number {
  return points.length < 3 ? 0 : Math.abs(area2(points)) / 2;
}

/** Centroid of the polygon (falls back to the average for degenerate rings). */
export function polygonCentroid(points: readonly WorldPoint[]): WorldPoint {
  if (!points.length) return { x: 0, y: 0 };
  const a = area2(points);
  if (points.length < 3 || Math.abs(a) < 1e-9) {
    let sx = 0;
    let sy = 0;
    for (const p of points) {
      sx += p.x;
      sy += p.y;
    }
    return { x: sx / points.length, y: sy / points.length };
  }
  let cx = 0;
  let cy = 0;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const f = points[j]!.x * points[i]!.y - points[i]!.x * points[j]!.y;
    cx += (points[j]!.x + points[i]!.x) * f;
    cy += (points[j]!.y + points[i]!.y) * f;
  }
  return { x: cx / (3 * a), y: cy / (3 * a) };
}

export function polygonBounds(points: readonly WorldPoint[]): Bounds | null {
  return boundsOf(points);
}

/** Ray casting, matching the runner's `in_zone`. */
export function pointInPolygon(points: readonly WorldPoint[], x: number, y: number): boolean {
  if (points.length < 3) return false;
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const a = points[i]!;
    const b = points[j]!;
    if (a.y > y !== b.y > y && x < ((b.x - a.x) * (y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

/** Index of the vertex within `tol` metres of (x, y), or -1. */
export function nearestVertex(points: readonly WorldPoint[], x: number, y: number, tol: number): number {
  let best = -1;
  let bestD = tol;
  for (let i = 0; i < points.length; i++) {
    const d = Math.hypot(points[i]!.x - x, points[i]!.y - y);
    if (d <= bestD) {
      bestD = d;
      best = i;
    }
  }
  return best;
}

function distanceToSegment(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

/** Distance from a point to the polygon outline (0 is on the outline). */
export function distanceToOutline(points: readonly WorldPoint[], x: number, y: number): number {
  if (points.length < 2) return Infinity;
  let best = Infinity;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    best = Math.min(best, distanceToSegment(x, y, points[j]!.x, points[j]!.y, points[i]!.x, points[i]!.y));
  }
  return best;
}

/** The zones of a map, sorted so the smallest is hit-tested first. */
export function zoneEntries(zones: Record<string, Zone>): { name: string; zone: Zone; points: WorldPoint[] }[] {
  return Object.entries(zones)
    .map(([name, zone]) => ({ name, zone, points: zonePolygon(zone) }))
    .sort((a, b) => polygonArea(a.points) - polygonArea(b.points));
}

/** Name of the smallest zone containing the point, or null (mirrors `zone_of`). */
export function zoneAt(zones: Record<string, Zone>, x: number, y: number): string | null {
  for (const e of zoneEntries(zones)) if (pointInPolygon(e.points, x, y)) return e.name;
  return null;
}

/** Bounds of every zone of a map (part of what "fit" shows). */
export function zonesBounds(zones: Record<string, Zone>): Bounds | null {
  const all: WorldPoint[] = [];
  for (const zone of Object.values(zones)) all.push(...zonePolygon(zone));
  return boundsOf(all);
}

/** A zone read out of a `sites.json` that may contain anything. */
export function isZone(v: unknown): v is Zone {
  return isRecord(v) && Array.isArray(v.polygon);
}

/** One line under the zone name: kind, speed and vertex count. */
export function zoneSummary(zone: Zone): string {
  const kind = zoneKindOf(zone);
  const bits = [ZONE_STYLES[kind].label];
  if (kind === "speed_limit" && typeof zone.speed_mps === "number") bits.push(`${zone.speed_mps} m/s`);
  const n = zonePolygon(zone).length;
  bits.push(`${n} point${n === 1 ? "" : "s"}`);
  const a = polygonArea(zonePolygon(zone));
  if (a >= 0.05) bits.push(`${a < 10 ? a.toFixed(1) : Math.round(a)} m²`);
  return bits.join(" · ");
}
