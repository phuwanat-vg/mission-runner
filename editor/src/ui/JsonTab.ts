/**
 * JSON tab: read-only pretty JSON with a copy button, and an edit mode whose
 * "Apply" parses + validates; invalid JSON never replaces the model.
 */

import type { Store } from "../state/Store";
import type { Mission } from "../model/types";
import { validate, pathToString } from "../model/validate";
import { h, replace, copyText } from "./dom";
import { icon } from "./icons";
import { toast } from "./dialogs";

export class JsonTab {
  readonly el: HTMLElement;
  #store: Store;
  #editing = false;
  #textarea: HTMLTextAreaElement | null = null;
  #errors: HTMLElement;
  #body: HTMLElement;

  constructor(store: Store) {
    this.#store = store;
    this.#errors = h("div", { class: "json-errors" });
    this.#body = h("div", { class: "json-body" });
    this.el = h("div", { class: "json-tab" }, this.#body);
    store.on("mission", () => {
      if (!this.#editing) this.render();
    });
    this.render();
  }

  render(): void {
    const m = this.#store.mission;
    if (!m) {
      replace(this.#body, h("div", { class: "empty", text: "No mission open." }));
      return;
    }
    const text = JSON.stringify(m, null, 2);
    if (this.#editing) {
      this.#textarea = h("textarea", { class: "mono json-edit", spellcheck: false });
      this.#textarea.value = text;
      this.#textarea.addEventListener("keydown", (e) => {
        if (e.key === "Tab") {
          e.preventDefault();
          const ta = this.#textarea!;
          const start = ta.selectionStart;
          ta.setRangeText("  ", start, ta.selectionEnd, "end");
        }
      });
      replace(
        this.#body,
        h(
          "div",
          { class: "json-toolbar" },
          h("span", { class: "muted grow", text: "Editing. Apply parses and validates; invalid JSON is never applied." }),
          h("button", { onclick: () => this.#cancel() }, "Cancel"),
          h("button", { class: "primary", onclick: () => this.#apply() }, icon("check"), "Apply"),
        ),
        this.#errors,
        this.#textarea,
      );
      this.#errors.replaceChildren();
    } else {
      replace(
        this.#body,
        h(
          "div",
          { class: "json-toolbar" },
          h("span", { class: "muted grow", text: `${text.length.toLocaleString()} characters` }),
          h("button", { onclick: () => void copyText(text).then((ok) => toast(ok ? "JSON copied" : "Copy failed", ok ? "ok" : "error")) }, icon("copy"), "Copy"),
          h("button", { onclick: () => this.#edit() }, icon("edit"), "Edit"),
        ),
        h("pre", { class: "json-view mono", text }),
      );
    }
  }

  #edit(): void {
    this.#editing = true;
    this.render();
    this.#textarea?.focus();
  }

  #cancel(): void {
    this.#editing = false;
    this.render();
  }

  #apply(): void {
    const ta = this.#textarea;
    if (!ta) return;
    let doc: unknown;
    try {
      doc = JSON.parse(ta.value);
    } catch (err) {
      this.#showErrors([`Invalid JSON: ${err instanceof Error ? err.message : String(err)}`]);
      return;
    }
    const r = validate(doc, this.#store.validateContext());
    if (r.errors.length) {
      this.#showErrors(r.errors.map((f) => `${pathToString(f.path)}: ${f.message}`));
      return;
    }
    this.#store.replaceMission(doc as Mission);
    this.#editing = false;
    this.render();
    toast(r.warnings.length ? `Applied with ${r.warnings.length} warning${r.warnings.length === 1 ? "" : "s"}` : "Applied", "ok");
  }

  #showErrors(lines: string[]): void {
    replace(this.#errors, h("div", { class: "finding-head", text: `${lines.length} problem${lines.length === 1 ? "" : "s"} — nothing was applied` }), h("ul", {}, ...lines.map((l) => h("li", { class: "error", text: l }))));
  }
}
