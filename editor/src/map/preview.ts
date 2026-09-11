/**
 * The two previews the runner can compute without moving the robot:
 * `POST /api/preview/route` (what the real planner would drive) and
 * `POST /api/preview/dryrun` (the whole flow replayed against a private
 * simulated robot). This module only parses and formats — no DOM, no fetch —
 * so the map view and the transport bar share one model.
 */

import type { WorldPoint } from "./geometry";
import { isRecord } from "../model/types";

const num = (v: unknown, dflt = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : dflt);
const str = (v: unknown): string => (typeof v === "string" ? v : "");

// ---- route preview ------------------------------------------------------------------

export interface PreviewLeg {
  /** Stable key for the SVG node. */
  key: string;
  stepId: string;
  index: number;
  /** The planned polyline (at least the two endpoints). */
  points: WorldPoint[];
  planned: boolean;
  error: string;
  lengthM: number;
}

export interface RoutePreview {
  legs: PreviewLeg[];
  distanceM: number;
  estimateS: number;
  speedMps: number;
  /** Every leg came from the real planner. */
  planned: boolean;
  /** "robot is busy; distances are straight-line" and friends. */
  note: string;
  /** Document version this was computed for (cleared when the mission changes). */
  version: number;
  at: number;
}

function posePoints(raw: unknown): WorldPoint[] {
  const poses = isRecord(raw) && Array.isArray(raw.poses) ? raw.poses : [];
  const out: WorldPoint[] = [];
  for (const p of poses) {
    if (!isRecord(p)) continue;
    if (typeof p.x !== "number" || typeof p.y !== "number") continue;
    out.push({ x: p.x, y: p.y });
  }
  return out;
}

function endpoint(raw: unknown): WorldPoint | null {
  if (!isRecord(raw) || typeof raw.x !== "number" || typeof raw.y !== "number") return null;
  return { x: raw.x, y: raw.y };
}

/** Parse `POST /api/preview/route`; returns null when the body is not a route. */
export function parseRoutePreview(raw: unknown, version: number): RoutePreview | null {
  if (!isRecord(raw) || !Array.isArray(raw.legs)) return null;
  const legs: PreviewLeg[] = [];
  raw.legs.forEach((entry, i) => {
    if (!isRecord(entry)) return;
    const stepId = str(entry.step_id);
    const index = num(entry.index, i);
    let points = posePoints(entry.path);
    if (points.length < 2) {
      const a = endpoint(entry.start);
      const b = endpoint(entry.goal);
      points = a && b ? [a, b] : [];
    }
    if (points.length < 2) return;
    legs.push({
      key: `${stepId}:${index}:${i}`,
      stepId,
      index,
      points,
      planned: entry.planned === true,
      error: str(entry.error),
      lengthM: num(entry.length_m),
    });
  });
  return {
    legs,
    distanceM: num(raw.distance_m),
    estimateS: num(raw.estimate_s),
    speedMps: num(raw.speed_mps, 0.5),
    planned: raw.planned === true,
    note: str(raw.note),
    version,
    at: Date.now(),
  };
}

export function formatDistance(m: number): string {
  if (!Number.isFinite(m)) return "";
  if (m < 10) return `${m.toFixed(1)} m`;
  if (m < 1000) return `${Math.round(m * 10) / 10} m`;
  return `${(m / 1000).toFixed(2)} km`;
}

/** "about 1 min 20 s" — deliberately vague, it is an estimate. */
export function formatEstimate(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "about 0 s";
  const s = Math.round(seconds);
  if (s < 60) return `about ${s} s`;
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  if (m < 60) return `about ${m} min${rest ? ` ${rest} s` : ""}`;
  const hh = Math.floor(m / 60);
  const mm = m - hh * 60;
  return `about ${hh} h${mm ? ` ${mm} min` : ""}`;
}

/** "18.4 m · about 1 min 20 s". */
export function routeSummary(p: RoutePreview): string {
  return `${formatDistance(p.distanceM)} · ${formatEstimate(p.estimateS)}`;
}

// ---- dry run ---------------------------------------------------------------------------

export interface DrySample {
  t: number;
  x: number;
  y: number;
  yaw: number;
  /** Step id this sample belongs to ("" between steps). */
  step: string;
}

export interface DryStep {
  id: string;
  name: string;
  type: string;
  t0: number;
  t1: number;
  status: string;
  error: string;
}

export interface DryOutput {
  kind: string;
  /** Everything else the runner recorded, for the detail line. */
  detail: string;
}

export interface DryRunResult {
  ok: boolean;
  status: string;
  error: string;
  durationS: number;
  distanceM: number;
  samples: DrySample[];
  steps: DryStep[];
  notes: string[];
  outputs: DryOutput[];
  truncated: boolean;
  version: number;
}

function outputOf(raw: unknown): DryOutput | null {
  if (!isRecord(raw)) return null;
  const kind = str(raw.kind) || "output";
  const parts: string[] = [];
  for (const [k, v] of Object.entries(raw)) {
    if (k === "kind") continue;
    parts.push(`${k}=${typeof v === "string" ? v : JSON.stringify(v)}`);
  }
  return { kind, detail: parts.join(" · ") };
}

/** Parse `POST /api/preview/dryrun`; returns null when the body is not a dry run. */
export function parseDryRun(raw: unknown, version: number): DryRunResult | null {
  if (!isRecord(raw) || !Array.isArray(raw.samples)) return null;
  const samples: DrySample[] = [];
  for (const s of raw.samples) {
    if (!isRecord(s) || typeof s.x !== "number" || typeof s.y !== "number") continue;
    samples.push({ t: num(s.t), x: s.x, y: s.y, yaw: num(s.yaw_deg), step: str(s.step) });
  }
  samples.sort((a, b) => a.t - b.t);
  const steps: DryStep[] = [];
  for (const s of Array.isArray(raw.steps) ? raw.steps : []) {
    if (!isRecord(s)) continue;
    const id = str(s.id);
    if (!id) continue;
    steps.push({ id, name: str(s.name) || id, type: str(s.type), t0: num(s.t0), t1: num(s.t1), status: str(s.status), error: str(s.error) });
  }
  const outputs: DryOutput[] = [];
  for (const o of Array.isArray(raw.outputs) ? raw.outputs : []) {
    const parsed = outputOf(o);
    if (parsed) outputs.push(parsed);
  }
  const last = samples.length ? samples[samples.length - 1]!.t : 0;
  return {
    ok: raw.ok === true,
    status: str(raw.status) || (raw.ok === true ? "succeeded" : "failed"),
    error: str(raw.error),
    durationS: Math.max(num(raw.duration_s), last),
    distanceM: num(raw.distance_m),
    samples,
    steps,
    notes: (Array.isArray(raw.notes) ? raw.notes : []).map((n) => (typeof n === "string" ? n : JSON.stringify(n))),
    outputs,
    truncated: raw.truncated === true,
    version,
  };
}

export interface GhostPose {
  x: number;
  y: number;
  yaw: number;
  step: string;
}

/** The ghost robot's pose at time `t`, interpolated between samples. */
export function poseAt(result: DryRunResult, t: number): GhostPose | null {
  const s = result.samples;
  if (!s.length) return null;
  if (t <= s[0]!.t) return { x: s[0]!.x, y: s[0]!.y, yaw: s[0]!.yaw, step: s[0]!.step };
  const last = s[s.length - 1]!;
  if (t >= last.t) return { x: last.x, y: last.y, yaw: last.yaw, step: last.step };
  let lo = 0;
  let hi = s.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (s[mid]!.t <= t) lo = mid;
    else hi = mid;
  }
  const a = s[lo]!;
  const b = s[hi]!;
  const span = b.t - a.t;
  const f = span > 0 ? (t - a.t) / span : 0;
  let dyaw = b.yaw - a.yaw;
  while (dyaw > 180) dyaw -= 360;
  while (dyaw < -180) dyaw += 360;
  return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f, yaw: a.yaw + dyaw * f, step: f < 0.5 ? a.step : b.step };
}

/** The path the ghost has driven up to `t` (the trail behind it). */
export function trailUpTo(result: DryRunResult, t: number): WorldPoint[] {
  const out: WorldPoint[] = [];
  for (const s of result.samples) {
    if (s.t > t) break;
    out.push({ x: s.x, y: s.y });
  }
  const head = poseAt(result, t);
  if (head) out.push({ x: head.x, y: head.y });
  return out;
}

/** Status of every step at time `t`: running now, or how it ended before `t`. */
export function stepStatesAt(result: DryRunResult, t: number): Map<string, { status: string; error: string }> {
  const out = new Map<string, { status: string; error: string }>();
  for (const s of result.steps) {
    if (s.t0 > t) continue;
    if (s.t1 > t) out.set(s.id, { status: "running", error: "" });
    else out.set(s.id, { status: s.status || "succeeded", error: s.error });
  }
  return out;
}
