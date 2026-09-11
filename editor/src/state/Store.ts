/**
 * Application state: the open mission, selection, validation, undo/redo
 * (snapshot stack, 50 deep), drafts in localStorage, the deployed copy for
 * dirty tracking, and the runner-side state the UI mirrors (status, mission
 * list, sites, connectors, capabilities, live run marks).
 *
 * Panels subscribe to topics and re-render; emits are coalesced per microtask
 * so one user action causes one re-render per panel.
 */

import type { Finding, MapDef, Mission, Path, Site, SitesDoc, Step, Trigger, ValidationResult, Zone } from "../model/types";
import { emptySites, isRecord } from "../model/types";
import { assignIds, allStepIds, canonicalJson, cloneStepWithFreshIds, deepClone, ensureList, findStep, getList, getStepAt, pathWithin, walkSteps } from "../model/ids";
import { validate } from "../model/validate";
import type { ValidateContext } from "../model/validate";
import type { ApiFinding, Capabilities, ConnectorState, Feedback, MissionSummary, RobotState, RunnerStatus, WsEvent } from "../api/RunnerClient";
import type { MapModel } from "../map/geometry";
import { buildMapModel, emptyMapModel } from "../map/geometry";
import type { DryRunResult, RoutePreview } from "../map/preview";
import { sha256Hex } from "./sha256";

export type Selection =
  | { kind: "step"; id: string }
  | { kind: "trigger"; index: number }
  | { kind: "interrupt"; index: number }
  | { kind: "site"; name: string }
  | { kind: "zone"; name: string }
  | null;

export type Topic = "mission" | "selection" | "validation" | "remote" | "connection" | "runmarks" | "drafts" | "sites" | "robot" | "preview";

export interface RunMark {
  status: "running" | "succeeded" | "failed" | "canceled" | "timeout";
  startedAt: number;
  duration?: number;
  feedback?: Feedback | null;
  error?: string;
}

export interface Draft {
  name: string;
  mission: Mission;
  /** Runner sha256 the draft was taken from (null for missions not on the robot). */
  baseSha: string | null;
  savedAt: string;
}

export type DeployState = "up to date" | "modified" | "not on robot";

interface Snapshot {
  json: string;
  selection: Selection;
}

export const DRAFTS_KEY = "mission-editor.drafts.v1";
const MAX_UNDO = 50;
const COALESCE_MS = 1200;
const GENERATED_ID_RE = /^[sti]_[0-9a-z]{6}$/;

/**
 * Canonical JSON with editor-generated ids removed, so a runner copy whose
 * steps had no ids compares equal to the same document after `assignIds`.
 */
export function comparableJson(mission: Mission): string {
  const doc = deepClone(mission);
  for (const v of walkSteps(doc)) if (typeof v.step.id === "string" && GENERATED_ID_RE.test(v.step.id)) delete v.step.id;
  for (const t of [...(doc.triggers ?? []), ...(doc.interrupts ?? [])]) if (typeof t.id === "string" && GENERATED_ID_RE.test(t.id)) delete t.id;
  return canonicalJson(doc);
}

export function selectionKey(sel: Selection): string {
  if (!sel) return "";
  if (sel.kind === "step") return `step:${sel.id}`;
  if (sel.kind === "site") return `site:${sel.name}`;
  if (sel.kind === "zone") return `zone:${sel.name}`;
  return `${sel.kind}:${sel.index}`;
}

/** Where the dry run's playhead is; the map draws the ghost from it. */
export interface DryRunPlayback {
  result: DryRunResult;
  /** Seconds into the recorded timeline. */
  t: number;
  playing: boolean;
  /** Playback rate: 1, 4 or 16. */
  speed: number;
}

export class Store {
  #mission: Mission | null = null;
  /** Name of the runner copy this document was opened from (null = never deployed). */
  #baseName: string | null = null;
  #baseSha: string | null = null;
  /** The runner copy as fetched (for "discard changes"). */
  #baseDoc: Mission | null = null;
  /** comparableJson of the runner copy (dirty tracking). */
  #baseComparable: string | null = null;
  #selection: Selection = null;
  #validation: ValidationResult = { errors: [], warnings: [] };
  #serverErrors: Finding[] = [];
  #undo: Snapshot[] = [];
  #redo: Snapshot[] = [];
  #lastCoalesce: { key: string; at: number } | null = null;
  #version = 0;
  #listeners = new Map<Topic, Set<() => void>>();
  #pending = new Set<Topic>();
  #flushScheduled = false;
  #drafts: Record<string, Draft> = {};
  #draftTimer: ReturnType<typeof setTimeout> | null = null;
  #remoteChanged = false;
  #activeMap: string | null = null;
  #missionsKnown = false;
  #connectorsKnown = false;
  /** Bumped whenever the sites document or the active map changes. */
  #sitesVersion = 0;
  #mapModel: MapModel | null = null;
  #mapModelKey = "";

  // ---- runner-side state (read by panels) ----
  connected = false;
  status: RunnerStatus | null = null;
  missions: MissionSummary[] = [];
  sites: SitesDoc | null = null;
  /** Canonical JSON of the runner's sites.json (dirty tracking of the sites panel). */
  sitesBase: string | null = null;
  connectors: Record<string, ConnectorState> = {};
  capabilities: Capabilities | null = null;
  examples: Mission[] = [];
  runMarks = new Map<string, RunMark>();
  /** Id of the run whose marks are shown, or null. */
  markedRunId: string | null = null;
  /** The planned route from POST /api/preview/route, or null. */
  routePreview: RoutePreview | null = null;
  /** The loaded dry run and its playhead, or null. */
  dryRun: DryRunPlayback | null = null;
  /** True while a preview request is in flight (buttons show a spinner). */
  previewBusy: "" | "route" | "dryrun" = "";

  constructor() {
    this.#loadDrafts();
  }

  // ---- subscriptions -------------------------------------------------------------

  on(topic: Topic, fn: () => void): () => void {
    let set = this.#listeners.get(topic);
    if (!set) {
      set = new Set();
      this.#listeners.set(topic, set);
    }
    set.add(fn);
    return () => set!.delete(fn);
  }

  #emit(...topics: Topic[]): void {
    for (const t of topics) this.#pending.add(t);
    if (this.#flushScheduled) return;
    this.#flushScheduled = true;
    queueMicrotask(() => {
      this.#flushScheduled = false;
      const topics = [...this.#pending];
      this.#pending.clear();
      for (const t of topics) {
        for (const fn of this.#listeners.get(t) ?? []) {
          try {
            fn();
          } catch (err) {
            console.error(`listener for '${t}' failed`, err);
          }
        }
      }
    });
  }

  // ---- getters ---------------------------------------------------------------------

  get mission(): Mission | null {
    return this.#mission;
  }
  get selection(): Selection {
    return this.#selection;
  }
  get validation(): ValidationResult {
    return this.#validation;
  }
  get version(): number {
    return this.#version;
  }
  get canUndo(): boolean {
    return this.#undo.length > 0;
  }
  get canRedo(): boolean {
    return this.#redo.length > 0;
  }
  get baseName(): string | null {
    return this.#baseName;
  }
  get baseSha(): string | null {
    return this.#baseSha;
  }
  get remoteChanged(): boolean {
    return this.#remoteChanged;
  }
  get hasErrors(): boolean {
    return this.#validation.errors.length > 0;
  }

  /** True when the document differs from the runner copy it was opened from. */
  get dirty(): boolean {
    if (!this.#mission) return false;
    if (this.#baseComparable === null) return true;
    return comparableJson(this.#mission) !== this.#baseComparable || this.#baseName !== this.#mission.name;
  }

  /** Runner summary of the mission with the document's name, if any. */
  get runnerCopy(): MissionSummary | null {
    const m = this.#mission;
    if (!m) return null;
    return this.missions.find((x) => x.name === m.name) ?? null;
  }

  get deployState(): DeployState {
    if (!this.#mission) return "not on robot";
    const onRobot = this.#missionsKnown ? this.runnerCopy !== null : this.#baseName === this.#mission.name;
    if (!onRobot) return "not on robot";
    return this.dirty ? "modified" : "up to date";
  }

  get activeMap(): string | null {
    if (this.#activeMap && this.sites?.maps[this.#activeMap]) return this.#activeMap;
    const cur = this.status?.current_map;
    if (cur && this.sites?.maps[cur]) return cur;
    return this.sites?.default_map ?? Object.keys(this.sites?.maps ?? {})[0] ?? null;
  }

  set activeMap(name: string | null) {
    this.#activeMap = name;
    this.#sitesVersion++;
    this.#revalidate();
    this.#emit("sites");
  }

  /** Sites of the active map (empty when there is no map or no sites document). */
  get activeSites(): Record<string, Site> {
    const map = this.activeMap;
    return (map ? this.sites?.maps[map]?.sites : undefined) ?? {};
  }

  /** Where every step of the open mission sits on the floor; rebuilt only when the mission or the sites change. */
  get mapModel(): MapModel {
    const key = `${this.#version}:${this.#sitesVersion}:${this.activeMap ?? ""}`;
    if (this.#mapModel && this.#mapModelKey === key) return this.#mapModel;
    this.#mapModel = this.#mission ? buildMapModel(this.#mission, this.activeSites) : emptyMapModel();
    this.#mapModelKey = key;
    return this.#mapModel;
  }

  /** Zones of the active map (empty when there is no map or no sites document). */
  get activeZones(): Record<string, Zone> {
    const map = this.activeMap;
    return (map ? this.sites?.maps[map]?.zones : undefined) ?? {};
  }

  /**
   * Change the active map's entry in sites.json (creating the document and a
   * "default" map when there is none). Site and zone edits are not part of the
   * mission, so they are not undoable; they mark the sites dirty until saved.
   */
  mutateMapDef(fn: (mapDef: MapDef) => void): void {
    const doc = this.sites ? deepClone(this.sites) : emptySites();
    let map = this.activeMap;
    if (!map) {
      map = "default";
      doc.maps[map] = { sites: {} };
      doc.default_map = map;
    }
    const mapDef = (doc.maps[map] ??= {});
    fn(mapDef);
    this.setSites(doc, false);
    if (!this.activeMap) this.activeMap = map;
  }

  mutateSites(fn: (sites: Record<string, Site>) => void): void {
    this.mutateMapDef((mapDef) => fn((mapDef.sites ??= {})));
  }

  /** Change the zone table of the active map; empties are tidied away. */
  mutateZones(fn: (zones: Record<string, Zone>) => void): void {
    this.mutateMapDef((mapDef) => {
      const table = (mapDef.zones ??= {});
      fn(table);
      if (Object.keys(table).length === 0) delete mapDef.zones;
    });
  }

  /** The selected site (with its name) or null. */
  selectedSite(): { name: string; site: Site } | null {
    const sel = this.#selection;
    if (sel?.kind !== "site") return null;
    const site = this.activeSites[sel.name];
    return site ? { name: sel.name, site } : null;
  }

  /** The selected zone (with its name) or null. */
  selectedZone(): { name: string; zone: Zone } | null {
    const sel = this.#selection;
    if (sel?.kind !== "zone") return null;
    const zone = this.activeZones[sel.name];
    return zone ? { name: sel.name, zone } : null;
  }

  /** Names of missions known on the runner, or null when the list was never loaded. */
  get missionNames(): string[] | null {
    return this.#missionsKnown ? this.missions.map((m) => m.name) : null;
  }

  get drafts(): Draft[] {
    return Object.values(this.#drafts).sort((a, b) => a.name.localeCompare(b.name));
  }

  draft(name: string): Draft | null {
    return this.#drafts[name] ?? null;
  }

  /** Active run of the open mission (running, paused or suspended), if any. */
  get currentRun(): RunnerStatus["run"] {
    const run = this.status?.run;
    if (run && this.#mission && run.mission === this.#mission.name) return run;
    return null;
  }

  // ---- document lifecycle ---------------------------------------------------------------

  /**
   * Open a document. `base` is the runner copy it was opened from (for dirty
   * tracking and the "Robot copy changed" banner); omit it for new drafts.
   */
  open(mission: Mission, base?: { name: string; sha: string | null; doc: Mission } | null): void {
    this.#flushDraft();
    const doc = deepClone(mission);
    assignIds(doc);
    this.#mission = doc;
    this.#baseName = base ? base.name : null;
    this.#baseSha = base ? base.sha : null;
    this.#baseDoc = base ? deepClone(base.doc) : null;
    this.#baseComparable = base ? comparableJson(base.doc) : null;
    this.#undo = [];
    this.#redo = [];
    this.#lastCoalesce = null;
    this.#selection = null;
    this.#serverErrors = [];
    this.#remoteChanged = false;
    this.runMarks.clear();
    this.markedRunId = null;
    this.clearPreviews();
    this.#version++;
    this.#revalidate();
    this.#emit("mission", "selection", "runmarks");
    this.#checkRemoteChanged();
  }

  close(): void {
    this.#flushDraft();
    this.#mission = null;
    this.#baseName = null;
    this.#baseSha = null;
    this.#baseDoc = null;
    this.#baseComparable = null;
    this.#undo = [];
    this.#redo = [];
    this.#selection = null;
    this.#serverErrors = [];
    this.#validation = { errors: [], warnings: [] };
    this.runMarks.clear();
    this.markedRunId = null;
    this.clearPreviews();
    this.#version++;
    this.#emit("mission", "selection", "validation", "runmarks");
  }

  /**
   * Attach the runner copy to a document that was opened without one (the
   * editor started offline or before the mission list arrived). Keeps the
   * draft's remembered sha so "Robot copy changed" still triggers.
   */
  attachBase(name: string, sha: string | null, doc: Mission): void {
    const m = this.#mission;
    if (!m || m.name !== name || this.#baseName !== null) return;
    this.#baseName = name;
    this.#baseSha = this.#drafts[name]?.baseSha ?? sha;
    this.#baseDoc = deepClone(doc);
    this.#baseComparable = comparableJson(doc);
    this.#scheduleDraft();
    this.#revalidate();
    this.#emit("mission");
    this.#checkRemoteChanged();
  }

  /** Record a successful deploy: the document is now the runner copy. */
  markDeployed(sha: string | null): void {
    if (!this.#mission) return;
    this.#baseName = this.#mission.name;
    this.#baseSha = sha;
    this.#baseDoc = deepClone(this.#mission);
    this.#baseComparable = comparableJson(this.#mission);
    this.#serverErrors = [];
    this.#remoteChanged = false;
    this.#deleteDraft(this.#mission.name);
    this.#revalidate();
    this.#emit("mission", "drafts");
  }

  /** Server-side errors from PUT (shown as badges until the next edit). */
  setServerErrors(errors: ApiFinding[]): void {
    this.#serverErrors = errors.map((e) => {
      const path = Array.isArray(e.path) ? e.path : [];
      const stepId = this.#stepIdForPath(path);
      return stepId ? { level: "error", path, stepId, message: e.message } : { level: "error", path, message: e.message };
    });
    this.#revalidate();
  }

  #stepIdForPath(path: Path): string | undefined {
    if (!this.#mission) return undefined;
    for (let n = path.length; n > 0; n--) {
      const step = getStepAt(this.#mission, path.slice(0, n));
      if (step && typeof step.id === "string") return step.id;
    }
    return undefined;
  }

  /** Called when the missions list was refreshed. */
  #checkRemoteChanged(): void {
    const m = this.#mission;
    if (!m || this.#baseName === null) {
      this.#remoteChanged = false;
      return;
    }
    const remote = this.missions.find((x) => x.name === this.#baseName);
    const changed = !!remote && !!remote.sha256 && !!this.#baseSha && remote.sha256 !== this.#baseSha;
    if (changed !== this.#remoteChanged) {
      this.#remoteChanged = changed;
      this.#emit("mission");
    }
  }

  /** "Keep mine": accept the new runner sha without loading it. */
  acknowledgeRemote(): void {
    const remote = this.missions.find((x) => x.name === this.#baseName);
    if (remote?.sha256) this.#baseSha = remote.sha256;
    this.#remoteChanged = false;
    this.#emit("mission");
  }

  // ---- mutations ------------------------------------------------------------------------------

  /**
   * Apply an undoable mutation. `coalesce` merges consecutive edits with the
   * same key (typing in one field) into one undo step.
   */
  update(mutate: (m: Mission) => void, opts: { coalesce?: string; select?: Selection } = {}): void {
    const m = this.#mission;
    if (!m) return;
    const now = Date.now();
    const coalesce = opts.coalesce !== undefined && this.#lastCoalesce !== null && this.#lastCoalesce.key === opts.coalesce && now - this.#lastCoalesce.at < COALESCE_MS;
    if (!coalesce) {
      this.#undo.push({ json: JSON.stringify(m), selection: this.#selection });
      if (this.#undo.length > MAX_UNDO) this.#undo.shift();
    }
    this.#redo = [];
    this.#lastCoalesce = opts.coalesce !== undefined ? { key: opts.coalesce, at: now } : null;
    mutate(m);
    this.#afterChange();
    if (opts.select !== undefined) this.select(opts.select);
  }

  /** Replace the whole document (JSON tab "Apply", import). */
  replaceMission(mission: Mission): void {
    const doc = deepClone(mission);
    assignIds(doc);
    this.update((m) => {
      for (const k of Object.keys(m)) delete (m as unknown as Record<string, unknown>)[k];
      Object.assign(m, doc);
    });
    if (this.#selection?.kind === "step" && !findStep(doc, this.#selection.id)) this.select(null);
    else if (this.#selection?.kind === "trigger" || this.#selection?.kind === "interrupt") {
      const list = this.#selection.kind === "trigger" ? doc.triggers : doc.interrupts;
      if (!list || this.#selection.index >= list.length) this.select(null);
    }
  }

  #afterChange(): void {
    this.#version++;
    this.#serverErrors = [];
    // a preview describes the document it was computed for; editing invalidates it
    if (this.routePreview || this.dryRun) this.clearPreviews();
    if (this.markedRunId && !this.currentRun) {
      this.runMarks.clear();
      this.markedRunId = null;
      this.#emit("runmarks");
    }
    this.#revalidate();
    this.#scheduleDraft();
    this.#emit("mission");
  }

  undo(): boolean {
    const m = this.#mission;
    const snap = this.#undo.pop();
    if (!m || !snap) return false;
    this.#redo.push({ json: JSON.stringify(m), selection: this.#selection });
    this.#restore(snap);
    return true;
  }

  redo(): boolean {
    const m = this.#mission;
    const snap = this.#redo.pop();
    if (!m || !snap) return false;
    this.#undo.push({ json: JSON.stringify(m), selection: this.#selection });
    this.#restore(snap);
    return true;
  }

  #restore(snap: Snapshot): void {
    this.#mission = JSON.parse(snap.json) as Mission;
    this.#lastCoalesce = null;
    this.#afterChange();
    this.select(snap.selection);
  }

  select(sel: Selection): void {
    if (selectionKey(sel) === selectionKey(this.#selection)) return;
    this.#selection = sel;
    this.#emit("selection");
  }

  /** The selected step (with its path) or null. */
  selectedStep(): { step: Step; path: Path } | null {
    if (!this.#mission || this.#selection?.kind !== "step") return null;
    const v = findStep(this.#mission, this.#selection.id);
    return v ? { step: v.step, path: v.path } : null;
  }

  selectedTrigger(): { trigger: Trigger; kind: "trigger" | "interrupt"; index: number } | null {
    const m = this.#mission;
    const sel = this.#selection;
    if (!m || !sel || (sel.kind !== "trigger" && sel.kind !== "interrupt")) return null;
    const list = sel.kind === "trigger" ? m.triggers : m.interrupts;
    const trigger = list?.[sel.index];
    return trigger ? { trigger, kind: sel.kind, index: sel.index } : null;
  }

  stepPath(id: string): Path | null {
    if (!this.#mission) return null;
    return findStep(this.#mission, id)?.path ?? null;
  }

  // ---- step operations ----------------------------------------------------------------------

  /** Insert a step (fresh ids assigned) into a list; returns its id. */
  insertStep(step: Step, listPath: Path, index: number): string | null {
    const m = this.#mission;
    if (!m) return null;
    const copy = cloneStepWithFreshIds(step, allStepIds(m));
    if (typeof step.id === "string" && !allStepIds(m).has(step.id)) copy.id = step.id;
    let inserted = false;
    this.update((doc) => {
      const list = ensureList(doc, listPath);
      if (!list) return;
      list.splice(Math.max(0, Math.min(index, list.length)), 0, copy);
      inserted = true;
    });
    if (!inserted) return null;
    this.select({ kind: "step", id: copy.id! });
    return copy.id!;
  }

  /** Where a new step goes: after the selection, into a selected empty container, or at the end of the flow. */
  insertionPoint(): { listPath: Path; index: number } {
    const sel = this.selectedStep();
    if (!sel) return { listPath: ["flow"], index: this.#mission?.flow.length ?? 0 };
    const step = sel.step;
    if (step.type === "if" && !(Array.isArray(step.then) && step.then.length)) return { listPath: [...sel.path, "then"], index: 0 };
    if (step.type === "loop" && !(Array.isArray(step.body) && step.body.length)) return { listPath: [...sel.path, "body"], index: 0 };
    return { listPath: sel.path.slice(0, -1), index: (sel.path[sel.path.length - 1] as number) + 1 };
  }

  moveStep(fromPath: Path, toListPath: Path, toIndex: number): boolean {
    const m = this.#mission;
    if (!m) return false;
    if (pathWithin(toListPath, fromPath)) return false; // cannot drop a container into itself
    const fromList = getList(m, fromPath.slice(0, -1));
    const fromIndex = fromPath[fromPath.length - 1];
    if (!fromList || typeof fromIndex !== "number" || !fromList[fromIndex]) return false;
    const sameList = pathKeyEq(fromPath.slice(0, -1), toListPath);
    let target = toIndex;
    if (sameList && target > fromIndex) target--;
    if (sameList && target === fromIndex) return false;
    const step = fromList[fromIndex]!;
    this.update((doc) => {
      const src = getList(doc, fromPath.slice(0, -1));
      if (!src) return;
      src.splice(fromIndex, 1);
      const dst = ensureList(doc, toListPath);
      if (!dst) return;
      dst.splice(Math.max(0, Math.min(target, dst.length)), 0, step);
    });
    this.select({ kind: "step", id: String(step.id) });
    return true;
  }

  /** Move a step up (-1) or down (+1) within its list. */
  nudgeStep(path: Path, delta: -1 | 1): boolean {
    const listPath = path.slice(0, -1);
    const index = path[path.length - 1] as number;
    const list = this.#mission ? getList(this.#mission, listPath) : null;
    if (!list) return false;
    const to = index + delta;
    if (to < 0 || to >= list.length) return false;
    return this.moveStep(path, listPath, delta > 0 ? to + 1 : to);
  }

  deleteStep(path: Path): void {
    const listPath = path.slice(0, -1);
    const index = path[path.length - 1] as number;
    let removed: Step | undefined;
    this.update((doc) => {
      const list = getList(doc, listPath);
      if (!list) return;
      removed = list.splice(index, 1)[0];
      // tidy empty optional containers
      const parentStep = listPath.length >= 2 ? getStepAt(doc, listPath.slice(0, -1)) : null;
      const key = listPath[listPath.length - 1];
      if (parentStep && key === "else" && list.length === 0) delete parentStep.else;
      if (list.length === 0 && key === "before_retry") {
        const owner = getStepAt(doc, listPath.slice(0, -2));
        if (owner && isRecord(owner.on_fail)) delete owner.on_fail.before_retry;
      }
    });
    if (removed && this.#selection?.kind === "step" && this.#selection.id === removed.id) this.select(null);
  }

  duplicateStep(path: Path): string | null {
    const m = this.#mission;
    const step = m ? getStepAt(m, path) : null;
    if (!step) return null;
    return this.insertStep(step, path.slice(0, -1), (path[path.length - 1] as number) + 1);
  }

  setStepEnabled(path: Path, enabled: boolean): void {
    this.update((doc) => {
      const step = getStepAt(doc, path);
      if (!step) return;
      if (enabled) delete step.enabled;
      else step.enabled = false;
    });
  }

  // ---- trigger operations ---------------------------------------------------------------------

  addTrigger(kind: "trigger" | "interrupt", trigger: Trigger): void {
    let index = 0;
    this.update((doc) => {
      const key = kind === "trigger" ? "triggers" : "interrupts";
      const list = (doc[key] ??= []);
      list.push(trigger as never);
      index = list.length - 1;
    });
    this.select({ kind, index });
  }

  deleteTrigger(kind: "trigger" | "interrupt", index: number): void {
    this.update((doc) => {
      const key = kind === "trigger" ? "triggers" : "interrupts";
      const list = doc[key];
      if (!list) return;
      list.splice(index, 1);
      if (list.length === 0) delete doc[key];
    });
    if (this.#selection && this.#selection.kind === kind && this.#selection.index === index) this.select(null);
  }

  // ---- validation ----------------------------------------------------------------------------------

  validateContext(): ValidateContext {
    return {
      sites: this.sites,
      activeMap: this.activeMap,
      missions: this.missionNames,
      connectors: this.#connectorsKnown ? Object.keys(this.connectors) : null,
      capabilities: this.capabilities,
    };
  }

  #revalidate(): void {
    if (!this.#mission) {
      this.#validation = { errors: [], warnings: [] };
    } else {
      const r = validate(this.#mission, this.validateContext());
      this.#validation = { errors: [...r.errors, ...this.#serverErrors], warnings: r.warnings };
    }
    this.#emit("validation");
  }

  /** Findings for one step id (badges). */
  findingsFor(stepId: string): Finding[] {
    const out: Finding[] = [];
    for (const f of this.#validation.errors) if (f.stepId === stepId) out.push(f);
    for (const f of this.#validation.warnings) if (f.stepId === stepId) out.push(f);
    return out;
  }

  /** Findings whose path starts with `prefix`. */
  findingsUnder(prefix: Path): Finding[] {
    const out: Finding[] = [];
    for (const f of [...this.#validation.errors, ...this.#validation.warnings]) if (pathWithin(f.path, prefix)) out.push(f);
    return out;
  }

  // ---- drafts ------------------------------------------------------------------------------------------

  #loadDrafts(): void {
    try {
      const raw = localStorage.getItem(DRAFTS_KEY);
      const parsed: unknown = raw ? JSON.parse(raw) : {};
      if (isRecord(parsed)) {
        for (const [name, d] of Object.entries(parsed)) {
          if (isRecord(d) && isRecord(d.mission) && typeof d.mission.name === "string") {
            this.#drafts[name] = { name, mission: d.mission as unknown as Mission, baseSha: typeof d.baseSha === "string" ? d.baseSha : null, savedAt: typeof d.savedAt === "string" ? d.savedAt : "" };
          }
        }
      }
    } catch {
      this.#drafts = {};
    }
  }

  #persistDrafts(): void {
    try {
      localStorage.setItem(DRAFTS_KEY, JSON.stringify(this.#drafts));
    } catch {
      // storage full or blocked: drafts stay in memory
    }
    this.#emit("drafts");
  }

  #scheduleDraft(): void {
    if (this.#draftTimer) clearTimeout(this.#draftTimer);
    this.#draftTimer = setTimeout(() => this.#flushDraft(), 300);
  }

  /** Write the open document to the drafts store now (no-op when identical to the runner copy). */
  #flushDraft(): void {
    if (this.#draftTimer) {
      clearTimeout(this.#draftTimer);
      this.#draftTimer = null;
    }
    const m = this.#mission;
    if (!m) return;
    if (this.#baseName && this.#baseName !== m.name) this.#deleteDraft(this.#baseName);
    if (!this.dirty) {
      this.#deleteDraft(m.name);
      return;
    }
    this.#drafts[m.name] = { name: m.name, mission: deepClone(m), baseSha: this.#baseSha, savedAt: new Date().toISOString() };
    this.#persistDrafts();
  }

  saveDraftNow(): void {
    this.#flushDraft();
  }

  #deleteDraft(name: string): void {
    if (!(name in this.#drafts)) return;
    delete this.#drafts[name];
    this.#persistDrafts();
  }

  deleteDraft(name: string): void {
    this.#deleteDraft(name);
  }

  /** Discard local edits: reopen the runner copy if there is one, else close. */
  discardChanges(): void {
    const m = this.#mission;
    if (!m) return;
    this.#deleteDraft(m.name);
    if (this.#baseDoc && this.#baseName) {
      const base = this.#baseDoc;
      this.open(base, { name: this.#baseName, sha: this.#baseSha, doc: base });
    } else this.close();
  }

  // ---- runner state ------------------------------------------------------------------------------------

  setConnected(v: boolean): void {
    if (this.connected === v) return;
    this.connected = v;
    this.#emit("connection", "remote");
  }

  setStatus(s: RunnerStatus): void {
    const prevPrompt = this.status?.prompt?.id ?? null;
    const prevMap = this.activeMap;
    this.status = s;
    if ((s.prompt?.id ?? null) !== prevPrompt) this.#emit("remote");
    this.#emit("remote", "robot");
    if (this.activeMap !== prevMap) {
      this.#sitesVersion++;
      this.#revalidate();
      this.#emit("sites");
    }
  }

  setMissions(list: MissionSummary[]): void {
    this.missions = list;
    this.#missionsKnown = true;
    this.#revalidate();
    this.#emit("remote");
    this.#checkRemoteChanged();
  }

  setSites(doc: SitesDoc | null, fromRunner: boolean): void {
    this.sites = doc;
    if (fromRunner) this.sitesBase = doc ? canonicalJson(doc) : null;
    this.#sitesVersion++;
    this.#revalidate();
    this.#emit("sites");
  }

  /** Live robot pose from the `robot` event (mutates the mirrored status). */
  setRobot(robot: RobotState | null): void {
    if (this.status) this.status.robot = robot;
    this.#emit("robot");
  }

  get sitesDirty(): boolean {
    if (!this.sites) return false;
    return this.sitesBase !== canonicalJson(this.sites);
  }

  setConnectors(c: Record<string, ConnectorState>): void {
    this.connectors = c;
    this.#connectorsKnown = true;
    this.#revalidate();
    this.#emit("remote");
  }

  setCapabilities(c: Capabilities | null): void {
    this.capabilities = c;
    this.#revalidate();
    this.#emit("remote");
  }

  setExamples(list: Mission[]): void {
    this.examples = list;
    this.#emit("remote");
  }

  // ---- previews (route + dry run) --------------------------------------------------------------------------

  setRoutePreview(p: RoutePreview | null): void {
    this.routePreview = p;
    this.#emit("preview");
  }

  setDryRun(result: DryRunResult | null): void {
    this.dryRun = result ? { result, t: 0, playing: true, speed: 4 } : null;
    this.#emit("preview");
  }

  /** Move the playhead / change the transport state of the loaded dry run. */
  updateDryRun(patch: Partial<Omit<DryRunPlayback, "result">>): void {
    const d = this.dryRun;
    if (!d) return;
    if (patch.t !== undefined) d.t = Math.max(0, Math.min(patch.t, d.result.durationS));
    if (patch.playing !== undefined) d.playing = patch.playing;
    if (patch.speed !== undefined) d.speed = patch.speed;
    this.#emit("preview");
  }

  setPreviewBusy(kind: "" | "route" | "dryrun"): void {
    if (this.previewBusy === kind) return;
    this.previewBusy = kind;
    this.#emit("preview");
  }

  clearPreviews(): void {
    if (!this.routePreview && !this.dryRun) return;
    this.routePreview = null;
    this.dryRun = null;
    this.#emit("preview");
  }

  // ---- live run marks ------------------------------------------------------------------------------------

  /** Feed a WebSocket event; updates the marks of the open mission's steps. */
  applyEvent(ev: WsEvent): void {
    const m = this.#mission;
    if (!m) return;
    switch (ev.type) {
      case "run.started":
      case "run.resumed": {
        if (ev.run?.mission !== m.name) return;
        if (ev.type === "run.started" || this.markedRunId !== ev.run.id) this.runMarks.clear();
        this.markedRunId = ev.run.id;
        this.#emit("runmarks");
        return;
      }
      case "step.started": {
        if (!this.#isOurRun(ev.run_id)) return;
        if (typeof ev.step_id !== "string") return;
        this.runMarks.set(ev.step_id, { status: "running", startedAt: Date.now() });
        this.#emit("runmarks");
        return;
      }
      case "step.finished": {
        if (!this.#isOurRun(ev.run_id) || typeof ev.step_id !== "string") return;
        const prev = this.runMarks.get(ev.step_id);
        const res = ev.result;
        const status = res?.status === "succeeded" || res?.status === "failed" || res?.status === "canceled" || res?.status === "timeout" ? res.status : res?.ok ? "succeeded" : "failed";
        const duration = typeof res?.duration_s === "number" ? res.duration_s : prev ? (Date.now() - prev.startedAt) / 1000 : undefined;
        const mark: RunMark = { status, startedAt: prev?.startedAt ?? Date.now() };
        if (duration !== undefined) mark.duration = duration;
        if (res?.error) mark.error = res.error;
        this.runMarks.set(ev.step_id, mark);
        this.#emit("runmarks");
        return;
      }
      case "feedback": {
        if (!this.#isOurRun(ev.run_id) || typeof ev.step_id !== "string") return;
        const mark = this.runMarks.get(ev.step_id);
        if (!mark) return;
        mark.feedback = ev.feedback ?? null;
        this.#emit("runmarks");
        return;
      }
      case "run.finished": {
        if (ev.run?.mission !== m.name || ev.run.id !== this.markedRunId) return;
        for (const mark of this.runMarks.values()) if (mark.status === "running") mark.status = ev.run.status === "succeeded" ? "succeeded" : ev.run.status === "canceled" ? "canceled" : "failed";
        this.#emit("runmarks");
        return;
      }
      default:
        return;
    }
  }

  #isOurRun(runId: unknown): boolean {
    if (typeof runId !== "string") return false;
    if (this.markedRunId === runId) return true;
    const run = this.status?.run;
    if (run && run.id === runId && this.#mission && run.mission === this.#mission.name) {
      this.markedRunId = runId;
      return true;
    }
    return false;
  }

  clearRunMarks(): void {
    this.runMarks.clear();
    this.markedRunId = null;
    this.#emit("runmarks");
  }

  /** sha256 of the document as the runner would compute it for the same JSON text. */
  localSha(): string | null {
    return this.#mission ? sha256Hex(canonicalJson(this.#mission)) : null;
  }
}

function pathKeyEq(a: Path, b: Path): boolean {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}
