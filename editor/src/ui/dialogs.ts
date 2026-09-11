/**
 * Modal dialogs and toasts. Every dialog returns a promise; Esc resolves with
 * null. The STOP button in the top bar stays above the backdrop (see CSS).
 */

import type { Finding, InputDef, Mission, SitesDoc, ZoneKind } from "../model/types";
import { MISSION_NAME_RE, ZONE_KINDS } from "../model/types";
import { ZONE_STYLES } from "../map/zones";
import type { BlockDef, TriggerDef } from "../model/blocks";
import { blocksByGroup, allTriggers } from "../model/blocks";
import { pathToString } from "../model/validate";
import { h, append, field, select, stopEvent } from "./dom";
import { icon } from "./icons";
import type { IconName } from "./icons";

export interface DialogButton {
  label: string;
  value: string;
  primary?: boolean;
  danger?: boolean;
}

export interface ModalOptions {
  title: string;
  body: (Node | string)[];
  buttons: DialogButton[];
  /** Extra class on the dialog box (e.g. "wide"). */
  cls?: string;
  /** Called after the dialog is in the DOM (focus a field, ...); `close` resolves the dialog. */
  onOpen?: (box: HTMLElement, close: (value: string | null) => void) => void;
  /** Return false to keep the dialog open. */
  onClose?: (value: string | null) => boolean | void;
}

let root: HTMLElement | null = null;
const openStack: HTMLElement[] = [];

function modalRoot(): HTMLElement {
  if (!root) {
    root = h("div", { class: "modal-root" });
    document.body.append(root);
  }
  return root;
}

export function modal(opts: ModalOptions): Promise<string | null> {
  return new Promise((resolve) => {
    const box = h("div", { class: `modal${opts.cls ? ` ${opts.cls}` : ""}`, role: "dialog", "aria-modal": "true" });
    const backdrop = h("div", { class: "modal-backdrop" }, box);
    const finish = (value: string | null): void => {
      if (opts.onClose && opts.onClose(value) === false) return;
      backdrop.remove();
      const i = openStack.indexOf(backdrop);
      if (i >= 0) openStack.splice(i, 1);
      document.removeEventListener("keydown", onKey, true);
      resolve(value);
    };
    const onKey = (e: KeyboardEvent): void => {
      if (openStack[openStack.length - 1] !== backdrop) return;
      if (e.key === "Escape") {
        stopEvent(e);
        finish(null);
      } else if (e.key === "Enter" && !(e.target instanceof HTMLTextAreaElement)) {
        const primary = opts.buttons.find((b) => b.primary);
        if (primary && !(e.target instanceof HTMLButtonElement)) {
          stopEvent(e);
          finish(primary.value);
        }
      }
    };
    const head = h("div", { class: "modal-head" }, h("span", { class: "grow", text: opts.title }), h("button", { class: "icon-only ghost", title: "Close", onclick: () => finish(null) }, icon("close")));
    const body = h("div", { class: "modal-body" });
    append(body, opts.body);
    const foot = h("div", { class: "modal-foot" });
    for (const b of opts.buttons) {
      foot.append(h("button", { class: b.primary ? "primary" : b.danger ? "danger" : "", onclick: () => finish(b.value) }, b.label));
    }
    box.append(head, body, foot);
    backdrop.addEventListener("mousedown", (e) => {
      if (e.target === backdrop) finish(null);
    });
    modalRoot().append(backdrop);
    openStack.push(backdrop);
    document.addEventListener("keydown", onKey, true);
    opts.onOpen?.(box, finish);
    if (!opts.onOpen) {
      const first = box.querySelector<HTMLElement>("input, select, textarea, button.primary");
      first?.focus();
    }
  });
}

export function confirm(text: string, opts: { title?: string; ok?: string; danger?: boolean; cancel?: string } = {}): Promise<boolean> {
  return modal({
    title: opts.title ?? "Confirm",
    body: [h("p", { text })],
    buttons: [
      { label: opts.cancel ?? "Cancel", value: "cancel" },
      { label: opts.ok ?? "OK", value: "ok", primary: !opts.danger, danger: opts.danger },
    ],
  }).then((v) => v === "ok");
}

export function alertDialog(title: string, text: string): Promise<void> {
  return modal({ title, body: [h("p", { text })], buttons: [{ label: "OK", value: "ok", primary: true }] }).then(() => undefined);
}

export interface PromptOptions {
  label?: string;
  value?: string;
  placeholder?: string;
  help?: string;
  mono?: boolean;
  validate?: (v: string) => string | null;
  ok?: string;
}

export function promptText(title: string, opts: PromptOptions = {}): Promise<string | null> {
  const input = h("input", { type: "text", value: opts.value ?? "", placeholder: opts.placeholder ?? "", class: opts.mono ? "mono" : "", spellcheck: false });
  const err = h("div", { class: "field-error" });
  const row = h("div", { class: "field wide" }, h("label", { text: opts.label ?? "" }), h("div", { class: "control" }, input), opts.help ? h("div", { class: "field-help", text: opts.help }) : null, err);
  const check = (): boolean => {
    const msg = opts.validate ? opts.validate(input.value) : null;
    err.textContent = msg ?? "";
    row.classList.toggle("has-error", !!msg);
    return !msg;
  };
  input.addEventListener("input", check);
  return modal({
    title,
    body: [row],
    buttons: [
      { label: "Cancel", value: "cancel" },
      { label: opts.ok ?? "OK", value: "ok", primary: true },
    ],
    onOpen: () => {
      input.focus();
      input.select();
    },
    onClose: (v) => (v === "ok" ? check() : true),
  }).then((v) => (v === "ok" ? input.value : null));
}

export function missionNameValidator(taken: ReadonlySet<string>, current?: string): (v: string) => string | null {
  return (v) => {
    if (!MISSION_NAME_RE.test(v)) return "Lowercase letters, digits, _ and -, starting with a letter (max 64).";
    if (v !== current && taken.has(v)) return `'${v}' already exists.`;
    return null;
  };
}

/** Dialog listing validation findings with their paths. */
export function showFindings(title: string, findings: Finding[], intro?: string): Promise<void> {
  const list = h("ul", { class: "finding-list" });
  for (const f of findings) {
    list.append(h("li", { class: f.level }, h("span", { class: "path mono", text: pathToString(f.path) }), h("span", { text: f.message })));
  }
  const body: Node[] = intro ? [h("p", { text: intro }), list] : [list];
  return modal({ title, cls: "wide", body, buttons: [{ label: "OK", value: "ok", primary: true }] }).then(() => undefined);
}

// ---- inputs form (Run) ---------------------------------------------------------------------

function siteNames(sites: SitesDoc | null, map: string | null): string[] {
  if (!sites || !map) return [];
  return Object.keys(sites.maps[map]?.sites ?? {}).sort();
}

/** Ask for the mission inputs; returns the values or null when canceled. */
export function inputsForm(mission: Mission, sites: SitesDoc | null, activeMap: string | null): Promise<Record<string, unknown> | null> {
  const defs = Object.entries(mission.inputs ?? {});
  const controls = new Map<string, { get: () => unknown; def: InputDef }>();
  const rows: HTMLElement[] = [];
  const errBox = h("div", { class: "field-error" });
  for (const [name, def] of defs) {
    const label = `${def.label ?? name}${def.required ? " *" : ""}`;
    let ctl: HTMLElement;
    let get: () => unknown;
    const dflt = def.default;
    switch (def.type) {
      case "site": {
        const names = siteNames(sites, activeMap);
        const cur = typeof dflt === "string" ? dflt : "";
        if (names.length) {
          const opts: { value: string; label?: string }[] = names.map((n) => ({ value: n }));
          if (!def.required) opts.unshift({ value: "", label: "(none)" });
          const sel = select(opts, cur);
          ctl = sel;
          get = () => (sel.value === "" ? undefined : sel.value);
        } else {
          const inp = h("input", { type: "text", value: cur, placeholder: "Site name" });
          ctl = inp;
          get = () => (inp.value === "" ? undefined : inp.value);
        }
        break;
      }
      case "number": {
        const inp = h("input", { type: "number", step: "any", value: typeof dflt === "number" ? String(dflt) : "" });
        ctl = inp;
        get = () => (inp.value === "" ? undefined : Number(inp.value));
        break;
      }
      case "boolean": {
        const inp = h("input", { type: "checkbox" });
        inp.checked = dflt === true;
        ctl = inp;
        get = () => inp.checked;
        break;
      }
      case "string": {
        const inp = h("input", { type: "text", value: typeof dflt === "string" ? dflt : "" });
        ctl = inp;
        get = () => (inp.value === "" ? undefined : inp.value);
        break;
      }
      default: {
        // pose / json: JSON text
        const ta = h("textarea", { class: "mono", rows: 3, spellcheck: false });
        ta.value = dflt === undefined ? "" : JSON.stringify(dflt, null, 1).replace(/\n\s*/g, " ");
        ctl = ta;
        get = () => {
          const t = ta.value.trim();
          if (t === "") return undefined;
          try {
            return JSON.parse(t) as unknown;
          } catch {
            return t; // a site name or $expression for pose inputs
          }
        };
      }
    }
    controls.set(name, { get, def });
    rows.push(h("div", { class: "field" }, h("label", { text: label, title: def.description ?? "" }), h("div", { class: "control" }, ctl), def.description ? h("div", { class: "field-help", text: def.description }) : null));
  }
  let values: Record<string, unknown> = {};
  return modal({
    title: `Run ${mission.title ?? mission.name}`,
    body: [h("div", { class: "form" }, ...rows), errBox],
    buttons: [
      { label: "Cancel", value: "cancel" },
      { label: "Run", value: "run", primary: true },
    ],
    onClose: (v) => {
      if (v !== "run") return true;
      values = {};
      for (const [name, c] of controls) {
        const val = c.get();
        if (val === undefined) {
          if (c.def.required && c.def.default === undefined) {
            errBox.textContent = `'${c.def.label ?? name}' is required.`;
            return false;
          }
          continue;
        }
        values[name] = val;
      }
      return true;
    },
  }).then((v) => (v === "run" ? values : null));
}

// ---- zones -------------------------------------------------------------------------------------

export interface ZoneFormValue {
  name: string;
  kind: ZoneKind;
  speed_mps?: number;
  notes?: string;
}

/** Name + kind (+ speed) of a zone; used by the zone tool and "rename". */
export function zoneForm(title: string, value: ZoneFormValue, taken: ReadonlySet<string>): Promise<ZoneFormValue | null> {
  const nameIn = h("input", { type: "text", value: value.name, spellcheck: false, placeholder: "Cold store" });
  const kindSel = select(
    ZONE_KINDS.map((k) => ({ value: k, label: ZONE_STYLES[k].label })),
    value.kind,
  );
  const speedIn = h("input", { type: "number", step: "0.05", min: "0.05", class: "num", value: value.speed_mps === undefined ? "0.3" : String(value.speed_mps) });
  const notesIn = h("input", { type: "text", value: value.notes ?? "", placeholder: "why this area exists" });
  const help = h("div", { class: "field-help", text: ZONE_STYLES[value.kind].help });
  const speedRow = field("Speed inside (m/s)", speedIn, { help: "The cap written into the Nav2 speed mask." });
  const err = h("div", { class: "field-error" });
  const sync = (): void => {
    const kind = kindSel.value as ZoneKind;
    help.textContent = ZONE_STYLES[kind].help;
    speedRow.hidden = kind !== "speed_limit";
  };
  kindSel.addEventListener("change", sync);
  sync();
  let out: ZoneFormValue | null = null;
  return modal({
    title,
    body: [h("div", { class: "form" }, field("Name", nameIn, { help: "Missions test it with in_zone('name')." }), field("Kind", kindSel), help, speedRow, field("Notes", notesIn)), err],
    buttons: [
      { label: "Cancel", value: "cancel" },
      { label: "Save", value: "ok", primary: true },
    ],
    onOpen: () => {
      nameIn.focus();
      nameIn.select();
    },
    onClose: (v) => {
      if (v !== "ok") return true;
      const name = nameIn.value.trim();
      if (!name) {
        err.textContent = "A zone needs a name.";
        return false;
      }
      if (name !== value.name && taken.has(name)) {
        err.textContent = `A zone named "${name}" already exists in this map.`;
        return false;
      }
      const kind = kindSel.value as ZoneKind;
      out = { name, kind };
      if (kind === "speed_limit") {
        const sp = Number(speedIn.value);
        if (!Number.isFinite(sp) || sp <= 0) {
          err.textContent = "A speed limit needs a positive speed.";
          return false;
        }
        out.speed_mps = Math.round(sp * 100) / 100;
      }
      const notes = notesIn.value.trim();
      if (notes) out.notes = notes;
      return true;
    },
  }).then((v) => (v === "ok" ? out : null));
}

// ---- pickers ------------------------------------------------------------------------------------

/** Capability availability per step type (from /api/capabilities.steps). */
export type BlockCaps = Record<string, { available?: boolean; reason?: string }> | undefined;

/**
 * Searchable, grouped block picker (replaces the old sidebar palette). The
 * search field has focus; Enter picks the highlighted entry, arrow keys move
 * the highlight, and entries stay draggable onto the steps list.
 */
export function pickBlock(title = "Add step", caps?: BlockCaps): Promise<BlockDef | null> {
  return new Promise((resolve) => {
    let chosen: BlockDef | null = null;
    const search = h("input", { type: "text", placeholder: "Search blocks…", class: "search", spellcheck: false });
    const list = h("div", { class: "picker-list" });
    let close: ((v: string | null) => void) | null = null;
    let cursor = 0;
    const items = (): HTMLButtonElement[] => [...list.querySelectorAll<HTMLButtonElement>("button.picker-item")];
    const highlight = (): void => {
      const all = items();
      cursor = Math.max(0, Math.min(cursor, all.length - 1));
      all.forEach((el, i) => el.classList.toggle("active", i === cursor));
      all[cursor]?.scrollIntoView({ block: "nearest" });
    };
    const render = (): void => {
      list.replaceChildren();
      const q = search.value.trim().toLowerCase();
      for (const [group, blocks] of blocksByGroup()) {
        const hits = blocks.filter((b) => !q || b.label.toLowerCase().includes(q) || b.type.includes(q) || b.help.toLowerCase().includes(q));
        if (!hits.length) continue;
        list.append(h("div", { class: "picker-group", text: group }));
        for (const b of hits) {
          const cap = caps?.[b.capability ?? b.type];
          const unavailable = !!cap && cap.available === false;
          list.append(
            h(
              "button",
              {
                class: `picker-item${unavailable ? " unavailable" : ""}`,
                draggable: true,
                title: unavailable ? `${b.type}\nNot available on this robot${cap?.reason ? `: ${cap.reason}` : ""}` : b.type,
                onclick: () => {
                  chosen = b;
                  close?.("ok");
                },
                ondragstart: (e: DragEvent) => {
                  e.dataTransfer?.setData("application/x-mission-block", b.type);
                  e.dataTransfer?.setData("text/plain", b.type);
                  if (e.dataTransfer) e.dataTransfer.effectAllowed = "copy";
                },
              },
              icon(b.icon),
              h("span", { class: "grow" }, h("div", { class: "title", text: b.label }), h("div", { class: "sub", text: unavailable ? "Not available on this robot" : b.help })),
            ),
          );
        }
      }
      if (!list.childElementCount) list.append(h("div", { class: "empty", text: "No blocks match." }));
      cursor = 0;
      highlight();
    };
    search.addEventListener("input", render);
    search.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const el = items()[cursor];
        if (el) {
          stopEvent(e);
          el.click();
        }
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        stopEvent(e);
        cursor += e.key === "ArrowDown" ? 1 : -1;
        highlight();
      }
    });
    render();
    modal({
      title,
      cls: "picker",
      body: [search, list],
      buttons: [{ label: "Cancel", value: "cancel" }],
      onOpen: (_box, closeDialog) => {
        close = closeDialog;
        search.focus();
      },
    }).then(() => resolve(chosen));
  });
}

/** Trigger-type picker. */
export function pickTriggerType(title: string): Promise<TriggerDef | null> {
  return new Promise((resolve) => {
    let chosen: TriggerDef | null = null;
    const list = h("div", { class: "picker-list" });
    let close: ((v: string | null) => void) | null = null;
    for (const t of allTriggers()) {
      list.append(
        h(
          "button",
          {
            class: "picker-item",
            onclick: () => {
              chosen = t;
              close?.("ok");
            },
          },
          icon(t.icon),
          h("span", { class: "grow" }, h("div", { class: "title", text: t.label }), h("div", { class: "sub", text: t.help })),
          h("span", { class: "mono muted", text: t.type }),
        ),
      );
    }
    modal({
      title,
      cls: "picker",
      body: [list],
      buttons: [{ label: "Cancel", value: "cancel" }],
      onOpen: (box, closeDialog) => {
        close = closeDialog;
        box.querySelector<HTMLButtonElement>("button.picker-item")?.focus();
      },
    }).then(() => resolve(chosen));
  });
}

export interface GalleryItem {
  id: string;
  title: string;
  description: string;
  source: "builtin" | "runner";
  icon?: IconName;
}

/** Template gallery; resolves with the chosen item id or null. */
export function templateGallery(items: GalleryItem[]): Promise<string | null> {
  return new Promise((resolve) => {
    let chosen: string | null = null;
    const grid = h("div", { class: "gallery" });
    let close: ((v: string | null) => void) | null = null;
    for (const it of items) {
      grid.append(
        h(
          "button",
          {
            class: "gallery-card",
            onclick: () => {
              chosen = it.id;
              close?.("ok");
            },
          },
          h("div", { class: "card-head" }, icon(it.icon ?? "file"), h("span", { class: "title grow", text: it.title }), h("span", { class: `tag ${it.source}`, text: it.source === "runner" ? "robot" : "built-in" })),
          h("div", { class: "sub", text: it.description || "No description." }),
        ),
      );
    }
    modal({
      title: "New mission",
      cls: "wide",
      body: [h("p", { class: "muted", text: "Pick a template. The new mission gets a unique name and is saved as a draft until you deploy it." }), grid],
      buttons: [{ label: "Cancel", value: "cancel" }],
      onOpen: (box, closeDialog) => {
        close = closeDialog;
        box.querySelector<HTMLButtonElement>("button.gallery-card")?.focus();
      },
    }).then(() => resolve(chosen));
  });
}

// ---- toasts ----------------------------------------------------------------------------------------------

let toastBox: HTMLElement | null = null;

export function toast(text: string, kind: "info" | "ok" | "warn" | "error" = "info", ms = 4000, action?: { label: string; onClick: () => void }): void {
  if (!toastBox) {
    toastBox = h("div", { class: "toasts" });
    document.body.append(toastBox);
  }
  const el = h(
    "div",
    { class: `toast ${kind}` },
    h("span", { class: "grow", text }),
    action
      ? h("button", { class: "small toast-action", onclick: () => {
        el.remove();
        action.onClick();
      } }, action.label)
      : null,
    h("button", { class: "icon-only ghost", onclick: () => el.remove() }, icon("close")),
  );
  toastBox.append(el);
  if (ms > 0) setTimeout(() => el.remove(), ms);
  while (toastBox.childElementCount > 5) toastBox.firstElementChild?.remove();
}
