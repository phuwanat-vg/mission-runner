/**
 * Application shell: layout, panel wiring, runner connection, and every
 * action the panels can trigger (run / deploy / import / export / ...).
 */

import { Store } from "../state/Store";
import type { Selection } from "../state/Store";
import { ApiError, RunnerClient, defaultRunnerUrl, saveRunnerUrl } from "../api/RunnerClient";
import type { RunnerStatus, WsEvent } from "../api/RunnerClient";
import type { Mission, Path, Site, Step } from "../model/types";
import { MISSION_SCHEMA_ID, isRecord } from "../model/types";
import { newStep, newTrigger } from "../model/blocks";
import { allStepIds, deepClone, genId } from "../model/ids";
import { validate } from "../model/validate";
import { BUILTIN_TEMPLATES, uniqueName } from "../model/templates";
import { generatePython } from "../codegen/python";
import { btFileName, renderBehaviorTree } from "../codegen/bt";
import type { AppActions } from "./actions";
import { TopBar } from "./TopBar";
import { MissionsPanel } from "./MissionsPanel";
import { SitesPanel } from "./SitesPanel";
import { StepsList } from "./StepsList";
import { TriggerBar } from "./TriggerBar";
import { Inspector } from "./Inspector";
import { JsonTab } from "./JsonTab";
import { RunLog } from "./RunLog";
import { PromptBanner } from "./PromptBanner";
import { MapView } from "./MapView";
import { MapSource } from "../map/MapSource";
import { LiveLayers } from "../map/LiveLayers";
import { parseDryRun, parseRoutePreview, routeSummary } from "../map/preview";
import { TOOL_BY_KEY } from "./MapTools";
import { ZONE_STYLES, zoneKindOf } from "../map/zones";
import { alertDialog, confirm, inputsForm, missionNameValidator, modal, pickBlock, pickTriggerType, promptText, showFindings, templateGallery, toast, zoneForm } from "./dialogs";
import type { GalleryItem } from "./dialogs";
import { h, downloadText, pickFile, replace } from "./dom";
import { icon } from "./icons";

const LAST_MISSION_KEY = "mission-editor.lastMission";
const INSPECTOR_H_KEY = "mission-editor.inspectorHeight";
const MIN_LIST_H = 140;
const MIN_INSPECTOR_H = 120;
export const APP_VERSION = "0.1.0";

export class App implements AppActions {
  readonly store = new Store();
  readonly client: RunnerClient;
  readonly el: HTMLElement;
  #topBar: TopBar;
  #sitesPanel: SitesPanel;
  #stepsList: StepsList;
  #jsonTab: JsonTab;
  #runLog: RunLog;
  #promptBanner: PromptBanner;
  #mapView: MapView;
  #mapSource: MapSource;
  #live: LiveLayers;
  #banner: HTMLElement;
  #tabBody: HTMLElement;
  #tabButtons: HTMLElement;
  #right: HTMLElement;
  #tab: "steps" | "json" = "steps";
  #unsub: (() => void)[] = [];
  #onKey = (e: KeyboardEvent): void => this.#keydown(e);
  #busy = false;

  constructor(root: HTMLElement) {
    this.client = new RunnerClient(defaultRunnerUrl());
    this.#mapSource = new MapSource(this.client);
    this.#live = new LiveLayers(this.client);
    const s = this.store;
    this.#topBar = new TopBar(s, this.client, this);
    const missionsPanel = new MissionsPanel(s, this);
    this.#sitesPanel = new SitesPanel(s, this);
    this.#stepsList = new StepsList(s, this);
    const triggerBar = new TriggerBar(s, this);
    const inspector = new Inspector(s, this.client, this);
    this.#jsonTab = new JsonTab(s);
    this.#runLog = new RunLog(s, this.client);
    this.#promptBanner = new PromptBanner(s, this.client);
    this.#mapView = new MapView(s, this, this.#mapSource, this.#live);
    this.#mapView.onHoverStep = (id) => this.#stepsList.setHover(id);
    this.#stepsList.onHoverStep = (id) => this.#mapView.setHoverStep(id);

    this.#banner = h("div", { class: "banner", hidden: true });
    this.#tabButtons = h("div", { class: "tabs" });
    this.#tabBody = h("div", { class: "tab-body" });
    const left = h("div", { class: "left" }, h("div", { class: "side-body" }, missionsPanel.el));
    const center = h("div", { class: "center" }, this.#banner, this.#mapView.el, this.#promptBanner.el);
    const divider = h("div", { class: "insp-divider", title: "Drag to resize the inspector" });
    this.#right = h(
      "div",
      { class: "right" },
      h("div", { class: "right-top" }, triggerBar.el, this.#tabButtons, this.#tabBody),
      divider,
      h("div", { class: "right-bottom" }, inspector.el),
    );
    const main = h("div", { class: "main" }, left, center, this.#right);
    const scrim = h("div", { class: "scrim", onclick: () => this.#closePanels() });
    this.el = h("div", { class: "app" }, this.#topBar.el, main, this.#runLog.el, scrim);
    this.#bindDivider(divider);
    root.replaceChildren(this.el);
    this.#renderTabs();

    this.#unsub.push(s.on("mission", () => this.#renderBanner()));
    this.#unsub.push(s.on("remote", () => this.#renderBanner()));
    this.#unsub.push(s.on("selection", () => this.#onSelection()));
    this.#unsub.push(this.client.onConnection((ok) => this.#onConnection(ok)));
    this.#unsub.push(s.on("mission", () => this.#stepsList.applyDryRun()));
    this.#unsub.push(this.client.onEvent((ev) => this.#onEvent(ev)));
    document.addEventListener("keydown", this.#onKey);
    window.addEventListener("beforeunload", () => s.saveDraftNow());

    this.client.connect();
    this.#openInitial();
  }

  dispose(): void {
    for (const u of this.#unsub) u();
    document.removeEventListener("keydown", this.#onKey);
    this.client.disconnect();
    this.#promptBanner.dispose();
    this.#mapView.dispose();
    this.store.saveDraftNow();
    this.el.remove();
  }

  // ---- layout ------------------------------------------------------------------------------------

  /** The draggable divider between the steps list and the inspector. */
  #bindDivider(divider: HTMLElement): void {
    let saved = 0;
    try {
      saved = Number(localStorage.getItem(INSPECTOR_H_KEY) ?? "0");
    } catch {
      saved = 0;
    }
    if (saved >= MIN_INSPECTOR_H) this.el.style.setProperty("--insp-h", `${Math.round(saved)}px`);
    let dragging = false;
    divider.addEventListener("pointerdown", (e) => {
      dragging = true;
      divider.setPointerCapture(e.pointerId);
      divider.classList.add("dragging");
      e.preventDefault();
    });
    divider.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const r = this.#right.getBoundingClientRect();
      const height = Math.max(MIN_INSPECTOR_H, Math.min(r.bottom - e.clientY, r.height - MIN_LIST_H));
      this.el.style.setProperty("--insp-h", `${Math.round(height)}px`);
    });
    const end = (e: PointerEvent): void => {
      if (!dragging) return;
      dragging = false;
      divider.classList.remove("dragging");
      if (divider.hasPointerCapture(e.pointerId)) divider.releasePointerCapture(e.pointerId);
      try {
        localStorage.setItem(INSPECTOR_H_KEY, this.el.style.getPropertyValue("--insp-h").replace("px", "").trim());
      } catch {
        // ignore
      }
    };
    divider.addEventListener("pointerup", end);
    divider.addEventListener("pointercancel", end);
  }

  #renderTabs(): void {
    replace(
      this.#tabButtons,
      h("button", { class: `tab${this.#tab === "steps" ? " active" : ""}`, onclick: () => this.#setTab("steps") }, icon("list", 12), "Steps"),
      h("button", { class: `tab${this.#tab === "json" ? " active" : ""}`, onclick: () => this.#setTab("json") }, icon("braces", 12), "JSON"),
    );
    replace(this.#tabBody, this.#tab === "steps" ? this.#stepsList.el : this.#jsonTab.el);
  }

  #setTab(tab: "steps" | "json"): void {
    if (tab === this.#tab) return;
    this.#tab = tab;
    this.#renderTabs();
    if (tab === "json") this.#jsonTab.render();
  }

  /** Narrow screens slide the column in; wide screens collapse it so the map grows. */
  togglePanel(which: "left" | "inspector"): void {
    if (window.innerWidth < 900) {
      const cls = which === "left" ? "show-left" : "show-right";
      const other = which === "left" ? "show-right" : "show-left";
      this.el.classList.remove(other);
      this.el.classList.toggle(cls);
      return;
    }
    this.el.classList.toggle(which === "left" ? "hide-left" : "hide-right");
  }

  /** Ctrl+I: only the inspector pane, so the steps list gets the whole right column. */
  #toggleInspector(): void {
    if (window.innerWidth < 900) {
      this.togglePanel("inspector");
      return;
    }
    this.el.classList.remove("hide-right");
    this.el.classList.toggle("hide-inspector");
  }

  #closePanels(): void {
    this.el.classList.remove("show-left", "show-right");
  }

  #renderBanner(): void {
    const s = this.store;
    if (!s.remoteChanged || !s.mission) {
      this.#banner.hidden = true;
      return;
    }
    this.#banner.hidden = false;
    replace(
      this.#banner,
      icon("alert", 14),
      h("span", { class: "grow", text: `Robot copy changed: "${s.baseName}" was modified on the robot since you opened it.` }),
      h("button", { class: "small", onclick: () => this.loadRobotCopy() }, "Load robot copy"),
      h("button", { class: "small", onclick: () => s.acknowledgeRemote() }, "Keep mine"),
    );
  }

  #onSelection(): void {
    const sel = this.store.selection;
    if (sel?.kind === "step") {
      this.#stepsList.scrollToStep(sel.id);
      if (this.#tab !== "steps") this.#setTab("steps");
    }
    if (sel && window.innerWidth < 900) {
      // on narrow screens selecting something on the map closes the sidebar
      this.el.classList.remove("show-left");
    }
  }

  // ---- connection ------------------------------------------------------------------------------------

  #onConnection(ok: boolean): void {
    this.store.setConnected(ok);
    this.#live.onConnection(ok);
    if (ok) void this.#refreshAll();
  }

  async #refreshAll(): Promise<void> {
    const s = this.store;
    const c = this.client;
    const tasks: Promise<void>[] = [
      c.getStatus().then((st) => {
        s.setStatus(st);
        this.#live.setAvailability(st.live?.available);
      }).catch(() => undefined),
      this.#refreshMissions(),
      this.#refreshSites(),
      c.getConnectors().then((x) => s.setConnectors(x)).catch(() => undefined),
      c.getCapabilities().then((x) => s.setCapabilities(x)).catch(() => s.setCapabilities(null)),
      c.getExamples().then((x) => s.setExamples(Array.isArray(x) ? x : [])).catch(() => undefined),
    ];
    await Promise.all(tasks);
    void this.#runLog.refresh();
    if (!s.mission) this.#openInitial();
  }

  async #refreshMissions(): Promise<void> {
    const s = this.store;
    try {
      const list = await this.client.listMissions();
      s.setMissions(Array.isArray(list) ? list : []);
    } catch {
      return; // keep the previous list
    }
    // A document opened before the runner answered (draft, offline start) gets its runner copy attached now.
    const m = s.mission;
    if (m && s.baseName === null) {
      const summary = s.missions.find((x) => x.name === m.name);
      if (!summary) return;
      try {
        const doc = await this.client.getMission(m.name);
        s.attachBase(m.name, summary.sha256 ?? null, doc);
      } catch {
        // stays "not on robot" until the next refresh
      }
    }
  }

  async #refreshSites(): Promise<void> {
    if (this.store.sitesDirty) {
      toast("Sites changed on the robot; your unsaved site edits are kept.", "warn");
      return;
    }
    try {
      const doc = await this.client.getSites();
      this.store.setSites(isRecord(doc) && isRecord(doc.maps) ? doc : null, true);
    } catch {
      // keep what we have
    }
  }

  #onEvent(ev: WsEvent): void {
    const s = this.store;
    if (ev.type.startsWith("live.")) {
      this.#live.handleEvent(ev);
      return;
    }
    switch (ev.type) {
      case "status": {
        const st = ev as unknown as RunnerStatus;
        s.setStatus(st);
        this.#live.setAvailability(st.live?.available);
        return;
      }
      case "missions.changed":
        void this.#refreshMissions();
        return;
      case "sites.changed":
        void this.#refreshSites();
        return;
      case "robot":
        s.setRobot({ x: Number(ev.x), y: Number(ev.y), yaw_deg: Number(ev.yaw_deg), frame: typeof ev.frame === "string" ? ev.frame : "map", battery: typeof ev.battery === "number" ? ev.battery : null });
        return;
      default:
        s.applyEvent(ev);
        this.#runLog.onEvent(ev);
        if (ev.type === "run.finished" && ev.run) {
          const r = ev.run;
          toast(`${r.mission} ${r.status}${r.error ? `: ${r.error}` : ""}`, r.status === "succeeded" ? "ok" : r.status === "canceled" ? "warn" : "error");
        }
    }
  }

  // ---- opening missions --------------------------------------------------------------------------------

  #openInitial(): void {
    const s = this.store;
    if (s.mission) return;
    let last: string | null = null;
    try {
      last = localStorage.getItem(LAST_MISSION_KEY);
    } catch {
      last = null;
    }
    const candidates = [last, ...s.drafts.map((d) => d.name), ...s.missions.map((m) => m.name)].filter((n): n is string => !!n);
    const name = candidates.find((n) => s.draft(n) || s.missions.some((m) => m.name === n));
    if (name) void this.openMission(name);
  }

  async #confirmLeave(): Promise<boolean> {
    const s = this.store;
    if (!s.mission || !s.dirty) return true;
    const v = await modal({
      title: "Unsaved changes",
      body: [h("p", { text: `"${s.mission.name}" has changes that are not on the robot. Keep them as a local draft or discard them?` })],
      buttons: [
        { label: "Cancel", value: "cancel" },
        { label: "Discard", value: "discard", danger: true },
        { label: "Keep draft", value: "keep", primary: true },
      ],
    });
    if (v === "keep") {
      s.saveDraftNow();
      return true;
    }
    if (v === "discard") {
      s.discardChanges();
      return true;
    }
    return false;
  }

  async openMission(name: string): Promise<void> {
    const s = this.store;
    if (s.mission?.name === name) return;
    if (!(await this.#confirmLeave())) {
      this.#topBar.render();
      return;
    }
    const summary = s.missions.find((m) => m.name === name) ?? null;
    const draft = s.draft(name);
    let runnerDoc: Mission | null = null;
    if (summary && s.connected) {
      try {
        runnerDoc = await this.client.getMission(name);
      } catch (err) {
        toast(`Could not load ${name} from the robot: ${msg(err)}`, "error");
      }
    }
    if (runnerDoc) {
      if (draft) s.open(draft.mission, { name, sha: draft.baseSha ?? summary?.sha256 ?? null, doc: runnerDoc });
      else s.open(runnerDoc, { name, sha: summary?.sha256 ?? null, doc: runnerDoc });
    } else if (draft) {
      s.open(draft.mission, null);
    } else {
      toast(`Mission "${name}" is not available${s.connected ? "" : " offline"}.`, "warn");
      return;
    }
    try {
      localStorage.setItem(LAST_MISSION_KEY, name);
    } catch {
      // ignore
    }
    this.#closePanels();
  }

  loadRobotCopy(): void {
    const s = this.store;
    const name = s.baseName;
    if (!name) return;
    void (async () => {
      try {
        const doc = await this.client.getMission(name);
        const summary = s.missions.find((m) => m.name === name);
        s.deleteDraft(name);
        s.open(doc, { name, sha: summary?.sha256 ?? null, doc });
        toast("Robot copy loaded", "ok");
      } catch (err) {
        toast(`Could not load the robot copy: ${msg(err)}`, "error");
      }
    })();
  }

  #takenNames(): Set<string> {
    const s = this.store;
    const taken = new Set<string>();
    for (const m of s.missions) taken.add(m.name);
    for (const d of s.drafts) taken.add(d.name);
    if (s.mission) taken.add(s.mission.name);
    return taken;
  }

  async newMission(): Promise<void> {
    const s = this.store;
    const items: GalleryItem[] = BUILTIN_TEMPLATES.map((t) => ({ id: `builtin:${t.id}`, title: t.title, description: t.description, source: "builtin", icon: t.id === "blank" ? "file" : "fileText" }));
    const builtinNames = new Set(BUILTIN_TEMPLATES.map((t) => t.mission.name));
    for (const ex of s.examples) {
      if (!ex || typeof ex.name !== "string" || builtinNames.has(ex.name)) continue;
      items.push({ id: `runner:${ex.name}`, title: ex.title ?? ex.name, description: ex.description ?? "", source: "runner", icon: "bot" });
    }
    const choice = await templateGallery(items);
    if (!choice) return;
    if (!(await this.#confirmLeave())) return;
    let source: Mission | null = null;
    if (choice.startsWith("builtin:")) source = BUILTIN_TEMPLATES.find((t) => `builtin:${t.id}` === choice)?.mission ?? null;
    else source = s.examples.find((ex) => `runner:${ex.name}` === choice) ?? null;
    if (!source) return;
    const doc = deepClone(source);
    doc.name = uniqueName(doc.name === "new_mission" ? "mission" : doc.name, this.#takenNames());
    delete doc.version;
    s.open(doc, null);
    toast(`Draft "${doc.name}" created. Deploy to put it on the robot.`, "info");
    try {
      localStorage.setItem(LAST_MISSION_KEY, doc.name);
    } catch {
      // ignore
    }
  }

  async renameMission(name: string): Promise<void> {
    const s = this.store;
    if (s.mission?.name !== name) await this.openMission(name);
    if (s.mission?.name !== name) return;
    const taken = this.#takenNames();
    const next = await promptText("Rename mission", { label: "Name", value: name, mono: true, validate: missionNameValidator(taken, name), help: "The robot copy keeps the old name until you deploy; delete it afterwards if you no longer need it.", ok: "Rename" });
    if (!next || next === name) return;
    s.update((m) => {
      m.name = next;
    });
    try {
      localStorage.setItem(LAST_MISSION_KEY, next);
    } catch {
      // ignore
    }
  }

  async duplicateMission(name: string): Promise<void> {
    const s = this.store;
    let source: Mission | null = null;
    if (s.mission?.name === name) source = s.mission;
    else if (s.draft(name)) source = s.draft(name)!.mission;
    else if (s.connected) {
      try {
        source = await this.client.getMission(name);
      } catch (err) {
        toast(`Could not load ${name}: ${msg(err)}`, "error");
        return;
      }
    }
    if (!source) return;
    if (!(await this.#confirmLeave())) return;
    const doc = deepClone(source);
    doc.name = uniqueName(`${name}_copy`, this.#takenNames());
    delete doc.version;
    s.open(doc, null);
    toast(`Draft "${doc.name}" created`, "ok");
  }

  async deleteMission(name: string): Promise<void> {
    const s = this.store;
    const onRobot = s.missions.some((m) => m.name === name);
    const what = onRobot ? `Delete "${name}" from the robot${s.draft(name) ? " and discard the local draft" : ""}?` : `Discard the local draft "${name}"?`;
    if (!(await confirm(what, { ok: "Delete", danger: true }))) return;
    if (onRobot) {
      if (!s.connected) {
        toast("Runner offline: cannot delete on the robot", "warn");
        return;
      }
      try {
        await this.client.deleteMission(name);
      } catch (err) {
        toast(`Delete failed: ${msg(err)}`, "error");
        return;
      }
    }
    s.deleteDraft(name);
    if (s.mission?.name === name) s.close();
    await this.#refreshMissions();
    toast(`${name} deleted`, "ok");
  }

  async importJson(): Promise<void> {
    const file = await pickFile(".json,application/json");
    if (!file) return;
    let doc: unknown;
    try {
      doc = JSON.parse(file.text);
    } catch (err) {
      await alertDialog("Import failed", `${file.name} is not valid JSON: ${msg(err)}`);
      return;
    }
    if (!isRecord(doc) || doc.schema !== MISSION_SCHEMA_ID) {
      await alertDialog("Import failed", `${file.name} is not a mission (schema must be "${MISSION_SCHEMA_ID}").`);
      return;
    }
    const r = validate(doc, this.store.validateContext());
    if (r.errors.length) {
      const v = await modal({
        title: "Mission has errors",
        cls: "wide",
        body: [h("p", { text: `${file.name} has ${r.errors.length} error${r.errors.length === 1 ? "" : "s"}. You can open it anyway and fix them in the editor.` }), h("ul", { class: "finding-list" }, ...r.errors.slice(0, 20).map((f) => h("li", { class: "error", text: `${f.path.join(".")}: ${f.message}` })))],
        buttons: [
          { label: "Cancel", value: "cancel" },
          { label: "Open anyway", value: "open", primary: true },
        ],
      });
      if (v !== "open") return;
    }
    const mission = doc as unknown as Mission;
    if (typeof mission.name !== "string" || mission.name === "") mission.name = uniqueName(file.name.replace(/\.json$/i, ""), this.#takenNames());
    if (!(await this.#confirmLeave())) return;
    const taken = this.#takenNames();
    if (taken.has(mission.name)) {
      const v = await modal({
        title: "Name already used",
        body: [h("p", { text: `A mission named "${mission.name}" already exists. Replace it (the import becomes a draft you can deploy over it) or import under a new name?` })],
        buttons: [
          { label: "Cancel", value: "cancel" },
          { label: `Import as ${uniqueName(mission.name, taken)}`, value: "rename" },
          { label: "Replace", value: "replace", primary: true },
        ],
      });
      if (v === "rename") mission.name = uniqueName(mission.name, taken);
      else if (v !== "replace") return;
    }
    const summary = this.store.missions.find((m) => m.name === mission.name);
    if (summary && this.store.connected) {
      try {
        const runnerDoc = await this.client.getMission(mission.name);
        this.store.open(mission, { name: mission.name, sha: summary.sha256 ?? null, doc: runnerDoc });
      } catch {
        this.store.open(mission, null);
      }
    } else this.store.open(mission, null);
    toast(`Imported ${file.name}`, "ok");
  }

  exportJson(): void {
    const m = this.store.mission;
    if (!m) return;
    downloadText(`${m.name}.json`, `${JSON.stringify(m, null, 2)}\n`);
  }

  exportPython(): void {
    const s = this.store;
    const m = s.mission;
    if (!m) return;
    const script = generatePython(m, { sites: s.sites, activeMap: s.activeMap, fileName: `${m.name}.json` });
    downloadText(`${m.name}.py`, script, "text/x-python");
    if (script.includes("# TODO:")) toast("Some steps have no Python equivalent; look for TODO in the script.", "warn", 6000);
  }

  exportBt(): void {
    const s = this.store;
    const sel = s.selectedStep();
    const m = s.mission;
    if (!m || !sel) {
      toast("Select a navigation step with a behavior-tree template first.", "warn");
      return;
    }
    const bt = sel.step.behavior_tree;
    if (!isRecord(bt)) {
      toast("The selected step has no behavior-tree template.", "warn");
      return;
    }
    try {
      const xml = renderBehaviorTree(bt);
      downloadText(btFileName(m.name, String(sel.step.id ?? "step")), xml, "application/xml");
    } catch (err) {
      toast(`Cannot render the behavior tree: ${msg(err)}`, "error");
    }
  }

  // ---- previews (the robot never moves) ----------------------------------------------------

  /** Plan the whole route with the robot's planner and draw it on the map. */
  async previewRoute(): Promise<void> {
    const s = this.store;
    const m = s.mission;
    if (!m) {
      toast("Open a mission first", "warn");
      return;
    }
    if (!s.connected) {
      toast("Runner offline: the route can only be planned by the robot.", "warn");
      return;
    }
    if (s.previewBusy) return;
    s.setPreviewBusy("route");
    const version = s.version;
    try {
      const raw = await this.client.previewRoute({ mission: m });
      if (s.version !== version || s.mission !== m) return; // edited while planning
      const parsed = parseRoutePreview(raw, version);
      if (!parsed || !parsed.legs.length) {
        toast("Nothing to plan: this mission has no navigation steps with a place on the map.", "warn", 6000);
        s.setRoutePreview(null);
        return;
      }
      s.setRoutePreview(parsed);
      toast(`Planned ${parsed.legs.length} leg${parsed.legs.length === 1 ? "" : "s"} · ${routeSummary(parsed)}`, parsed.planned ? "ok" : "warn", 4000);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) toast("This runner is too old for route previews (no POST /api/preview/route).", "warn", 8000);
      else toast(`Preview failed: ${msg(err)}`, "error", 8000);
    } finally {
      s.setPreviewBusy("");
    }
  }

  /** Replay the whole flow against a private simulated robot and animate it. */
  async previewDryRun(): Promise<void> {
    const s = this.store;
    const m = s.mission;
    if (!m) {
      toast("Open a mission first", "warn");
      return;
    }
    if (!s.connected) {
      toast("Runner offline: a dry run needs the robot's runner.", "warn");
      return;
    }
    if (s.previewBusy) return;
    let inputs: Record<string, unknown> | undefined;
    if (m.inputs && Object.keys(m.inputs).length) {
      const values = await inputsForm(m, s.sites, s.activeMap);
      if (!values) return;
      inputs = values;
    }
    s.setPreviewBusy("dryrun");
    const version = s.version;
    try {
      const raw = await this.client.previewDryRun(inputs ? { mission: m, inputs } : { mission: m });
      if (s.version !== version || s.mission !== m) return;
      const parsed = parseDryRun(raw, version);
      if (!parsed) {
        toast("The runner returned no timeline for this mission.", "warn");
        return;
      }
      s.setDryRun(parsed);
      if (!parsed.samples.length) toast("The dry run recorded no movement; the step list still shows what ran.", "warn", 6000);
      else if (parsed.truncated) toast("The dry run was cut short by the wall-clock limit; the timeline is incomplete.", "warn", 8000);
      else if (!parsed.ok) toast(`Dry run ${parsed.status}${parsed.error ? `: ${parsed.error}` : ""}`, "warn", 8000);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) toast("This runner is too old for dry runs (no POST /api/preview/dryrun).", "warn", 8000);
      else toast(`Dry run failed: ${msg(err)}`, "error", 8000);
    } finally {
      s.setPreviewBusy("");
    }
  }

  // ---- run controls ---------------------------------------------------------------------------------------

  async run(): Promise<void> {
    const s = this.store;
    const m = s.mission;
    if (!m || !s.connected || this.#busy) return;
    this.#busy = true;
    try {
      let name = m.name;
      const ds = s.deployState;
      if (ds !== "up to date") {
        const buttons = [{ label: "Cancel", value: "cancel" }];
        if (ds === "modified") buttons.push({ label: "Run robot copy", value: "robot" });
        buttons.push({ label: "Deploy and run", value: "deploy" });
        const v = await modal({
          title: ds === "modified" ? "Mission modified" : "Mission not on robot",
          body: [h("p", { text: ds === "modified" ? `"${m.name}" differs from the copy on the robot.` : `"${m.name}" has not been deployed yet.` })],
          buttons: buttons.map((b, i) => ({ ...b, primary: i === buttons.length - 1 })),
        });
        if (v === "deploy") {
          if (!(await this.#deployNow())) return;
          name = s.mission?.name ?? name;
        } else if (v !== "robot") return;
      }
      let inputs: Record<string, unknown> | undefined;
      if (m.inputs && Object.keys(m.inputs).length) {
        const values = await inputsForm(m, s.sites, s.activeMap);
        if (!values) return;
        inputs = values;
      }
      const res = await this.client.runMission(name, inputs ? { inputs } : {});
      if (res.accepted) toast(`${name} started${res.run_id ? ` (${res.run_id})` : ""}`, "ok");
      else toast(`${name} not started: ${res.reason ?? "rejected"}`, "warn", 6000);
      void this.#runLog.refresh();
    } catch (err) {
      toast(`Run failed: ${msg(err)}`, "error");
    } finally {
      this.#busy = false;
    }
  }

  async pauseResume(): Promise<void> {
    const run = this.store.status?.run;
    if (!run || !this.store.connected) return;
    try {
      if (run.status === "paused" || this.store.status?.state === "paused") {
        await this.client.resumeRun(run.id);
        toast("Resumed", "ok", 1500);
      } else {
        await this.client.pauseRun(run.id);
        toast("Paused", "ok", 1500);
      }
    } catch (err) {
      toast(`Pause/resume failed: ${msg(err)}`, "error");
    }
  }

  stop(): void {
    if (!this.store.connected) return;
    this.client
      .stop()
      .then(() => toast("Stopped: active run, queue and suspended runs canceled", "warn"))
      .catch((err: unknown) => toast(`STOP failed: ${msg(err)}`, "error", 8000));
  }

  async deploy(): Promise<void> {
    if (this.#busy) return;
    this.#busy = true;
    try {
      await this.#deployNow();
    } finally {
      this.#busy = false;
    }
  }

  /** Validate and PUT; returns true on success. */
  async #deployNow(): Promise<boolean> {
    const s = this.store;
    const m = s.mission;
    if (!m) return false;
    if (!s.connected) {
      toast("Runner offline", "warn");
      return false;
    }
    const r = validate(m, s.validateContext());
    if (r.errors.length) {
      await showFindings("Cannot deploy", r.errors, "Fix these errors first:");
      return false;
    }
    try {
      const res = await this.client.putMission(m.name, m);
      s.markDeployed(res.sha256 ?? null);
      for (const w of res.warnings ?? []) toast(`Warning: ${w.message}`, "warn", 6000);
      toast(`Deployed ${m.name}${res.version ? ` (version ${res.version})` : ""}`, "ok");
      await this.#refreshMissions();
      const summary = s.missions.find((x) => x.name === m.name);
      if (summary?.sha256 && !res.sha256) s.markDeployed(summary.sha256);
      if (summary?.version !== undefined && s.mission && s.mission.version !== summary.version) {
        // the runner bumps the version on deploy; mirror it without marking the doc dirty
        s.update((doc) => {
          doc.version = summary.version;
        });
        s.markDeployed(summary.sha256 ?? res.sha256 ?? null);
      }
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.errors.length) {
        s.setServerErrors(err.errors);
        await showFindings("Deploy rejected", err.errors.map((e) => ({ level: "error" as const, path: e.path ?? [], message: e.message })), err.message);
      } else toast(`Deploy failed: ${msg(err)}`, "error", 8000);
      return false;
    }
  }

  // ---- settings -----------------------------------------------------------------------------------------------

  async setRunnerUrl(): Promise<void> {
    const v = await promptText("Runner URL", {
      label: "URL",
      value: this.client.baseUrl,
      mono: true,
      placeholder: "http://robot:8080",
      help: "Leave empty to use the default (this page's origin when served by the runner, else http://localhost:8080). Stored in this browser.",
      validate: (x) => (x.trim() === "" || /^https?:\/\/[^\s/]+/.test(x.trim()) ? null : "Enter an http(s) URL"),
      ok: "Connect",
    });
    if (v === null) return;
    const url = v.trim().replace(/\/+$/, "");
    saveRunnerUrl(url || null);
    this.client.setBaseUrl(url || defaultRunnerUrl());
    if (!this.client.connected) this.client.connect();
    this.#topBar.render();
  }

  /** The sites of the active map, in a dialog: the map itself is where they are placed. */
  showSites(): void {
    void modal({
      title: "Sites",
      cls: "wide sites-modal",
      body: [h("p", { class: "muted small", text: "Sites and zones belong to a map, not to a mission. Draw them on the map (S and Z) or edit them here; “Save” writes sites.json to the robot." }), this.#sitesPanel.el],
      buttons: [
        { label: "Export Nav2 filters", value: "filters" },
        { label: "Close", value: "ok", primary: true },
      ],
      onClose: (v) => {
        if (v === "filters") void this.exportFilters();
        return true;
      },
    });
  }

  showAbout(): void {
    const st = this.store.status;
    void modal({
      title: "About",
      body: [
        h("p", {}, h("b", { text: `Mission editor ${APP_VERSION}` })),
        h("p", { text: "Builds missions for mission_runner without writing code. Missions are JSON (schema mission/1); the runner executes them against Nav2 and the editor can export them as nav2_simple_commander scripts or behavior-tree XML." }),
        h("p", { class: "mono small", text: `Runner: ${this.client.baseUrl}${st?.runner ? ` · ${st.runner.version ?? ""} · ${st.runner.backend ?? ""}` : " (offline)"}` }),
        h("p", { class: "muted small", text: "Editing: Ctrl+Z / Ctrl+Y undo & redo · Ctrl+D duplicate · Del delete · Alt+Up/Down move · Esc clear selection · Ctrl+S deploy" }),
        h("p", { class: "muted small", text: "Map: V select · G goal · P path · W waypoints · S site · Z zone · F fit · R reset · wheel zooms, middle-drag or space-drag pans" }),
        h("p", { class: "muted small", text: "Previews: Shift+P plans the real route · Shift+D dry-runs the mission against a simulated robot (the robot never moves)" }),
        h("p", { class: "muted small", text: "Panels: Ctrl+B sidebar · Ctrl+I inspector" }),
      ],
      buttons: [{ label: "OK", value: "ok", primary: true }],
    });
  }

  // ---- editing --------------------------------------------------------------------------------------------------

  async addStepAt(listPath: Path, index: number): Promise<void> {
    const def = await pickBlock("Add step", this.store.capabilities?.steps);
    if (!def) return;
    this.insertBlock(def.type, { listPath, index });
  }

  insertBlock(type: string, at?: { listPath: Path; index: number }): void {
    const s = this.store;
    if (!s.mission) {
      toast("Open a mission first", "warn");
      return;
    }
    const step = newStep(type, genId("s", allStepIds(s.mission)));
    this.insertStep(step, at ?? s.insertionPoint());
  }

  insertSite(site: string, at?: { listPath: Path; index: number }): void {
    const s = this.store;
    if (!s.mission) return;
    const step = newStep("nav.go_to_pose", genId("s", allStepIds(s.mission)));
    step.pose = site;
    step.name = `Go to ${site}`;
    this.insertStep(step, at ?? s.insertionPoint());
  }

  insertStep(step: Step, at: { listPath: Path; index: number }): void {
    const id = this.store.insertStep(step, at.listPath, at.index);
    if (id) this.#setTab("steps");
  }

  async addTrigger(kind: "trigger" | "interrupt"): Promise<void> {
    const s = this.store;
    const m = s.mission;
    if (!m) return;
    const def = await pickTriggerType(kind === "trigger" ? "Add trigger" : "Add interrupt");
    if (!def) return;
    const taken = new Set<string>();
    for (const t of [...(m.triggers ?? []), ...(m.interrupts ?? [])]) if (typeof t.id === "string") taken.add(t.id);
    const t = newTrigger(def.type, genId(kind === "trigger" ? "t" : "i", taken));
    if (kind === "interrupt") (t as Record<string, unknown>).run = "";
    s.addTrigger(kind, t);
  }

  // ---- sites ------------------------------------------------------------------------------------------------------

  async saveSites(): Promise<void> {
    const s = this.store;
    if (!s.sites || !s.connected) return;
    try {
      await this.client.putSites(s.sites);
      s.setSites(s.sites, true);
      toast("Sites saved to the robot", "ok");
    } catch (err) {
      toast(`Saving sites failed: ${msg(err)}`, "error", 8000);
    }
  }

  async captureSite(name: string, existing?: Site): Promise<void> {
    if (!this.store.connected) {
      toast("Runner offline", "warn");
      return;
    }
    try {
      const p = await this.client.getRobotPose();
      const site: Site = { ...(existing ?? { kind: "station" }), x: Math.round(p.x * 1000) / 1000, y: Math.round(p.y * 1000) / 1000, yaw_deg: Math.round(p.yaw_deg * 10) / 10 };
      this.store.mutateSites((sites) => {
        sites[name] = site;
      });
      toast(`${name} set to (${site.x}, ${site.y}, ${site.yaw_deg}°). Save to keep it on the robot.`, "ok");
    } catch (err) {
      toast(`Could not read the robot pose: ${msg(err)}`, "error");
    }
  }

  async deleteSite(name: string): Promise<void> {
    const s = this.store;
    if (!(await confirm(`Delete site "${name}" from map "${s.activeMap ?? ""}"?`, { ok: "Delete", danger: true }))) return;
    s.mutateSites((sites) => {
      delete sites[name];
    });
    if (s.selection?.kind === "site" && s.selection.name === name) s.select(null);
    toast(`${name} deleted. Sites are not saved on the robot yet.`, "warn", 6000, s.connected ? { label: "Save sites", onClick: () => void this.saveSites() } : undefined);
  }

  // ---- zones ------------------------------------------------------------------------------------------------------

  async editZone(name: string): Promise<void> {
    const s = this.store;
    const zone = s.activeZones[name];
    if (!zone) return;
    const taken = new Set(Object.keys(s.activeZones));
    const current = { name, kind: zoneKindOf(zone), ...(zone.speed_mps !== undefined ? { speed_mps: zone.speed_mps } : {}), ...(zone.notes ? { notes: zone.notes } : {}) };
    const v = await zoneForm(`Zone ${name}`, current, taken);
    if (!v) return;
    s.mutateZones((zones) => {
      const z = zones[name];
      if (!z) return;
      z.kind = v.kind;
      if (v.kind === "speed_limit" && v.speed_mps !== undefined) z.speed_mps = v.speed_mps;
      else delete z.speed_mps;
      if (v.notes) z.notes = v.notes;
      else delete z.notes;
      if (v.name !== name) {
        delete zones[name];
        zones[v.name] = z;
      }
    });
    s.select({ kind: "zone", name: v.name });
  }

  async deleteZone(name: string): Promise<void> {
    const s = this.store;
    if (!(await confirm(`Delete zone "${name}" from map "${s.activeMap ?? ""}"?`, { ok: "Delete", danger: true }))) return;
    s.mutateZones((zones) => {
      delete zones[name];
    });
    if (s.selection?.kind === "zone" && s.selection.name === name) s.select(null);
    toast(`${name} deleted. Zones are not saved on the robot yet.`, "warn", 6000, s.connected ? { label: "Save sites", onClick: () => void this.saveSites() } : undefined);
  }

  /** Write the Nav2 costmap filter masks for this map's zones. */
  async exportFilters(): Promise<void> {
    const s = this.store;
    const map = s.activeMap;
    if (!map) {
      toast("No map selected", "warn");
      return;
    }
    if (!s.connected) {
      toast("Runner offline: only the robot can write the masks.", "warn");
      return;
    }
    const zones = Object.values(s.activeZones);
    const enforced = zones.filter((z) => zoneKindOf(z) === "keepout" || (zoneKindOf(z) === "speed_limit" && z.speed_mps));
    if (!enforced.length) {
      await alertDialog("Nothing to export", `Map "${map}" has no keep-out zone and no speed limit with a speed, so there is no Nav2 filter mask to write. ${ZONE_STYLES.preferred.help}`);
      return;
    }
    if (s.sitesDirty) {
      const v = await modal({
        title: "Unsaved zones",
        body: [h("p", { text: "The masks are written from the zones on the robot, not from your unsaved edits. Save the sites first?" })],
        buttons: [
          { label: "Cancel", value: "cancel" },
          { label: "Export anyway", value: "anyway" },
          { label: "Save and export", value: "save", primary: true },
        ],
      });
      if (v === "cancel" || v === null) return;
      if (v === "save") await this.saveSites();
    }
    try {
      const res = await this.client.exportFilters(map);
      const rows = (res.masks ?? []).map((m) =>
        h("div", { class: "filter-row" }, h("b", { text: m.kind === "keepout" ? "Keep-out mask" : "Speed mask" }), h("div", { class: "mono small", text: m.yaml }), h("div", { class: "mono small muted", text: m.image }), h("div", { class: "muted small", text: `zones: ${m.zones}` })),
      );
      await modal({
        title: "Nav2 filter masks written",
        cls: "wide",
        body: [
          h("p", { text: `Written into ${res.directory} on the robot.` }),
          ...(rows.length ? rows : [h("p", { class: "muted", text: "The runner wrote nothing." })]),
          h("p", { class: "muted small", text: "Nothing takes effect until Nav2 reads them: point your costmap_filter_info_server at the .yaml above (one server per filter type) and enable the matching costmap filter plugin." }),
        ],
        buttons: [{ label: "OK", value: "ok", primary: true }],
      });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) toast("This runner is too old for zone filters (no POST /api/maps/{name}/filters).", "warn", 8000);
      else toast(`Export failed: ${msg(err)}`, "error", 8000);
    }
  }

  // ---- keyboard -------------------------------------------------------------------------------------------------------

  #keydown(e: KeyboardEvent): void {
    const target = e.target as HTMLElement | null;
    const inField = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
    if (document.querySelector(".modal-backdrop")) return;
    const s = this.store;
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && e.key.toLowerCase() === "s") {
      e.preventDefault();
      void this.deploy();
      return;
    }
    if (ctrl && e.key.toLowerCase() === "b") {
      e.preventDefault();
      this.togglePanel("left");
      return;
    }
    if (ctrl && e.key.toLowerCase() === "i" && !e.shiftKey) {
      e.preventDefault();
      this.#toggleInspector();
      return;
    }
    if (inField) return;
    if (ctrl && e.key.toLowerCase() === "z" && !e.shiftKey) {
      e.preventDefault();
      s.undo();
      return;
    }
    if ((ctrl && e.key.toLowerCase() === "y") || (ctrl && e.shiftKey && e.key.toLowerCase() === "z")) {
      e.preventDefault();
      s.redo();
      return;
    }
    if (e.key === "Escape") {
      if (this.el.classList.contains("show-left") || this.el.classList.contains("show-right")) this.#closePanels();
      else if (!this.#mapView.handleKey(e)) s.select(null);
      return;
    }
    if (e.shiftKey && !ctrl && !e.altKey) {
      const k = e.key.toUpperCase();
      if (k === "P") {
        e.preventDefault();
        void this.previewRoute();
        return;
      }
      if (k === "D") {
        e.preventDefault();
        void this.previewDryRun();
        return;
      }
    }
    // map tools and view (V G P W S Z, F, R, Enter/Backspace while drawing)
    if (!ctrl && !e.altKey && !e.metaKey) {
      const wantsMap = TOOL_BY_KEY.has(e.key.toUpperCase()) || e.key.toUpperCase() === "F" || e.key.toUpperCase() === "R" || e.key === "Enter" || (e.key === "Backspace" && this.#mapView.tool !== "select");
      if (wantsMap && this.#mapView.handleKey(e)) {
        e.preventDefault();
        return;
      }
    }
    const sel: Selection = s.selection;
    if (!sel) return;
    if (sel.kind === "step") {
      const st = s.selectedStep();
      if (!st) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        void this.#stepsList.deleteStep(st.path, st.step);
      } else if (ctrl && e.key.toLowerCase() === "d") {
        e.preventDefault();
        s.duplicateStep(st.path);
      } else if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
        e.preventDefault();
        s.nudgeStep(st.path, e.key === "ArrowUp" ? -1 : 1);
      }
    } else if (sel.kind === "site") {
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        void this.deleteSite(sel.name);
      }
    } else if (sel.kind === "zone") {
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        void this.deleteZone(sel.name);
      }
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      s.deleteTrigger(sel.kind, sel.index);
    }
  }
}

function msg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
