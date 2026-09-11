/**
 * Sites panel: map selector, site rows (inline editable), add / delete /
 * capture from robot, save to the runner, drag a site onto the steps list.
 */

import type { Store } from "../state/Store";
import type { AppActions } from "./actions";
import type { Site, SiteKind } from "../model/types";
import { SITE_KINDS, emptySites } from "../model/types";
import { h, replace, select, stopEvent } from "./dom";
import { icon } from "./icons";
import type { IconName } from "./icons";
import { promptText, toast } from "./dialogs";
import { SITE_DND_TYPE } from "./StepsList";

const KIND_ICON: Record<SiteKind, IconName> = { station: "mapPin", dock: "anchor", waypoint: "flag", home: "home" };

export class SitesPanel {
  readonly el: HTMLElement;
  #store: Store;
  #actions: AppActions;
  #mapSel: HTMLSelectElement;
  #list: HTMLElement;
  #saveBtn: HTMLButtonElement;
  #editing: string | null = null;

  constructor(store: Store, actions: AppActions) {
    this.#store = store;
    this.#actions = actions;
    this.#mapSel = h("select", { class: "map-select", title: "Active map" });
    this.#mapSel.addEventListener("change", () => {
      store.activeMap = this.#mapSel.value;
    });
    this.#list = h("div", { class: "site-list" });
    this.#saveBtn = h("button", { class: "primary small", onclick: () => actions.saveSites() }, "Save");
    const toolbar = h(
      "div",
      { class: "side-foot" },
      h("button", { class: "ghost small", title: "Add a site by hand", onclick: () => void this.#addSite() }, icon("plus", 12), "Add site"),
      h("button", { class: "ghost small capture", title: "Add a site at the robot's current pose (GET /api/robot/pose)", onclick: () => void this.#capture() }, icon("crosshair", 12), "Capture"),
      h("span", { class: "grow" }),
      this.#saveBtn,
    );
    const mapRow = h("div", { class: "map-row" }, h("span", { class: "muted", text: "Map" }), this.#mapSel, h("button", { class: "icon-only ghost", title: "Add a map", onclick: () => void this.#addMap() }, icon("plus", 12)));
    this.el = h("div", { class: "side-panel sites", id: "sites-section" }, mapRow, this.#list, toolbar);
    for (const t of ["sites", "remote", "connection"] as const) store.on(t, () => this.render());
    this.render();
  }

  render(): void {
    const s = this.#store;
    const sites = s.sites;
    const active = s.activeMap;
    const current = s.status?.current_map ?? null;
    const maps = Object.keys(sites?.maps ?? {}).sort();
    this.#mapSel.replaceChildren();
    if (!maps.length) this.#mapSel.append(h("option", { value: "", text: sites ? "no maps" : "sites unknown" }));
    for (const name of maps) this.#mapSel.append(h("option", { value: name, text: name === current ? `${name} (robot)` : name }));
    this.#mapSel.value = active ?? "";
    this.#mapSel.disabled = maps.length === 0;

    const rows: HTMLElement[] = [];
    const map = active && sites ? sites.maps[active] : undefined;
    const entries = Object.entries(map?.sites ?? {}).sort(([a], [b]) => a.localeCompare(b));
    for (const [name, site] of entries) rows.push(this.#editing === name ? this.#editRow(name, site) : this.#row(name, site));
    if (!rows.length) rows.push(h("div", { class: "empty", text: sites ? (active ? "No sites in this map." : "No map selected.") : s.connected ? "Loading sites…" : "Offline: sites unknown. Add a map to define sites locally." }));
    replace(this.#list, ...rows);
    const dirty = s.sitesDirty;
    this.#saveBtn.disabled = !s.connected || !dirty;
    this.#saveBtn.title = !s.connected ? "Runner offline" : dirty ? "PUT /api/sites" : "No changes";
    this.#saveBtn.classList.toggle("attention", dirty && s.connected);
    const capture = this.el.querySelector<HTMLButtonElement>("button.capture");
    if (capture) {
      capture.disabled = !s.connected;
      capture.title = s.connected ? "Add a site at the robot's current pose" : "Runner offline";
    }
  }

  #row(name: string, site: Site): HTMLElement {
    const kind = site.kind ?? "station";
    const row = h(
      "div",
      {
        class: "site-row",
        draggable: true,
        title: `${name} · ${kind}${site.dock_id ? ` · dock ${site.dock_id}` : ""}${site.notes ? `\n${site.notes}` : ""}\nDrag onto the steps list to add "Go to ${name}"; double-click to edit.`,
        onclick: () => this.#store.select({ kind: "site", name }),
        ondblclick: () => {
          this.#editing = name;
          this.render();
        },
        ondragstart: (e: DragEvent) => {
          e.dataTransfer?.setData(SITE_DND_TYPE, name);
          e.dataTransfer?.setData("text/plain", name);
          if (e.dataTransfer) e.dataTransfer.effectAllowed = "copy";
        },
      },
      h("span", { class: "kind" }, icon(KIND_ICON[kind] ?? "mapPin", 12)),
      h("span", { class: "name grow", text: name }),
      h("span", { class: "coords mono muted", text: `${fmt(site.x)}, ${fmt(site.y)} · ${fmt(site.yaw_deg ?? 0)}°` }),
      h("span", { class: "row-actions" },
        h("button", { class: "icon-only ghost", title: "Insert a 'Go to' step", onclick: (e: Event) => {
          stopEvent(e);
          this.#actions.insertSite(name);
        } }, icon("navigation", 11)),
        h("button", { class: "icon-only ghost", title: "Set coordinates from the robot pose", onclick: (e: Event) => {
          stopEvent(e);
          this.#actions.captureSite(name, site);
        } }, icon("crosshair", 11)),
        h("button", { class: "icon-only ghost", title: "Edit", onclick: (e: Event) => {
          stopEvent(e);
          this.#editing = name;
          this.render();
        } }, icon("edit", 11)),
        h("button", { class: "icon-only ghost danger", title: "Delete", onclick: (e: Event) => {
          stopEvent(e);
          void this.#delete(name);
        } }, icon("trash", 11)),
      ),
    );
    return row;
  }

  #editRow(name: string, site: Site): HTMLElement {
    const nameIn = h("input", { type: "text", value: name, class: "name", placeholder: "Name" });
    const x = h("input", { type: "number", step: "0.01", value: String(site.x), class: "num" });
    const y = h("input", { type: "number", step: "0.01", value: String(site.y), class: "num" });
    const yaw = h("input", { type: "number", step: "1", value: String(site.yaw_deg ?? 0), class: "num" });
    const kind = select(SITE_KINDS.map((k) => ({ value: k })), site.kind ?? "station");
    const dockId = h("input", { type: "text", value: site.dock_id ?? "", placeholder: "dock id" });
    const dockType = h("input", { type: "text", value: site.dock_type ?? "", placeholder: "dock type" });
    const notes = h("input", { type: "text", value: site.notes ?? "", placeholder: "notes" });
    const commit = (): void => {
      const newName = nameIn.value.trim();
      if (!newName) {
        toast("Site name cannot be empty", "warn");
        return;
      }
      const updated: Site = { x: Number(x.value) || 0, y: Number(y.value) || 0, yaw_deg: Number(yaw.value) || 0, kind: kind.value as SiteKind };
      if (dockId.value.trim()) updated.dock_id = dockId.value.trim();
      if (dockType.value.trim()) updated.dock_type = dockType.value.trim();
      if (notes.value.trim()) updated.notes = notes.value.trim();
      this.#mutate((sites) => {
        if (newName !== name) delete sites[name];
        sites[newName] = updated;
      });
      this.#editing = null;
      this.render();
    };
    const cancel = (): void => {
      this.#editing = null;
      this.render();
    };
    const onKey = (e: KeyboardEvent): void => {
      e.stopPropagation();
      if (e.key === "Enter") commit();
      else if (e.key === "Escape") cancel();
    };
    for (const el of [nameIn, x, y, yaw, dockId, dockType, notes]) el.addEventListener("keydown", onKey);
    const dockRow = h("div", { class: "line", hidden: kind.value !== "dock" }, dockId, dockType);
    kind.addEventListener("change", () => {
      dockRow.hidden = kind.value !== "dock";
    });
    const row = h(
      "div",
      { class: "site-row editing" },
      h("div", { class: "line" }, nameIn, kind),
      h("div", { class: "line" }, h("span", { class: "muted", text: "x" }), x, h("span", { class: "muted", text: "y" }), y, h("span", { class: "muted", text: "yaw°" }), yaw),
      dockRow,
      h("div", { class: "line" }, notes),
      h("div", { class: "line right" }, h("button", { class: "small", onclick: cancel }, "Cancel"), h("button", { class: "small primary", onclick: commit }, icon("check", 11), "OK")),
    );
    setTimeout(() => nameIn.focus(), 0);
    return row;
  }

  /** Apply a change to the active map's site table (creating the sites doc if needed). */
  #mutate(fn: (sites: Record<string, Site>) => void): void {
    this.#store.mutateSites(fn);
  }

  async #addSite(): Promise<void> {
    const taken = new Set(Object.keys(this.#store.sites?.maps[this.#store.activeMap ?? ""]?.sites ?? {}));
    const name = await promptText("New site", { label: "Name", placeholder: "Rack3", validate: (v) => (v.trim() === "" ? "Name is required" : taken.has(v.trim()) ? "A site with this name exists" : null) });
    if (!name) return;
    this.#mutate((sites) => {
      sites[name.trim()] = { x: 0, y: 0, yaw_deg: 0, kind: "station" };
    });
    this.#editing = name.trim();
    this.render();
  }

  async #addMap(): Promise<void> {
    const name = await promptText("New map", { label: "Map name", placeholder: "warehouse_b", help: "Use the map name Nav2 knows; set the .yaml path after saving if nav.change_map should switch to it.", validate: (v) => (v.trim() === "" ? "Name is required" : this.#store.sites?.maps[v.trim()] ? "This map exists" : null) });
    if (!name) return;
    const s = this.#store;
    const doc = s.sites ? structuredClone(s.sites) : emptySites();
    doc.maps[name.trim()] = { sites: {} };
    if (!doc.default_map) doc.default_map = name.trim();
    s.setSites(doc, false);
    s.activeMap = name.trim();
  }

  async #capture(): Promise<void> {
    const taken = new Set(Object.keys(this.#store.sites?.maps[this.#store.activeMap ?? ""]?.sites ?? {}));
    const name = await promptText("Capture site from robot", { label: "Name", placeholder: "Site name", validate: (v) => (v.trim() === "" ? "Name is required" : taken.has(v.trim()) ? "A site with this name exists" : null) });
    if (!name) return;
    this.#actions.captureSite(name.trim());
  }

  #delete(name: string): void {
    this.#actions.deleteSite(name);
  }

  /** Set a site's coordinates (used by "capture from robot"). */
  setSite(name: string, site: Site): void {
    this.#mutate((sites) => {
      sites[name] = site;
    });
  }
}

function fmt(v: number): string {
  return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(2)));
}
