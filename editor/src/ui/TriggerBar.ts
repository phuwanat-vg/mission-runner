/**
 * "Starts when" chip line above the steps list. Chips show only the trigger
 * summary; policy, priority and problems live in the tooltip, and a small
 * dot marks chips with validation findings or runner trigger problems. The
 * "While running" line is shown only when the mission has interrupts;
 * otherwise a muted "+ interrupt" link ends the first line.
 */

import type { Store } from "../state/Store";
import type { AppActions } from "./actions";
import type { Trigger } from "../model/types";
import { triggerSummary } from "../model/blocks";
import { h, replace } from "./dom";
import { icon } from "./icons";

const POLICY_TEXT: Record<string, string> = {
  queue: "queue",
  preempt: "preempt",
  preempt_latest: "preempt (latest wins)",
  reject_if_busy: "only when idle",
  interrupt_and_resume: "interrupt and resume",
};

export class TriggerBar {
  readonly el: HTMLElement;
  #store: Store;
  #actions: AppActions;
  #starts: HTMLElement;
  #whileRow: HTMLElement;
  #while: HTMLElement;

  constructor(store: Store, actions: AppActions) {
    this.#store = store;
    this.#actions = actions;
    this.#starts = h("div", { class: "chips" });
    this.#while = h("div", { class: "chips" });
    this.#whileRow = h("div", { class: "trigger-row" }, h("span", { class: "row-label", text: "While running" }), this.#while);
    this.el = h("div", { class: "trigger-bar" }, h("div", { class: "trigger-row" }, h("span", { class: "row-label", text: "Starts when" }), this.#starts), this.#whileRow);
    for (const t of ["mission", "selection", "validation", "remote"] as const) store.on(t, () => this.render());
    this.render();
  }

  render(): void {
    const s = this.#store;
    const m = s.mission;
    if (!m) {
      this.el.hidden = true;
      return;
    }
    this.el.hidden = false;
    const sel = s.selection;
    const problems = this.#runnerProblems();
    const manual = h(
      "button",
      { class: `chip trigger manual${sel === null ? " selected" : ""}`, title: `Manual start (editor, HTTP, ROS service)\npolicy: ${POLICY_TEXT[m.policy ?? "queue"] ?? m.policy}, priority ${m.priority ?? 50}\nClick for mission settings.`, onclick: () => s.select(null) },
      "Manual",
    );
    const startChips = (m.triggers ?? []).map((t, i) => this.#chip(t, "trigger", i, problems));
    const interrupts = m.interrupts ?? [];
    const addTrigger = h("button", { class: "chip add", title: "Add a trigger", onclick: () => this.#actions.addTrigger("trigger") }, icon("plus", 12));
    const unmatched = problems.unmatched.length ? h("span", { class: "prob-dot err standalone", title: problems.unmatched.join("\n") }) : null;
    const interruptLink = interrupts.length ? null : h("button", { class: "ghost link-btn", title: "Add an interrupt: a trigger that runs another mission while this one is running", onclick: () => this.#actions.addTrigger("interrupt") }, "+ interrupt");
    replace(this.#starts, manual, ...startChips, addTrigger, unmatched, interruptLink);
    this.#whileRow.hidden = interrupts.length === 0;
    if (interrupts.length) {
      replace(this.#while, ...interrupts.map((t, i) => this.#chip(t, "interrupt", i, problems)), h("button", { class: "chip add", title: "Add an interrupt", onclick: () => this.#actions.addTrigger("interrupt") }, icon("plus", 12)));
    }
  }

  /** Runner-reported trigger problems of the open mission, keyed by trigger id where the message allows it ("id: message"). */
  #runnerProblems(): { byId: Map<string, string[]>; unmatched: string[] } {
    const s = this.#store;
    const m = s.mission;
    const byId = new Map<string, string[]>();
    const unmatched: string[] = [];
    if (!m) return { byId, unmatched };
    const summary = s.missions.find((x) => x.name === m.name);
    const ids = new Set<string>();
    for (const t of [...(m.triggers ?? []), ...(m.interrupts ?? [])]) if (typeof t.id === "string") ids.add(t.id);
    for (const p of summary?.trigger_problems ?? []) {
      const i = p.indexOf(":");
      const id = i > 0 ? p.slice(0, i).trim() : "";
      if (id && ids.has(id)) {
        const list = byId.get(id) ?? [];
        list.push(p.slice(i + 1).trim());
        byId.set(id, list);
      } else unmatched.push(p);
    }
    return { byId, unmatched };
  }

  #chip(t: Trigger, kind: "trigger" | "interrupt", index: number, problems: { byId: Map<string, string[]> }): HTMLElement {
    const s = this.#store;
    const sel = s.selection;
    const selected = !!sel && sel.kind === kind && sel.index === index;
    const findings = s.findingsUnder([kind === "trigger" ? "triggers" : "interrupts", index]);
    const errs = findings.filter((f) => f.level === "error");
    const runnerProblems = typeof t.id === "string" ? (problems.byId.get(t.id) ?? []) : [];
    const summary = triggerSummary(t);
    const label = typeof t.name === "string" && t.name !== "" ? t.name : summary;
    const policy = t.policy ?? (kind === "interrupt" ? "interrupt_and_resume" : "queue");
    const run = kind === "interrupt" && typeof t.run === "string" ? t.run : "";
    const lines = [
      `${summary}${run ? ` → ${run}` : ""}`,
      `${t.type} · policy: ${POLICY_TEXT[policy] ?? policy}${typeof t.priority === "number" ? ` · priority ${t.priority}` : ""}${t.enabled === false ? " · disabled" : ""}`,
      ...findings.map((f) => `${f.level === "error" ? "Error" : "Warning"}: ${f.message}`),
      ...runnerProblems.map((p) => `Robot: ${p}`),
    ];
    const dotCls = errs.length || runnerProblems.length ? "err" : findings.length ? "warn" : "";
    return h(
      "button",
      {
        class: `chip trigger${selected ? " selected" : ""}${t.enabled === false ? " disabled" : ""}`,
        title: lines.join("\n"),
        onclick: () => s.select({ kind, index }),
      },
      h("span", { class: "label", text: label }),
      run ? h("span", { class: "arrow", text: `→ ${run}` }) : null,
      dotCls ? h("span", { class: `prob-dot ${dotCls}` }) : null,
    );
  }
}
