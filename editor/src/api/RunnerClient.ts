/**
 * REST + WebSocket client for mission_runner (docs/runner-api.md). The
 * WebSocket reconnects with backoff; `connected` reflects the socket state and
 * is what the UI uses for "offline".
 */

import type { Mission, Path, SitesDoc } from "../model/types";
import type { CapabilityInfo } from "../model/validate";

export type RunStatus = "queued" | "running" | "paused" | "suspended" | "succeeded" | "failed" | "canceled";

export interface RobotState {
  x: number;
  y: number;
  yaw_deg: number;
  frame?: string;
  battery?: number | null;
  nav_active?: boolean;
}

export interface RunSource {
  kind: "trigger" | "manual" | "interrupt" | "resume" | "sub" | string;
  id?: string;
  detail?: string;
}

export interface StepRef {
  id: string;
  name?: string;
  path?: Path;
  started_at?: string;
}

export interface Feedback {
  distance_remaining?: number;
  recoveries?: number;
  eta_s?: number;
  [k: string]: unknown;
}

export interface Run {
  id: string;
  mission: string;
  inputs?: Record<string, unknown>;
  source?: RunSource;
  priority?: number;
  status: RunStatus;
  started_at?: string | null;
  finished_at?: string | null;
  step?: StepRef | null;
  error?: string;
  feedback?: Feedback | null;
}

export interface Prompt {
  id: string;
  run_id?: string;
  mission?: string;
  text: string;
  options: string[];
  default?: string | null;
  expires_at?: string | null;
}

export interface RunnerInfo {
  version?: string;
  uptime_s?: number;
  backend?: string;
  home?: string;
}

export interface ConnectorState {
  type: string;
  connected?: boolean;
  config?: Record<string, unknown>;
}

export interface RunnerStatus {
  runner?: RunnerInfo;
  state: "idle" | "running" | "paused";
  run: Run | null;
  queue?: Run[];
  suspended?: Run[];
  robot?: RobotState | null;
  current_map?: string | null;
  connectors?: Record<string, ConnectorState>;
  prompt?: Prompt | null;
  /** What this robot can stream on the map, and what it streams right now. */
  live?: { available?: Record<string, boolean>; layers?: string[] };
}

export interface MissionSummary {
  name: string;
  title?: string;
  description?: string;
  version?: number;
  updated_at?: string;
  triggers?: string[];
  sha256?: string;
  /** Added by GET /api/missions: dispatcher state of the mission. */
  state?: "running" | "queued" | "suspended" | "idle";
  trigger_problems?: string[];
  errors?: ApiFinding[];
}

export interface RunEvent {
  t?: string;
  type: string;
  step_id?: string;
  path?: Path;
  data?: unknown;
}

export interface RunDetail extends Run {
  events?: RunEvent[];
}

export interface Capabilities {
  backend?: string;
  steps?: Record<string, CapabilityInfo>;
  triggers?: Record<string, CapabilityInfo>;
  connectors?: string[];
  ros_distro?: string;
}

export interface StepResult {
  ok: boolean;
  status: "succeeded" | "failed" | "canceled" | "timeout" | string;
  value?: unknown;
  error?: string;
  duration_s?: number;
}

/** One WebSocket message. Fields depend on `type` (see runner-api.md). */
export interface WsEvent {
  type: string;
  run?: Run;
  run_id?: string;
  step_id?: string;
  path?: Path;
  name?: string;
  result?: StepResult;
  feedback?: Feedback;
  prompt?: Prompt;
  answer?: string;
  names?: string[];
  t?: string;
  level?: string;
  text?: string;
  [k: string]: unknown;
}

export interface RunRequest {
  inputs?: Record<string, unknown>;
  policy?: string;
  priority?: number;
}

export interface RunAccepted {
  accepted: boolean;
  run_id?: string;
  reason?: string;
}

export interface ApiFinding {
  path?: Path;
  message: string;
}

/** `GET /api/maps/{name}`: where the floor image sits in world coordinates. */
export interface MapMeta {
  name: string;
  frame?: string;
  file?: string;
  width: number;
  height: number;
  /** Metres per pixel. */
  resolution: number;
  origin: { x: number; y: number; yaw_deg: number };
  /** [min_x, min_y, max_x, max_y] in metres. */
  bounds: [number, number, number, number];
  /** The map file it was read from, or "synthetic" for the plain room. */
  source: string;
  image_url?: string;
}

/** Body of `POST /api/preview/route` and `POST /api/preview/dryrun`. */
export interface PreviewRequest {
  /** A saved mission by name... */
  name?: string;
  /** ...or the open (possibly unsaved) document. */
  mission?: Mission;
  inputs?: Record<string, unknown>;
  start?: { x: number; y: number; yaw_deg?: number };
  planner_id?: string;
  time_scale?: number;
  max_wall_s?: number;
}

/** One mask written by `POST /api/maps/{name}/filters`. */
export interface FilterMask {
  kind: string;
  yaml: string;
  image: string;
  zones: string;
}

export interface FilterResult {
  ok: boolean;
  masks: FilterMask[];
  directory: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly errors: ApiFinding[];
  constructor(message: string, status: number, errors: ApiFinding[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.errors = errors;
  }
}

export const RUNNER_URL_KEY = "mission-editor.runnerUrl";
const DEV_PORTS = new Set(["5173", "5174", "1420", "1421", "4173"]);

/** Origin when served by the runner, else http://localhost:8080; overridable in localStorage. */
export function defaultRunnerUrl(): string {
  try {
    const stored = localStorage.getItem(RUNNER_URL_KEY);
    if (stored) return stored;
  } catch {
    // storage blocked
  }
  const loc = window.location;
  if (loc.protocol.startsWith("http") && !DEV_PORTS.has(loc.port)) return loc.origin;
  return "http://localhost:8080";
}

export function saveRunnerUrl(url: string | null): void {
  try {
    if (url) localStorage.setItem(RUNNER_URL_KEY, url);
    else localStorage.removeItem(RUNNER_URL_KEY);
  } catch {
    // ignore
  }
}

type ConnHandler = (connected: boolean) => void;
type EventHandler = (ev: WsEvent) => void;

export class RunnerClient {
  #baseUrl: string;
  #ws: WebSocket | null = null;
  #connected = false;
  #closing = false;
  #retryMs = 1000;
  #retryTimer: ReturnType<typeof setTimeout> | null = null;
  #pingTimer: ReturnType<typeof setInterval> | null = null;
  #connHandlers = new Set<ConnHandler>();
  #eventHandlers = new Set<EventHandler>();

  constructor(baseUrl: string) {
    this.#baseUrl = baseUrl.replace(/\/+$/, "");
  }

  get baseUrl(): string {
    return this.#baseUrl;
  }

  get connected(): boolean {
    return this.#connected;
  }

  setBaseUrl(url: string): void {
    const clean = url.replace(/\/+$/, "");
    if (clean === this.#baseUrl) return;
    this.#baseUrl = clean;
    this.#retryMs = 1000;
    if (this.#ws || this.#retryTimer) {
      this.disconnect();
      this.connect();
    }
  }

  onConnection(h: ConnHandler): () => void {
    this.#connHandlers.add(h);
    return () => this.#connHandlers.delete(h);
  }

  onEvent(h: EventHandler): () => void {
    this.#eventHandlers.add(h);
    return () => this.#eventHandlers.delete(h);
  }

  // ---- WebSocket --------------------------------------------------------------

  connect(): void {
    this.#closing = false;
    if (this.#ws) return;
    if (this.#retryTimer) {
      clearTimeout(this.#retryTimer);
      this.#retryTimer = null;
    }
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.#wsUrl());
    } catch {
      this.#scheduleReconnect();
      return;
    }
    this.#ws = ws;
    ws.addEventListener("open", () => {
      if (this.#ws !== ws) return;
      this.#retryMs = 1000;
      this.#setConnected(true);
      this.#pingTimer = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
      }, 20_000);
    });
    ws.addEventListener("message", (e) => {
      if (this.#ws !== ws) return;
      let data: unknown;
      try {
        data = JSON.parse(String(e.data));
      } catch {
        return;
      }
      if (typeof data !== "object" || data === null || typeof (data as WsEvent).type !== "string") return;
      const ev = data as WsEvent;
      if (ev.type === "pong") return;
      for (const h of this.#eventHandlers) {
        try {
          h(ev);
        } catch (err) {
          console.error("event handler failed", err);
        }
      }
    });
    const onClose = (): void => {
      if (this.#ws !== ws) return;
      this.#ws = null;
      if (this.#pingTimer) {
        clearInterval(this.#pingTimer);
        this.#pingTimer = null;
      }
      this.#setConnected(false);
      if (!this.#closing) this.#scheduleReconnect();
    };
    ws.addEventListener("close", onClose);
    ws.addEventListener("error", () => {
      // "close" follows; nothing else to do
    });
  }

  /** Send a JSON message on the events socket. False when it is not open. */
  send(message: unknown): boolean {
    const ws = this.#ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify(message));
      return true;
    } catch {
      return false;
    }
  }

  disconnect(): void {
    this.#closing = true;
    if (this.#retryTimer) {
      clearTimeout(this.#retryTimer);
      this.#retryTimer = null;
    }
    const ws = this.#ws;
    this.#ws = null;
    if (this.#pingTimer) {
      clearInterval(this.#pingTimer);
      this.#pingTimer = null;
    }
    if (ws) {
      try {
        ws.close();
      } catch {
        // ignore
      }
    }
    this.#setConnected(false);
  }

  #scheduleReconnect(): void {
    if (this.#closing || this.#retryTimer) return;
    const delay = this.#retryMs;
    this.#retryMs = Math.min(this.#retryMs * 2, 15_000);
    this.#retryTimer = setTimeout(() => {
      this.#retryTimer = null;
      this.connect();
    }, delay);
  }

  #setConnected(v: boolean): void {
    if (this.#connected === v) return;
    this.#connected = v;
    for (const h of this.#connHandlers) h(v);
  }

  #wsUrl(): string {
    const u = new URL(this.#baseUrl);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    u.pathname = `${u.pathname.replace(/\/+$/, "")}/api/events`;
    u.search = "";
    return u.toString();
  }

  // ---- HTTP ------------------------------------------------------------------------

  async #request<T>(method: string, path: string, body?: unknown, timeoutMs = 10_000): Promise<T> {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    let res: Response;
    try {
      res = await fetch(`${this.#baseUrl}${path}`, {
        method,
        headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: ctl.signal,
      });
    } catch (err) {
      throw new ApiError(err instanceof Error && err.name === "AbortError" ? "runner did not answer in time" : "runner unreachable", 0);
    } finally {
      clearTimeout(timer);
    }
    const text = await res.text();
    let data: unknown = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { error: text };
      }
    }
    if (!res.ok) {
      const d = (typeof data === "object" && data !== null ? data : {}) as { error?: string; errors?: ApiFinding[] };
      throw new ApiError(d.error ?? `${res.status} ${res.statusText}`, res.status, Array.isArray(d.errors) ? d.errors : []);
    }
    return data as T;
  }

  getStatus(): Promise<RunnerStatus> {
    return this.#request("GET", "/api/status");
  }
  listMissions(): Promise<MissionSummary[]> {
    return this.#request("GET", "/api/missions");
  }
  getMission(name: string): Promise<Mission> {
    return this.#request("GET", `/api/missions/${encodeURIComponent(name)}`);
  }
  putMission(name: string, doc: Mission): Promise<{ ok: boolean; sha256?: string; version?: number; warnings?: ApiFinding[] }> {
    return this.#request("PUT", `/api/missions/${encodeURIComponent(name)}`, doc);
  }
  deleteMission(name: string): Promise<unknown> {
    return this.#request("DELETE", `/api/missions/${encodeURIComponent(name)}`);
  }
  validateMission(doc: Mission): Promise<{ ok: boolean; errors?: ApiFinding[]; warnings?: ApiFinding[] }> {
    return this.#request("POST", "/api/missions/validate", doc);
  }
  runMission(name: string, req: RunRequest = {}): Promise<RunAccepted> {
    return this.#request("POST", `/api/missions/${encodeURIComponent(name)}/run`, req);
  }
  cancelRun(id: string): Promise<unknown> {
    return this.#request("POST", `/api/runs/${encodeURIComponent(id)}/cancel`, {});
  }
  pauseRun(id: string): Promise<unknown> {
    return this.#request("POST", `/api/runs/${encodeURIComponent(id)}/pause`, {});
  }
  resumeRun(id: string): Promise<unknown> {
    return this.#request("POST", `/api/runs/${encodeURIComponent(id)}/resume`, {});
  }
  stop(): Promise<unknown> {
    return this.#request("POST", "/api/stop", {}, 5000);
  }
  listRuns(limit = 50, mission?: string): Promise<Run[]> {
    const q = new URLSearchParams({ limit: String(limit) });
    if (mission) q.set("mission", mission);
    return this.#request("GET", `/api/runs?${q.toString()}`);
  }
  getRun(id: string): Promise<RunDetail> {
    return this.#request("GET", `/api/runs/${encodeURIComponent(id)}`);
  }
  getPrompt(): Promise<Prompt | null> {
    return this.#request("GET", "/api/prompt");
  }
  answerPrompt(id: string, answer: string): Promise<unknown> {
    return this.#request("POST", `/api/prompt/${encodeURIComponent(id)}/answer`, { answer });
  }
  getSites(): Promise<SitesDoc> {
    return this.#request("GET", "/api/sites");
  }
  putSites(doc: SitesDoc): Promise<unknown> {
    return this.#request("PUT", "/api/sites", doc);
  }
  getRobotPose(): Promise<RobotState> {
    return this.#request("GET", "/api/robot/pose");
  }
  getMap(name: string): Promise<MapMeta> {
    return this.#request("GET", `/api/maps/${encodeURIComponent(name)}`);
  }
  /** The floor PNG as a data: URL, so it can be cached in localStorage and drawn offline. */
  async getMapImage(name: string, timeoutMs = 15_000): Promise<string> {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    let res: Response;
    try {
      res = await fetch(`${this.#baseUrl}/api/maps/${encodeURIComponent(name)}/image`, { signal: ctl.signal });
    } catch (err) {
      throw new ApiError(err instanceof Error && err.name === "AbortError" ? "runner did not answer in time" : "runner unreachable", 0);
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) throw new ApiError(`${res.status} ${res.statusText}`, res.status);
    const blob = await res.blob();
    return await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new ApiError("the map image could not be read", 0));
      reader.readAsDataURL(blob);
    });
  }
  /** Plan every leg of a mission without running it (10 s is plenty). */
  previewRoute(req: PreviewRequest): Promise<Record<string, unknown>> {
    return this.#request("POST", "/api/preview/route", req, 20_000);
  }
  /** Replay the whole flow against a private simulated robot. */
  previewDryRun(req: PreviewRequest): Promise<Record<string, unknown>> {
    return this.#request("POST", "/api/preview/dryrun", req, 90_000);
  }
  /** Write Nav2 costmap filter masks for a map's zones. */
  exportFilters(name: string, opts: { resolution?: number; max_speed_mps?: number } = {}): Promise<FilterResult> {
    return this.#request("POST", `/api/maps/${encodeURIComponent(name)}/filters`, opts, 30_000);
  }
  getConnectors(): Promise<Record<string, ConnectorState>> {
    return this.#request("GET", "/api/connectors");
  }
  getCapabilities(): Promise<Capabilities> {
    return this.#request("GET", "/api/capabilities");
  }
  getExamples(): Promise<Mission[]> {
    return this.#request("GET", "/api/examples");
  }
}
