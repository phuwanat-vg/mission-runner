/**
 * Built-in "new mission" templates. They are the bundled examples
 * (../examples/*.json) plus a blank mission, so the gallery works offline and
 * mirrors what the runner serves at GET /api/examples.
 */

import type { Mission } from "./types";
import { MISSION_SCHEMA_ID } from "./types";
import goToPoint from "../../../examples/go_to_point.json";
import patrol from "../../../examples/patrol.json";
import followLine from "../../../examples/follow_line.json";
import pickupJob from "../../../examples/pickup_job.json";
import goCharge from "../../../examples/go_charge.json";
import mqttGoalServer from "../../../examples/mqtt_goal_server.json";
import emergencyStop from "../../../examples/emergency_stop.json";
import globalMission from "../../../examples/global.json";

export interface MissionTemplate {
  id: string;
  title: string;
  description: string;
  mission: Mission;
  source: "builtin" | "runner";
}

function tpl(id: string, doc: unknown): MissionTemplate {
  const m = doc as Mission;
  return { id, title: m.title ?? m.name, description: m.description ?? "", mission: m, source: "builtin" };
}

export const BLANK_TEMPLATE: MissionTemplate = {
  id: "blank",
  title: "Blank mission",
  description: "Wait for Nav2, then add your own steps.",
  mission: { schema: MISSION_SCHEMA_ID, name: "new_mission", title: "New mission", flow: [{ id: "wait", type: "nav.wait_active" }] },
  source: "builtin",
};

export const BUILTIN_TEMPLATES: readonly MissionTemplate[] = [
  BLANK_TEMPLATE,
  tpl("go_to_point", goToPoint),
  tpl("patrol", patrol),
  tpl("follow_line", followLine),
  tpl("pickup_job", pickupJob),
  tpl("go_charge", goCharge),
  tpl("mqtt_goal_server", mqttGoalServer),
  tpl("emergency_stop", emergencyStop),
  tpl("global", globalMission),
];

/** Pick a mission name that is not taken: base, base_2, base_3, ... */
export function uniqueName(base: string, taken: ReadonlySet<string>): string {
  const clean = base.toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^[^a-z]+/, "") || "mission";
  if (!taken.has(clean)) return clean;
  for (let i = 2; ; i++) {
    const cand = `${clean.slice(0, 60)}_${i}`;
    if (!taken.has(cand)) return cand;
  }
}
