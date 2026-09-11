/**
 * Tiny DOM helpers in the style of iViz: `h(tag, props, ...children)` plus a
 * few composites used by every panel.
 */

export type Props = Record<string, unknown>;
export type Child = Node | string | number | null | undefined | false;

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, props: Props = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = String(v);
    else if (k === "text") el.textContent = String(v);
    else if (k === "style") el.setAttribute("style", String(v));
    else if (k === "dataset") Object.assign(el.dataset, v as Record<string, string>);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v as EventListener);
    else if (k in el) (el as unknown as Record<string, unknown>)[k] = v;
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** `h` for SVG: every property becomes an attribute (SVG has no DOM setters worth using). */
export function svg<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number | boolean | null | undefined> = {}, ...children: Child[]): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "text") el.textContent = String(v);
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

export function append(el: Node, children: Child[]): void {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
}

export function clear(el: HTMLElement): void {
  el.replaceChildren();
}

export function replace(el: HTMLElement, ...children: Child[]): void {
  el.replaceChildren();
  append(el, children);
}

/** Collapsible section with a quiet sentence-case title. */
export function section(title: string, body: Child[], opts: { collapsed?: boolean; head?: Child[]; id?: string; icon?: Node } = {}): HTMLElement {
  const caret = h("span", { class: "caret" }, chevron());
  const head = h("div", { class: "section-title" }, caret, opts.icon ?? null, h("span", { class: "grow", text: title }), ...(opts.head ?? []));
  const sec = h("div", { class: `section${opts.collapsed ? " collapsed" : ""}`, id: opts.id }, head, h("div", { class: "section-body" }, ...body));
  head.addEventListener("click", (e) => {
    if ((e.target as HTMLElement).closest("button, input, select")) return;
    sec.classList.toggle("collapsed");
  });
  return sec;
}

function chevron(): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "12");
  svg.setAttribute("height", "12");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.classList.add("icon");
  svg.innerHTML = '<path d="m6 9 6 6 6-6"/>';
  return svg;
}

/** Labelled form row: label on the left, control on the right, optional help below. */
export function field(label: string, control: Child, opts: { help?: string; error?: string; mono?: boolean; wide?: boolean } = {}): HTMLElement {
  const row = h("div", { class: `field${opts.wide ? " wide" : ""}${opts.error ? " has-error" : ""}` });
  row.append(h("label", { text: label, title: opts.help ?? "" }));
  const ctl = h("div", { class: `control${opts.mono ? " mono" : ""}` });
  append(ctl, [control]);
  row.append(ctl);
  if (opts.error) row.append(h("div", { class: "field-error", text: opts.error }));
  else if (opts.help) row.append(h("div", { class: "field-help", text: opts.help }));
  return row;
}

export function select(options: readonly { value: string; label?: string; disabled?: boolean }[], value: string, props: Props = {}): HTMLSelectElement {
  const sel = h("select", props);
  for (const o of options) {
    const opt = h("option", { value: o.value, text: o.label ?? o.value });
    if (o.disabled) opt.disabled = true;
    sel.append(opt);
  }
  sel.value = value;
  if (sel.value !== value && value !== "") {
    // value not among the options: add it so the control shows the real state
    const opt = h("option", { value, text: value });
    sel.append(opt);
    sel.value = value;
  }
  return sel;
}

export function stopEvent(e: Event): void {
  e.preventDefault();
  e.stopPropagation();
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "";
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds - m * 60);
  if (m < 60) return `${m}m ${s.toString().padStart(2, "0")}s`;
  const hh = Math.floor(m / 60);
  return `${hh}h ${(m - hh * 60).toString().padStart(2, "0")}m`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? t : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${t}`;
}

export function downloadText(fileName: string, text: string, mime = "application/json"): void {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: fileName });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  const ta = h("textarea", { value: text, style: "position:fixed;left:-1000px;top:-1000px" });
  document.body.append(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  ta.remove();
  return ok;
}

/** Read a local file picked by the user as text. */
export function pickFile(accept: string): Promise<{ name: string; text: string } | null> {
  return new Promise((resolve) => {
    const input = h("input", { type: "file", accept, style: "display:none" });
    input.addEventListener("change", () => {
      const f = input.files?.[0];
      if (!f) return resolve(null);
      f.text().then((text) => resolve({ name: f.name, text })).catch(() => resolve(null));
    });
    document.body.append(input);
    input.click();
    // Removing immediately breaks Safari; defer.
    setTimeout(() => input.remove(), 60_000);
  });
}
