/**
 * Actions the panels can trigger on the application. App implements them;
 * panels only see this interface so they stay independent of each other.
 */

import type { Path, Site, Step } from "../model/types";

export interface AppActions {
  // run controls
  run(): void;
  pauseResume(): void;
  stop(): void;
  deploy(): void;
  // missions
  newMission(): void;
  openMission(name: string): void;
  renameMission(name: string): void;
  duplicateMission(name: string): void;
  deleteMission(name: string): void;
  importJson(): void;
  exportJson(): void;
  exportPython(): void;
  exportBt(): void;
  loadRobotCopy(): void;
  // settings
  setRunnerUrl(): void;
  showSites(): void;
  showAbout(): void;
  // editing
  /** Open the block picker and insert the chosen step at the position. */
  addStepAt(listPath: Path, index: number): void;
  /** Insert a new step of a type at the position (or after the selection when omitted). */
  insertBlock(type: string, at?: { listPath: Path; index: number }): void;
  /** Insert a nav.go_to_pose for a site. */
  insertSite(site: string, at?: { listPath: Path; index: number }): void;
  insertStep(step: Step, at: { listPath: Path; index: number }): void;
  addTrigger(kind: "trigger" | "interrupt"): void;
  // previews (the robot never moves)
  /** POST /api/preview/route for the open document; draws the planned legs. */
  previewRoute(): void;
  /** POST /api/preview/dryrun for the open document; loads the transport bar. */
  previewDryRun(): void;
  // sites and zones
  saveSites(): void;
  captureSite(name: string, existing?: Site): void;
  deleteSite(name: string): void;
  /** Name / kind / speed / notes of a zone, in a dialog. */
  editZone(name: string): void;
  deleteZone(name: string): void;
  /** POST /api/maps/{name}/filters and show where the masks were written. */
  exportFilters(): void;
  // layout
  togglePanel(which: "left" | "inspector"): void;
}
