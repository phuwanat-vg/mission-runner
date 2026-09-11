import { describe, expect, it } from "vitest";
import { validate } from "../src/model/validate";
import type { Path, SitesDoc } from "../src/model/types";
import { checkExpression, cronToWords } from "../src/model/expressions";

function mission(extra: Record<string, unknown>): Record<string, unknown> {
  return { schema: "mission/1", name: "t", flow: [], ...extra };
}

function errorPaths(doc: unknown, ctx = {}): Path[] {
  return validate(doc, ctx).errors.map((f) => f.path);
}

function warningMessages(doc: unknown, ctx = {}): string[] {
  return validate(doc, ctx).warnings.map((f) => f.message);
}

const sites: SitesDoc = { schema: "sites/1", default_map: "m", maps: { m: { sites: { Home: { x: 0, y: 0 } } }, other: { sites: { Far: { x: 1, y: 1 } } } } };

describe("validate: errors", () => {
  it("accepts a minimal mission", () => {
    const r = validate(mission({ flow: [{ id: "a", type: "nav.wait_active" }] }));
    expect(r.errors).toEqual([]);
  });

  it("rejects an invalid mission name", () => {
    expect(errorPaths(mission({ name: "Bad Name" }))).toContainEqual(["name"]);
    expect(errorPaths(mission({ name: "9x" }))).toContainEqual(["name"]);
  });

  it("rejects the wrong schema and unknown top-level fields", () => {
    expect(errorPaths({ schema: "mission/2", name: "t", flow: [] })).toContainEqual(["schema"]);
    expect(errorPaths(mission({ bogus: 1 }))).toContainEqual(["bogus"]);
  });

  it("flags unknown step types", () => {
    expect(errorPaths(mission({ flow: [{ id: "a", type: "nav.fly" }] }))).toContainEqual(["flow", 0, "type"]);
  });

  it("flags missing required parameters and wrong kinds", () => {
    expect(errorPaths(mission({ flow: [{ id: "a", type: "nav.go_to_pose" }] }))).toContainEqual(["flow", 0, "pose"]);
    expect(errorPaths(mission({ flow: [{ id: "a", type: "wait", seconds: "soon" }] }))).toContainEqual(["flow", 0, "seconds"]);
    expect(errorPaths(mission({ flow: [{ id: "a", type: "nav.spin", angle_deg: "$turn" }] }))).toEqual([]);
    expect(errorPaths(mission({ flow: [{ id: "a", type: "nav.clear_costmap", which: "everything" }] }))).toContainEqual(["flow", 0, "which"]);
    expect(errorPaths(mission({ flow: [{ id: "a", type: "nav.go_to_pose", pose: { x: 1 } }] }))).toContainEqual(["flow", 0, "pose"]);
  });

  it("flags duplicate step ids, including nested ones", () => {
    const doc = mission({ flow: [{ id: "a", type: "nav.wait_active" }, { id: "l", type: "loop", body: [{ id: "a", type: "log", text: "x" }] }] });
    expect(errorPaths(doc)).toContainEqual(["flow", 1, "body", 0, "id"]);
  });

  it("flags break outside a loop but not inside", () => {
    expect(errorPaths(mission({ flow: [{ id: "b", type: "break" }] }))).toContainEqual(["flow", 0]);
    expect(errorPaths(mission({ flow: [{ id: "l", type: "loop", body: [{ id: "i", type: "if", condition: "x", then: [{ id: "b", type: "break" }] }] }] }))).toEqual([]);
  });

  it("flags a loop with both count and while", () => {
    expect(errorPaths(mission({ flow: [{ id: "l", type: "loop", count: 2, while: "x", body: [] }] }))).toContainEqual(["flow", 0]);
  });

  it("flags if without then", () => {
    expect(errorPaths(mission({ flow: [{ id: "i", type: "if", condition: "x" }] }))).toContainEqual(["flow", 0, "then"]);
  });

  it("flags run_mission to an unknown mission only when the list is known", () => {
    const doc = mission({ flow: [{ id: "r", type: "run_mission", mission: "nope" }] });
    expect(errorPaths(doc, { missions: ["a", "b"] })).toContainEqual(["flow", 0, "mission"]);
    expect(errorPaths(doc)).toEqual([]);
    expect(errorPaths(doc, { missions: ["nope"] })).toEqual([]);
  });

  it("flags interrupts without run and unknown trigger types", () => {
    expect(errorPaths(mission({ interrupts: [{ id: "i", type: "gpio.input", pin: 1 }] }))).toContainEqual(["interrupts", 0, "run"]);
    expect(errorPaths(mission({ triggers: [{ id: "t", type: "timer.moon" }] }))).toContainEqual(["triggers", 0, "type"]);
    expect(errorPaths(mission({ triggers: [{ id: "t", type: "timer.cron" }] }))).toContainEqual(["triggers", 0, "cron"]);
  });

  it("checks on_fail and common fields", () => {
    const doc = mission({ flow: [{ id: "a", type: "nav.wait_active", timeout_s: -1, out: "last", on_fail: { retry: 200, then: "explode" } }] });
    const paths = errorPaths(doc);
    expect(paths).toContainEqual(["flow", 0, "timeout_s"]);
    expect(paths).toContainEqual(["flow", 0, "out"]);
    expect(paths).toContainEqual(["flow", 0, "on_fail", "retry"]);
    expect(paths).toContainEqual(["flow", 0, "on_fail", "then"]);
  });

  it("walks on_abort and before_retry", () => {
    const doc = mission({
      flow: [{ id: "a", type: "nav.go_to_pose", pose: "Home", on_fail: { retry: 1, before_retry: [{ id: "x", type: "bogus" }] } }],
      on_abort: [{ id: "y", type: "log" }],
    });
    const paths = errorPaths(doc);
    expect(paths).toContainEqual(["flow", 0, "on_fail", "before_retry", 0, "type"]);
    expect(paths).toContainEqual(["on_abort", 0, "text"]);
  });
});

describe("validate: warnings", () => {
  it("warns about empty flow and disabled steps", () => {
    expect(warningMessages(mission({}))).toContain("mission has no steps");
    expect(warningMessages(mission({ flow: [{ id: "a", type: "nav.wait_active", enabled: false }] }))).toContain("step is disabled");
  });

  it("warns about sites missing from the active map", () => {
    const doc = mission({ flow: [{ id: "a", type: "nav.go_to_pose", pose: "Nowhere" }, { id: "b", type: "nav.follow_waypoints", poses: ["Home", { site: "Far" }] }] });
    const w = validate(doc, { sites }).warnings;
    expect(w.map((f) => f.path)).toContainEqual(["flow", 0, "pose"]);
    expect(w.map((f) => f.path)).toContainEqual(["flow", 1, "poses", 1]);
    expect(w.find((f) => f.path[0] === "flow" && f.path[1] === 1)?.message).toMatch(/another map/);
    expect(validate(doc, {}).warnings.filter((f) => /site/.test(f.message))).toEqual([]);
  });

  it("warns about unknown connectors and unavailable capabilities", () => {
    const doc = mission({ flow: [{ id: "a", type: "mqtt.publish", connector: "nope", topic: "t" }, { id: "d", type: "nav.dock", dock_id: "x" }] });
    const w = validate(doc, { connectors: ["broker"], capabilities: { steps: { "nav.dock": { available: false, reason: "no docking server" } } } }).warnings;
    expect(w.map((f) => f.path)).toContainEqual(["flow", 0, "connector"]);
    expect(w.some((f) => f.message.includes("no docking server"))).toBe(true);
  });

  it("warns about ask_user without timeout only in unattended missions", () => {
    const step = { id: "q", type: "ask_user", text: "?" };
    expect(warningMessages(mission({ flow: [step] })).some((m) => /forever/.test(m))).toBe(false);
    expect(warningMessages(mission({ flow: [step], triggers: [{ id: "t", type: "timer.boot" }] })).some((m) => /forever/.test(m))).toBe(true);
  });

  it("warns about expressions that do not parse", () => {
    const doc = mission({ flow: [{ id: "i", type: "if", condition: "(a > 1", then: [] }, { id: "s", type: "set", var: "x", value: "${rounds + }" }] });
    const w = validate(doc).warnings;
    expect(w.map((f) => f.path)).toContainEqual(["flow", 0, "condition"]);
    expect(w.map((f) => f.path)).toContainEqual(["flow", 1, "value"]);
  });
});

describe("expressions", () => {
  it("light syntax check", () => {
    expect(checkExpression("battery < 0.2 and not charging")).toBeNull();
    expect(checkExpression("payload.data == 'start'")).toBeNull();
    expect(checkExpression("distance(robot, sites.Charger) > 5")).toBeNull();
    expect(checkExpression("'SUCCEEDED' if go.ok else 'FAILED'")).toBeNull();
    expect(checkExpression("$rounds + 1")).toBeNull();
    expect(checkExpression("")).toBe("empty expression");
    expect(checkExpression("(1 + 2")).toMatch(/unclosed/);
    expect(checkExpression("a == 'x")).toMatch(/unterminated/);
    expect(checkExpression("a ==")).toMatch(/ends with/);
  });

  it("renders cron in words", () => {
    expect(cronToWords("0 22 * * *")).toBe("22:00 daily");
    expect(cronToWords("30 6 * * 1")).toBe("06:30 every Mon");
    expect(cronToWords("0 8 * * 1-5")).toBe("08:00 weekdays");
    expect(cronToWords("*/5 * * * *")).toBe("*/5 * * * *");
  });
});
