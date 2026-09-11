/**
 * Missions list (sidebar tab): one row per runner mission or local draft,
 * title only plus a status dot while running / queued / suspended. Drafts
 * and modified missions are italic; details live in the tooltip and the
 * per-row menu (open, rename, duplicate, export, delete).
 */

import type { Store } from "../state/Store";
import type { AppActions } from "./actions";
import { h, replace, stopEvent } from "./dom";
import { icon } from "./icons";

export class MissionsPanel {
  readonly el: HTMLElement;
  #store: Store;
  #actions: AppActions;
  #list: HTMLElement;
  #menu: HTMLElement | null = null;

  constructor(store: Store, actions: AppActions) {
    this.#store = store;
    this.#actions = actions;
    this.#list = h("div", { class: "mission-list" });
    const foot = h(
      "div",
      { class: "side-foot" },
      h("button", { class: "ghost small", title: "New mission from a template", onclick: () => actions.newMission() }, icon("plus", 12), "New mission"),
      h("button", { class: "ghost small", title: "Import a mission JSON file", onclick: () => actions.importJson() }, icon("folder", 12), "Import"),
    );
    this.el = h("div", { class: "side-panel missions", id: "missions-section" }, this.#list, foot);
    for (const t of ["mission", "remote", "drafts", "connection"] as const) store.on(t, () => this.render());
    this.render();
  }

  render(): void {
    const s = this.#store;
    const open = s.mission;
    const rows: HTMLElement[] = [];
    const seen = new Set<string>();
    const summaries = [...s.missions].sort((a, b) => a.name.localeCompare(b.name));
    for (const m of summaries) {
      seen.add(m.name);
      const draft = s.draft(m.name);
      const isOpen = !!open && open.name === m.name;
      const modified = isOpen ? s.dirty : !!draft;
      const state = m.state ?? "idle";
      const dotCls = state === "running" ? "run" : state === "queued" || state === "suspended" ? "wait" : "";
      const problems = [...(m.errors ?? []).map((e) => e.message), ...(m.trigger_problems ?? [])];
      const tip = [`${m.name}${m.version ? ` · v${m.version}` : ""}`, m.triggers?.length ? `starts: ${m.triggers.join(", ")}` : "manual only", state !== "idle" ? state : "", modified ? "modified locally" : "", ...problems].filter(Boolean).join("\n");
      rows.push(this.#row({ name: m.name, title: m.title || m.name, dot: dotCls, tip, isOpen, italic: modified, draftOnly: false }));
    }
    for (const d of s.drafts) {
      if (seen.has(d.name)) continue;
      seen.add(d.name);
      const isOpen = !!open && open.name === d.name;
      rows.push(this.#row({ name: d.name, title: d.mission.title || d.name, dot: "", tip: `${d.name}\ndraft · not on the robot`, isOpen, italic: true, draftOnly: true }));
    }
    if (open && !seen.has(open.name)) {
      rows.unshift(this.#row({ name: open.name, title: open.title || open.name, dot: "", tip: `${open.name}\nnew · not on the robot`, isOpen: true, italic: true, draftOnly: true }));
    }
    if (!rows.length) rows.push(h("div", { class: "empty", text: s.connected ? "No missions on the robot yet." : "Offline. Create a draft or import a file." }));
    replace(this.#list, ...rows);
  }

  #row(o: { name: string; title: string; dot: string; tip: string; isOpen: boolean; italic: boolean; draftOnly: boolean }): HTMLElement {
    return h(
      "div",
      { class: `mission-row${o.isOpen ? " open" : ""}${o.italic ? " draft" : ""}`, title: o.tip, onclick: () => this.#actions.openMission(o.name) },
      h("span", { class: "name grow", text: o.title }),
      o.dot ? h("span", { class: `dot ${o.dot}` }) : null,
      h("button", { class: "icon-only ghost row-menu", title: "Actions", onclick: (e: MouseEvent) => this.#openMenu(e, o.name, o.draftOnly) }, icon("more", 12)),
    );
  }

  #openMenu(e: MouseEvent, name: string, draftOnly: boolean): void {
    stopEvent(e);
    this.#closeMenu();
    const a = this.#actions;
    const items: { label: string; icon: "folder" | "edit" | "copy" | "download" | "trash"; action: () => void; danger?: boolean }[] = [
      { label: "Open", icon: "folder", action: () => a.openMission(name) },
      { label: "Rename…", icon: "edit", action: () => a.renameMission(name) },
      { label: "Duplicate", icon: "copy", action: () => a.duplicateMission(name) },
      { label: "Export JSON", icon: "download", action: () => a.exportJson() },
      { label: draftOnly ? "Delete draft…" : "Delete…", icon: "trash", action: () => a.deleteMission(name), danger: true },
    ];
    const menu = h("div", { class: "menu" });
    for (const it of items) {
      menu.append(h("button", { class: `menu-item${it.danger ? " danger" : ""}`, onclick: () => {
        this.#closeMenu();
        if (it.label === "Export JSON") {
          // export needs the mission open
          if (this.#store.mission?.name !== name) a.openMission(name);
          setTimeout(() => it.action(), 50);
        } else it.action();
      } }, icon(it.icon), it.label));
    }
    const btn = e.currentTarget as HTMLElement;
    const r = btn.getBoundingClientRect();
    menu.style.top = `${r.bottom + 2}px`;
    menu.style.left = `${Math.min(r.left, window.innerWidth - 200)}px`;
    document.body.append(menu);
    this.#menu = menu;
    setTimeout(() => {
      document.addEventListener("mousedown", (ev) => {
        if (!menu.contains(ev.target as Node)) this.#closeMenu();
      }, { once: true });
      document.addEventListener("keydown", () => this.#closeMenu(), { once: true });
    }, 0);
  }

  #closeMenu(): void {
    this.#menu?.remove();
    this.#menu = null;
  }
}
