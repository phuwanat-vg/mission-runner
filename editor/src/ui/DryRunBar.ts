/**
 * The transport bar under the map while a dry run is loaded: play/pause, a
 * scrubber over the recorded timeline, 1x/4x/16x and a close button, plus a
 * collapsible panel with what the preview assumed (`notes`), what it would
 * have written (`outputs`) and the totals.
 *
 * The playhead is owned by the store so the map (ghost robot) and the steps
 * list (per-step status) follow it; this class only runs the clock. Nothing
 * here talks to the robot: a dry run never moves it.
 */

import type { Store } from "../state/Store";
import type { AppActions } from "./actions";
import type { DryRunResult } from "../map/preview";
import { formatDistance } from "../map/preview";
import { h, formatDuration, replace } from "./dom";
import { icon } from "./icons";

const SPEEDS = [1, 4, 16] as const;
const PANEL_KEY = "mission-editor.dryrun.details";

export class DryRunBar {
  readonly el: HTMLElement;
  #store: Store;
  #actions: AppActions;
  #bar: HTMLElement;
  #panel: HTMLElement;
  #scrub: HTMLInputElement;
  #time: HTMLElement;
  #playBtn: HTMLButtonElement;
  #speedBtns: HTMLElement;
  #shown: DryRunResult | null = null;
  #open = false;
  #raf = 0;
  #last = 0;
  #unsub: (() => void)[] = [];

  constructor(store: Store, actions: AppActions) {
    this.#store = store;
    this.#actions = actions;
    try {
      this.#open = localStorage.getItem(PANEL_KEY) === "1";
    } catch {
      this.#open = false;
    }
    this.#scrub = h("input", { type: "range", class: "dry-scrub", min: "0", max: "1000", value: "0", step: "1", title: "Scrub the recorded timeline" });
    this.#scrub.addEventListener("input", () => {
      const d = this.#store.dryRun;
      if (!d) return;
      this.#store.updateDryRun({ playing: false, t: (Number(this.#scrub.value) / 1000) * d.result.durationS });
    });
    this.#time = h("span", { class: "dry-time mono muted" });
    this.#playBtn = h("button", { class: "icon-only", title: "Play / pause", onclick: () => this.toggle() }, icon("play", 13));
    this.#speedBtns = h("div", { class: "seg dry-speed" });
    this.#panel = h("div", { class: "dry-panel", hidden: true });
    this.#bar = h("div", { class: "dry-bar" });
    this.el = h("div", { class: "dry-transport", hidden: true }, this.#panel, this.#bar);
    this.#unsub.push(store.on("preview", () => this.render()));
    this.render();
  }

  dispose(): void {
    for (const u of this.#unsub) u();
    this.#unsub = [];
    this.#stopClock();
  }

  toggle(): void {
    const d = this.#store.dryRun;
    if (!d) return;
    if (!d.playing && d.t >= d.result.durationS) this.#store.updateDryRun({ t: 0, playing: true });
    else this.#store.updateDryRun({ playing: !d.playing });
  }

  // ---- clock ---------------------------------------------------------------------------

  #startClock(): void {
    if (this.#raf) return;
    this.#last = performance.now();
    const tick = (now: number): void => {
      this.#raf = 0;
      const d = this.#store.dryRun;
      if (!d || !d.playing) return;
      const dt = Math.min(0.25, (now - this.#last) / 1000);
      this.#last = now;
      const next = d.t + dt * d.speed;
      if (next >= d.result.durationS) this.#store.updateDryRun({ t: d.result.durationS, playing: false });
      else this.#store.updateDryRun({ t: next });
      if (this.#store.dryRun?.playing) this.#raf = requestAnimationFrame(tick);
    };
    this.#raf = requestAnimationFrame(tick);
  }

  #stopClock(): void {
    if (this.#raf) cancelAnimationFrame(this.#raf);
    this.#raf = 0;
  }

  // ---- rendering -----------------------------------------------------------------------

  render(): void {
    const d = this.#store.dryRun;
    if (!d) {
      this.#stopClock();
      this.#shown = null;
      this.el.hidden = true;
      return;
    }
    this.el.hidden = false;
    if (d.result !== this.#shown) {
      this.#shown = d.result;
      this.#renderBar(d.result);
      this.#renderPanel(d.result);
    }
    const frac = d.result.durationS > 0 ? d.t / d.result.durationS : 0;
    if (document.activeElement !== this.#scrub) this.#scrub.value = String(Math.round(frac * 1000));
    this.#time.textContent = `${formatDuration(d.t)} / ${formatDuration(d.result.durationS)}`;
    replace(this.#playBtn, icon(d.playing ? "pause" : "play", 13));
    this.#playBtn.classList.toggle("primary", !d.playing);
    for (const btn of this.#speedBtns.children) btn.classList.toggle("active", (btn as HTMLElement).dataset.speed === String(d.speed));
    if (d.playing) this.#startClock();
    else this.#stopClock();
  }

  #renderBar(result: DryRunResult): void {
    replace(this.#speedBtns);
    for (const sp of SPEEDS) {
      const btn = h("button", { class: "small", title: `Play at ${sp}x mission time`, onclick: () => this.#store.updateDryRun({ speed: sp }) }, `${sp}x`);
      btn.dataset.speed = String(sp);
      this.#speedBtns.append(btn);
    }
    const bad = !result.ok;
    const summary = `${formatDistance(result.distanceM)} · ${formatDuration(result.durationS)} · ${result.status}`;
    replace(
      this.#bar,
      h("span", { class: "dry-tag", title: "A preview against a simulated robot: the real robot never moved." }, icon("ghost", 12), "Dry run"),
      this.#playBtn,
      this.#scrub,
      this.#time,
      this.#speedBtns,
      h("span", { class: `dry-sum${bad ? " bad" : ""}`, text: summary, title: result.error || summary }),
      h(
        "button",
        { class: "small", title: "What the preview assumed, and what it would have published", onclick: () => this.#setOpen(!this.#open) },
        icon(this.#open ? "chevronDown" : "chevronUp", 11),
        `Details${result.notes.length + result.outputs.length ? ` (${result.notes.length + result.outputs.length})` : ""}`,
      ),
      h("button", { class: "small", title: "Run the preview again", onclick: () => this.#actions.previewDryRun() }, icon("refresh", 11)),
      h("button", { class: "icon-only ghost", title: "Close the dry run", onclick: () => this.#store.setDryRun(null) }, icon("close", 12)),
    );
  }

  #setOpen(open: boolean): void {
    this.#open = open;
    try {
      localStorage.setItem(PANEL_KEY, open ? "1" : "0");
    } catch {
      // ignore
    }
    this.#panel.hidden = !open;
    if (this.#shown) this.#renderBar(this.#shown);
  }

  #renderPanel(result: DryRunResult): void {
    this.#panel.hidden = !this.#open;
    const cols: HTMLElement[] = [];
    cols.push(
      h(
        "div",
        { class: "dry-col" },
        h("div", { class: "dry-col-head", text: "Result" }),
        h("div", { class: `dry-line${result.ok ? "" : " bad"}`, text: `${result.status}${result.error ? `: ${result.error}` : ""}` }),
        h("div", { class: "dry-line muted", text: `${formatDistance(result.distanceM)} driven · ${formatDuration(result.durationS)} of mission time · ${result.steps.length} step results` }),
        result.truncated ? h("div", { class: "dry-line warn", text: "The preview was cut short: it hit the wall-clock limit, so the timeline stops before the end of the mission." }) : null,
      ),
    );
    cols.push(
      h(
        "div",
        { class: "dry-col" },
        h("div", { class: "dry-col-head", text: `What it assumed (${result.notes.length})` }),
        ...(result.notes.length ? result.notes.map((n) => h("div", { class: "dry-line", text: n })) : [h("div", { class: "dry-line muted", text: "Nothing was assumed: no prompts, waits or events on this path." })]),
      ),
    );
    cols.push(
      h(
        "div",
        { class: "dry-col" },
        h("div", { class: "dry-col-head", text: `Would have written (${result.outputs.length})` }),
        ...(result.outputs.length
          ? result.outputs.map((o) => h("div", { class: "dry-line" }, h("b", { text: o.kind }), h("span", { class: "muted mono small", text: o.detail ? ` ${o.detail}` : "" })))
          : [h("div", { class: "dry-line muted", text: "Nothing: this run publishes and writes nothing." })]),
      ),
    );
    replace(this.#panel, ...cols);
  }
}
