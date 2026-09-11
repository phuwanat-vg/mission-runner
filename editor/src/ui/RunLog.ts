/**
 * Run log drawer: history from GET /api/runs, live events from the WebSocket,
 * per-run detail (step events), filter by mission, "Clear view". Collapsed
 * by default to a 28px bar with the last run's result; expands when a run
 * starts. The open/closed state is remembered in localStorage.
 */

import type { Store } from "../state/Store";
import type { Run, RunEvent, RunnerClient, WsEvent } from "../api/RunnerClient";
import { h, formatDateTime, formatDuration, replace } from "./dom";
import { icon } from "./icons";

interface LiveLine {
  t: string;
  text: string;
  level: "info" | "ok" | "warn" | "error";
  runId?: string;
  mission?: string;
}

const MAX_LIVE = 300;

export class RunLog {
  readonly el: HTMLElement;
  #store: Store;
  #client: RunnerClient;
  #runs: Run[] = [];
  #live: LiveLine[] = [];
  #filter = "";
  #open = false;
  #detail: { run: Run; events: RunEvent[] } | null = null;
  #body: HTMLElement;
  #filterSel: HTMLSelectElement;
  #toggleBtn: HTMLButtonElement;
  #lastEl: HTMLElement;
  #tools: HTMLElement;
  #liveEl: HTMLElement;
  #runsEl: HTMLElement;
  #detailEl: HTMLElement;

  constructor(store: Store, client: RunnerClient) {
    this.#store = store;
    this.#client = client;
    this.#filterSel = h("select", { class: "small", title: "Filter by mission" });
    this.#filterSel.addEventListener("change", () => {
      this.#filter = this.#filterSel.value;
      this.render();
    });
    this.#toggleBtn = h("button", { class: "icon-only ghost", title: "Expand", onclick: () => this.toggle() }, icon("chevronUp", 12));
    this.#liveEl = h("div", { class: "log-live" });
    this.#runsEl = h("div", { class: "log-runs" });
    this.#detailEl = h("div", { class: "log-detail", hidden: true });
    this.#body = h("div", { class: "drawer-body" }, this.#runsEl, this.#liveEl, this.#detailEl);
    this.#lastEl = h("span", { class: "last-run" });
    this.#tools = h(
      "span",
      { class: "drawer-tools" },
      this.#filterSel,
      h("button", { class: "icon-only ghost", title: "Refresh history", onclick: () => void this.refresh() }, icon("refresh", 12)),
      h("button", { class: "ghost small", title: "Clear the live event view", onclick: () => this.clearView() }, "Clear view"),
    );
    const head = h("div", { class: "drawer-head" }, this.#toggleBtn, h("span", { class: "title", text: "Run log" }), this.#lastEl, h("span", { class: "grow" }), this.#tools);
    head.addEventListener("click", (e) => {
      if ((e.target as HTMLElement).closest("button, select")) return;
      this.toggle();
    });
    this.el = h("div", { class: "drawer" }, head, this.#body);
    try {
      this.#open = localStorage.getItem("mission-editor.runlog.open") === "1";
    } catch {
      this.#open = false;
    }
    this.#applyOpen();
    store.on("remote", () => this.#renderFilter());
    store.on("connection", () => {
      if (store.connected) void this.refresh();
    });
    this.render();
  }

  toggle(): void {
    this.setOpen(!this.#open);
  }

  /** Expand (a run started) or collapse the drawer; remembered in localStorage. */
  setOpen(open: boolean): void {
    if (open === this.#open) return;
    this.#open = open;
    this.#applyOpen();
    try {
      localStorage.setItem("mission-editor.runlog.open", open ? "1" : "0");
    } catch {
      // ignore
    }
  }

  #applyOpen(): void {
    this.el.classList.toggle("collapsed", !this.#open);
    replace(this.#toggleBtn, icon(this.#open ? "chevronDown" : "chevronUp", 12));
    this.#toggleBtn.title = this.#open ? "Collapse" : "Expand";
    this.#tools.hidden = !this.#open;
  }

  async refresh(): Promise<void> {
    if (!this.#store.connected) return;
    try {
      this.#runs = await this.#client.listRuns(50);
      this.render();
    } catch {
      // stays as it was; the connection chip already says offline
    }
  }

  clearView(): void {
    this.#live = [];
    this.#detail = null;
    this.render();
  }

  /** Feed a WebSocket event. */
  onEvent(ev: WsEvent): void {
    const t = typeof ev.t === "string" ? ev.t : new Date().toISOString();
    const push = (text: string, level: LiveLine["level"], runId?: string, mission?: string): void => {
      const line: LiveLine = { t, text, level };
      if (runId) line.runId = runId;
      if (mission) line.mission = mission;
      this.#live.push(line);
      if (this.#live.length > MAX_LIVE) this.#live.splice(0, this.#live.length - MAX_LIVE);
    };
    switch (ev.type) {
      case "run.queued":
        if (ev.run) push(`${ev.run.mission} queued (${sourceText(ev.run)})`, "info", ev.run.id, ev.run.mission);
        break;
      case "run.started":
        if (ev.run) {
          push(`${ev.run.mission} started (${sourceText(ev.run)})`, "info", ev.run.id, ev.run.mission);
          this.#upsertRun(ev.run);
          this.setOpen(true);
        }
        break;
      case "run.suspended":
        if (ev.run) push(`${ev.run.mission} suspended`, "warn", ev.run.id, ev.run.mission);
        break;
      case "run.resumed":
        if (ev.run) push(`${ev.run.mission} resumed`, "info", ev.run.id, ev.run.mission);
        break;
      case "run.finished":
        if (ev.run) {
          const ok = ev.run.status === "succeeded";
          push(`${ev.run.mission} ${ev.run.status}${ev.run.error ? `: ${ev.run.error}` : ""}`, ok ? "ok" : ev.run.status === "canceled" ? "warn" : "error", ev.run.id, ev.run.mission);
          this.#upsertRun(ev.run);
        }
        break;
      case "step.started":
        push(`▶ ${ev.name || ev.step_id || "?"}${typeof ev.step_type === "string" ? ` (${ev.step_type})` : ""}`, "info", ev.run_id, this.#missionOf(ev.run_id));
        break;
      case "step.finished": {
        const r = ev.result;
        const ok = r?.ok !== false && r?.status !== "failed";
        push(`${ok ? "✓" : "✗"} ${ev.step_id ?? "?"}${r?.duration_s !== undefined ? ` · ${formatDuration(r.duration_s)}` : ""}${r?.error ? ` · ${r.error}` : ""}`, ok ? "ok" : "error", ev.run_id, this.#missionOf(ev.run_id));
        break;
      }
      case "log":
        push(`${ev.text ?? ""}`, ev.level === "error" ? "error" : ev.level === "warn" ? "warn" : "info", ev.run_id, this.#missionOf(ev.run_id));
        break;
      case "prompt":
        if (ev.prompt) push(`? ${ev.prompt.text}`, "warn", ev.prompt.run_id, ev.prompt.mission);
        break;
      case "prompt.answered":
        push(`answer: ${ev.answer ?? ""}`, "info", ev.prompt?.run_id, ev.prompt?.mission);
        break;
      default:
        return;
    }
    this.#renderLive();
  }

  #missionOf(runId: unknown): string | undefined {
    if (typeof runId !== "string") return undefined;
    const run = this.#store.status?.run;
    if (run?.id === runId) return run.mission;
    return this.#runs.find((r) => r.id === runId)?.mission;
  }

  #upsertRun(run: Run): void {
    const i = this.#runs.findIndex((r) => r.id === run.id);
    if (i >= 0) this.#runs[i] = run;
    else this.#runs.unshift(run);
    if (this.#runs.length > 50) this.#runs.length = 50;
    this.#renderRuns();
    this.#renderLast();
  }

  render(): void {
    this.#renderFilter();
    this.#renderRuns();
    this.#renderLive();
    this.#renderDetail();
    this.#renderLast();
  }

  /** Runs newest first (the API order and live upserts may differ). */
  #sorted(): Run[] {
    return [...this.#runs].sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""));
  }

  /** The collapsed bar's summary: "✓ patrol · 13 s" for the most recent run. */
  #renderLast(): void {
    const r = this.#sorted()[0];
    if (!r) {
      replace(this.#lastEl);
      return;
    }
    const { glyph, cls } = glyphOf(r);
    const dur = durationOf(r);
    const live = r.status === "running" || r.status === "queued" || r.status === "paused";
    const text = live ? `${r.mission} · ${r.status}` : `${r.mission}${dur ? ` · ${dur}` : ""}${r.status === "failed" ? " · failed" : r.status === "canceled" ? " · canceled" : ""}`;
    replace(this.#lastEl, h("span", { class: `g ${cls}`, text: glyph }), h("span", { class: "muted", text }));
    this.#lastEl.title = `${formatDateTime(r.started_at)} · ${sourceText(r)}${r.error ? `\n${r.error}` : ""}`;
  }

  #renderFilter(): void {
    const names = new Set<string>();
    for (const m of this.#store.missions) names.add(m.name);
    for (const r of this.#runs) names.add(r.mission);
    const cur = this.#filter;
    this.#filterSel.replaceChildren(h("option", { value: "", text: "All missions" }));
    for (const n of [...names].sort()) this.#filterSel.append(h("option", { value: n, text: n }));
    this.#filterSel.value = names.has(cur) ? cur : "";
    if (!names.has(cur)) this.#filter = "";
  }

  #renderRuns(): void {
    const runs = this.#sorted().filter((r) => !this.#filter || r.mission === this.#filter);
    const rows = runs.map((r) => {
      const { glyph, cls } = glyphOf(r);
      const dur = durationOf(r);
      const fail = r.status === "failed" && r.step ? ` · failed at ${r.step.name || r.step.id}` : "";
      const active = this.#detail?.run.id === r.id;
      return h(
        "button",
        { class: `log-run ${cls}${active ? " active" : ""}`, title: `${r.id}\n${r.error ?? ""}`, onclick: () => void this.#showDetail(r) },
        h("span", { class: "g", text: glyph }),
        h("span", { class: "mono", text: formatDateTime(r.started_at) }),
        h("span", { class: "name", text: r.mission }),
        h("span", { class: "muted", text: sourceText(r) }),
        h("span", { class: "mono muted", text: dur }),
        h("span", { class: "muted", text: `${r.status}${fail}` }),
      );
    });
    replace(this.#runsEl, h("div", { class: "log-head", text: "History" }), ...(rows.length ? rows : [h("div", { class: "empty", text: this.#store.connected ? "No runs yet." : "Offline: history unavailable." })]));
  }

  #renderLive(): void {
    const lines = this.#live.filter((l) => !this.#filter || l.mission === this.#filter);
    const atBottom = this.#liveEl.scrollHeight - this.#liveEl.scrollTop - this.#liveEl.clientHeight < 30;
    replace(
      this.#liveEl,
      h("div", { class: "log-head", text: "Live events" }),
      ...(lines.length ? lines.map((l) => h("div", { class: `log-line ${l.level}` }, h("span", { class: "mono t", text: formatDateTime(l.t) }), h("span", { class: "text", text: l.text }))) : [h("div", { class: "empty", text: "Events appear here while a mission runs." })]),
    );
    if (atBottom) this.#liveEl.scrollTop = this.#liveEl.scrollHeight;
  }

  async #showDetail(run: Run): Promise<void> {
    if (this.#detail?.run.id === run.id) {
      this.#detail = null;
      this.render();
      return;
    }
    let events: RunEvent[] = [];
    try {
      const d = await this.#client.getRun(run.id);
      events = d.events ?? [];
      run = { ...run, ...d };
    } catch {
      events = [];
    }
    this.#detail = { run, events };
    this.render();
  }

  #renderDetail(): void {
    const d = this.#detail;
    if (!d) {
      this.#detailEl.hidden = true;
      return;
    }
    this.#detailEl.hidden = false;
    const lines = d.events.map((e) => {
      const data = e.data;
      let text = e.type;
      if (e.type === "step.started") text = `▶ ${e.step_id ?? ""}`;
      else if (e.type === "step.finished") {
        const r = (data ?? {}) as { result?: { ok?: boolean; status?: string; duration_s?: number; error?: string } };
        const res = r.result ?? (data as { ok?: boolean; status?: string; duration_s?: number; error?: string } | undefined);
        text = `${res?.ok === false || res?.status === "failed" ? "✗" : "✓"} ${e.step_id ?? ""}${res?.duration_s !== undefined ? ` · ${formatDuration(res.duration_s)}` : ""}${res?.error ? ` · ${res.error}` : ""}`;
      } else if (e.type === "log") {
        const l = (data ?? {}) as { text?: string; level?: string };
        text = `[${l.level ?? "info"}] ${l.text ?? ""}`;
      } else if (data !== undefined) text = `${e.type} ${JSON.stringify(data).slice(0, 160)}`;
      return h("div", { class: "log-line" }, h("span", { class: "mono t", text: formatDateTime(e.t) }), h("span", { class: "text", text }));
    });
    replace(
      this.#detailEl,
      h("div", { class: "log-head" }, h("span", { class: "grow", text: `${d.run.mission} · ${formatDateTime(d.run.started_at)} · ${d.run.status}${d.run.error ? ` · ${d.run.error}` : ""}` }), h("button", { class: "icon-only ghost", title: "Close", onclick: () => {
        this.#detail = null;
        this.render();
      } }, icon("close", 12))),
      h("div", { class: "mono muted small", text: `run ${d.run.id}${d.run.inputs && Object.keys(d.run.inputs).length ? ` · inputs ${JSON.stringify(d.run.inputs)}` : ""}` }),
      ...(lines.length ? lines : [h("div", { class: "empty", text: "No step events recorded." })]),
    );
  }
}

function glyphOf(r: Run): { glyph: string; cls: string } {
  const glyph = r.status === "succeeded" ? "✓" : r.status === "running" || r.status === "queued" || r.status === "paused" ? "▶" : r.status === "canceled" ? "○" : "✗";
  const cls = r.status === "succeeded" ? "ok" : r.status === "failed" ? "error" : r.status === "canceled" ? "warn" : "info";
  return { glyph, cls };
}

function sourceText(r: Run): string {
  const s = r.source;
  if (!s) return "manual";
  if (s.detail) return s.detail;
  return s.kind === "trigger" && s.id ? `trigger ${s.id}` : s.kind;
}

function durationOf(r: Run): string {
  if (!r.started_at) return "";
  const end = r.finished_at ? new Date(r.finished_at).getTime() : Date.now();
  const start = new Date(r.started_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return "";
  return formatDuration((end - start) / 1000);
}
