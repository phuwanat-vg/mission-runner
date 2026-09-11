/**
 * Live map layers from the robot (docs/runner-api.md, "Live map layers"):
 * the subscription is one `{"type": "live", "layers": [...]}` message on the
 * existing WebSocket, and the runner streams `live.*` events back at 1-2 Hz.
 *
 * This class owns *what* is subscribed and holds the latest frame of each
 * layer; the map view owns the pixels and mutates its SVG nodes when
 * `onChange` fires. The chosen layers are remembered in `localStorage`, and
 * the subscription is dropped while the map is hidden or the runner is gone,
 * so an idle robot pays nothing.
 */

import type { RunnerClient, WsEvent } from "../api/RunnerClient";
import type { WorldPoint } from "./geometry";
import { isRecord } from "../model/types";

export const LIVE_LAYERS = ["costmap", "scan", "plan", "footprint"] as const;
export type LiveLayer = (typeof LIVE_LAYERS)[number];

export const LIVE_LABELS: Record<LiveLayer, string> = {
  costmap: "Costmap",
  scan: "Laser scan",
  plan: "Nav2 plan",
  footprint: "Footprint",
};

export const LIVE_HELP: Record<LiveLayer, string> = {
  costmap: "The local costmap the controller sees, tinted over the floor.",
  scan: "The latest laser scan, as dots in the map frame.",
  plan: "The path Nav2 is following right now (not the mission route).",
  footprint: "The robot's real footprint polygon.",
};

const KEY = "mission-editor.map.liveLayers";
/** A frame older than this is stale: the robot stopped publishing. */
const STALE_MS = 8000;

export interface LiveCostmap {
  /** `data:image/png;base64,…` of the greyscale cost image. */
  image: string;
  /** [min_x, min_y, max_x, max_y] in metres. */
  bounds: [number, number, number, number];
  at: number;
}

export interface LivePoints {
  points: WorldPoint[];
  at: number;
}

function isLayer(v: unknown): v is LiveLayer {
  return typeof v === "string" && (LIVE_LAYERS as readonly string[]).includes(v);
}

function readPoints(raw: unknown): WorldPoint[] {
  if (!Array.isArray(raw)) return [];
  const out: WorldPoint[] = [];
  for (const p of raw) {
    if (!Array.isArray(p) || p.length < 2) continue;
    const [x, y] = p as unknown[];
    if (typeof x !== "number" || typeof y !== "number" || !Number.isFinite(x) || !Number.isFinite(y)) continue;
    out.push({ x, y });
  }
  return out;
}

export class LiveLayers {
  #client: RunnerClient;
  #enabled = new Set<LiveLayer>();
  #available: Partial<Record<LiveLayer, boolean>> = {};
  /** True once the runner told us what it can serve (status or live.layers). */
  #known = false;
  #visible = true;
  #sent = "";
  #handlers = new Set<() => void>();

  costmap: LiveCostmap | null = null;
  scan: LivePoints | null = null;
  plan: LivePoints | null = null;
  footprint: LivePoints | null = null;

  constructor(client: RunnerClient) {
    this.#client = client;
    try {
      const raw = localStorage.getItem(KEY);
      const parsed: unknown = raw ? JSON.parse(raw) : [];
      if (Array.isArray(parsed)) for (const v of parsed) if (isLayer(v)) this.#enabled.add(v);
    } catch {
      // storage blocked or corrupt: start with nothing
    }
  }

  onChange(fn: () => void): () => void {
    this.#handlers.add(fn);
    return () => this.#handlers.delete(fn);
  }

  #emit(): void {
    for (const fn of this.#handlers) {
      try {
        fn();
      } catch (err) {
        console.error("live layer listener failed", err);
      }
    }
  }

  get enabled(): ReadonlySet<LiveLayer> {
    return this.#enabled;
  }

  /** True while the runner has not said what it can serve (buttons stay usable). */
  get unknown(): boolean {
    return !this.#known;
  }

  available(layer: LiveLayer): boolean {
    return this.#available[layer] !== false;
  }

  /** Why a layer cannot be chosen, or "" when it can. */
  reason(layer: LiveLayer): string {
    if (!this.#client.connected) return "Runner offline";
    if (!this.#known) return "";
    if (this.#available[layer] === false) return "This robot does not publish it (sim, or the topic is missing)";
    return "";
  }

  /** True when a layer is on but no frame arrived recently. */
  waiting(layer: LiveLayer): boolean {
    if (!this.#enabled.has(layer)) return false;
    const frame = layer === "costmap" ? this.costmap : layer === "scan" ? this.scan : layer === "plan" ? this.plan : this.footprint;
    return !frame || Date.now() - frame.at > STALE_MS;
  }

  /** What `GET /api/status` reports under `live.available`. */
  setAvailability(raw: unknown): void {
    if (!isRecord(raw)) return;
    const next: Partial<Record<LiveLayer, boolean>> = {};
    for (const layer of LIVE_LAYERS) if (typeof raw[layer] === "boolean") next[layer] = raw[layer];
    if (!Object.keys(next).length) return;
    this.#available = next;
    this.#known = true;
    // a layer the robot cannot serve stays remembered but is not subscribed
    this.sync();
    this.#emit();
  }

  set(layer: LiveLayer, on: boolean): void {
    if (on === this.#enabled.has(layer)) return;
    if (on) this.#enabled.add(layer);
    else {
      this.#enabled.delete(layer);
      this.#clear(layer);
    }
    try {
      localStorage.setItem(KEY, JSON.stringify([...this.#enabled]));
    } catch {
      // ignore
    }
    this.sync();
    this.#emit();
  }

  clearAll(): void {
    if (!this.#enabled.size) return;
    for (const layer of [...this.#enabled]) this.set(layer, false);
  }

  /** The map was hidden or shown: unsubscribe while nothing is looking. */
  setVisible(v: boolean): void {
    if (this.#visible === v) return;
    this.#visible = v;
    this.sync();
  }

  #clear(layer: LiveLayer): void {
    if (layer === "costmap") this.costmap = null;
    else if (layer === "scan") this.scan = null;
    else if (layer === "plan") this.plan = null;
    else this.footprint = null;
  }

  /** Send the subscription if it changed (cheap to call on every event). */
  sync(): void {
    const wanted = this.#visible && this.#client.connected ? [...this.#enabled].filter((l) => this.available(l)).sort() : [];
    const key = wanted.join(",");
    if (key === this.#sent) return;
    if (!this.#client.connected) {
      // the socket is gone; re-send on the next connect
      this.#sent = "";
      return;
    }
    if (this.#client.send({ type: "live", layers: wanted })) this.#sent = key;
  }

  /** The socket came back (or went away): re-subscribe from scratch. */
  onConnection(connected: boolean): void {
    this.#sent = "";
    if (!connected) {
      this.costmap = null;
      this.scan = null;
      this.plan = null;
      this.footprint = null;
      this.#emit();
      return;
    }
    this.sync();
  }

  /** Feed a WebSocket event. Returns true when it was a live-layer event. */
  handleEvent(ev: WsEvent): boolean {
    if (ev.type === "live.layers") {
      this.setAvailability(ev.available);
      return true;
    }
    if (!ev.type.startsWith("live.")) return false;
    const layer = ev.type.slice(5);
    if (!isLayer(layer)) return true;
    const at = Date.now();
    if (layer === "costmap") {
      const png = typeof ev.png === "string" ? ev.png : "";
      const bounds = Array.isArray(ev.bounds) && ev.bounds.length === 4 && ev.bounds.every((n) => typeof n === "number") ? (ev.bounds as [number, number, number, number]) : null;
      if (!png || !bounds) return true;
      this.costmap = { image: `data:image/png;base64,${png}`, bounds, at };
    } else {
      const points = readPoints(ev.points);
      const frame: LivePoints = { points, at };
      if (layer === "scan") this.scan = frame;
      else if (layer === "plan") this.plan = frame;
      else this.footprint = frame;
    }
    this.#emit();
    return true;
  }
}
