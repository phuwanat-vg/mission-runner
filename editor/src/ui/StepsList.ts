/**
 * Nested steps list: flat rows separated by hairlines with status glyphs,
 * short summaries, badges, hover actions, inline rename, insert buttons,
 * HTML5 drag and drop (within and across lists, from the block picker and
 * from the sites panel) and live run highlighting. Nested bodies are
 * indented behind a single left rule with a small inline label.
 */

import type { Store, RunMark } from "../state/Store";
import type { AppActions } from "./actions";
import type { Mission, Path, Step } from "../model/types";
import { isRecord } from "../model/types";
import { blockDefOrUnknown, containerKeys, stepSummaryShort, stepTitle } from "../model/blocks";
import { getList, nestedStepCount, pathKey } from "../model/ids";
import { hasMapPresence, stopRange } from "../map/geometry";
import { poseAt, stepStatesAt } from "../map/preview";
import { h, formatDuration, replace, stopEvent } from "./dom";
import { icon } from "./icons";
import { confirm } from "./dialogs";

export const STEP_DND_TYPE = "application/x-mission-step";
export const SITE_DND_TYPE = "application/x-mission-site";
export const BLOCK_DND_TYPE = "application/x-mission-block";

interface DropTarget {
  listPath: Path;
  index: number;
}

const CONTAINER_LABEL: Record<string, string> = { then: "then", else: "else", body: "body", before_retry: "before retry" };

export class StepsList {
  readonly el: HTMLElement;
  /** Told which step the pointer is over, so the map can highlight its markers. */
  onHoverStep: ((id: string | null) => void) | null = null;
  #store: Store;
  #actions: AppActions;
  #dragPath: Path | null = null;
  #dropLine: HTMLElement;
  #dropTarget: DropTarget | null = null;
  #rowByStepId = new Map<string, HTMLElement>();
  /** The status glyph of every row, so a dry-run playhead can be applied without a re-render. */
  #glyphByStepId = new Map<string, HTMLElement>();
  #lastScrolled: string | null = null;
  #collapsed = new Set<string>();
  #hover: string | null = null;

  constructor(store: Store, actions: AppActions) {
    this.#store = store;
    this.#actions = actions;
    this.#dropLine = h("div", { class: "drop-line", hidden: true });
    this.el = h("div", { class: "steps", tabindex: 0 });
    this.el.append(this.#dropLine);
    for (const t of ["mission", "selection", "validation", "runmarks", "remote"] as const) store.on(t, () => this.render());
    // the dry-run playhead moves at 60 Hz: mutate the rows, never rebuild them
    store.on("preview", () => this.applyDryRun());
    this.el.addEventListener("dragover", (e) => this.#onDragOver(e));
    this.el.addEventListener("dragleave", (e) => {
      if (!this.el.contains(e.relatedTarget as Node | null)) this.#hideDrop();
    });
    this.el.addEventListener("drop", (e) => this.#onDrop(e));
    this.el.addEventListener("click", (e) => {
      if (e.target === this.el) store.select(null);
    });
    this.el.addEventListener("pointerover", (e) => {
      const row = (e.target as HTMLElement | null)?.closest<HTMLElement>(".step");
      this.#emitHover(row?.dataset.id ?? null);
    });
    this.el.addEventListener("pointerleave", () => this.#emitHover(null));
    this.render();
  }

  /** Highlight the row of a step hovered on the map (no re-render). */
  setHover(id: string | null): void {
    if (this.#hover === id) return;
    if (this.#hover) this.#rowByStepId.get(this.#hover)?.classList.remove("hover");
    this.#hover = id;
    if (id) this.#rowByStepId.get(id)?.classList.add("hover");
  }

  #emitHover(id: string | null): void {
    if (this.#hover === id) return;
    this.setHover(id);
    this.onHoverStep?.(id);
  }

  render(): void {
    const m = this.#store.mission;
    this.#rowByStepId.clear();
    this.#glyphByStepId.clear();
    if (!m) {
      replace(this.el, this.#dropLine, h("div", { class: "empty big", text: "Open a mission from the sidebar, or create one with ⋯ → New mission." }));
      return;
    }
    const flow = h("div", { class: "step-list root" });
    this.#renderList(flow, m, m.flow, ["flow"]);
    const nodes: (Node | null)[] = [this.#dropLine, flow];
    const onAbort = m.on_abort;
    const abortSection = h("div", { class: "abort-section" });
    const abortList = h("div", { class: "step-list root" });
    this.#renderList(abortList, m, Array.isArray(onAbort) ? onAbort : [], ["on_abort"]);
    abortSection.append(h("div", { class: "list-head", title: "Steps run when the mission fails or is canceled (navigation is already canceled)", text: "On abort" }), abortList);
    nodes.push(abortSection);
    replace(this.el, ...nodes);
    this.#scrollToRunning();
    this.applyDryRun();
  }

  /**
   * Paint the loaded dry run's playhead onto the rows: the step the current
   * sample belongs to is highlighted, steps that already finished show their
   * status. Called on every frame while the preview plays, so it only touches
   * classes and one glyph per row.
   */
  applyDryRun(): void {
    const d = this.#store.dryRun;
    this.el.classList.toggle("dry", !!d);
    if (!d) {
      for (const [id, row] of this.#rowByStepId) {
        row.classList.remove("dry-current");
        row.dataset.dry = "";
        const glyph = this.#glyphByStepId.get(id);
        if (glyph) replace(glyph, statusGlyph(this.#store.runMarks.get(id)));
      }
      return;
    }
    const states = stepStatesAt(d.result, d.t);
    const current = poseAt(d.result, d.t)?.step ?? "";
    for (const [id, row] of this.#rowByStepId) {
      const st = states.get(id);
      row.classList.toggle("dry-current", id === current);
      row.dataset.dry = st?.status ?? "";
      const glyph = this.#glyphByStepId.get(id);
      if (!glyph) continue;
      replace(glyph, statusGlyph(st ? { status: dryStatus(st.status), startedAt: 0 } : undefined));
      glyph.title = st ? `dry run: ${st.status}${st.error ? ` · ${st.error}` : ""}` : "";
    }
    if (current) this.#rowByStepId.get(current)?.scrollIntoView({ block: "nearest" });
  }

  #renderList(container: HTMLElement, mission: Mission, steps: Step[], listPath: Path): void {
    const key = pathKey(listPath);
    container.dataset.list = key;
    if (steps.length === 0) {
      const empty = h("div", { class: "drop-empty", text: listPath.length === 1 ? (listPath[0] === "flow" ? "No steps yet" : "No cleanup steps") : "empty" });
      empty.dataset.list = key;
      container.append(empty);
    }
    steps.forEach((step, i) => {
      container.append(this.#insertLine(listPath, i));
      container.append(this.#row(mission, step, [...listPath, i]));
    });
    container.append(this.#addButton(listPath, steps.length));
  }

  #insertLine(listPath: Path, index: number): HTMLElement {
    return h("div", { class: "insert-line" }, h("button", { class: "insert-btn", title: "Insert a step here", onclick: (e: Event) => {
      stopEvent(e);
      this.#actions.addStepAt(listPath, index);
    } }, icon("plus", 10)));
  }

  #addButton(listPath: Path, index: number): HTMLElement {
    const btn = h("button", { class: "add-step ghost", onclick: () => this.#actions.addStepAt(listPath, index) }, icon("plus", 12), "Add step");
    btn.dataset.list = pathKey(listPath);
    btn.dataset.index = String(index);
    return btn;
  }

  #row(mission: Mission, step: Step, path: Path): HTMLElement {
    const s = this.#store;
    const def = blockDefOrUnknown(step.type);
    const id = typeof step.id === "string" ? step.id : "";
    const selected = s.selection?.kind === "step" && s.selection.id === id;
    const findings = id ? s.findingsFor(id) : [];
    const errs = findings.filter((f) => f.level === "error" && pathStartsWith(f.path, path) && !isNestedFinding(f.path, path));
    const warns = findings.filter((f) => f.level === "warning" && pathStartsWith(f.path, path) && !isNestedFinding(f.path, path));
    const mark = id ? s.runMarks.get(id) : undefined;
    const disabled = step.enabled === false;
    const index = path[path.length - 1] as number;
    const containers = containerKeys(step.type);
    const before = isRecord(step.on_fail) && Array.isArray(step.on_fail.before_retry) ? (step.on_fail.before_retry as Step[]) : null;
    const hasChildren = containers.length > 0 || before !== null;
    const collapsed = this.#collapsed.has(id);

    const row = h("div", { class: `step${selected ? " selected" : ""}${disabled ? " off" : ""}${mark ? ` run-${mark.status}` : ""}${errs.length ? " has-error" : warns.length ? " has-warning" : ""}${id && id === this.#hover ? " hover" : ""}`, draggable: true });
    row.dataset.path = pathKey(path);
    row.dataset.id = id;
    if (id) this.#rowByStepId.set(id, row);
    const stops = stopRange(id ? s.mapModel.byStep.get(id) : undefined);
    const mapGlyph = stops
      ? h("span", { class: "stop-no", title: `Stop ${stops} on the map`, text: stops })
      : hasMapPresence(step.type)
        ? h("span", { class: "stop-no empty", title: "This step has a pose, but it cannot be placed on the map" }, icon("mapPinOff", 10))
        : h("span", { class: "no-map", title: "Nothing to show on the map for this step" }, icon("mapPinOff", 10));

    const title = h("span", { class: "title", text: stepTitle(step), title: `${def.label}\nDouble-click to rename` });
    title.addEventListener("dblclick", (e) => {
      stopEvent(e);
      this.#editName(title, step, path);
    });
    const badges: Node[] = [];
    if (errs.length) badges.push(h("span", { class: "badge err", title: errs.map((f) => f.message).join("\n"), text: errs.length === 1 ? "error" : `${errs.length} errors` }));
    if (warns.length) badges.push(h("span", { class: "badge warn", title: warns.map((f) => f.message).join("\n"), text: warns.length === 1 ? "warning" : `${warns.length} warnings` }));
    if (disabled) badges.push(h("span", { class: "badge off", text: "off" }));

    let summary = stepSummaryShort(step, { mission });
    if (typeof step.out === "string" && !summary.includes(`→ ${step.out}`)) summary = summary ? `${summary} · → ${step.out}` : `→ ${step.out}`;
    if (!summary && stepTitle(step) !== def.label) summary = def.label;

    const duration = mark && mark.status !== "running" && mark.duration !== undefined ? formatDuration(mark.duration) : mark?.status === "running" ? "…" : "";
    const glyph = h("span", { class: "glyph", title: mark ? markTitle(mark) : "" }, statusGlyph(mark));
    if (id) this.#glyphByStepId.set(id, glyph);
    const actions = h(
      "span",
      { class: "row-actions" },
      h("button", { class: "icon-only ghost", title: disabled ? "Enable" : "Disable", onclick: (e: Event) => {
        stopEvent(e);
        s.setStepEnabled(path, disabled);
      } }, icon(disabled ? "eyeOff" : "eye", 12)),
      h("button", { class: "icon-only ghost", title: "Duplicate (Ctrl+D)", onclick: (e: Event) => {
        stopEvent(e);
        s.duplicateStep(path);
      } }, icon("copy", 12)),
      h("button", { class: "icon-only ghost danger", title: "Delete (Del)", onclick: (e: Event) => {
        stopEvent(e);
        void this.deleteStep(path, step);
      } }, icon("trash", 12)),
    );
    const main = h(
      "div",
      { class: "step-main" },
      h("span", { class: "grip", title: "Drag to reorder" }, icon("grip", 12)),
      h("span", { class: "idx mono", text: String(index + 1) }),
      mapGlyph,
      glyph,
      h("div", { class: "step-text" }, h("div", { class: "line1" }, title, ...badges), summary ? h("div", { class: "summary", text: summary }) : null),
      h("span", { class: "duration mono", text: duration }),
      actions,
      hasChildren
        ? h("button", { class: "icon-only ghost fold", title: collapsed ? "Expand" : "Collapse", onclick: (e: Event) => {
          stopEvent(e);
          this.#toggleFold(id);
        } }, icon(collapsed ? "chevronRight" : "chevronDown", 12))
        : null,
    );
    row.append(main);
    if (mark?.status === "running" && mark.feedback) row.append(h("div", { class: "feedback mono", text: feedbackText(mark.feedback) }));
    else if (mark && mark.status !== "running" && mark.error) row.append(h("div", { class: "feedback error", text: mark.error }));

    row.addEventListener("click", (e) => {
      if ((e.target as HTMLElement).closest("input")) return;
      stopEvent(e);
      s.select({ kind: "step", id });
    });
    row.addEventListener("dragstart", (e) => {
      if ((e.target as HTMLElement).closest("input")) {
        e.preventDefault();
        return;
      }
      this.#dragPath = path;
      e.dataTransfer?.setData(STEP_DND_TYPE, pathKey(path));
      e.dataTransfer?.setData("text/plain", JSON.stringify(step));
      if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
      e.stopPropagation();
      setTimeout(() => row.classList.add("dragging"), 0);
    });
    row.addEventListener("dragend", () => {
      row.classList.remove("dragging");
      this.#dragPath = null;
      this.#hideDrop();
    });

    if (hasChildren && !collapsed) {
      const children = h("div", { class: "children" });
      for (const key of containers) {
        const list = Array.isArray(step[key]) ? (step[key] as Step[]) : [];
        if (key === "else" && list.length === 0) {
          children.append(h("div", { class: "list-head optional" }, h("button", { class: "ghost small", onclick: (e: Event) => {
            stopEvent(e);
            this.#actions.addStepAt([...path, "else"], 0);
          } }, icon("plus", 11), "else")));
          continue;
        }
        const sub = h("div", { class: "step-list" });
        children.append(h("div", { class: "list-head", text: CONTAINER_LABEL[key] ?? key }), sub);
        this.#renderList(sub, mission, list, [...path, key]);
      }
      if (before) {
        const sub = h("div", { class: "step-list" });
        children.append(h("div", { class: "list-head", text: `before retry${retryText(step)}` }), sub);
        this.#renderList(sub, mission, before, [...path, "on_fail", "before_retry"]);
      }
      row.append(children);
    } else if (hasChildren && collapsed) {
      row.append(h("div", { class: "children-collapsed muted", text: `${nestedStepCount(step)} nested step${nestedStepCount(step) === 1 ? "" : "s"}` }));
    }
    return row;
  }

  #toggleFold(id: string): void {
    if (this.#collapsed.has(id)) this.#collapsed.delete(id);
    else this.#collapsed.add(id);
    this.render();
  }

  #editName(title: HTMLElement, step: Step, path: Path): void {
    const input = h("input", { type: "text", class: "rename", value: typeof step.name === "string" ? step.name : "", placeholder: blockDefOrUnknown(step.type).label });
    const done = (commit: boolean): void => {
      if (commit) {
        const v = input.value.trim();
        this.#store.update((doc) => {
          const list = getList(doc, path.slice(0, -1));
          const st = list?.[path[path.length - 1] as number];
          if (!st) return;
          if (v === "") delete st.name;
          else st.name = v;
        });
      } else this.render();
    };
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") done(true);
      else if (e.key === "Escape") done(false);
    });
    input.addEventListener("blur", () => done(true));
    input.addEventListener("click", (e) => e.stopPropagation());
    title.replaceWith(input);
    input.focus();
    input.select();
  }

  async deleteStep(path: Path, step: Step): Promise<void> {
    const nested = nestedStepCount(step);
    if (nested > 0) {
      const ok = await confirm(`Delete "${stepTitle(step)}" and its ${nested} nested step${nested === 1 ? "" : "s"}?`, { ok: "Delete", danger: true });
      if (!ok) return;
    }
    this.#store.deleteStep(path);
  }

  // ---- drag and drop --------------------------------------------------------------------

  #onDragOver(e: DragEvent): void {
    const types = e.dataTransfer?.types ?? [];
    const kind = types.includes(STEP_DND_TYPE) ? "move" : types.includes(BLOCK_DND_TYPE) || types.includes(SITE_DND_TYPE) ? "copy" : null;
    if (!kind) return;
    const target = this.#targetAt(e);
    if (!target) {
      this.#hideDrop();
      return;
    }
    if (kind === "move" && this.#dragPath && pathStartsWith(target.listPath, this.#dragPath)) {
      this.#hideDrop();
      return;
    }
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = kind;
    this.#showDrop(target);
  }

  #targetAt(e: DragEvent): (DropTarget & { anchor: HTMLElement; before: boolean }) | null {
    const el = e.target as HTMLElement;
    const empty = el.closest<HTMLElement>(".drop-empty");
    if (empty?.dataset.list !== undefined) return { listPath: parsePath(empty.dataset.list), index: 0, anchor: empty, before: true };
    const add = el.closest<HTMLElement>(".add-step");
    if (add?.dataset.list !== undefined) return { listPath: parsePath(add.dataset.list), index: Number(add.dataset.index), anchor: add, before: true };
    const row = el.closest<HTMLElement>(".step");
    if (!row?.dataset.path) return null;
    const main = row.querySelector<HTMLElement>(":scope > .step-main");
    if (!main) return null;
    const path = parsePath(row.dataset.path);
    const r = main.getBoundingClientRect();
    const before = e.clientY < r.top + r.height / 2;
    const listPath = path.slice(0, -1);
    const index = (path[path.length - 1] as number) + (before ? 0 : 1);
    return { listPath, index, anchor: main, before };
  }

  #showDrop(t: DropTarget & { anchor: HTMLElement; before: boolean }): void {
    this.#dropTarget = { listPath: t.listPath, index: t.index };
    const r = t.anchor.getBoundingClientRect();
    const host = this.el.getBoundingClientRect();
    const y = (t.before ? r.top : r.bottom) - host.top + this.el.scrollTop;
    this.#dropLine.style.top = `${y - 1}px`;
    this.#dropLine.style.left = `${r.left - host.left}px`;
    this.#dropLine.style.width = `${r.width}px`;
    this.#dropLine.hidden = false;
  }

  #hideDrop(): void {
    this.#dropLine.hidden = true;
    this.#dropTarget = null;
  }

  #onDrop(e: DragEvent): void {
    const target = this.#dropTarget;
    this.#hideDrop();
    if (!target || !e.dataTransfer) return;
    e.preventDefault();
    const stepKey = e.dataTransfer.getData(STEP_DND_TYPE);
    if (stepKey) {
      const from = this.#dragPath ?? parsePath(stepKey);
      this.#store.moveStep(from, target.listPath, target.index);
      this.#dragPath = null;
      return;
    }
    const block = e.dataTransfer.getData(BLOCK_DND_TYPE);
    if (block) {
      this.#actions.insertBlock(block, target);
      return;
    }
    const site = e.dataTransfer.getData(SITE_DND_TYPE);
    if (site) this.#actions.insertSite(site, target);
  }

  // ---- live ------------------------------------------------------------------------------

  #scrollToRunning(): void {
    let runningId: string | null = null;
    for (const [id, mark] of this.#store.runMarks) if (mark.status === "running") runningId = id;
    if (!runningId || runningId === this.#lastScrolled) return;
    this.#lastScrolled = runningId;
    this.#rowByStepId.get(runningId)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  scrollToStep(id: string): void {
    this.#rowByStepId.get(id)?.scrollIntoView({ block: "nearest" });
  }
}

function parsePath(key: string): Path {
  return key.split("/").map((seg) => (/^\d+$/.test(seg) ? Number(seg) : seg));
}

function pathStartsWith(path: Path, prefix: Path): boolean {
  if (path.length < prefix.length) return false;
  for (let i = 0; i < prefix.length; i++) if (path[i] !== prefix[i]) return false;
  return true;
}

/** True when a finding at `fpath` belongs to a nested step of the step at `path` (it gets its own badge). */
function isNestedFinding(fpath: Path, path: Path): boolean {
  const rest = fpath.slice(path.length);
  for (let i = 0; i < rest.length - 1; i++) {
    const seg = rest[i];
    if ((seg === "then" || seg === "else" || seg === "body" || seg === "before_retry") && typeof rest[i + 1] === "number") return true;
  }
  return false;
}

/** Dry-run step statuses reuse the run-mark glyphs. */
function dryStatus(status: string): RunMark["status"] {
  if (status === "running" || status === "succeeded" || status === "canceled" || status === "timeout") return status;
  return status === "failed" ? "failed" : "succeeded";
}

function statusGlyph(mark: RunMark | undefined): Node {
  if (!mark) return h("span", { class: "g idle", text: "" });
  switch (mark.status) {
    case "running":
      return h("span", { class: "g running", text: "▶" });
    case "succeeded":
      return h("span", { class: "g ok", text: "✓" });
    case "canceled":
      return h("span", { class: "g canceled", text: "○" });
    default:
      return h("span", { class: "g fail", text: "✗" });
  }
}

function markTitle(mark: RunMark): string {
  const parts: string[] = [mark.status];
  if (mark.duration !== undefined) parts.push(formatDuration(mark.duration));
  if (mark.error) parts.push(mark.error);
  return parts.join(" · ");
}

function feedbackText(fb: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof fb.distance_remaining === "number") parts.push(`${fb.distance_remaining.toFixed(1)} m left`);
  if (typeof fb.eta_s === "number") parts.push(`eta ${formatDuration(fb.eta_s)}`);
  if (typeof fb.recoveries === "number") parts.push(`${fb.recoveries} recover${fb.recoveries === 1 ? "y" : "ies"}`);
  if (!parts.length) {
    for (const [k, v] of Object.entries(fb)) if (typeof v === "number" || typeof v === "string") parts.push(`${k} ${typeof v === "number" ? Number(v.toFixed(2)) : v}`);
  }
  return parts.join(" · ");
}

function retryText(step: Step): string {
  const f = step.on_fail;
  if (!isRecord(f)) return "";
  const parts: string[] = [];
  if (typeof f.retry === "number") parts.push(`retry ${f.retry}`);
  if (typeof f.retry_delay_s === "number" && f.retry_delay_s > 0) parts.push(`delay ${f.retry_delay_s} s`);
  return parts.length ? ` · ${parts.join(", ")}` : "";
}
