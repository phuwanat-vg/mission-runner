/**
 * The floor of the map: metadata (`GET /api/maps/{name}`) and the PNG
 * (`GET /api/maps/{name}/image`) per map name, kept in memory and mirrored to
 * `localStorage` so an editor that starts offline still draws the last known
 * floor. Nothing here touches the DOM.
 */

import type { MapMeta, RunnerClient } from "../api/RunnerClient";
import { isRecord } from "../model/types";

export interface MapData {
  name: string;
  meta: MapMeta;
  /** The floor PNG as a data: URL, or null when only the metadata is known. */
  image: string | null;
  /** True when this came from localStorage because the runner was unreachable. */
  stale: boolean;
}

export const MAPS_KEY = "mission-editor.maps.v1";
/** Maps kept in localStorage (newest first). */
const MAX_CACHED = 4;
/** Bigger images are used but not written to localStorage (quota is ~5 MB). */
const MAX_IMAGE_CHARS = 1_200_000;
/** Do not hammer the runner for a map that just failed. */
const RETRY_MS = 5000;

interface StoredMap {
  meta: MapMeta;
  image: string | null;
  savedAt: number;
}

function isMeta(v: unknown): v is MapMeta {
  return (
    isRecord(v) &&
    typeof v.width === "number" &&
    typeof v.height === "number" &&
    typeof v.resolution === "number" &&
    Array.isArray(v.bounds) &&
    v.bounds.length === 4 &&
    v.bounds.every((n) => typeof n === "number")
  );
}

export class MapSource {
  #client: RunnerClient;
  /** Fetched in this session. */
  #live = new Map<string, MapData>();
  /** Read back from localStorage at startup (used until the runner answers). */
  #cached = new Map<string, MapData>();
  #pending = new Set<string>();
  #failedAt = new Map<string, number>();
  #handlers = new Set<() => void>();

  constructor(client: RunnerClient) {
    this.#client = client;
    this.#load();
  }

  onChange(fn: () => void): () => void {
    this.#handlers.add(fn);
    return () => this.#handlers.delete(fn);
  }

  #emit(): void {
    for (const fn of this.#handlers) {
      try {
        fn();
      } catch (err) {
        console.error("map listener failed", err);
      }
    }
  }

  /** The best copy of a map that is available right now, or null. */
  get(name: string | null): MapData | null {
    if (!name) return null;
    return this.#live.get(name) ?? this.#cached.get(name) ?? null;
  }

  /** True while the first fetch of this map is in flight. */
  loading(name: string | null): boolean {
    return !!name && this.#pending.has(name);
  }

  /**
   * Make sure a map is loaded. Cheap to call on every render: it fetches once
   * per name (and once per `RETRY_MS` after a failure).
   */
  request(name: string | null, force = false): void {
    if (!name) return;
    if (this.#pending.has(name)) return;
    if (!force && this.#live.has(name)) return;
    const failed = this.#failedAt.get(name);
    if (!force && failed !== undefined && Date.now() - failed < RETRY_MS) return;
    this.#pending.add(name);
    void this.#fetch(name);
  }

  /** Forget the fetched copy so the next `request` reloads it (sites changed). */
  invalidate(name?: string | null): void {
    if (name) {
      this.#live.delete(name);
      this.#failedAt.delete(name);
    } else {
      this.#live.clear();
      this.#failedAt.clear();
    }
  }

  async #fetch(name: string): Promise<void> {
    try {
      const meta = await this.#client.getMap(name);
      if (!isMeta(meta)) throw new Error("bad map metadata");
      let image: string | null = null;
      try {
        image = await this.#client.getMapImage(name);
      } catch {
        image = this.get(name)?.image ?? null; // metadata is still useful
      }
      const data: MapData = { name, meta: { ...meta, name }, image, stale: false };
      this.#live.set(name, data);
      this.#cached.delete(name);
      this.#failedAt.delete(name);
      this.#save(name, data);
      this.#emit();
    } catch {
      this.#failedAt.set(name, Date.now());
      // the localStorage copy (if any) keeps being used; tell the view anyway
      this.#emit();
    } finally {
      this.#pending.delete(name);
    }
  }

  // ---- localStorage --------------------------------------------------------------------

  #load(): void {
    let raw: string | null = null;
    try {
      raw = localStorage.getItem(MAPS_KEY);
    } catch {
      return;
    }
    if (!raw) return;
    try {
      const parsed: unknown = JSON.parse(raw);
      if (!isRecord(parsed)) return;
      for (const [name, entry] of Object.entries(parsed)) {
        if (!isRecord(entry) || !isMeta(entry.meta)) continue;
        this.#cached.set(name, { name, meta: { ...entry.meta, name }, image: typeof entry.image === "string" ? entry.image : null, stale: true });
      }
    } catch {
      // corrupt cache: ignore it
    }
  }

  #save(name: string, data: MapData): void {
    let store: Record<string, StoredMap> = {};
    try {
      const raw = localStorage.getItem(MAPS_KEY);
      const parsed: unknown = raw ? JSON.parse(raw) : {};
      if (isRecord(parsed)) store = parsed as Record<string, StoredMap>;
    } catch {
      store = {};
    }
    const image = data.image && data.image.length <= MAX_IMAGE_CHARS ? data.image : null;
    store[name] = { meta: data.meta, image, savedAt: Date.now() };
    const names = Object.keys(store).sort((a, b) => (store[b]?.savedAt ?? 0) - (store[a]?.savedAt ?? 0));
    for (const drop of names.slice(MAX_CACHED)) delete store[drop];
    try {
      localStorage.setItem(MAPS_KEY, JSON.stringify(store));
    } catch {
      // quota exceeded or storage blocked: keep the map in memory only
      try {
        localStorage.setItem(MAPS_KEY, JSON.stringify({ [name]: { meta: data.meta, image: null, savedAt: Date.now() } }));
      } catch {
        // give up silently
      }
    }
  }
}
