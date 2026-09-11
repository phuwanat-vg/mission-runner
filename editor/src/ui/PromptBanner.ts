/**
 * ask_user banner: shown above the steps list while /api/status.prompt is
 * set. One button per option, the default marked, a countdown to expires_at.
 */

import type { Store } from "../state/Store";
import type { Prompt, RunnerClient } from "../api/RunnerClient";
import { h, replace } from "./dom";
import { icon } from "./icons";
import { toast } from "./dialogs";

export class PromptBanner {
  readonly el: HTMLElement;
  #store: Store;
  #client: RunnerClient;
  #timer: ReturnType<typeof setInterval> | null = null;
  #countdown: HTMLElement | null = null;
  #promptId: string | null = null;
  #busy = false;

  constructor(store: Store, client: RunnerClient) {
    this.#store = store;
    this.#client = client;
    this.el = h("div", { class: "prompt-banner", hidden: true });
    store.on("remote", () => this.render());
    store.on("connection", () => this.render());
    this.render();
  }

  render(): void {
    const prompt = this.#store.connected ? (this.#store.status?.prompt ?? null) : null;
    if (!prompt) {
      this.#promptId = null;
      this.el.hidden = true;
      this.#stopTimer();
      return;
    }
    if (prompt.id === this.#promptId && !this.el.hidden) return;
    this.#promptId = prompt.id;
    this.#busy = false;
    this.#countdown = h("span", { class: "countdown mono" });
    const buttons = prompt.options.map((opt) =>
      h(
        "button",
        {
          class: opt === prompt.default ? "primary" : "",
          title: opt === prompt.default ? "Default (chosen automatically on timeout)" : "",
          onclick: () => this.#answer(prompt, opt),
        },
        opt,
        opt === prompt.default ? h("span", { class: "muted", text: " (default)" }) : null,
      ),
    );
    replace(
      this.el,
      icon("message", 16),
      h("div", { class: "grow" }, h("div", { class: "prompt-text", text: prompt.text }), h("div", { class: "muted small", text: `${prompt.mission ?? ""}${prompt.mission ? " · " : ""}waiting for an answer` })),
      this.#countdown,
      h("div", { class: "prompt-buttons" }, ...buttons),
    );
    this.el.hidden = false;
    this.#stopTimer();
    this.#tick(prompt);
    this.#timer = setInterval(() => this.#tick(prompt), 1000);
  }

  #tick(prompt: Prompt): void {
    if (!this.#countdown) return;
    if (!prompt.expires_at) {
      this.#countdown.textContent = "";
      return;
    }
    const left = Math.max(0, Math.round((new Date(prompt.expires_at).getTime() - Date.now()) / 1000));
    this.#countdown.textContent = `${left} s`;
    this.#countdown.classList.toggle("warn", left <= 10);
  }

  async #answer(prompt: Prompt, answer: string): Promise<void> {
    if (this.#busy) return;
    this.#busy = true;
    try {
      await this.#client.answerPrompt(prompt.id, answer);
      this.el.hidden = true;
      this.#stopTimer();
    } catch (err) {
      this.#busy = false;
      toast(`Answer failed: ${err instanceof Error ? err.message : String(err)}`, "error");
    }
  }

  #stopTimer(): void {
    if (this.#timer) clearInterval(this.#timer);
    this.#timer = null;
  }

  dispose(): void {
    this.#stopTimer();
  }
}
