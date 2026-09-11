import { describe, expect, it } from "vitest";
import { generatePython, pyValue } from "../src/codegen/python";
import { fmtG4, renderBehaviorTree, btFileName } from "../src/codegen/bt";
import type { Mission, SitesDoc } from "../src/model/types";

// Vite's glob imports work under vitest and need no Node typings.
const EXAMPLES = import.meta.glob<Mission>("../../examples/*.json", { eager: true, import: "default" });
const FIXTURE_SPECS = import.meta.glob<Record<string, unknown>>("./fixtures/*.json", { eager: true, import: "default" });
const FIXTURE_XML = import.meta.glob<string>("./fixtures/*.xml", { eager: true, import: "default", query: "?raw" });

function example(name: string): Mission {
  const doc = EXAMPLES[`../../examples/${name}.json`];
  if (!doc) throw new Error(`missing example ${name}`);
  return JSON.parse(JSON.stringify(doc)) as Mission;
}
const sites = EXAMPLES["../../examples/sites.json"] as unknown as SitesDoc;

describe("python export", () => {
  it("go_to_point uses goToPose with a retry loop and clear costmap", () => {
    const py = generatePython(example("go_to_point"), { sites });
    expect(py).toContain("#!/usr/bin/env python3");
    expect(py).toContain("nav.waitUntilNav2Active()");
    expect(py).toContain("nav.goToPose(");
    expect(py).toContain("for attempt in range(2):");
    expect(py).toContain("nav.clearAllCostmaps()");
    expect(py).toContain('val("Goal reached in ${round(last.duration_s, 1)} s", ctx)');
    expect(py).not.toContain("# TODO");
  });

  it("patrol loops with range() over followWaypoints", () => {
    const py = generatePython(example("patrol"), { sites });
    expect(py).toContain('for _i in range(int(ctx["rounds"])):');
    expect(py).toContain("nav.followWaypoints(");
    expect(py).toContain("nav.goToPose(");
    expect(py).toContain('"A": (1, 1, 0)');
    expect(py).toContain('ctx = {"rounds": 1}');
  });

  it("follow_line builds a path and follows it", () => {
    const py = generatePython(example("follow_line"), { sites });
    expect(py).toContain("nav.followPath(make_path(nav, [[0, 0], [3, 0.3], [3, -3], [0, -3]], spacing=0.05, from_robot=True, ctx=ctx))");
    expect(py).toContain("nav.getPath(");
    expect(py).toContain("nav.smoothPath(");
    expect(py).toContain('nav.followPath(ref(ctx, "smoothed", "value"))');
  });

  it("pickup_job marks unsupported steps with TODO and emits if/break", () => {
    const py = generatePython(example("pickup_job"), { sites });
    expect(py).toContain('# TODO: step "wait_loaded" (wait_event)');
    expect(py).toContain("while True:");
    expect(py).toContain("if ev(\"answer.value == 'No'\", ctx):");
    expect(py).toContain("break");
    expect(py).toContain("input(");
  });

  it("mqtt_goal_server sets up paho from environment variables", () => {
    const py = generatePython(example("mqtt_goal_server"), { sites });
    expect(py).toContain("import paho.mqtt.client as mqtt");
    expect(py).toContain('os.environ.get("MQTT_HOST"');
    expect(py).toContain("mqtt_publish(");
    expect(py).toContain('ref(ctx, "goal", "x")');
  });

  it("resolves values like the runner", () => {
    expect(pyValue(12)).toBe("12");
    expect(pyValue("plain")).toBe('"plain"');
    expect(pyValue("$rounds")).toBe('ctx["rounds"]');
    expect(pyValue("$goal.x")).toBe('ref(ctx, "goal", "x")');
    expect(pyValue("${rounds + 1}")).toBe('ev("rounds + 1", ctx)');
    expect(pyValue("Round ${rounds}")).toBe('val("Round ${rounds}", ctx)');
    expect(pyValue({ a: "$x" })).toBe('val({"a": "$x"}, ctx)');
  });
});

describe("behavior tree export", () => {
  const fixtures = Object.keys(FIXTURE_SPECS);
  it("has fixtures", () => {
    expect(fixtures.length).toBeGreaterThan(0);
  });
  for (const f of fixtures) {
    const xmlPath = f.replace(/\.json$/, ".xml");
    it(`matches ${xmlPath.replace("./fixtures/", "")}`, () => {
      const spec = FIXTURE_SPECS[f]!;
      const expected = FIXTURE_XML[xmlPath];
      expect(expected, `fixture ${xmlPath}`).toBeDefined();
      expect(renderBehaviorTree(spec)).toBe(expected!.replace(/\r\n/g, "\n"));
    });
  }

  it("formats numbers like Python's :.4g", () => {
    expect(fmtG4(Math.PI / 2)).toBe("1.571");
    expect(fmtG4(Math.PI)).toBe("3.142");
    expect(fmtG4(5)).toBe("5");
    expect(fmtG4(0.3)).toBe("0.3");
    expect(fmtG4(2.5)).toBe("2.5");
    expect(fmtG4(0.5)).toBe("0.5");
    expect(fmtG4(12345)).toBe("1.234e+04"); // exact tie: half to even, like CPython
    expect(fmtG4(12355)).toBe("1.236e+04");
    expect(fmtG4(1.0625)).toBe("1.062");
    expect(fmtG4(0.00001)).toBe("1e-05");
  });

  it("rejects unknown templates and sanitizes file names", () => {
    expect(() => renderBehaviorTree({ template: "nope" })).toThrow(/unknown/);
    expect(btFileName("pickup job", "to.drop")).toBe("pickup_job__to_drop.xml");
  });
});
