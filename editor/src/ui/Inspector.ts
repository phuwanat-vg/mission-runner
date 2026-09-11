/**
 * Inspector: mission settings (nothing selected), the type-specific form of
 * the selected step (from the registry) with "On failure" and "Advanced"
 * sections, or the selected trigger / interrupt. Every change goes through
 * the store (undoable) and re-validates.
 */

import type { Store } from "../state/Store";
import type { RunnerClient } from "../api/RunnerClient";
import type { AppActions } from "./actions";
import type { InputDef, Mission, Path, Site, SiteKind, Step, Trigger } from "../model/types";
import { BT_TEMPLATES, INPUT_TYPES, INTERRUPT_POLICIES, MISSION_NAME_RE, POLICIES, RECOVERIES, SITE_KINDS, isRecord } from "../model/types";
import type { ParamDef } from "../model/blocks";
import { allTriggers, blockDefOrUnknown, newEventSource, poseText, triggerDef } from "../model/blocks";
import { getStepAt, pathKey, walkSteps } from "../model/ids";
import { EXPRESSION_FUNCTIONS, hasExpression } from "../model/expressions";
import { BT_DEFAULTS } from "../codegen/bt";
import { h, copyText, field, replace, section, select, stopEvent } from "./dom";
import { icon } from "./icons";
import type { IconName } from "./icons";
import { modal, toast } from "./dialogs";

const SITE_ICON: Record<SiteKind, IconName> = { station: "mapPin", dock: "anchor", waypoint: "flag", home: "home" };

type Resolve = (doc: Mission) => Record<string, unknown> | null;

interface FieldCtx {
  /** Locates the object holding the parameter inside a (mutable) document. */
  resolve: Resolve;
  /** Current (read-only) object. */
  obj: Record<string, unknown>;
  /** Path of the object (for validation lookups and coalescing). */
  path: Path;
}

const NUMBER_INPUT_RE = /^-?\d+(\.\d+)?([eE][-+]?\d+)?$/;

export class Inspector {
  readonly el: HTMLElement;
  #store: Store;
  #client: RunnerClient;
  #actions: AppActions;
  #jsonDrafts = new Map<string, string>();
  #lastKey = "";
  #showHelp = false;

  constructor(store: Store, client: RunnerClient, actions: AppActions) {
    this.#store = store;
    this.#client = client;
    this.#actions = actions;
    try {
      this.#showHelp = localStorage.getItem("mission-editor.inspector.help") === "1";
    } catch {
      this.#showHelp = false;
    }
    this.el = h("aside", { class: `inspector${this.#showHelp ? " show-help" : ""}` });
    for (const t of ["mission", "selection", "validation", "sites", "remote"] as const) store.on(t, () => this.render());
    this.el.addEventListener("keydown", (e) => e.stopPropagation());
    this.render();
  }

  render(): void {
    const s = this.#store;
    const m = s.mission;
    // remember focus
    const active = document.activeElement as HTMLElement | null;
    const focusKey = active && this.el.contains(active) ? active.dataset.f : undefined;
    const selStart = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement ? active.selectionStart : null;
    const selEnd = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement ? active.selectionEnd : null;
    const scrollTop = this.el.scrollTop;

    const selKey = m ? `${m.name}:${s.selection ? JSON.stringify(s.selection) : ""}` : "";
    if (selKey !== this.#lastKey) this.#jsonDrafts.clear();
    this.#lastKey = selKey;

    if (!m) {
      replace(this.el, h("div", { class: "empty", text: "No mission open." }));
      return;
    }
    const step = s.selectedStep();
    const trig = s.selectedTrigger();
    const site = s.selectedSite();
    if (step) replace(this.el, ...this.#stepForm(step.step, step.path));
    else if (trig) replace(this.el, ...this.#triggerForm(trig.trigger, trig.kind, trig.index));
    else if (site) replace(this.el, ...this.#siteForm(site.name, site.site));
    else replace(this.el, ...this.#missionForm(m));

    if (focusKey) {
      const el = this.el.querySelector<HTMLElement>(`[data-f="${CSS.escape(focusKey)}"]`);
      if (el) {
        el.focus({ preventScroll: true });
        if ((el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) && selStart !== null && selEnd !== null && el.type !== "number" && el.type !== "checkbox") {
          try {
            el.setSelectionRange(selStart, selEnd);
          } catch {
            // not a text control
          }
        }
      }
    }
    this.el.scrollTop = scrollTop;
  }

  /** "Show help" toggle for the inspector header: reveals the per-field help lines (otherwise label tooltips). */
  #helpToggle(): HTMLElement {
    return h("button", { class: `ghost small help-toggle${this.#showHelp ? " active" : ""}`, title: this.#showHelp ? "Hide the help text under fields" : "Show help text under every field (also in the label tooltips)", onclick: () => {
      this.#showHelp = !this.#showHelp;
      this.el.classList.toggle("show-help", this.#showHelp);
      try {
        localStorage.setItem("mission-editor.inspector.help", this.#showHelp ? "1" : "0");
      } catch {
        // ignore
      }
      this.render();
    } }, this.#showHelp ? "Hide help" : "Show help");
  }

  // ---- writing --------------------------------------------------------------------------

  #write(ctx: FieldCtx, key: string, value: unknown, coalesce = true): void {
    this.#store.update(
      (doc) => {
        const obj = ctx.resolve(doc);
        if (!obj) return;
        if (value === undefined) delete obj[key];
        else obj[key] = value;
      },
      coalesce ? { coalesce: `${pathKey(ctx.path)}.${key}` } : {},
    );
  }

  #errorFor(path: Path): string | undefined {
    const f = this.#store.findingsUnder(path).find((x) => x.path.length === path.length);
    return f?.message;
  }

  #warnFor(path: Path): string | undefined {
    const f = this.#store.findingsUnder(path).find((x) => x.level === "warning" && x.path.length === path.length);
    return f?.message;
  }

  // ---- mission settings --------------------------------------------------------------------

  #missionForm(m: Mission): Node[] {
    const s = this.#store;
    const ctx: FieldCtx = { resolve: (doc) => doc as unknown as Record<string, unknown>, obj: m as unknown as Record<string, unknown>, path: [] };
    const taken = new Set([...s.missions.map((x) => x.name), ...s.drafts.map((d) => d.name)]);
    taken.delete(m.name);
    const nameIn = this.#textInput(ctx, "name", { mono: true, keepEmpty: true });
    const nameErr = !MISSION_NAME_RE.test(m.name) ? "lowercase letters, digits, _ and -, starting with a letter" : taken.has(m.name) ? "another mission has this name" : undefined;
    const head = h("div", { class: "insp-head" }, h("div", { class: "grow" }, h("div", { class: "title", text: "Mission settings" }), h("div", { class: "sub", text: `${m.name} · version ${m.version ?? 1}` })), this.#helpToggle());
    const general = section("General", [
      field("Name", nameIn, { error: nameErr, help: "File, service and URL name. Renaming creates a new copy on the robot when deployed." }),
      field("Title", this.#textInput(ctx, "title", {})),
      field("Description", this.#textArea(ctx, "description", {}), { wide: true }),
      field("Policy", this.#selectInput(ctx, "policy", [...POLICIES], "queue"), { help: "Used for manual starts (editor, HTTP, ROS service)." }),
      field("Priority", this.#numberInput(ctx, "priority", { min: 0, max: 100, step: 1, integer: true, placeholder: "50" }), { error: this.#errorFor(["priority"]) }),
    ]);
    const hasInputs = Object.keys(m.inputs ?? {}).length > 0;
    const hasVars = Object.keys(m.vars ?? {}).length > 0;
    const inputs = hasInputs
      ? section("Inputs", [this.#inputsTable(m)], { head: [h("button", { class: "icon-only ghost", title: "Add input", onclick: (e: Event) => {
        stopEvent(e);
        this.#addInput(m);
      } }, icon("plus", 12))] })
      : this.#emptyLine("No inputs", "Add", "Inputs are filled by triggers, callers or the Run dialog.", () => this.#addInput(m));
    const vars = hasVars
      ? section("Variables", [this.#varsTable(m)], { head: [h("button", { class: "icon-only ghost", title: "Add variable", onclick: (e: Event) => {
        stopEvent(e);
        this.#addVar(m);
      } }, icon("plus", 12))] })
      : this.#emptyLine("No variables", "Add", "Set steps can create variables at run time too.", () => this.#addVar(m));
    const problems = this.#problemsSection();
    return [head, general, inputs, vars, problems].filter((x): x is HTMLElement => x !== null);
  }

  /** One quiet line for an empty table: "No inputs · Add". */
  #emptyLine(text: string, action: string, tip: string, onAdd: () => void): HTMLElement {
    return h("div", { class: "empty-line", title: tip }, h("span", { class: "muted", text: `${text} · ` }), h("button", { class: "link-btn ghost", onclick: onAdd }, action));
  }

  #problemsSection(): HTMLElement | null {
    const v = this.#store.validation;
    const all = [...v.errors, ...v.warnings];
    if (!all.length) return null;
    const list = h("ul", { class: "finding-list compact" });
    for (const f of all.slice(0, 30)) {
      const li = h("li", { class: f.level }, h("span", { text: f.message }));
      if (f.stepId) {
        const id = f.stepId;
        li.classList.add("link");
        li.title = `step ${id}`;
        li.addEventListener("click", () => this.#store.select({ kind: "step", id }));
      } else if (f.path[0] === "triggers" || f.path[0] === "interrupts") {
        const kind = f.path[0] === "triggers" ? "trigger" : "interrupt";
        const index = f.path[1];
        if (typeof index === "number") {
          li.classList.add("link");
          li.addEventListener("click", () => this.#store.select({ kind, index }));
        }
      }
      list.append(li);
    }
    if (all.length > 30) list.append(h("li", { class: "muted", text: `… ${all.length - 30} more` }));
    return section(`Problems (${v.errors.length} errors, ${v.warnings.length} warnings)`, [list]);
  }

  #inputsTable(m: Mission): HTMLElement {
    const entries = Object.entries(m.inputs ?? {});
    if (!entries.length) return h("div", { class: "empty", text: "No inputs. Inputs are filled by triggers, callers or the Run dialog." });
    const table = h("div", { class: "table inputs" });
    for (const [name, def] of entries) {
      const path: Path = ["inputs", name];
      const err = this.#errorFor(path) ?? this.#errorFor([...path, "type"]);
      const nameIn = h("input", { type: "text", value: name, class: "mono", "data-f": `input.${name}.name`, spellcheck: false });
      nameIn.addEventListener("change", () => this.#renameKey("inputs", name, nameIn.value.trim()));
      const typeSel = select(INPUT_TYPES.map((t) => ({ value: t })), def.type, { "data-f": `input.${name}.type` });
      typeSel.addEventListener("change", () => this.#setInput(name, { ...def, type: typeSel.value as InputDef["type"] }));
      const dflt = h("input", { type: "text", class: "mono", value: def.default === undefined ? "" : typeof def.default === "string" ? def.default : JSON.stringify(def.default), placeholder: "default", "data-f": `input.${name}.default`, spellcheck: false });
      dflt.addEventListener("change", () => {
        const next = { ...def };
        const v = parseLoose(dflt.value, def.type === "string" || def.type === "site");
        if (v === undefined) delete next.default;
        else next.default = v;
        this.#setInput(name, next);
      });
      const req = h("input", { type: "checkbox", title: "Required", "data-f": `input.${name}.required` });
      req.checked = def.required === true;
      req.addEventListener("change", () => {
        const next = { ...def };
        if (req.checked) next.required = true;
        else delete next.required;
        this.#setInput(name, next);
      });
      const label = h("input", { type: "text", value: def.label ?? "", placeholder: "label", "data-f": `input.${name}.label` });
      label.addEventListener("change", () => {
        const next = { ...def };
        if (label.value.trim()) next.label = label.value.trim();
        else delete next.label;
        this.#setInput(name, next);
      });
      const del = h("button", { class: "icon-only ghost danger", title: "Remove input", onclick: () => this.#removeKey("inputs", name) }, icon("trash", 11));
      table.append(h("div", { class: `trow${err ? " has-error" : ""}`, title: err ?? "" }, nameIn, typeSel, dflt, h("label", { class: "chk", title: "Required" }, req, "req"), label, del));
    }
    return table;
  }

  #setInput(name: string, def: InputDef): void {
    this.#store.update((doc) => {
      doc.inputs ??= {};
      doc.inputs[name] = def;
    });
  }

  #addInput(m: Mission): void {
    let i = 1;
    while (m.inputs?.[`input${i}`]) i++;
    this.#setInput(`input${i}`, { type: "string" });
  }

  #varsTable(m: Mission): HTMLElement {
    const entries = Object.entries(m.vars ?? {});
    if (!entries.length) return h("div", { class: "empty", text: "No variables. Set steps can create them at run time too." });
    const table = h("div", { class: "table vars" });
    for (const [name, value] of entries) {
      const err = this.#errorFor(["vars", name]);
      const nameIn = h("input", { type: "text", value: name, class: "mono", "data-f": `var.${name}.name`, spellcheck: false });
      nameIn.addEventListener("change", () => this.#renameKey("vars", name, nameIn.value.trim()));
      const key = `var.${name}.value`;
      const draft = this.#jsonDrafts.get(key);
      const valIn = h("input", { type: "text", class: "mono", value: draft ?? JSON.stringify(value), "data-f": key, spellcheck: false });
      const jsonErr = draft !== undefined ? "not valid JSON" : undefined;
      valIn.addEventListener("input", () => {
        try {
          const v = JSON.parse(valIn.value) as unknown;
          this.#jsonDrafts.delete(key);
          this.#store.update((doc) => {
            doc.vars ??= {};
            doc.vars[name] = v;
          }, { coalesce: key });
        } catch {
          this.#jsonDrafts.set(key, valIn.value);
          valIn.classList.add("invalid");
        }
      });
      if (jsonErr) valIn.classList.add("invalid");
      const del = h("button", { class: "icon-only ghost danger", title: "Remove variable", onclick: () => this.#removeKey("vars", name) }, icon("trash", 11));
      table.append(h("div", { class: `trow${err || jsonErr ? " has-error" : ""}`, title: err ?? jsonErr ?? "JSON value" }, nameIn, valIn, del));
    }
    return table;
  }

  #addVar(m: Mission): void {
    let i = 1;
    while (m.vars && `var${i}` in m.vars) i++;
    this.#store.update((doc) => {
      doc.vars ??= {};
      doc.vars[`var${i}`] = 0;
    });
  }

  #renameKey(which: "inputs" | "vars", from: string, to: string): void {
    if (!to || to === from) {
      this.render();
      return;
    }
    this.#store.update((doc) => {
      const table = doc[which];
      if (!table || !(from in table)) return;
      if (to in table) return;
      const rebuilt: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(table)) rebuilt[k === from ? to : k] = v;
      (doc as unknown as Record<string, unknown>)[which] = rebuilt;
    });
  }

  #removeKey(which: "inputs" | "vars", name: string): void {
    this.#store.update((doc) => {
      const table = doc[which];
      if (!table) return;
      delete table[name];
      if (Object.keys(table).length === 0) delete doc[which];
    });
  }

  // ---- site form (a marker on the map is selected) --------------------------------------------

  #siteForm(name: string, site: Site): Node[] {
    const s = this.#store;
    const kind = site.kind ?? "station";
    const set = (fn: (site: Site) => void): void => {
      s.mutateSites((sites) => {
        const cur = sites[name];
        if (cur) fn(cur);
      });
    };
    const head = h(
      "div",
      { class: "insp-head" },
      icon(SITE_ICON[kind] ?? "mapPin", 16),
      h("div", { class: "grow" }, h("div", { class: "title", text: name }), h("div", { class: "sub", text: `site · map ${s.activeMap ?? "?"}` })),
      this.#helpToggle(),
      h("button", { class: "icon-only ghost danger", title: "Delete site", onclick: () => this.#actions.deleteSite(name) }, icon("trash", 12)),
    );
    const nameIn = h("input", { type: "text", value: name, "data-f": "site.name", spellcheck: false });
    nameIn.addEventListener("change", () => {
      const next = nameIn.value.trim();
      if (next === "" || next === name) return;
      if (s.activeSites[next]) {
        toast(`A site named “${next}” already exists in this map.`, "warn");
        nameIn.value = name;
        return;
      }
      s.mutateSites((sites) => {
        const cur = sites[name];
        if (!cur) return;
        delete sites[name];
        sites[next] = cur;
      });
      s.select({ kind: "site", name: next });
      toast(`Renamed to ${next}. Steps that used “${name}” now report a missing site until you update them.`, "warn", 8000);
    });
    const kindSel = select(SITE_KINDS.map((k) => ({ value: k })), kind, { "data-f": "site.kind" });
    kindSel.addEventListener("change", () => set((x) => void (x.kind = kindSel.value as SiteKind)));
    const num = (key: "x" | "y" | "yaw_deg", stepSize: string): HTMLInputElement => {
      const input = h("input", { type: "number", step: stepSize, class: "num", value: String(site[key] ?? 0), "data-f": `site.${key}` });
      input.addEventListener("change", () => {
        const v = Number(input.value);
        if (Number.isFinite(v)) set((x) => void (x[key] = key === "yaw_deg" ? Math.round(v * 10) / 10 : Math.round(v * 1000) / 1000));
      });
      return input;
    };
    const text = (key: "dock_id" | "dock_type" | "notes", placeholder: string): HTMLInputElement => {
      const input = h("input", { type: "text", value: site[key] ?? "", placeholder, "data-f": `site.${key}` });
      input.addEventListener("change", () => {
        const v = input.value.trim();
        set((x) => {
          if (v === "") delete x[key];
          else x[key] = v;
        });
      });
      return input;
    };
    const users = [...s.mapModel.byKey.values()].filter((p) => p.site === name);
    const seen = new Set<string>();
    const userRows: HTMLElement[] = [];
    for (const p of users) {
      if (seen.has(p.stepId)) continue;
      seen.add(p.stepId);
      const place = s.mapModel.byStep.get(p.stepId);
      userRows.push(h("button", { class: "link-btn ghost", onclick: () => s.select({ kind: "step", id: p.stepId }) }, place?.title ?? p.stepId));
    }
    const place = section("Place", [
      field("Name", nameIn, { help: "Steps reference sites by name; renaming does not update them." }),
      field("Kind", kindSel, { help: "Station, dock, waypoint or home. Only a label, except that docks can carry a dock id." }),
      field("x (m)", num("x", "0.01")),
      field("y (m)", num("y", "0.01")),
      field("Heading (°)", num("yaw_deg", "1"), { help: "Where the robot faces when it arrives, unless the step overrides it." }),
      field(
        "From robot",
        h("button", { class: "small", disabled: !s.connected, title: s.connected ? "Fill from GET /api/robot/pose" : "Runner offline", onclick: () => this.#actions.captureSite(name, site) }, icon("crosshair", 11), "Use robot pose"),
      ),
    ]);
    const docking = kind === "dock" ? section("Docking", [field("Dock id", text("dock_id", "home_dock"), { help: "Matches the dock id known to the Nav2 docking server." }), field("Dock type", text("dock_type", "nova_carter_dock"))]) : null;
    const notes = section("Notes", [field("Notes", text("notes", ""), { wide: true })], { collapsed: !site.notes });
    const used = section(`Used by (${userRows.length})`, [userRows.length ? h("div", { class: "inline wrap" }, ...userRows) : h("div", { class: "muted small", text: "No step of this mission goes here." })], { collapsed: userRows.length === 0 });
    const dirty = s.sitesDirty;
    const save = h(
      "div",
      { class: "insp-foot" },
      h("span", { class: "muted small grow", text: dirty ? "Sites changed since the last save." : "Sites match the robot." }),
      h("button", { class: `small${dirty ? " primary attention" : ""}`, disabled: !dirty || !s.connected, title: s.connected ? "PUT /api/sites" : "Runner offline", onclick: () => this.#actions.saveSites() }, "Save sites"),
    );
    return [head, place, docking, notes, used, save].filter((x): x is HTMLElement => x !== null);
  }

  // ---- step form ---------------------------------------------------------------------------

  #stepForm(step: Step, path: Path): Node[] {
    const s = this.#store;
    const def = blockDefOrUnknown(step.type);
    const ctx: FieldCtx = { resolve: (doc) => getStepAt(doc, path), obj: step, path };
    const id = typeof step.id === "string" ? step.id : "";
    const head = h(
      "div",
      { class: "insp-head" },
      icon(def.icon, 16),
      h("div", { class: "grow" }, h("div", { class: "title", text: def.label }), h("div", { class: "sub mono", text: step.type })),
      this.#helpToggle(),
      h("button", { class: "icon-only ghost danger", title: "Delete step", onclick: () => s.deleteStep(path) }, icon("trash", 12)),
    );
    const help = h("div", { class: "help", text: def.help, title: def.help });
    const nameRow = field("Name", this.#textInput(ctx, "name", { placeholder: def.label }), { help: "Shown in the list and the run log." });

    const main: Node[] = [nameRow];
    const advanced: Node[] = [];
    for (const p of def.params) {
      const node = this.#paramField(ctx, p);
      if (!node) continue;
      (p.advanced ? advanced : main).push(node);
    }
    if (step.type === "break" || step.type === "nav.cancel" || step.type === "nav.wait_active") main.push(h("div", { class: "muted small", text: "This step has no parameters." }));

    const failCtx: FieldCtx = { resolve: (doc) => {
      const st = getStepAt(doc, path);
      if (!st) return null;
      if (!isRecord(st.on_fail)) st.on_fail = {};
      return st.on_fail as Record<string, unknown>;
    }, obj: isRecord(step.on_fail) ? step.on_fail : {}, path: [...path, "on_fail"] };
    const before = isRecord(step.on_fail) && Array.isArray(step.on_fail.before_retry) ? (step.on_fail.before_retry as Step[]) : [];
    const onFail = section(
      "On failure",
      [
        field("Retry", this.#numberInput(failCtx, "retry", { min: 0, max: 100, step: 1, integer: true, placeholder: "0" }), { error: this.#errorFor([...path, "on_fail", "retry"]), help: "Number of retries after the first failure." }),
        field("Retry delay (s)", this.#numberInput(failCtx, "retry_delay_s", { min: 0, placeholder: "0" }), { error: this.#errorFor([...path, "on_fail", "retry_delay_s"]) }),
        field(
          "Before retry",
          h("div", { class: "inline" }, h("span", { class: "muted", text: before.length ? `${before.length} step${before.length === 1 ? "" : "s"} (edited in the list)` : "nothing" }), h("button", { class: "small", onclick: () => this.#actions.addStepAt([...path, "on_fail", "before_retry"], before.length) }, icon("plus", 11), "Add")),
          { help: "Steps run between attempts, e.g. Clear costmap." },
        ),
        field("Then", this.#selectInput(failCtx, "then", ["abort", "continue"], "abort"), { help: "After the last attempt fails: abort the run or continue with the next step." }),
      ],
      { collapsed: !isRecord(step.on_fail) },
    );
    const idRow = field("Id", h("div", { class: "inline" }, h("code", { class: "mono", text: id }), h("button", { class: "icon-only ghost", title: "Copy id", onclick: () => void copyText(id).then(() => toast("Id copied", "ok", 1500)) }, icon("copy", 11))));
    const adv = section(
      "Advanced",
      [
        ...advanced,
        idRow,
        field("Out", this.#textInput(ctx, "out", { mono: true, placeholder: "variable name" }), { error: this.#errorFor([...path, "out"]), help: "Store the result {ok, status, value, error, duration_s} under this name. It is always available as `last`." }),
        field("Timeout (s)", this.#numberInput(ctx, "timeout_s", { min: 0, placeholder: "none" }), { error: this.#errorFor([...path, "timeout_s"]), help: step.type === "ask_user" ? "Without a timeout the mission waits forever." : "Fail the step after this long." }),
        field("Enabled", this.#boolInput(ctx, "enabled", true)),
      ],
      { collapsed: advanced.length === 0 && step.out === undefined && step.timeout_s === undefined && step.enabled !== false },
    );
    const findings = id ? s.findingsFor(id).filter((f) => f.path.length >= path.length && !hasNestedIndex(f.path.slice(path.length))) : [];
    const problems = findings.length ? h("div", { class: "insp-problems" }, ...findings.map((f) => h("div", { class: f.level }, icon(f.level === "error" ? "ban" : "alert", 11), f.message))) : null;
    return [head, help, problems, section("Parameters", main), onFail, adv].filter((x): x is HTMLElement => x !== null);
  }

  #paramField(ctx: FieldCtx, p: ParamDef): Node | null {
    const path = [...ctx.path, p.key];
    const err = this.#errorFor(path);
    const warn = this.#warnFor(path);
    const opts = { help: p.help, error: err ?? warn };
    switch (p.kind) {
      case "steps":
        return null;
      case "string":
        return field(p.label, this.#textInput(ctx, p.key, { placeholder: p.placeholder, keepEmpty: p.required }), opts);
      case "text":
        return field(p.label, this.#textArea(ctx, p.key, { placeholder: p.placeholder, keepEmpty: p.required }), { ...opts, wide: true });
      case "number":
      case "integer":
        return field(p.label, this.#numberInput(ctx, p.key, { min: p.min, max: p.max, step: p.step, integer: p.kind === "integer", allowExpr: p.allowExpr, placeholder: p.placeholder ?? (p.default !== undefined ? String(p.default) : "") }), opts);
      case "boolean":
        return field(p.label, this.#boolInput(ctx, p.key, p.default === true), opts);
      case "select":
        return field(p.label, this.#selectInput(ctx, p.key, p.options ?? [], typeof p.default === "string" ? p.default : undefined, !p.required), opts);
      case "expression":
        return field(p.label, this.#expressionInput(ctx, p.key, p.placeholder), { ...opts, wide: true });
      case "value":
        return field(p.label, this.#valueInput(ctx, p.key, p.placeholder), opts);
      case "json":
        return field(p.label, this.#jsonInput(ctx, p.key, p.required), { ...opts, wide: true });
      case "site":
        return field(p.label, this.#siteSelect(ctx, p.key, !p.required), opts);
      case "map":
        return field(p.label, this.#mapSelect(ctx, p.key), opts);
      case "connector":
        return field(p.label, this.#connectorSelect(ctx, p.key), opts);
      case "pose":
        return field(p.label, this.#poseWidget(ctx, p.key, !p.required), { ...opts, wide: true });
      case "poses":
      case "points":
        return field(p.label, this.#posesWidget(ctx, p.key, p.kind === "points"), { ...opts, wide: true });
      case "options":
        return field(p.label, this.#optionsWidget(ctx, p.key), { ...opts, wide: true });
      case "behavior_tree":
        return field(p.label, this.#btWidget(ctx, p.key), { ...opts, wide: true });
      case "event_source":
        return field(p.label, this.#eventSourceWidget(ctx, p.key), { ...opts, wide: true });
      default:
        return null;
    }
  }

  // ---- basic widgets ------------------------------------------------------------------------

  #fkey(ctx: FieldCtx, key: string): string {
    return `${pathKey(ctx.path)}.${key}`;
  }

  #textInput(ctx: FieldCtx, key: string, o: { placeholder?: string; mono?: boolean; keepEmpty?: boolean }): HTMLInputElement {
    const v = ctx.obj[key];
    const input = h("input", { type: "text", value: typeof v === "string" ? v : v === undefined ? "" : String(v), placeholder: o.placeholder ?? "", class: o.mono ? "mono" : "", "data-f": this.#fkey(ctx, key), spellcheck: false });
    input.addEventListener("input", () => this.#write(ctx, key, input.value === "" && !o.keepEmpty ? undefined : input.value));
    return input;
  }

  #textArea(ctx: FieldCtx, key: string, o: { placeholder?: string; keepEmpty?: boolean }): HTMLTextAreaElement {
    const v = ctx.obj[key];
    const ta = h("textarea", { rows: 2, placeholder: o.placeholder ?? "", "data-f": this.#fkey(ctx, key), spellcheck: false });
    ta.value = typeof v === "string" ? v : v === undefined ? "" : String(v);
    ta.addEventListener("input", () => this.#write(ctx, key, ta.value === "" && !o.keepEmpty ? undefined : ta.value));
    return ta;
  }

  #numberInput(ctx: FieldCtx, key: string, o: { min?: number; max?: number; step?: number; integer?: boolean; allowExpr?: boolean; placeholder?: string }): HTMLInputElement {
    const v = ctx.obj[key];
    const isExpr = typeof v === "string";
    const input = h("input", { type: isExpr || o.allowExpr ? "text" : "number", inputmode: "decimal", class: `mono${isExpr ? " expr" : ""}`, value: v === undefined ? "" : String(v), placeholder: o.placeholder ?? "", "data-f": this.#fkey(ctx, key), spellcheck: false });
    if (!isExpr && !o.allowExpr) {
      if (o.min !== undefined) input.min = String(o.min);
      if (o.max !== undefined) input.max = String(o.max);
      input.step = o.step !== undefined ? String(o.step) : o.integer ? "1" : "any";
    }
    input.addEventListener("input", () => {
      const t = input.value.trim();
      if (t === "") return this.#write(ctx, key, undefined);
      if (NUMBER_INPUT_RE.test(t)) {
        const n = Number(t);
        return this.#write(ctx, key, o.integer ? Math.trunc(n) : n);
      }
      if (o.allowExpr || t.startsWith("$")) return this.#write(ctx, key, t);
      // number input rejects other text; keep the model unchanged
    });
    return input;
  }

  #boolInput(ctx: FieldCtx, key: string, dflt: boolean): HTMLElement {
    const v = ctx.obj[key];
    const cb = h("input", { type: "checkbox", "data-f": this.#fkey(ctx, key) });
    cb.checked = typeof v === "boolean" ? v : dflt;
    cb.addEventListener("change", () => this.#write(ctx, key, cb.checked === dflt ? undefined : cb.checked, false));
    return h("label", { class: "chk" }, cb, h("span", { class: "muted", text: cb.checked ? "yes" : "no" }));
  }

  #selectInput(ctx: FieldCtx, key: string, options: string[], dflt?: string, allowEmpty = true): HTMLSelectElement {
    const v = ctx.obj[key];
    const opts = options.map((o) => ({ value: o, label: o === dflt ? `${o} (default)` : o }));
    if (allowEmpty && dflt === undefined) opts.unshift({ value: "", label: "(not set)" });
    const sel = select(opts, typeof v === "string" ? v : (dflt ?? ""), { "data-f": this.#fkey(ctx, key) });
    sel.addEventListener("change", () => this.#write(ctx, key, sel.value === "" || sel.value === dflt ? undefined : sel.value, false));
    return sel;
  }

  #variableNames(): string[] {
    const m = this.#store.mission;
    const names = new Set<string>(["last", "payload", "robot", "mission", "run_id", "current_map", "sites"]);
    if (m) {
      for (const k of Object.keys(m.inputs ?? {})) names.add(k);
      for (const k of Object.keys(m.vars ?? {})) names.add(k);
      for (const v of walkSteps(m)) if (typeof v.step.out === "string") names.add(v.step.out);
    }
    return [...names];
  }

  #expressionInput(ctx: FieldCtx, key: string, placeholder?: string): HTMLElement {
    const v = ctx.obj[key];
    const input = h("input", { type: "text", class: "mono", value: typeof v === "string" ? v : "", placeholder: placeholder ?? "", "data-f": this.#fkey(ctx, key), spellcheck: false });
    input.addEventListener("input", () => this.#write(ctx, key, input.value === "" ? undefined : input.value));
    const hint = h("div", { class: "hint mono", title: `Functions: ${EXPRESSION_FUNCTIONS.join(", ")}`, text: `vars: ${this.#variableNames().join(", ")}` });
    return h("div", { class: "stack" }, input, hint);
  }

  #valueInput(ctx: FieldCtx, key: string, placeholder?: string): HTMLElement {
    const v = ctx.obj[key];
    const text = v === undefined ? "" : typeof v === "string" ? v : JSON.stringify(v);
    const input = h("input", { type: "text", class: "mono grow", value: text, placeholder: placeholder ?? "", "data-f": this.#fkey(ctx, key), spellcheck: false });
    const badge = h("span", { class: "badge expr", text: hasExpression(v) ? "expr" : typeof v === "string" ? "text" : v === undefined ? "" : typeof v === "boolean" ? "bool" : typeof v === "number" ? "number" : "json" });
    input.addEventListener("input", () => {
      const parsed = parseLoose(input.value, false);
      this.#write(ctx, key, parsed);
      badge.textContent = hasExpression(parsed) ? "expr" : typeof parsed === "string" ? "text" : parsed === undefined ? "" : typeof parsed === "boolean" ? "bool" : typeof parsed === "number" ? "number" : "json";
    });
    return h("div", { class: "inline", title: 'Numbers, true/false and {…}/[…] are parsed as JSON; "quoted" keeps text; $name and ${expr} are evaluated at run time.' }, input, badge);
  }

  #jsonInput(ctx: FieldCtx, key: string, required?: boolean): HTMLElement {
    const fkey = this.#fkey(ctx, key);
    const v = ctx.obj[key];
    const draft = this.#jsonDrafts.get(fkey);
    const ta = h("textarea", { rows: 4, class: `mono${draft !== undefined ? " invalid" : ""}`, "data-f": fkey, spellcheck: false, placeholder: "{ }" });
    ta.value = draft ?? (v === undefined ? "" : JSON.stringify(v, null, 2));
    const err = h("div", { class: "field-error", text: draft !== undefined ? this.#jsonError(draft) : "" });
    ta.addEventListener("input", () => {
      const t = ta.value.trim();
      if (t === "") {
        this.#jsonDrafts.delete(fkey);
        ta.classList.remove("invalid");
        err.textContent = "";
        this.#write(ctx, key, required ? {} : undefined);
        return;
      }
      try {
        const parsed = JSON.parse(t) as unknown;
        this.#jsonDrafts.delete(fkey);
        ta.classList.remove("invalid");
        err.textContent = "";
        this.#write(ctx, key, parsed);
      } catch (e) {
        this.#jsonDrafts.set(fkey, ta.value);
        ta.classList.add("invalid");
        err.textContent = e instanceof Error ? e.message : "invalid JSON";
      }
    });
    return h("div", { class: "stack" }, ta, err);
  }

  #jsonError(text: string): string {
    try {
      JSON.parse(text);
      return "";
    } catch (e) {
      return e instanceof Error ? e.message : "invalid JSON";
    }
  }

  #siteNames(): string[] {
    const s = this.#store;
    const map = s.activeMap;
    return map && s.sites ? Object.keys(s.sites.maps[map]?.sites ?? {}).sort() : [];
  }

  #comboSelect(ctx: FieldCtx, key: string, options: string[], o: { allowEmpty?: boolean; missingLabel: string; placeholder: string }): HTMLElement {
    const v = typeof ctx.obj[key] === "string" ? (ctx.obj[key] as string) : "";
    const known = options.includes(v);
    const opts: { value: string; label?: string }[] = options.map((n) => ({ value: n }));
    if (o.allowEmpty || v === "") opts.unshift({ value: "", label: "(not set)" });
    if (v !== "" && !known) opts.push({ value: v, label: `${v} (${o.missingLabel})` });
    opts.push({ value: " other", label: "Other…" });
    const sel = select(opts, v, { "data-f": this.#fkey(ctx, key), class: v !== "" && !known ? "missing" : "" });
    const other = h("input", { type: "text", class: "mono", placeholder: o.placeholder, value: v, "data-f": `${this.#fkey(ctx, key)}.other`, hidden: true, spellcheck: false });
    sel.addEventListener("change", () => {
      if (sel.value === " other") {
        other.hidden = false;
        other.focus();
        return;
      }
      this.#write(ctx, key, sel.value === "" ? undefined : sel.value, false);
    });
    other.addEventListener("input", () => this.#write(ctx, key, other.value === "" ? undefined : other.value));
    return h("div", { class: "stack" }, sel, other);
  }

  #siteSelect(ctx: FieldCtx, key: string, allowEmpty: boolean): HTMLElement {
    return this.#comboSelect(ctx, key, this.#siteNames(), { allowEmpty, missingLabel: "not found", placeholder: "Site name" });
  }

  #mapSelect(ctx: FieldCtx, key: string): HTMLElement {
    return this.#comboSelect(ctx, key, Object.keys(this.#store.sites?.maps ?? {}).sort(), { missingLabel: "not in sites.json", placeholder: "map name or /path/to/map.yaml" });
  }

  #connectorSelect(ctx: FieldCtx, key: string): HTMLElement {
    return this.#comboSelect(ctx, key, Object.keys(this.#store.connectors).sort(), { missingLabel: "not configured", placeholder: "connector name" });
  }

  // ---- pose widgets ----------------------------------------------------------------------------------

  #poseWidget(ctx: FieldCtx, key: string, allowEmpty: boolean): HTMLElement {
    const v = ctx.obj[key];
    const mode: "site" | "coords" | "expr" = v === undefined ? "site" : typeof v === "string" ? (hasExpression(v) ? "expr" : "site") : Array.isArray(v) ? "coords" : isRecord(v) && "site" in v ? "site" : "coords";
    const fk = this.#fkey(ctx, key);
    const seg = h(
      "div",
      { class: "seg small" },
      ...(["site", "coords", "expr"] as const).map((m) => h("button", { class: m === mode ? "active" : "", "data-f": `${fk}.mode.${m}`, onclick: () => this.#switchPoseMode(ctx, key, m, v) }, m === "site" ? "Site" : m === "coords" ? "Coordinates" : "Expression")),
    );
    const body = h("div", { class: "pose-body" });
    if (mode === "site") {
      const name = typeof v === "string" ? v : isRecord(v) && typeof v.site === "string" ? v.site : "";
      const yaw = isRecord(v) && v.yaw_deg !== undefined ? v.yaw_deg : undefined;
      const sites = this.#siteNames();
      const opts: { value: string; label?: string }[] = sites.map((n) => ({ value: n }));
      if (name === "" || allowEmpty) opts.unshift({ value: "", label: "(choose a site)" });
      if (name !== "" && !sites.includes(name)) opts.push({ value: name, label: `${name} (not found)` });
      const sel = select(opts, name, { "data-f": `${fk}.site`, class: name !== "" && !sites.includes(name) ? "missing" : "" });
      const yawIn = h("input", { type: "text", class: "mono short", inputmode: "decimal", placeholder: "site heading", value: yaw === undefined ? "" : String(yaw), "data-f": `${fk}.yaw`, title: "Override the site's yaw (degrees)" });
      const write = (): void => {
        const n = sel.value;
        const y = yawIn.value.trim();
        if (n === "" && !allowEmpty) return this.#write(ctx, key, "", false);
        if (n === "") return this.#write(ctx, key, undefined, false);
        if (y === "") return this.#write(ctx, key, n);
        this.#write(ctx, key, { site: n, yaw_deg: NUMBER_INPUT_RE.test(y) ? Number(y) : y });
      };
      sel.addEventListener("change", write);
      yawIn.addEventListener("input", write);
      body.append(h("div", { class: "inline" }, sel, h("span", { class: "muted", text: "yaw" }), yawIn));
      const inputSites = Object.entries(this.#store.mission?.inputs ?? {}).filter(([, d]) => d.type === "site" || d.type === "pose").map(([k]) => `$${k}`);
      if (inputSites.length) body.append(h("div", { class: "hint", text: `Inputs: use Expression mode with ${inputSites.join(", ")}` }));
    } else if (mode === "coords") {
      const arr = Array.isArray(v) ? (v as number[]) : null;
      const obj = isRecord(v) ? v : {};
      const x = arr ? arr[0] : obj.x;
      const y = arr ? arr[1] : obj.y;
      const yaw = arr ? arr[2] : obj.yaw_deg;
      const frame = typeof obj.frame === "string" ? obj.frame : "";
      const mk = (label: string, val: unknown, k: string): HTMLInputElement => h("input", { type: "text", class: "mono short", inputmode: "decimal", placeholder: label, value: val === undefined ? "" : String(val), "data-f": `${fk}.${k}`, title: `${label} (number or $expression)` });
      const xi = mk("x", x, "x");
      const yi = mk("y", y, "y");
      const yi2 = mk("yaw°", yaw, "yaw");
      const fi = h("input", { type: "text", class: "mono short", placeholder: "map", value: frame, "data-f": `${fk}.frame`, title: "Frame (default map)" });
      const write = (): void => {
        const out: Record<string, unknown> = { x: parseNumOrExpr(xi.value), y: parseNumOrExpr(yi.value) };
        const yv = yi2.value.trim();
        if (yv !== "") out.yaw_deg = parseNumOrExpr(yv);
        if (fi.value.trim() !== "" && fi.value.trim() !== "map") out.frame = fi.value.trim();
        this.#write(ctx, key, out);
      };
      for (const el of [xi, yi, yi2, fi]) el.addEventListener("input", write);
      const robotBtn = h("button", { class: "small", title: this.#store.connected ? "Fill from GET /api/robot/pose" : "Runner offline", onclick: () => void this.#useRobotPose(ctx, key) }, icon("crosshair", 11), "Use robot pose");
      robotBtn.disabled = !this.#store.connected;
      body.append(h("div", { class: "inline wrap" }, h("span", { class: "muted", text: "x" }), xi, h("span", { class: "muted", text: "y" }), yi, h("span", { class: "muted", text: "yaw" }), yi2, h("span", { class: "muted", text: "frame" }), fi), robotBtn);
    } else {
      const input = h("input", { type: "text", class: "mono", value: typeof v === "string" ? v : "", placeholder: "$pickup or ${...}", "data-f": `${fk}.expr`, spellcheck: false });
      input.addEventListener("input", () => this.#write(ctx, key, input.value));
      body.append(input, h("div", { class: "hint mono", text: `vars: ${this.#variableNames().join(", ")}` }));
    }
    return h("div", { class: "pose" }, seg, body);
  }

  #switchPoseMode(ctx: FieldCtx, key: string, mode: "site" | "coords" | "expr", current: unknown): void {
    let next: unknown;
    if (mode === "site") {
      const name = typeof current === "string" && !hasExpression(current) ? current : isRecord(current) && typeof current.site === "string" ? current.site : "";
      next = name;
    } else if (mode === "coords") {
      if (isRecord(current) && "x" in current) return;
      const siteName = typeof current === "string" ? current : isRecord(current) && typeof current.site === "string" ? current.site : "";
      const site = siteName ? this.#store.sites?.maps[this.#store.activeMap ?? ""]?.sites?.[siteName] : undefined;
      next = site ? { x: site.x, y: site.y, yaw_deg: site.yaw_deg ?? 0 } : { x: 0, y: 0, yaw_deg: 0 };
    } else {
      next = typeof current === "string" && hasExpression(current) ? current : "$";
    }
    this.#write(ctx, key, next, false);
  }

  async #useRobotPose(ctx: FieldCtx, key: string): Promise<void> {
    try {
      const p = await this.#client.getRobotPose();
      const out: Record<string, unknown> = { x: round(p.x), y: round(p.y), yaw_deg: round(p.yaw_deg) };
      if (p.frame && p.frame !== "map") out.frame = p.frame;
      this.#write(ctx, key, out, false);
      toast(`Pose set to ${poseText(out)}`, "ok", 2000);
    } catch (err) {
      toast(`Could not read the robot pose: ${err instanceof Error ? err.message : String(err)}`, "error");
    }
  }

  #posesWidget(ctx: FieldCtx, key: string, points: boolean): HTMLElement {
    const list = Array.isArray(ctx.obj[key]) ? (ctx.obj[key] as unknown[]) : [];
    const rows: Node[] = [];
    list.forEach((_, i) => {
      const itemCtx: FieldCtx = {
        resolve: (doc) => {
          const obj = ctx.resolve(doc);
          const arr = obj?.[key];
          return Array.isArray(arr) ? (arr as unknown as Record<string, unknown>) : null;
        },
        obj: list as unknown as Record<string, unknown>,
        path: [...ctx.path, key],
      };
      const move = (delta: number): void =>
        this.#store.update((doc) => {
          const arr = ctx.resolve(doc)?.[key];
          if (!Array.isArray(arr)) return;
          const j = i + delta;
          if (j < 0 || j >= arr.length) return;
          const tmp = arr[i];
          arr[i] = arr[j];
          arr[j] = tmp;
        });
      const remove = (): void =>
        this.#store.update((doc) => {
          const arr = ctx.resolve(doc)?.[key];
          if (Array.isArray(arr)) arr.splice(i, 1);
        });
      const err = this.#errorFor([...ctx.path, key, i]) ?? this.#warnFor([...ctx.path, key, i]);
      rows.push(
        h(
          "div",
          { class: `pose-item${err ? " has-error" : ""}`, title: err ?? "" },
          h("div", { class: "pose-tools" }, h("span", { class: "idx mono", text: String(i + 1) }), h("button", { class: "icon-only ghost", title: "Move up", onclick: () => move(-1) }, icon("arrowUp", 11)), h("button", { class: "icon-only ghost", title: "Move down", onclick: () => move(1) }, icon("arrowDown", 11)), h("button", { class: "icon-only ghost danger", title: "Remove", onclick: remove }, icon("trash", 11))),
          this.#poseWidget(itemCtx, String(i), false),
        ),
      );
    });
    const add = h("button", { class: "small", onclick: () => this.#store.update((doc) => {
      const obj = ctx.resolve(doc);
      if (!obj) return;
      const arr = Array.isArray(obj[key]) ? (obj[key] as unknown[]) : (obj[key] = []);
      arr.push(points ? { x: 0, y: 0 } : "");
    }) }, icon("plus", 11), points ? "Add point" : "Add pose");
    const tools: Node[] = [add];
    if (points) tools.push(h("button", { class: "small", title: "Paste lines of 'x, y' or 'x, y, yaw'", onclick: () => void this.#pastePoints(ctx, key) }, icon("fileText", 11), "Paste x, y lines"));
    return h("div", { class: "poses" }, ...rows, rows.length ? null : h("div", { class: "muted small", text: "empty" }), h("div", { class: "inline" }, ...tools));
  }

  async #pastePoints(ctx: FieldCtx, key: string): Promise<void> {
    const ta = h("textarea", { class: "mono", rows: 8, placeholder: "0, 0\n3.0, 0.3\n3.0, -3.0, 90", spellcheck: false });
    const v = await modal({ title: "Paste points", body: [h("p", { class: "muted", text: "One point per line: x, y or x, y, yaw_deg (comma, space or tab separated). Replaces the current points." }), ta], buttons: [{ label: "Cancel", value: "cancel" }, { label: "Apply", value: "ok", primary: true }], onOpen: () => ta.focus() });
    if (v !== "ok") return;
    const pts: number[][] = [];
    for (const line of ta.value.split(/\r?\n/)) {
      const nums = line.split(/[,\s;]+/).filter((x) => x !== "").map(Number);
      if (nums.length >= 2 && nums.slice(0, 3).every((n) => Number.isFinite(n))) pts.push(nums.slice(0, 3));
    }
    if (!pts.length) {
      toast("No points found", "warn");
      return;
    }
    this.#write(ctx, key, pts, false);
    toast(`${pts.length} points set`, "ok", 1500);
  }

  #optionsWidget(ctx: FieldCtx, key: string): HTMLElement {
    const list = Array.isArray(ctx.obj[key]) ? (ctx.obj[key] as unknown[]).map(String) : [];
    const ta = h("textarea", { rows: Math.max(2, Math.min(6, list.length + 1)), class: "mono", placeholder: "Continue\nStop", "data-f": this.#fkey(ctx, key), spellcheck: false });
    ta.value = list.join("\n");
    ta.addEventListener("input", () => {
      const opts = ta.value.split("\n").map((x) => x.trim()).filter((x) => x !== "");
      this.#write(ctx, key, opts.length ? opts : undefined);
    });
    return h("div", { class: "stack" }, ta, h("div", { class: "hint", text: "One option per line. Default: Continue, Stop." }));
  }

  #btWidget(ctx: FieldCtx, key: string): HTMLElement {
    const v = ctx.obj[key];
    const mode: "none" | "file" | "template" = v === undefined ? "none" : typeof v === "string" ? "file" : "template";
    const fk = this.#fkey(ctx, key);
    const seg = h(
      "div",
      { class: "seg small" },
      h("button", { class: mode === "none" ? "active" : "", onclick: () => this.#write(ctx, key, undefined, false) }, "None"),
      h("button", { class: mode === "file" ? "active" : "", onclick: () => this.#write(ctx, key, typeof v === "string" ? v : "", false) }, "File path"),
      h("button", { class: mode === "template" ? "active" : "", onclick: () => this.#write(ctx, key, isRecord(v) ? v : { template: "navigate_with_recovery" }, false) }, "Template"),
    );
    const body = h("div", { class: "stack" });
    if (mode === "file") {
      const input = h("input", { type: "text", class: "mono", value: typeof v === "string" ? v : "", placeholder: "/home/pi/bt/my_tree.xml", "data-f": `${fk}.file`, spellcheck: false });
      input.addEventListener("input", () => this.#write(ctx, key, input.value));
      body.append(input, h("div", { class: "hint", text: "Path of a BehaviorTree.CPP XML file on the robot." }));
    } else if (mode === "template") {
      const t = isRecord(v) ? v : {};
      const tctx: FieldCtx = { resolve: (doc) => {
        const obj = ctx.resolve(doc);
        return obj && isRecord(obj[key]) ? (obj[key] as Record<string, unknown>) : null;
      }, obj: t, path: [...ctx.path, key] };
      const tplSel = select(BT_TEMPLATES.map((x) => ({ value: x })), typeof t.template === "string" ? t.template : BT_TEMPLATES[0], { "data-f": `${fk}.template` });
      tplSel.addEventListener("change", () => this.#write(tctx, "template", tplSel.value, false));
      const recov = h("div", { class: "inline wrap" });
      const current = Array.isArray(t.recoveries) ? t.recoveries.map(String) : [...BT_DEFAULTS.recoveries];
      for (const r of RECOVERIES) {
        const cb = h("input", { type: "checkbox", "data-f": `${fk}.rec.${r}` });
        cb.checked = current.includes(r);
        cb.addEventListener("change", () => {
          const next = RECOVERIES.filter((x) => (x === r ? cb.checked : current.includes(x)));
          this.#write(tctx, "recoveries", next, false);
        });
        recov.append(h("label", { class: "chk" }, cb, r.replace("_", " ")));
      }
      body.append(
        field("Template", tplSel),
        field("Retries", this.#numberInput(tctx, "retries", { min: 0, max: 20, step: 1, integer: true, placeholder: String(BT_DEFAULTS.retries) })),
        field("Recoveries", recov, { help: "Tried round-robin between attempts (order: clear costmap, spin, wait, back up)." }),
        field("Replan rate (Hz)", this.#numberInput(tctx, "replan_rate_hz", { min: 0.1, max: 20, placeholder: String(BT_DEFAULTS.replan_rate_hz) })),
        field("Spin (deg)", this.#numberInput(tctx, "spin_deg", { placeholder: String(BT_DEFAULTS.spin_deg) })),
        field("Back up (m)", this.#numberInput(tctx, "backup_m", { placeholder: String(BT_DEFAULTS.backup_m) })),
        field("Wait (s)", this.#numberInput(tctx, "wait_s", { placeholder: String(BT_DEFAULTS.wait_s) })),
        field("Planner id", this.#textInput(tctx, "planner_id", { placeholder: BT_DEFAULTS.planner_id, mono: true })),
        field("Controller id", this.#textInput(tctx, "controller_id", { placeholder: BT_DEFAULTS.controller_id, mono: true })),
        h("div", { class: "hint", text: "The runner writes the XML to <home>/bt/ at deploy time. Menu → Export BT XML shows it." }),
      );
    } else body.append(h("div", { class: "hint", text: "Nav2's default behavior tree is used." }));
    return h("div", { class: "bt" }, seg, body);
  }

  #eventSourceWidget(ctx: FieldCtx, key: string): HTMLElement {
    const v = isRecord(ctx.obj[key]) ? ctx.obj[key] : { type: "ros.topic" };
    const sctx: FieldCtx = { resolve: (doc) => {
      const obj = ctx.resolve(doc);
      if (!obj) return null;
      if (!isRecord(obj[key])) obj[key] = { type: "ros.topic" };
      return obj[key] as Record<string, unknown>;
    }, obj: v, path: [...ctx.path, key] };
    const typeSel = select(allTriggers().map((t) => ({ value: t.type, label: t.label })), typeof v.type === "string" ? v.type : "ros.topic", { "data-f": `${this.#fkey(ctx, key)}.type` });
    typeSel.addEventListener("change", () => this.#write(ctx, key, newEventSource(typeSel.value), false));
    const nodes: Node[] = [field("Type", typeSel)];
    nodes.push(...this.#eventSourceFields(sctx, typeof v.type === "string" ? v.type : ""));
    return h("div", { class: "stack boxed" }, ...nodes);
  }

  #eventSourceFields(ctx: FieldCtx, type: string): Node[] {
    const def = triggerDef(type);
    const nodes: Node[] = [];
    if (def) {
      nodes.push(h("div", { class: "hint", text: def.help }));
      for (const p of def.params) {
        const n = this.#paramField(ctx, p);
        if (n) nodes.push(n);
      }
    }
    nodes.push(field("When", this.#expressionInput(ctx, "when", "payload.value"), { wide: true, error: this.#errorFor([...ctx.path, "when"]) ?? this.#warnFor([...ctx.path, "when"]), help: "Only fire when this is true for the payload." }));
    nodes.push(field("Edge", this.#selectInput(ctx, "edge", ["any", "rising"], "any"), { help: "rising: fire only when 'when' turns from false to true." }));
    nodes.push(field("Debounce (s)", this.#numberInput(ctx, "debounce_s", { min: 0, placeholder: "0" }), { error: this.#errorFor([...ctx.path, "debounce_s"]) }));
    return nodes;
  }

  // ---- trigger form ----------------------------------------------------------------------------------

  #triggerForm(t: Trigger, kind: "trigger" | "interrupt", index: number): Node[] {
    const s = this.#store;
    const listKey = kind === "trigger" ? "triggers" : "interrupts";
    const path: Path = [listKey, index];
    const ctx: FieldCtx = { resolve: (doc) => (doc[listKey]?.[index] as Record<string, unknown> | undefined) ?? null, obj: t as unknown as Record<string, unknown>, path };
    const def = triggerDef(t.type);
    const head = h(
      "div",
      { class: "insp-head" },
      icon(def?.icon ?? "alert", 16),
      h("div", { class: "grow" }, h("div", { class: "title", text: `${kind === "trigger" ? "Trigger" : "Interrupt"}: ${def?.label ?? t.type}` }), h("div", { class: "sub mono", text: t.type })),
      this.#helpToggle(),
      h("button", { class: "icon-only ghost danger", title: `Delete ${kind}`, onclick: () => s.deleteTrigger(kind, index) }, icon("trash", 12)),
    );
    const typeSel = select(allTriggers().map((x) => ({ value: x.type, label: x.label })), t.type, { "data-f": `${pathKey(path)}.type` });
    typeSel.addEventListener("change", () => {
      const fresh = newEventSource(typeSel.value);
      this.#store.update((doc) => {
        const cur = doc[listKey]?.[index] as Record<string, unknown> | undefined;
        if (!cur) return;
        const keep: Record<string, unknown> = {};
        for (const k of ["id", "name", "enabled", "policy", "priority", "set", "run", "when", "edge", "debounce_s"]) if (cur[k] !== undefined) keep[k] = cur[k];
        for (const k of Object.keys(cur)) delete cur[k];
        Object.assign(cur, fresh, keep);
      });
    });
    const eventFields = this.#eventSourceFields(ctx, t.type);
    const policies: readonly string[] = kind === "interrupt" ? INTERRUPT_POLICIES : POLICIES;
    const missionNames = (s.missionNames ?? []).filter((n) => n !== s.mission?.name).sort();
    const dispatch: Node[] = [];
    if (kind === "interrupt") {
      dispatch.push(field("Run mission", this.#comboSelect(ctx, "run", missionNames, { missingLabel: "unknown", placeholder: "mission name" }), { error: this.#errorFor([...path, "run"]), help: "Mission started when this fires." }));
    }
    dispatch.push(
      field("Policy", this.#selectInput(ctx, "policy", [...policies], kind === "interrupt" ? "interrupt_and_resume" : undefined), { error: this.#errorFor([...path, "policy"]), help: kind === "interrupt" ? "interrupt_and_resume suspends the current run and resumes it afterwards; preempt cancels it." : "How the run is dispatched when something else is running." }),
      field("Priority", this.#numberInput(ctx, "priority", { min: 0, max: 100, step: 1, integer: true, placeholder: "50" }), { error: this.#errorFor([...path, "priority"]) }),
      field("Enabled", this.#boolInput(ctx, "enabled", true)),
    );
    const findings = s.findingsUnder(path);
    const problems = findings.length ? h("div", { class: "insp-problems" }, ...findings.map((f) => h("div", { class: f.level }, icon(f.level === "error" ? "ban" : "alert", 11), f.message))) : null;
    return [
      head,
      problems,
      section("Event", [field("Type", typeSel), field("Name", this.#textInput(ctx, "name", { placeholder: def ? def.label : "" })), ...eventFields]),
      section("Dispatch", dispatch),
      section("Set inputs", [this.#setTable(ctx)], { head: [h("button", { class: "icon-only ghost", title: "Add", onclick: (e: Event) => {
        stopEvent(e);
        this.#addSetRow(ctx);
      } }, icon("plus", 12))] }),
      section("Advanced", [field("Id", h("code", { class: "mono", text: typeof t.id === "string" ? t.id : "" }))], { collapsed: true }),
    ].filter((x): x is HTMLElement => x !== null);
  }

  #setTable(ctx: FieldCtx): HTMLElement {
    const set = isRecord(ctx.obj.set) ? ctx.obj.set : {};
    const entries = Object.entries(set);
    if (!entries.length) return h("div", { class: "empty", text: "Inputs computed from the payload, e.g. goal = payload, drop = payload.drop." });
    const table = h("div", { class: "table set" });
    const declared = new Set([...Object.keys(this.#store.mission?.inputs ?? {}), ...Object.keys(this.#store.mission?.vars ?? {})]);
    for (const [k, v] of entries) {
      const err = this.#errorFor([...ctx.path, "set", k]);
      const warn = this.#warnFor([...ctx.path, "set", k]);
      const keyIn = h("input", { type: "text", class: `mono${declared.has(k) ? "" : " missing"}`, value: k, "data-f": `${this.#fkey(ctx, "set")}.${k}.key`, list: "insp-input-names", spellcheck: false });
      keyIn.addEventListener("change", () => {
        const to = keyIn.value.trim();
        if (!to || to === k) return this.render();
        this.#store.update((doc) => {
          const obj = ctx.resolve(doc);
          if (!obj || !isRecord(obj.set)) return;
          const rebuilt: Record<string, unknown> = {};
          for (const [kk, vv] of Object.entries(obj.set)) rebuilt[kk === k ? to : kk] = vv;
          obj.set = rebuilt;
        });
      });
      const valIn = h("input", { type: "text", class: "mono", value: typeof v === "string" ? v : JSON.stringify(v), placeholder: "expression", "data-f": `${this.#fkey(ctx, "set")}.${k}.value`, spellcheck: false });
      valIn.addEventListener("input", () =>
        this.#store.update((doc) => {
          const obj = ctx.resolve(doc);
          if (obj && isRecord(obj.set)) obj.set[k] = valIn.value;
        }, { coalesce: `${this.#fkey(ctx, "set")}.${k}` }),
      );
      const del = h("button", { class: "icon-only ghost danger", title: "Remove", onclick: () =>
        this.#store.update((doc) => {
          const obj = ctx.resolve(doc);
          if (!obj || !isRecord(obj.set)) return;
          delete obj.set[k];
          if (Object.keys(obj.set).length === 0) delete obj.set;
        }) }, icon("trash", 11));
      table.append(h("div", { class: `trow${err ? " has-error" : warn ? " has-warning" : ""}`, title: err ?? warn ?? "" }, keyIn, h("span", { class: "muted", text: "=" }), valIn, del));
    }
    const dl = h("datalist", { id: "insp-input-names" }, ...[...declared].map((n) => h("option", { value: n })));
    table.append(dl);
    return table;
  }

  #addSetRow(ctx: FieldCtx): void {
    const declared = Object.keys(this.#store.mission?.inputs ?? {});
    const set = isRecord(ctx.obj.set) ? ctx.obj.set : {};
    let name = declared.find((n) => !(n in set)) ?? "input";
    let i = 2;
    while (name in set) name = `input${i++}`;
    const nm = name;
    this.#store.update((doc) => {
      const obj = ctx.resolve(doc);
      if (!obj) return;
      if (!isRecord(obj.set)) obj.set = {};
      (obj.set as Record<string, unknown>)[nm] = "payload";
    });
  }
}

// ---- helpers --------------------------------------------------------------------------------------------

/** Text → JSON value when it looks like one, else the text. `"quoted"` forces text. */
export function parseLoose(text: string, preferText: boolean): unknown {
  const t = text.trim();
  if (t === "") return undefined;
  if (preferText && !t.startsWith('"')) return text;
  if (/^"(?:[^"\\]|\\.)*"$/.test(t)) {
    try {
      return JSON.parse(t) as unknown;
    } catch {
      return text;
    }
  }
  if (t === "true") return true;
  if (t === "false") return false;
  if (t === "null") return null;
  if (NUMBER_INPUT_RE.test(t)) return Number(t);
  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      return JSON.parse(t) as unknown;
    } catch {
      return text;
    }
  }
  return text;
}

function parseNumOrExpr(text: string): number | string {
  const t = text.trim();
  return NUMBER_INPUT_RE.test(t) ? Number(t) : t;
}

function round(v: number): number {
  return Math.round(v * 1000) / 1000;
}

function hasNestedIndex(rest: Path): boolean {
  for (let i = 0; i < rest.length - 1; i++) {
    const seg = rest[i];
    if ((seg === "then" || seg === "else" || seg === "body" || seg === "before_retry") && typeof rest[i + 1] === "number") return true;
  }
  return false;
}
