/**
 * The map: one SVG with a single world transform (metres, x right, y up; the
 * y flip happens once in the matrix) and, bottom to top: floor, live costmap,
 * grid, zones, route (mission legs and the planned preview), live scan and
 * plan, sites, stops, the tool draft, the dry-run ghost and the robot.
 *
 * The tree is rebuilt only when the mission, the sites, the zones or the map
 * change. Panning is one matrix update; zooming additionally rewrites the
 * counter scale of the marker groups so markers keep their pixel size and
 * re-runs the label collision pass. Everything live (robot pose, run marks,
 * dry-run playhead, live layers, hover, selection, drags) mutates existing
 * nodes: nothing here rebuilds per frame.
 */

import type { Store } from "../state/Store";
import type { AppActions } from "./actions";
import type { Site } from "../model/types";
import type { MapData } from "../map/MapSource";
import type { MapSource } from "../map/MapSource";
import type { Bounds, MapModel, PlacedPoint, RouteLeg, View, WorldPoint } from "../map/geometry";
import { MAX_SCALE, MIN_SCALE, centerOn, contentBounds, distance, emptyMapModel, expandBounds, fitView, headingDeg, round1, round3, screenToWorld, unionBounds, viewMatrix, worldToScreen, zoomAt } from "../map/geometry";
import { distanceToOutline, nearestVertex, pointInPolygon, polygonCentroid, polygonPath, zoneEntries, zoneKindOf, zonePolygon, zoneSummary, zonesBounds } from "../map/zones";
import type { RoutePreview } from "../map/preview";
import { poseAt, routeSummary, stepStatesAt, trailUpTo } from "../map/preview";
import type { LiveLayers } from "../map/LiveLayers";
import { LIVE_HELP, LIVE_LABELS, LIVE_LAYERS } from "../map/LiveLayers";
import type { DraftShape, MapHit, PreviewState, ToolHost, ToolName } from "./MapTools";
import { MapTools, TOOLS } from "./MapTools";
import { DryRunBar } from "./DryRunBar";
import { h, replace, svg } from "./dom";
import { icon } from "./icons";
import { modal } from "./dialogs";

const STOP_R = 11;
const HANDLE_PX = 30;
const HIT_PX = 15;
const SITE_R = 6.5;
const SITE_OFFSET = 15;
/** Below this many pixels per metre the names under the markers are hidden. */
const LABEL_MIN_SCALE = 16;
const GRID_MIN_SCALE = 14;
const TRAIL_MAX = 200;
const FOOTPRINT_M = 0.3;
const FOLLOW_KEY = "mission-editor.map.follow";
/** Half the height of a label box, in pixels (11px text). */
const LABEL_H = 7;
/** Rough width of one character of the 11px label font. */
const LABEL_CHAR_W = 5.9;
const ZONE_VERTEX_R = 4.5;

/** One label competing for space; `#layoutLabels` hides the ones that collide. */
interface LabelNode {
  el: SVGElement;
  x: number;
  y: number;
  /** Pixel offset of the text from the anchor (y is screen-down). */
  dx: number;
  dy: number;
  width: number;
  /** Lower wins when two labels overlap. */
  prio: number;
  /** Drawn at every zoom (zone names). */
  always: boolean;
}

interface ZoneNode {
  outer: SVGGElement;
  shape: SVGPathElement;
  hatch: SVGPathElement;
  verts: SVGGElement;
  label: SVGGElement;
}

interface StopNode {
  outer: SVGGElement;
  inner: SVGGElement;
  head: SVGGElement | null;
  handle: SVGGElement;
  point: PlacedPoint;
}

interface LegNode {
  el: SVGPathElement;
  arrow: SVGGElement | null;
  leg: RouteLeg;
}

export class MapView implements ToolHost {
  readonly el: HTMLElement;
  readonly store: Store;
  readonly actions: AppActions;
  /** Told which step the pointer is over, so the steps list can highlight its row. */
  onHoverStep: ((id: string | null) => void) | null = null;

  #source: MapSource;
  #tools: MapTools;
  #svg: SVGSVGElement;
  #world: SVGGElement;
  #floor: SVGGElement;
  #gridMinor: SVGPathElement;
  #gridMajor: SVGPathElement;
  #origin: SVGPathElement;
  #lCostmap: SVGGElement;
  #lZones: SVGGElement;
  #lRoute: SVGGElement;
  #lPreview: SVGGElement;
  #lLive: SVGGElement;
  #lSites: SVGGElement;
  #lStops: SVGGElement;
  #lRobot: SVGGElement;
  #lDraft: SVGGElement;
  #lGhost: SVGGElement;
  #toolbar: HTMLElement;
  #notes: HTMLElement;
  #hint: HTMLElement;
  #status: HTMLElement;
  #info: HTMLElement;
  #layerMenu: HTMLElement | null = null;
  #inlineForm: HTMLElement | null = null;
  #live: LiveLayers;
  #dryBar: DryRunBar;
  #costmapImg: SVGImageElement;
  #scanDots: SVGPathElement;
  #planLine: SVGPathElement;
  #footprint: SVGPathElement;
  #ghost: { group: SVGGElement; body: SVGGElement; trail: SVGPathElement } | null = null;
  #zoneNodes = new Map<string, ZoneNode>();
  #labels: LabelNode[] = [];
  #shownPreview: RoutePreview | null = null;

  #view: View = { scale: 40, tx: 300, ty: 300 };
  #w = 0;
  #h = 0;
  #lastScale = 0;
  #model: MapModel = emptyMapModel();
  #scaled: SVGGElement[] = [];
  #stopByKey = new Map<string, StopNode>();
  #nodesByStep = new Map<string, SVGElement[]>();
  #legNodes: LegNode[] = [];
  #legsByKey = new Map<string, LegNode[]>();
  #siteNodes = new Map<string, SVGGElement>();
  #robot: { group: SVGGElement; body: SVGGElement; trail: SVGPathElement; goalLine: SVGPathElement } | null = null;
  #trail: WorldPoint[] = [];
  #trailRun: string | null = null;
  #hover: string | null = null;
  #preview: PreviewState | null = null;
  #draft: DraftShape | null = null;
  #follow = false;
  #space = false;
  #pointers = new Map<number, WorldPoint>();
  #pan: { sx: number; sy: number; tx: number; ty: number } | null = null;
  #pinch: { dist: number; cx: number; cy: number } | null = null;
  #fittedFor = "";
  #buildQueued = false;
  #viewQueued = false;
  #unsub: (() => void)[] = [];
  #onVisibility = (): void => this.#live.setVisible(!document.hidden);
  #onDocKey = (e: KeyboardEvent): void => this.#docKey(e, true);
  #onDocKeyUp = (e: KeyboardEvent): void => this.#docKey(e, false);
  #ro: ResizeObserver | null = null;

  constructor(store: Store, actions: AppActions, source: MapSource, live: LiveLayers) {
    this.store = store;
    this.actions = actions;
    this.#source = source;
    this.#live = live;
    this.#tools = new MapTools(this);
    try {
      this.#follow = localStorage.getItem(FOLLOW_KEY) === "1";
    } catch {
      this.#follow = false;
    }

    this.#floor = svg("g", { class: "l-floor" });
    this.#costmapImg = svg("image", { class: "costmap-img", preserveAspectRatio: "none", width: 1, height: 1, x: 0, y: 0 });
    this.#lCostmap = svg("g", { class: "l-costmap" }, svg("g", { transform: "scale(1 -1)" }, this.#costmapImg));
    this.#lCostmap.setAttribute("hidden", "hidden");
    this.#gridMinor = svg("path", { class: "grid minor", "vector-effect": "non-scaling-stroke" });
    this.#gridMajor = svg("path", { class: "grid major", "vector-effect": "non-scaling-stroke" });
    this.#origin = svg("path", { class: "grid origin", "vector-effect": "non-scaling-stroke" });
    this.#lZones = svg("g", { class: "l-zones" });
    this.#lRoute = svg("g", { class: "l-route" });
    this.#lPreview = svg("g", { class: "l-preview" });
    this.#scanDots = svg("path", { class: "live-scan", d: "" });
    this.#planLine = svg("path", { class: "live-plan", "vector-effect": "non-scaling-stroke", d: "" });
    this.#lLive = svg("g", { class: "l-live" }, this.#scanDots, this.#planLine);
    this.#lSites = svg("g", { class: "l-sites" });
    this.#lStops = svg("g", { class: "l-stops" });
    this.#footprint = svg("path", { class: "live-footprint", "vector-effect": "non-scaling-stroke", d: "" });
    this.#lRobot = svg("g", { class: "l-robot" });
    this.#lDraft = svg("g", { class: "l-draft" });
    this.#lGhost = svg("g", { class: "l-ghost" });
    this.#world = svg(
      "g",
      { class: "world" },
      this.#floor,
      this.#lCostmap,
      svg("g", { class: "l-grid" }, this.#gridMinor, this.#gridMajor, this.#origin),
      this.#lZones,
      this.#lRoute,
      this.#lPreview,
      this.#lLive,
      this.#footprint,
      this.#lSites,
      this.#lStops,
      this.#lDraft,
      this.#lGhost,
      this.#lRobot,
    );
    this.#svg = svg("svg", { class: "map-svg", xmlns: "http://www.w3.org/2000/svg" }, hatchDefs(), this.#world);

    this.#toolbar = h("div", { class: "map-toolbar" });
    this.#notes = h("div", { class: "map-notes" });
    this.#hint = h("div", { class: "map-hint", hidden: true });
    this.#status = h("div", { class: "map-status" });
    this.#info = h("div", { class: "map-info", hidden: true });
    this.#dryBar = new DryRunBar(store, actions);
    this.el = h("div", { class: "mapview" }, this.#svg, h("div", { class: "map-overlay" }, this.#toolbar, this.#notes), this.#hint, this.#status, this.#info, this.#dryBar.el);
    this.el.dataset.tool = "select";

    this.#bindPointer();
    this.#unsub.push(store.on("mission", () => this.#queueBuild()));
    this.#unsub.push(store.on("sites", () => this.#onSitesChanged()));
    this.#unsub.push(store.on("selection", () => this.#applySelection()));
    this.#unsub.push(store.on("runmarks", () => this.#applyMarks()));
    this.#unsub.push(store.on("robot", () => this.#applyRobot()));
    this.#unsub.push(store.on("connection", () => this.#onConnectionChanged()));
    this.#unsub.push(store.on("preview", () => this.#applyPreviews()));
    this.#unsub.push(source.onChange(() => this.#onMapData()));
    this.#unsub.push(live.onChange(() => this.#applyLive()));
    document.addEventListener("keydown", this.#onDocKey);
    document.addEventListener("keyup", this.#onDocKeyUp);
    document.addEventListener("visibilitychange", this.#onVisibility);
    if (typeof ResizeObserver !== "undefined") {
      this.#ro = new ResizeObserver(() => this.#measure());
      this.#ro.observe(this.el);
    }
    this.#renderToolbar();
    this.#queueBuild();
    queueMicrotask(() => this.#measure());
  }

  dispose(): void {
    for (const u of this.#unsub) u();
    this.#unsub = [];
    document.removeEventListener("keydown", this.#onDocKey);
    document.removeEventListener("keyup", this.#onDocKeyUp);
    document.removeEventListener("visibilitychange", this.#onVisibility);
    this.#dryBar.dispose();
    this.#live.setVisible(false);
    this.#ro?.disconnect();
  }

  // ---- geometry helpers (ToolHost) --------------------------------------------------------

  get scale(): number {
    return this.#view.scale;
  }

  toWorld(sx: number, sy: number): WorldPoint {
    const [x, y] = screenToWorld(this.#view, sx, sy);
    return { x, y };
  }

  siteAt(x: number, y: number, radiusPx: number): string | null {
    const r = radiusPx / this.#view.scale;
    let best: string | null = null;
    let bestD = r;
    for (const [name, site] of Object.entries(this.store.activeSites)) {
      const d = distance(x, y, site.x, site.y);
      if (d <= bestD) {
        bestD = d;
        best = name;
      }
    }
    return best;
  }

  hitAt(sx: number, sy: number): MapHit | null {
    const { x, y } = this.toWorld(sx, sy);
    const tol = HIT_PX / this.#view.scale;
    const sel = this.store.selection;
    // 0. a vertex of the selected zone
    if (sel?.kind === "zone") {
      const pts = zonePolygon(this.store.activeZones[sel.name]);
      const i = nearestVertex(pts, x, y, tol);
      if (i >= 0) return { kind: "zoneVertex", name: sel.name, index: i };
    }
    // 1. the heading handle of the selected / hovered marker
    for (const node of this.#stopByKey.values()) {
      const p = node.point;
      const active = (sel?.kind === "step" && sel.id === p.stepId) || this.#hover === p.stepId;
      if (!active || !p.editable) continue;
      const yaw = ((p.yaw ?? 0) * Math.PI) / 180;
      const d = HANDLE_PX / this.#view.scale;
      if (distance(x, y, p.x + Math.cos(yaw) * d, p.y + Math.sin(yaw) * d) <= tol) return { kind: "handle", point: p };
    }
    // 2. stop markers, nearest first
    let bestPoint: PlacedPoint | null = null;
    let bestD = tol;
    for (const node of this.#stopByKey.values()) {
      const d = distance(x, y, node.point.x, node.point.y);
      if (d <= bestD) {
        bestD = d;
        bestPoint = node.point;
      }
    }
    if (bestPoint) return { kind: "point", point: bestPoint };
    // 3. sites
    const site = this.siteAt(x, y, HIT_PX);
    if (site) return { kind: "site", name: site };
    // 4. zones: their outline always, their inside only once selected (so a big
    //    keep-out does not swallow every click on the floor)
    const selZone = sel?.kind === "zone" ? sel.name : null;
    for (const e of zoneEntries(this.store.activeZones)) {
      if (distanceToOutline(e.points, x, y) <= tol || (selZone === e.name && pointInPolygon(e.points, x, y))) return { kind: "zone", name: e.name };
    }
    return null;
  }

  setHint(text: string | null): void {
    this.#hint.textContent = text ?? "";
    this.#hint.hidden = !text;
  }

  setDraft(draft: DraftShape | null): void {
    this.#draft = draft;
    this.#renderDraft();
  }

  setPreview(preview: PreviewState | null): void {
    this.#preview = preview;
    this.#applyPreview();
  }

  askName(sx: number, sy: number, value: string): Promise<string | null> {
    return new Promise((resolve) => {
      this.#closeInlineForm();
      const input = h("input", { type: "text", value, spellcheck: false });
      const done = (ok: boolean): void => {
        this.#closeInlineForm();
        resolve(ok ? input.value : null);
      };
      const form = h(
        "div",
        { class: "map-inline-form", style: `left:${Math.round(sx)}px; top:${Math.round(sy)}px` },
        h("span", { class: "muted small", text: "New site" }),
        input,
        h("button", { class: "small primary", onclick: () => done(true) }, icon("check", 11)),
        h("button", { class: "small", onclick: () => done(false) }, icon("close", 11)),
      );
      input.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (e.key === "Enter") done(true);
        else if (e.key === "Escape") done(false);
      });
      this.#inlineForm = form;
      this.el.append(form);
      setTimeout(() => {
        input.focus();
        input.select();
      }, 0);
    });
  }

  #closeInlineForm(): void {
    this.#inlineForm?.remove();
    this.#inlineForm = null;
  }

  // ---- public API used by the app ---------------------------------------------------------

  get tool(): ToolName {
    return this.#tools.tool;
  }

  setTool(tool: ToolName): void {
    this.#tools.setTool(tool);
    this.#renderToolbar();
    this.el.dataset.tool = tool;
  }

  /** Keys the map owns. Returns true when the key was used. */
  handleKey(e: KeyboardEvent): boolean {
    if (this.#tools.key(e)) return true;
    const k = e.key.toUpperCase();
    if (e.key === "Escape") {
      if (this.#tools.busy) {
        this.#tools.cancel();
        return true;
      }
      if (this.#tools.tool !== "select") {
        this.setTool("select");
        return true;
      }
      return false;
    }
    if (k === "F") {
      this.fit();
      return true;
    }
    if (k === "R") {
      this.reset();
      return true;
    }
    const tool = TOOLS.find((t) => t.key === k);
    if (tool) {
      this.setTool(tool.name);
      return true;
    }
    return false;
  }

  /** Highlight the markers of a step (hover in the steps list). */
  setHoverStep(id: string | null): void {
    if (this.#hover === id) return;
    this.#hover = id;
    this.#applyHighlight();
  }

  /** Bounds of everything worth showing: the mission, the sites and the zones. */
  #contentBounds(): Bounds | null {
    return unionBounds(contentBounds(this.#model, this.store.activeSites), zonesBounds(this.store.activeZones));
  }

  /** Fit the mission, the sites and the zones. Returns false when it could not. */
  fit(): boolean {
    const b = this.#contentBounds();
    const map = this.#mapData();
    const fallback = map ? boundsFromMeta(map) : null;
    const target = b ? expandBounds(b, 1.5) : fallback;
    if (!target || this.#w < 40) return false;
    this.#setView(fitView(target, this.#w, this.#h));
    return true;
  }

  /** Reset to the whole floor. */
  reset(): void {
    const map = this.#mapData();
    const b = map ? boundsFromMeta(map) : this.#contentBounds();
    if (!b || this.#w < 40) return;
    this.#setView(fitView(b, this.#w, this.#h));
  }

  // ---- layout -----------------------------------------------------------------------------

  #measure(): void {
    const r = this.el.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width));
    const hh = Math.max(1, Math.round(r.height));
    if (w === this.#w && hh === this.#h) return;
    const first = this.#w === 0;
    this.#w = w;
    this.#h = hh;
    this.#svg.setAttribute("viewBox", `0 0 ${w} ${hh}`);
    if (first) this.fit();
    else this.#queueView();
  }

  #setView(v: View): void {
    this.#view = { scale: Math.max(MIN_SCALE, Math.min(MAX_SCALE, v.scale)), tx: v.tx, ty: v.ty };
    this.#queueView();
  }

  #queueView(): void {
    if (this.#viewQueued) return;
    this.#viewQueued = true;
    requestAnimationFrame(() => {
      this.#viewQueued = false;
      this.#applyView();
    });
  }

  #applyView(): void {
    const v = this.#view;
    this.#world.setAttribute("transform", viewMatrix(v));
    if (v.scale !== this.#lastScale) {
      this.#lastScale = v.scale;
      const k = 1 / v.scale;
      const t = `scale(${k} ${-k})`;
      for (const g of this.#scaled) g.setAttribute("transform", t);
      this.#svg.classList.toggle("far", v.scale < LABEL_MIN_SCALE);
    }
    this.#renderGrid();
    this.#layoutLabels();
  }

  /**
   * Hide the labels that would collide at the current zoom. Labels are sorted
   * by priority when they are built (stops first, then zones, then sites), so
   * the ones that matter survive; the rest reappear as you zoom in.
   */
  #layoutLabels(): void {
    if (!this.#labels.length) return;
    const v = this.#view;
    const far = v.scale < LABEL_MIN_SCALE;
    const kept: { x0: number; y0: number; x1: number; y1: number }[] = [];
    for (const l of this.#labels) {
      if (far && !l.always) {
        l.el.setAttribute("visibility", "hidden");
        continue;
      }
      const [sx, sy] = worldToScreen(v, l.x, l.y);
      const cx = sx + l.dx;
      const cy = sy + l.dy;
      const box = { x0: cx - l.width / 2, y0: cy - LABEL_H, x1: cx + l.width / 2, y1: cy + LABEL_H };
      if (box.x1 < 0 || box.x0 > this.#w || box.y1 < 0 || box.y0 > this.#h) {
        l.el.setAttribute("visibility", "hidden");
        continue;
      }
      const hit = kept.some((k) => box.x0 < k.x1 && box.x1 > k.x0 && box.y0 < k.y1 && box.y1 > k.y0);
      l.el.setAttribute("visibility", hit ? "hidden" : "visible");
      if (!hit) kept.push(box);
    }
  }

  #addLabel(el: SVGElement, x: number, y: number, dx: number, dy: number, text: string, prio: number, always = false): void {
    this.#labels.push({ el, x, y, dx, dy, width: text.length * LABEL_CHAR_W + 8, prio, always });
  }

  // ---- toolbar and notes -------------------------------------------------------------------

  #renderToolbar(): void {
    const s = this.store;
    const tool = this.#tools.tool;
    const tools = h("div", { class: "seg map-tool-group" });
    for (const t of TOOLS) {
      tools.append(h("button", { class: tool === t.name ? "active" : "", title: `${t.label} (${t.key})\n${t.hint}`, onclick: () => this.setTool(t.name) }, icon(t.icon, 12), t.label));
    }
    const offline = !s.connected;
    const noMission = !s.mission;
    const busy = s.previewBusy;
    const why = offline ? "Runner offline" : noMission ? "Open a mission first" : "";
    const preview = h(
      "button",
      {
        class: `small${s.routePreview ? " active" : ""}${busy === "route" ? " busy" : ""}`,
        disabled: offline || noMission || busy !== "",
        title: why || "Plan every leg with the robot's planner and show the real route (Shift+P). The robot does not move.",
        onclick: () => this.actions.previewRoute(),
      },
      icon("route2", 12),
      busy === "route" ? "Planning…" : "Preview",
    );
    const dry = h(
      "button",
      {
        class: `small${s.dryRun ? " active" : ""}${busy === "dryrun" ? " busy" : ""}`,
        disabled: offline || noMission || busy !== "",
        title: why || "Replay the whole mission against a simulated robot (Shift+D). The real robot never moves.",
        onclick: () => this.actions.previewDryRun(),
      },
      icon("ghost", 12),
      busy === "dryrun" ? "Running…" : "Dry run",
    );
    const on = this.#live.enabled.size;
    const layers = h(
      "button",
      { class: `small${on ? " active" : ""}`, title: "Live data from the robot on the map (costmap, scan, plan, footprint)", onclick: (e: Event) => this.#toggleLayerMenu(e) },
      icon("layers", 12),
      `Layers${on ? ` (${on})` : ""}`,
    );
    const maps = Object.keys(s.sites?.maps ?? {}).sort();
    const mapSel = h("select", { class: "map-select", title: "Map shown on the floor" });
    if (!maps.length) mapSel.append(h("option", { value: "", text: s.sites ? "no maps" : "sites unknown" }));
    const current = s.status?.current_map ?? null;
    for (const name of maps) mapSel.append(h("option", { value: name, text: name === current ? `${name} (robot)` : name }));
    mapSel.value = s.activeMap ?? "";
    mapSel.disabled = !maps.length;
    mapSel.addEventListener("change", () => {
      s.activeMap = mapSel.value;
    });
    const dirty = s.sitesDirty;
    replace(
      this.#toolbar,
      tools,
      h("div", { class: "map-btn-group" }, preview, dry, layers),
      h("span", { class: "grow" }),
      dirty ? h("button", { class: "small primary attention", title: s.connected ? "PUT /api/sites" : "Runner offline", disabled: !s.connected, onclick: () => this.actions.saveSites() }, "Save sites") : null,
      h("button", { class: "icon-only ghost", title: "Sites and zones of this map", onclick: () => this.actions.showSites() }, icon("mapPin", 13)),
      mapSel,
      h("button", { class: `icon-only ghost${this.#follow ? " active" : ""}`, title: "Keep the robot centred while a run is active", onclick: () => this.#toggleFollow() }, icon("target", 13)),
      h("button", { class: "icon-only ghost", title: "Fit the mission and the sites (F)", onclick: () => this.fit() }, icon("maximize", 13)),
      h("button", { class: "icon-only ghost", title: "Show the whole map (R)", onclick: () => this.reset() }, icon("refresh", 13)),
    );
  }

  /** The layers popover: one checkbox per live layer, disabled with a reason. */
  #toggleLayerMenu(e: Event): void {
    e.stopPropagation();
    if (this.#layerMenu) {
      this.#closeLayerMenu();
      return;
    }
    const body = h("div", { class: "layer-menu-body" });
    for (const layer of LIVE_LAYERS) {
      const reason = this.#live.reason(layer);
      const box = h("input", { type: "checkbox", disabled: reason !== "" });
      box.checked = this.#live.enabled.has(layer);
      box.addEventListener("change", () => {
        this.#live.set(layer, box.checked);
        this.#renderToolbar();
        this.#renderNotes();
      });
      const waiting = this.#live.waiting(layer) && reason === "";
      body.append(
        h(
          "label",
          { class: `layer-row${reason ? " disabled" : ""}`, title: reason || LIVE_HELP[layer] },
          box,
          h("span", { class: "grow" }, h("div", { class: "title", text: LIVE_LABELS[layer] }), h("div", { class: "sub", text: reason || (waiting ? "waiting for data…" : LIVE_HELP[layer]) })),
        ),
      );
    }
    const menu = h(
      "div",
      { class: "layer-menu" },
      h("div", { class: "layer-menu-head" }, h("span", { class: "grow", text: "Live layers" }), h("button", { class: "icon-only ghost", title: "Close", onclick: () => this.#closeLayerMenu() }, icon("close", 11))),
      body,
      h("div", { class: "layer-menu-foot muted small", text: this.store.connected ? "Streamed over the events socket while the map is open." : "Runner offline: nothing is streamed." }),
    );
    menu.addEventListener("pointerdown", (ev) => ev.stopPropagation());
    this.#layerMenu = menu;
    this.el.append(menu);
    setTimeout(() => document.addEventListener("pointerdown", this.#closeLayerMenuOnce, { once: true }), 0);
  }

  #closeLayerMenuOnce = (): void => this.#closeLayerMenu();

  #closeLayerMenu(): void {
    this.#layerMenu?.remove();
    this.#layerMenu = null;
    document.removeEventListener("pointerdown", this.#closeLayerMenuOnce);
  }

  #onConnectionChanged(): void {
    this.#renderNotes();
    this.#renderToolbar();
  }

  #toggleFollow(): void {
    this.#follow = !this.#follow;
    try {
      localStorage.setItem(FOLLOW_KEY, this.#follow ? "1" : "0");
    } catch {
      // ignore
    }
    this.#renderToolbar();
    if (this.#follow) this.#applyRobot();
  }

  #renderNotes(): void {
    const s = this.store;
    const map = this.#mapData();
    const nodes: (Node | null)[] = [];
    if (!s.mission) nodes.push(note("info", "No mission open. Pick one on the left, or create one with “+ New”."));
    if (map && map.meta.source === "synthetic") {
      nodes.push(
        h(
          "div",
          { class: "map-note" },
          icon("info", 12),
          h("span", { text: "No saved map; showing a plain room." }),
          h("button", { class: "link-btn ghost", onclick: () => void this.#explainMap() }, "Why?"),
        ),
      );
    }
    if (map?.stale || (!s.connected && map)) nodes.push(note("warn", "Runner offline: the floor and the sites are the last copies this browser saw."));
    if (!map && s.activeMap) nodes.push(note("warn", this.#source.loading(s.activeMap) ? "Loading the map…" : "No floor image for this map."));
    const waiting = [...this.#live.enabled].filter((l) => this.#live.waiting(l));
    if (waiting.length && s.connected) nodes.push(note("info", `Waiting for ${waiting.map((l) => LIVE_LABELS[l].toLowerCase()).join(", ")} from the robot…`));
    const p = s.routePreview;
    if (p?.note) nodes.push(note("info", p.note));
    if (p && !p.planned) {
      const bad = p.legs.filter((l) => !l.planned).length;
      if (bad) nodes.push(note("warn", `${bad} of ${p.legs.length} legs could not be planned; they are drawn dashed (hover for the reason).`));
    }
    const unplaced = this.#model.unplaced;
    if (unplaced.length) {
      const list = h("div", { class: "map-note warn" }, icon("mapPinOff", 12), h("span", { text: `${unplaced.length} pose${unplaced.length === 1 ? "" : "s"} not placeable: ` }));
      unplaced.slice(0, 4).forEach((u, i) => {
        if (i) list.append(h("span", { class: "muted", text: ", " }));
        list.append(h("button", { class: "link-btn ghost", title: `${u.title} — resolved when the mission runs`, onclick: () => s.select({ kind: "step", id: u.stepId }) }, u.text || "(empty)"));
      });
      if (unplaced.length > 4) list.append(h("span", { class: "muted", text: ` … ${unplaced.length - 4} more` }));
      nodes.push(list);
    }
    replace(this.#notes, ...nodes);
  }

  async #explainMap(): Promise<void> {
    await modal({
      title: "About this floor plan",
      body: [
        h("p", { text: "The robot has no map file for this map name, so the runner drew a plain room around the sites it knows (GET /api/maps/{name} reports source “synthetic”)." }),
        h("p", { text: "Save a map with Nav2 (map_saver_cli) and point the map's “file” in sites.json at the resulting .yaml. The runner then serves the real floor and everything you place here lines up with it." }),
        h("p", { class: "muted small", text: "docs/runner-api.md · docs/editor-map-view.md" }),
      ],
      buttons: [{ label: "OK", value: "ok", primary: true }],
    });
  }

  #mapData(): MapData | null {
    return this.#source.get(this.store.activeMap);
  }

  #onSitesChanged(): void {
    this.#source.request(this.store.activeMap);
    this.#queueBuild();
  }

  #onMapData(): void {
    this.#renderFloor();
    this.#renderNotes();
    this.#autoFit();
  }

  /**
   * Fit once per mission and per map: opening a document should show it,
   * panning should stick. The key is only remembered when the fit actually
   * happened, so a fit attempted before the map had a size is retried.
   */
  #autoFit(): void {
    const key = `${this.store.mission?.name ?? ""}|${this.store.activeMap ?? ""}|${this.#mapData() ? "floor" : ""}`;
    if (key === this.#fittedFor) return;
    if (this.fit()) this.#fittedFor = key;
  }

  // ---- building the tree ---------------------------------------------------------------------

  #queueBuild(): void {
    if (this.#buildQueued) return;
    this.#buildQueued = true;
    queueMicrotask(() => {
      this.#buildQueued = false;
      this.#build();
    });
  }

  #build(): void {
    const s = this.store;
    this.#source.request(s.activeMap);
    this.#model = s.mapModel;
    this.#scaled = [];
    this.#stopByKey.clear();
    this.#nodesByStep.clear();
    this.#legNodes = [];
    this.#legsByKey.clear();
    this.#siteNodes.clear();
    this.#zoneNodes.clear();
    this.#labels = [];
    this.#renderFloor();
    this.#renderZones();
    this.#renderRoute();
    this.#renderPreviewRoute();
    this.#renderSites();
    this.#renderStops();
    this.#renderGhostLayer();
    this.#renderRobotLayer();
    this.#renderToolbar();
    this.#renderNotes();
    this.#renderInfo();
    this.#lastScale = 0; // force the counter scale of the new markers
    this.#applyView();
    this.#applyMarks();
    this.#applySelection();
    this.#applyLive();
    this.#applyDryRun();
    this.#autoFit();
  }

  /**
   * A counter-scaled group at a world position: its children are drawn in
   * pixels with y down, so markers keep their size and their text stays
   * upright. Registered groups get their scale rewritten on zoom; the draft
   * layer is rebuilt anyway and passes `register: false`.
   */
  #marker(x: number, y: number, cls: string, register = true): { outer: SVGGElement; inner: SVGGElement } {
    const inner = svg("g", { class: "mk" });
    const outer = svg("g", { class: cls, transform: `translate(${x} ${y})` }, inner);
    if (register) this.#scaled.push(inner);
    else {
      const k = 1 / this.#view.scale;
      inner.setAttribute("transform", `scale(${k} ${-k})`);
    }
    return { outer, inner };
  }

  #track(stepId: string, el: SVGElement): void {
    if (!stepId) return;
    const list = this.#nodesByStep.get(stepId);
    if (list) list.push(el);
    else this.#nodesByStep.set(stepId, [el]);
  }

  #renderFloor(): void {
    const map = this.#mapData();
    if (!map || !map.image) {
      this.#floor.replaceChildren();
      return;
    }
    const [minX, minY, maxX, maxY] = map.meta.bounds;
    const w = Math.max(maxX - minX, 0.001);
    const hh = Math.max(maxY - minY, 0.001);
    const img = svg("image", { x: minX, y: -maxY, width: w, height: hh, preserveAspectRatio: "none", class: "floor-img" });
    img.setAttributeNS("http://www.w3.org/1999/xlink", "href", map.image);
    img.setAttribute("href", map.image);
    this.#floor.replaceChildren(svg("g", { transform: "scale(1 -1)" }, img));
  }

  #renderGrid(): void {
    const v = this.#view;
    if (this.#w < 2) return;
    const [x0, y1] = screenToWorld(v, 0, 0);
    const [x1, y0] = screenToWorld(v, this.#w, this.#h);
    const minor: string[] = [];
    const major: string[] = [];
    const showMinor = v.scale >= GRID_MIN_SCALE;
    const push = (out: string[], step: number): void => {
      const sx = Math.ceil(x0 / step) * step;
      for (let x = sx; x <= x1; x += step) out.push(`M${x.toFixed(3)} ${y0.toFixed(3)}V${y1.toFixed(3)}`);
      const sy = Math.ceil(y0 / step) * step;
      for (let y = sy; y <= y1; y += step) out.push(`M${x0.toFixed(3)} ${y.toFixed(3)}H${x1.toFixed(3)}`);
    };
    if (showMinor) push(minor, 1);
    push(major, 5);
    this.#gridMinor.setAttribute("d", minor.join(""));
    this.#gridMajor.setAttribute("d", major.join(""));
    const c = 0.5;
    this.#origin.setAttribute("d", `M${-c} 0H${c}M0 ${-c}V${c}`);
  }

  /** Zone polygons: a low-opacity fill, a hatch for the enforced kinds, a name. */
  #renderZones(): void {
    const nodes: SVGElement[] = [];
    for (const { name, zone, points } of zoneEntries(this.store.activeZones)) {
      if (points.length < 3) continue;
      const kind = zoneKindOf(zone);
      const d = polygonPath(points);
      const outer = svg("g", { class: `zone k-${kind}` });
      outer.dataset.zone = name;
      if (zone.color) outer.setAttribute("style", `--zone-color: ${zone.color}`);
      const shape = svg("path", { class: "zone-fill", "vector-effect": "non-scaling-stroke", d });
      const hatch = svg("path", { class: "zone-hatch", d, fill: `url(#zone-hatch-${kind})` });
      const verts = svg("g", { class: "zone-verts" });
      for (const pt of points) {
        const { outer: vo, inner: vi } = this.#marker(pt.x, pt.y, "zone-vertex");
        vi.append(svg("circle", { r: ZONE_VERTEX_R }));
        verts.append(vo);
      }
      const c = polygonCentroid(points);
      const { outer: lo, inner: li } = this.#marker(c.x, c.y, "zone-label");
      li.append(svg("text", { class: "zone-name", "text-anchor": "middle", dy: "0.35em", text: name }));
      li.append(svg("title", { text: `${name}\n${zoneSummary(zone)}${zone.notes ? `\n${zone.notes}` : ""}` }));
      outer.append(hatch, shape, verts, lo);
      this.#zoneNodes.set(name, { outer, shape, hatch, verts, label: lo });
      this.#addLabel(lo, c.x, c.y, 0, 0, name, 1, true);
      nodes.push(outer);
    }
    this.#lZones.replaceChildren(...nodes);
  }

  #renderRoute(): void {
    const nodes: SVGElement[] = [];
    for (const leg of this.#model.legs) {
      const el = svg("path", { class: `leg ${leg.kind}`, "vector-effect": "non-scaling-stroke", d: legPath(leg.from, leg.to) });
      let arrow: SVGGElement | null = null;
      const mid = { x: (leg.from.x + leg.to.x) / 2, y: (leg.from.y + leg.to.y) / 2 };
      const marker = this.#marker(mid.x, mid.y, "leg-arrow");
      const deg = -headingDeg(leg.to.x - leg.from.x, leg.to.y - leg.from.y);
      marker.inner.append(svg("path", { class: "arrow", transform: `rotate(${deg.toFixed(2)})`, d: "M-5 -4.5 L5 0 L-5 4.5 Z" }));
      arrow = marker.outer;
      nodes.push(el, marker.outer);
      const node: LegNode = { el, arrow, leg };
      this.#legNodes.push(node);
      for (const key of [leg.from.key, leg.to.key]) {
        const list = this.#legsByKey.get(key);
        if (list) list.push(node);
        else this.#legsByKey.set(key, [node]);
      }
      this.#track(leg.stepId, el);
    }
    this.#lRoute.replaceChildren(...nodes);
  }

  #renderSites(): void {
    const s = this.store;
    const used = new Set<string>();
    for (const p of this.#model.byKey.values()) if (p.site) used.add(p.site);
    const nodes: SVGElement[] = [];
    for (const [name, site] of Object.entries(s.activeSites)) {
      const isUsed = used.has(name);
      const { outer, inner } = this.#marker(site.x, site.y, `site${isUsed ? " used" : ""}`);
      outer.dataset.site = name;
      const ox = isUsed ? -SITE_OFFSET : 0;
      const oy = isUsed ? SITE_OFFSET : 0;
      if (isUsed) inner.append(svg("path", { class: "site-link", d: `M0 0L${ox} ${oy}` }));
      const g = svg("g", { transform: `translate(${ox} ${oy})` });
      g.append(svg("path", { class: "site-mark", d: `M0 ${-SITE_R}L${SITE_R} 0L0 ${SITE_R}L${-SITE_R} 0Z` }));
      const glyph = siteGlyph(name, site);
      if (glyph) g.append(glyph);
      const nameText = svg("text", { class: "site-name", "text-anchor": "middle", y: SITE_R + 11, text: name });
      g.append(nameText);
      this.#addLabel(nameText, site.x, site.y, ox, oy + SITE_R + 11, name, 1000 + (isUsed ? 0 : 500));
      inner.append(g);
      inner.append(svg("title", { text: `${name} · ${site.kind ?? "station"} · ${round3(site.x)}, ${round3(site.y)}${site.yaw_deg === undefined ? "" : ` · ${round1(site.yaw_deg)}°`}` }));
      this.#siteNodes.set(name, outer);
      nodes.push(outer);
    }
    this.#lSites.replaceChildren(...nodes);
  }

  #renderStops(): void {
    const nodes: SVGElement[] = [];
    for (const place of this.#model.places) {
      for (const point of place.points) {
        const { outer, inner } = this.#marker(point.x, point.y, `stop g-${place.group}${place.aside ? " aside" : ""}${point.kind === "missing" ? " missing" : ""}${point.editable ? "" : " locked"}`);
        outer.dataset.key = point.key;
        outer.dataset.step = point.stepId;
        const handle = svg("g", { class: "handle" });
        const yaw = point.yaw ?? 0;
        handle.setAttribute("transform", `rotate(${(-yaw).toFixed(2)})`);
        handle.append(svg("path", { class: "handle-line", d: `M${STOP_R} 0H${HANDLE_PX - 4}` }));
        handle.append(svg("circle", { class: "handle-knob", cx: HANDLE_PX, cy: 0, r: 4 }));
        let head: SVGGElement | null = null;
        if (point.yaw !== null) {
          head = svg("g", { class: "head", transform: `rotate(${(-yaw).toFixed(2)})` });
          head.append(svg("path", { class: "head-tri", d: `M${STOP_R + 1} -5L${STOP_R + 9} 0L${STOP_R + 1} 5Z` }));
          inner.append(head);
        }
        inner.append(handle);
        const small = point.stopNo === null && place.points.length > 1;
        inner.append(svg("circle", { class: "disc", r: small ? 5 : STOP_R }));
        if (point.stopNo !== null) inner.append(svg("text", { class: "no", "text-anchor": "middle", dy: "0.35em", text: String(point.stopNo) }));
        if (point.last) {
          // the site name is what the operator knows the place by; only a
          // coordinate stop falls back to the step's title
          const label = point.site ?? place.title;
          const text = svg("text", { class: `label${point.kind === "missing" ? " missing" : ""}`, "text-anchor": "middle", y: STOP_R + 14, text: label });
          inner.append(text);
          this.#addLabel(text, point.x, point.y, 0, STOP_R + 14, label, point.stopNo ?? 500);
        }
        inner.append(svg("title", { text: `${place.title}\n${point.text}${point.kind === "missing" ? "\nthis site is not in the current map" : ""}` }));
        this.#stopByKey.set(point.key, { outer, inner, head, handle, point });
        this.#track(point.stepId, outer);
        nodes.push(outer);
      }
    }
    this.#lStops.replaceChildren(...nodes);
    this.#labels.sort((a, b) => a.prio - b.prio);
  }

  /**
   * The planned route from `POST /api/preview/route`. Drawn over the mission's
   * straight legs, which are faded out while a preview is shown so the two do
   * not fight; a leg the planner refused stays dashed and carries its error.
   */
  #renderPreviewRoute(): void {
    const p = this.store.routePreview;
    this.#shownPreview = p;
    this.#lRoute.classList.toggle("previewed", !!p && p.legs.length > 0);
    if (!p) {
      this.#lPreview.replaceChildren();
      return;
    }
    const nodes: SVGElement[] = [];
    for (const leg of p.legs) {
      const path = svg("path", { class: `plan-leg${leg.planned ? "" : " unplanned"}`, "vector-effect": "non-scaling-stroke", d: polyPath(leg.points) });
      path.dataset.step = leg.stepId;
      const tip = leg.planned ? `${leg.lengthM.toFixed(2)} m` : `Not planned${leg.error ? `: ${leg.error}` : ""} — straight line`;
      path.append(svg("title", { text: tip }));
      nodes.push(path);
      this.#track(leg.stepId, path);
    }
    this.#lPreview.replaceChildren(...nodes);
  }

  /** The dry run's ghost robot and the trail it has driven so far. */
  #renderGhostLayer(): void {
    const trail = svg("path", { class: "ghost-trail", "vector-effect": "non-scaling-stroke", d: "" });
    const { outer, inner } = this.#marker(0, 0, "ghost-robot");
    const body = svg("g", { class: "ghost-rot" }, svg("path", { class: "ghost-body", d: "M13 0L-8 -8L-4 0L-8 8Z" }));
    inner.append(body);
    this.#ghost = { group: outer, body, trail };
    this.#lGhost.replaceChildren(trail, outer);
  }

  #renderRobotLayer(): void {
    const trail = svg("path", { class: "robot-trail", "vector-effect": "non-scaling-stroke", d: "" });
    const goalLine = svg("path", { class: "robot-goal", "vector-effect": "non-scaling-stroke", d: "" });
    const { outer, inner } = this.#marker(0, 0, "robot");
    const body = svg("g", { class: "robot-rot" }, svg("path", { class: "robot-body", d: "M13 0L-8 -8L-4 0L-8 8Z" }));
    inner.append(body);
    // the footprint is a real 0.3 m circle, so it lives in world units
    outer.insertBefore(svg("circle", { class: "robot-foot", r: FOOTPRINT_M, "vector-effect": "non-scaling-stroke" }), outer.firstChild);
    this.#robot = { group: outer, body, trail, goalLine };
    this.#lRobot.replaceChildren(trail, goalLine, outer);
    this.#applyRobot();
  }

  // ---- live updates ------------------------------------------------------------------------

  #applyRobot(): void {
    const r = this.#robot;
    if (!r) return;
    const robot = this.store.status?.robot ?? null;
    r.group.classList.toggle("hidden", !robot);
    r.trail.classList.toggle("hidden", !robot);
    if (!robot) {
      r.goalLine.setAttribute("d", "");
      return;
    }
    r.group.setAttribute("transform", `translate(${robot.x} ${robot.y})`);
    r.body.setAttribute("transform", `rotate(${(-(robot.yaw_deg ?? 0)).toFixed(2)})`);
    const run = this.store.currentRun;
    const runId = run?.id ?? null;
    if (runId !== this.#trailRun) {
      this.#trailRun = runId;
      this.#trail = [];
    }
    if (runId) {
      const last = this.#trail[this.#trail.length - 1];
      if (!last || distance(last.x, last.y, robot.x, robot.y) > 0.02) {
        this.#trail.push({ x: robot.x, y: robot.y });
        if (this.#trail.length > TRAIL_MAX) this.#trail.shift();
      }
      r.trail.setAttribute("d", polyPath(this.#trail));
      const goal = this.#runningPoint();
      r.goalLine.setAttribute("d", goal ? `M${robot.x} ${robot.y}L${goal.x} ${goal.y}` : "");
    } else {
      r.trail.setAttribute("d", "");
      r.goalLine.setAttribute("d", "");
    }
    if (this.#follow && runId && !this.#pan && !this.#tools.busy) this.#setView(centerOn(this.#view, robot.x, robot.y, this.#w, this.#h));
    this.#renderStatus();
  }

  /** The last point of the step that is running now (what the robot drives to). */
  #runningPoint(): PlacedPoint | null {
    for (const [id, mark] of this.store.runMarks) {
      if (mark.status !== "running") continue;
      const place = this.#model.byStep.get(id);
      const point = place?.points.find((p) => p.last) ?? place?.points[place.points.length - 1];
      if (point) return point;
    }
    return null;
  }

  #applyMarks(): void {
    if (this.store.dryRun) {
      this.#applyDryRun();
      return;
    }
    const marks = this.store.runMarks;
    for (const node of this.#stopByKey.values()) {
      const mark = marks.get(node.point.stepId);
      setMarkClass(node.outer, mark?.status);
    }
    for (const node of this.#legNodes) {
      const mark = marks.get(node.leg.stepId);
      setMarkClass(node.el, mark?.status);
      if (node.arrow) setMarkClass(node.arrow, mark?.status);
    }
    this.#renderStatus();
    this.#applyRobot();
  }

  #renderStatus(): void {
    const parts: string[] = [];
    let error = "";
    for (const [id, mark] of this.store.runMarks) {
      if (mark.status === "running") {
        const place = this.#model.byStep.get(id);
        const fb = mark.feedback;
        const bits: string[] = [];
        if (place) bits.push(place.title);
        if (fb && typeof fb.distance_remaining === "number") bits.push(`${fb.distance_remaining.toFixed(1)} m left`);
        if (fb && typeof fb.recoveries === "number") bits.push(`${fb.recoveries} recover${fb.recoveries === 1 ? "y" : "ies"}`);
        if (bits.length) parts.push(bits.join(" · "));
      } else if (mark.status === "failed" && mark.error && !error) {
        error = mark.error;
      }
    }
    const robot = this.store.status?.robot;
    const nodes: (Node | null)[] = [];
    if (parts.length) nodes.push(h("span", { class: "run", text: parts.join(" · ") }));
    else if (error) nodes.push(h("span", { class: "err", text: error }));
    if (robot) nodes.push(h("span", { class: "mono muted", text: `robot ${round1(robot.x)}, ${round1(robot.y)} · ${Math.round(robot.yaw_deg ?? 0)}°` }));
    nodes.push(h("span", { class: "mono muted", text: `${Math.round(this.#view.scale)} px/m` }));
    replace(this.#status, ...nodes);
  }

  /** "18.4 m \u00b7 about 1 min 20 s" in the map's corner while a preview is shown. */
  #renderInfo(): void {
    const p = this.store.routePreview;
    if (!p) {
      this.#info.hidden = true;
      replace(this.#info);
      return;
    }
    this.#info.hidden = false;
    const planned = p.legs.filter((l) => l.planned).length;
    replace(
      this.#info,
      h("span", { class: "kv", title: `${p.legs.length} leg${p.legs.length === 1 ? "" : "s"}, ${planned} planned by ${p.planned ? "the planner" : "the planner where it answered"}` }, icon("route2", 12), h("b", { text: routeSummary(p) })),
      h("span", { class: "muted small", text: p.speedMps ? `at ${p.speedMps} m/s` : "" }),
      h("button", { class: "icon-only ghost", title: "Plan again (Shift+P)", onclick: () => this.actions.previewRoute() }, icon("refresh", 11)),
      h("button", { class: "icon-only ghost", title: "Clear the preview", onclick: () => this.store.setRoutePreview(null) }, icon("close", 11)),
    );
  }

  /** The store's preview state changed: rebuild the plan, mutate the ghost. */
  #applyPreviews(): void {
    if (this.store.routePreview !== this.#shownPreview) {
      this.#renderPreviewRoute();
      this.#renderInfo();
      this.#renderNotes();
      this.#renderToolbar();
      this.#lastScale = 0;
      this.#applyView();
    }
    this.#applyDryRun();
  }

  /** Move the ghost robot to the playhead; nothing is rebuilt here. */
  #applyDryRun(): void {
    const g = this.#ghost;
    if (!g) return;
    const d = this.store.dryRun;
    this.el.classList.toggle("has-dryrun", !!d);
    if (!d) {
      g.group.classList.add("hidden");
      g.trail.setAttribute("d", "");
      for (const node of this.#stopByKey.values()) node.outer.classList.remove("dry-current");
      return;
    }
    const pose = poseAt(d.result, d.t);
    g.group.classList.toggle("hidden", !pose);
    if (!pose) return;
    g.group.setAttribute("transform", `translate(${pose.x} ${pose.y})`);
    g.body.setAttribute("transform", `rotate(${(-pose.yaw).toFixed(2)})`);
    g.trail.setAttribute("d", polyPath(trailUpTo(d.result, d.t)));
    const states = stepStatesAt(d.result, d.t);
    for (const node of this.#stopByKey.values()) {
      const st = states.get(node.point.stepId);
      node.outer.classList.toggle("dry-current", node.point.stepId === pose.step);
      setMarkClass(node.outer, st?.status);
    }
    for (const node of this.#legNodes) {
      const st = states.get(node.leg.stepId);
      setMarkClass(node.el, st?.status);
      if (node.arrow) setMarkClass(node.arrow, st?.status);
    }
  }

  /** The latest frame of every live layer (mutates four existing nodes). */
  #applyLive(): void {
    const live = this.#live;
    const cm = live.enabled.has("costmap") ? live.costmap : null;
    if (cm) {
      const [minX, minY, maxX, maxY] = cm.bounds;
      this.#costmapImg.setAttribute("x", String(minX));
      this.#costmapImg.setAttribute("y", String(-maxY));
      this.#costmapImg.setAttribute("width", String(Math.max(maxX - minX, 0.001)));
      this.#costmapImg.setAttribute("height", String(Math.max(maxY - minY, 0.001)));
      this.#costmapImg.setAttributeNS("http://www.w3.org/1999/xlink", "href", cm.image);
      this.#costmapImg.setAttribute("href", cm.image);
      this.#lCostmap.removeAttribute("hidden");
    } else this.#lCostmap.setAttribute("hidden", "hidden");
    const scan = live.enabled.has("scan") ? live.scan : null;
    this.#scanDots.setAttribute("d", scan ? dotsPath(scan.points, 0.035) : "");
    const plan = live.enabled.has("plan") ? live.plan : null;
    this.#planLine.setAttribute("d", plan ? polyPath(plan.points) : "");
    const foot = live.enabled.has("footprint") ? live.footprint : null;
    this.#footprint.setAttribute("d", foot && foot.points.length > 2 ? polygonPath(foot.points) : "");
    this.#renderNotes();
  }

  #applySelection(): void {
    const sel = this.store.selection;
    const stepId = sel?.kind === "step" ? sel.id : null;
    const siteName = sel?.kind === "site" ? sel.name : null;
    for (const node of this.#stopByKey.values()) node.outer.classList.toggle("selected", stepId !== null && node.point.stepId === stepId);
    for (const [name, node] of this.#siteNodes) node.classList.toggle("selected", name === siteName);
    const zoneName = sel?.kind === "zone" ? sel.name : null;
    for (const [name, node] of this.#zoneNodes) node.outer.classList.toggle("selected", name === zoneName);
    for (const node of this.#legNodes) node.el.classList.toggle("selected", stepId !== null && node.leg.stepId === stepId);
    this.#applyHighlight();
  }

  #applyHighlight(): void {
    const id = this.#hover;
    for (const node of this.#stopByKey.values()) node.outer.classList.toggle("hover", id !== null && node.point.stepId === id);
    for (const node of this.#legNodes) node.el.classList.toggle("hover", id !== null && node.leg.stepId === id);
    for (const el of this.#lPreview.children) el.classList.toggle("hover", id !== null && (el as SVGElement).dataset.step === id);
  }

  #applyPreview(): void {
    const p = this.#preview;
    this.el.classList.toggle("dragging", p !== null);
    // move a stop marker
    for (const [key, node] of this.#stopByKey) {
      const active = p && p.pointKey === key;
      const x = active ? p.x : node.point.x;
      const y = active ? p.y : node.point.y;
      node.outer.setAttribute("transform", `translate(${x} ${y})`);
      node.outer.classList.toggle("moving", !!active);
      if (active) {
        const yaw = p.yaw ?? 0;
        node.handle.setAttribute("transform", `rotate(${(-yaw).toFixed(2)})`);
        node.head?.setAttribute("transform", `rotate(${(-yaw).toFixed(2)})`);
      } else {
        const yaw = node.point.yaw ?? 0;
        node.handle.setAttribute("transform", `rotate(${(-yaw).toFixed(2)})`);
        node.head?.setAttribute("transform", `rotate(${(-yaw).toFixed(2)})`);
      }
    }
    // move a site marker
    for (const [name, node] of this.#siteNodes) {
      const site = this.store.activeSites[name];
      const active = p && p.siteName === name;
      const x = active ? p.x : site?.x ?? 0;
      const y = active ? p.y : site?.y ?? 0;
      node.setAttribute("transform", `translate(${x} ${y})`);
      node.classList.toggle("moving", !!active);
    }
    // a zone being dragged or reshaped
    for (const [name, node] of this.#zoneNodes) {
      const zp = p && p.zone && p.zone.name === name ? p.zone : null;
      node.outer.classList.toggle("moving", zp !== null);
      if (!zp) continue;
      const d = polygonPath(zp.points);
      node.shape.setAttribute("d", d);
      node.hatch.setAttribute("d", d);
      const verts = [...node.verts.children];
      zp.points.forEach((pt, i) => verts[i]?.setAttribute("transform", `translate(${pt.x} ${pt.y})`));
      const c = polygonCentroid(zp.points);
      node.label.setAttribute("transform", `translate(${c.x} ${c.y})`);
    }
    // legs follow the dragged point
    if (p?.pointKey) {
      for (const node of this.#legsByKey.get(p.pointKey) ?? []) {
        const from = node.leg.from.key === p.pointKey ? { x: p.x, y: p.y } : node.leg.from;
        const to = node.leg.to.key === p.pointKey ? { x: p.x, y: p.y } : node.leg.to;
        node.el.setAttribute("d", legPath(from, to));
        node.arrow?.setAttribute("transform", `translate(${(from.x + to.x) / 2} ${(from.y + to.y) / 2})`);
      }
    } else {
      for (const node of this.#legNodes) {
        node.el.setAttribute("d", legPath(node.leg.from, node.leg.to));
        node.arrow?.setAttribute("transform", `translate(${(node.leg.from.x + node.leg.to.x) / 2} ${(node.leg.from.y + node.leg.to.y) / 2})`);
      }
    }
    this.#renderDraft();
  }

  #renderDraft(): void {
    const nodes: SVGElement[] = [];
    const d = this.#draft;
    if (d) {
      const pts = d.cursor ? [...d.points, d.cursor] : d.points;
      if (pts.length > 1) nodes.push(svg("path", { class: `draft-line ${d.kind}`, "vector-effect": "non-scaling-stroke", d: polyPath(pts) }));
      for (const p of d.points) {
        const { outer, inner } = this.#marker(p.x, p.y, "draft-dot", false);
        inner.append(svg("circle", { r: 4 }));
        nodes.push(outer);
      }
      if (d.cursor) {
        const { outer, inner } = this.#marker(d.cursor.x, d.cursor.y, `draft-cursor${d.snap ? " snap" : ""}`, false);
        inner.append(svg("circle", { r: 5 }));
        if (d.snap) inner.append(svg("text", { class: "label", "text-anchor": "middle", y: 18, text: d.snap }));
        nodes.push(outer);
      }
    }
    const p = this.#preview;
    if (p?.ghost) {
      const { outer, inner } = this.#marker(p.x, p.y, `ghost${p.snap ? " snap" : ""}`, false);
      const yaw = p.yaw ?? 0;
      inner.append(svg("circle", { class: "disc", r: STOP_R }));
      if (p.yaw !== null) inner.append(svg("path", { class: "head-tri", transform: `rotate(${(-yaw).toFixed(2)})`, d: `M${STOP_R + 1} -5L${STOP_R + 9} 0L${STOP_R + 1} 5Z` }));
      if (p.snap) inner.append(svg("text", { class: "label", "text-anchor": "middle", y: STOP_R + 14, text: p.snap }));
      nodes.push(outer);
    }
    this.#lDraft.replaceChildren(...nodes);
  }

  // ---- pointer, wheel, keys -------------------------------------------------------------------

  #local(e: PointerEvent | WheelEvent): { sx: number; sy: number } {
    const r = this.#svg.getBoundingClientRect();
    return { sx: e.clientX - r.left, sy: e.clientY - r.top };
  }

  #bindPointer(): void {
    const el = this.#svg;
    el.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const { sx, sy } = this.#local(e);
        const delta = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
        this.#setView(zoomAt(this.#view, sx, sy, Math.exp(-delta * 0.0018)));
      },
      { passive: false },
    );
    el.addEventListener("contextmenu", (e) => e.preventDefault());
    el.addEventListener("pointerdown", (e) => {
      this.#closeInlineForm();
      const { sx, sy } = this.#local(e);
      el.setPointerCapture(e.pointerId);
      this.#pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (this.#pointers.size === 2) {
        this.#tools.cancel();
        this.#pan = null;
        this.#pinch = this.#pinchState();
        return;
      }
      const wantsPan = e.button === 1 || e.button === 2 || this.#space || (e.pointerType === "touch" && this.#tools.tool === "select" && !this.hitAt(sx, sy));
      if (wantsPan || (e.button === 0 && !this.#tools.pointerDown(sx, sy))) {
        this.#pan = { sx, sy, tx: this.#view.tx, ty: this.#view.ty };
        this.el.classList.add("panning");
      }
    });
    el.addEventListener("pointermove", (e) => {
      const { sx, sy } = this.#local(e);
      if (this.#pointers.has(e.pointerId)) this.#pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (this.#pinch) {
        const next = this.#pinchState();
        if (next && this.#pinch.dist > 0) {
          const r = this.#svg.getBoundingClientRect();
          const cx = next.cx - r.left;
          const cy = next.cy - r.top;
          const v = zoomAt(this.#view, cx, cy, next.dist / this.#pinch.dist);
          this.#setView({ scale: v.scale, tx: v.tx + (next.cx - this.#pinch.cx), ty: v.ty + (next.cy - this.#pinch.cy) });
          this.#pinch = next;
        }
        return;
      }
      if (this.#pan) {
        this.#setView({ scale: this.#view.scale, tx: this.#pan.tx + (sx - this.#pan.sx), ty: this.#pan.ty + (sy - this.#pan.sy) });
        return;
      }
      this.#tools.pointerMove(sx, sy);
      if (this.#tools.tool === "select") this.#hoverAt(sx, sy);
    });
    const end = (e: PointerEvent): void => {
      const { sx, sy } = this.#local(e);
      this.#pointers.delete(e.pointerId);
      if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
      if (this.#pinch) {
        if (this.#pointers.size < 2) this.#pinch = null;
        return;
      }
      if (this.#pan) {
        this.#pan = null;
        this.el.classList.remove("panning");
        return;
      }
      this.#tools.pointerUp(sx, sy);
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", (e) => {
      this.#pointers.delete(e.pointerId);
      this.#pan = null;
      this.#pinch = null;
      this.#tools.cancel();
      this.el.classList.remove("panning");
    });
    el.addEventListener("pointerleave", () => {
      if (!this.#pan && !this.#tools.busy) this.#setHover(null);
    });
    el.addEventListener("dblclick", (e) => {
      e.preventDefault();
      this.#tools.doubleClick();
    });
  }

  #pinchState(): { dist: number; cx: number; cy: number } | null {
    const pts = [...this.#pointers.values()];
    if (pts.length < 2) return null;
    const a = pts[0]!;
    const b = pts[1]!;
    return { dist: Math.hypot(b.x - a.x, b.y - a.y), cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2 };
  }

  #hoverAt(sx: number, sy: number): void {
    const hit = this.hitAt(sx, sy);
    const overStep = hit !== null && (hit.kind === "point" || hit.kind === "handle");
    this.#setHover(overStep ? hit.point.stepId : null);
    this.el.classList.toggle("over-target", hit !== null);
  }

  #setHover(id: string | null): void {
    if (this.#hover === id) return;
    this.#hover = id;
    this.#applyHighlight();
    this.onHoverStep?.(id);
  }

  #docKey(e: KeyboardEvent, down: boolean): void {
    if (e.key !== " ") return;
    const t = e.target as HTMLElement | null;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (down === this.#space) return;
    this.#space = down;
    this.el.classList.toggle("space", down);
  }
}

// ---- helpers ---------------------------------------------------------------------------------

function note(kind: "info" | "warn", text: string): HTMLElement {
  return h("div", { class: `map-note ${kind}` }, icon(kind === "warn" ? "alert" : "info", 12), h("span", { text }));
}

function boundsFromMeta(map: MapData): Bounds {
  const [minX, minY, maxX, maxY] = map.meta.bounds;
  return { minX, minY, maxX, maxY };
}

function legPath(from: WorldPoint, to: WorldPoint): string {
  return `M${from.x.toFixed(3)} ${from.y.toFixed(3)}L${to.x.toFixed(3)} ${to.y.toFixed(3)}`;
}

/** One tiny square per point: far cheaper than a circle element each. */
function dotsPath(points: readonly WorldPoint[], size: number): string {
  const out: string[] = [];
  for (const p of points) out.push(`M${(p.x - size).toFixed(3)} ${(p.y - size).toFixed(3)}h${(size * 2).toFixed(3)}v${(size * 2).toFixed(3)}h${(-size * 2).toFixed(3)}Z`);
  return out.join("");
}

/** The diagonal hatch each zone kind fills itself with. */
function hatchDefs(): SVGDefsElement {
  const defs = svg("defs");
  // the tile lives in world units: 0.4 m, so the hatch keeps its size on the floor
  const specs: { id: string; d: string; cls: string }[] = [
    { id: "zone-hatch-keepout", d: "M0 0.4L0.4 0", cls: "hatch keepout" },
    { id: "zone-hatch-speed_limit", d: "M0 0L0.4 0.4", cls: "hatch speed" },
    { id: "zone-hatch-preferred", d: "M0.2 0V0.4", cls: "hatch soft" },
    { id: "zone-hatch-work", d: "M0 0.2H0.4", cls: "hatch soft" },
  ];
  for (const spec of specs) {
    const pat = svg("pattern", { id: spec.id, width: 0.4, height: 0.4, patternUnits: "userSpaceOnUse" });
    pat.append(svg("path", { class: spec.cls, d: spec.d }));
    defs.append(pat);
  }
  return defs;
}

function polyPath(points: readonly WorldPoint[]): string {
  if (points.length < 2) return "";
  return points.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(3)} ${p.y.toFixed(3)}`).join("");
}

function setMarkClass(el: Element, status: string | undefined): void {
  el.classList.toggle("run-running", status === "running");
  el.classList.toggle("run-succeeded", status === "succeeded");
  el.classList.toggle("run-failed", status === "failed" || status === "timeout");
  el.classList.toggle("run-canceled", status === "canceled");
}

/** Battery for chargers, a bar for docks, a dot for waypoints; stations get the plain diamond. */
function siteGlyph(name: string, site: Site): SVGElement | null {
  const hay = `${name} ${site.dock_id ?? ""} ${site.dock_type ?? ""}`.toLowerCase();
  if (hay.includes("charg")) {
    return svg("g", { class: "site-glyph" }, svg("rect", { x: -4, y: -2.5, width: 7, height: 5, rx: 1 }), svg("path", { d: "M4 -1.2V1.2" }));
  }
  if (site.kind === "dock") return svg("path", { class: "site-glyph", d: "M-3.5 2H3.5M-3.5 2V-2M3.5 2V-2" });
  if (site.kind === "home") return svg("path", { class: "site-glyph", d: "M-3 3V-0.5L0 -3L3 -0.5V3Z" });
  if (site.kind === "waypoint") return svg("circle", { class: "site-glyph", r: 1.7 });
  return null;
}
