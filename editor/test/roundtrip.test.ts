import { describe, expect, it } from "vitest";
import { validate } from "../src/model/validate";
import { assignIds, deepClone, walkSteps } from "../src/model/ids";
import type { Mission, SitesDoc } from "../src/model/types";

// Raw text so the round-trip compares against the file exactly as written.
const RAW = import.meta.glob<string>("../../examples/*.json", { eager: true, import: "default", query: "?raw" });
const files = Object.keys(RAW).filter((f) => !f.endsWith("/sites.json"));
const sites = JSON.parse(RAW["../../examples/sites.json"]!) as SitesDoc;
const names = files.map((f) => f.replace(/^.*\//, "").replace(/\.json$/, ""));

describe("examples", () => {
  it("has examples to test", () => {
    expect(files.length).toBeGreaterThan(0);
  });

  for (const path of files) {
    const file = path.replace(/^.*\//, "");
    const text = RAW[path]!;
    const doc = JSON.parse(text) as Mission;

    it(`${file} validates with zero errors`, () => {
      const r = validate(doc, { sites, missions: names, connectors: ["broker", "plc1"] });
      expect(r.errors).toEqual([]);
    });

    it(`${file} round-trips through JSON`, () => {
      expect(JSON.parse(JSON.stringify(doc))).toEqual(JSON.parse(text));
    });

    it(`${file} only gains generated ids on load`, () => {
      const loaded = deepClone(doc);
      const original = new Set<string>();
      for (const v of walkSteps(doc)) if (typeof v.step.id === "string") original.add(v.step.id);
      assignIds(loaded);
      for (const v of walkSteps(loaded)) {
        expect(typeof v.step.id).toBe("string");
        if (!original.has(v.step.id as string)) {
          expect(v.step.id).toMatch(/^s_[0-9a-z]{6}$/);
          delete v.step.id;
        }
      }
      for (const t of [...(loaded.triggers ?? []), ...(loaded.interrupts ?? [])]) if (/^[ti]_[0-9a-z]{6}$/.test(String(t.id))) delete t.id;
      expect(loaded).toEqual(doc);
    });
  }
});
