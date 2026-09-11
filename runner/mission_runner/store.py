"""On-disk state: missions, sites, connectors config, generated behavior trees
and the run log (SQLite)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import Finding, Mission, MissionValidationError, SitesBook, load_mission, mission_sha256

log = logging.getLogger("mission.store")

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@dataclass
class StoredMission:
    mission: Mission
    path: Path
    updated_at: str


class MissionStore:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.missions_dir = self.home / "missions"
        self.sites_path = self.home / "sites.json"
        self.connectors_path = self.home / "connectors.yaml"
        self.bt_dir = self.home / "bt"
        self.missions: dict[str, StoredMission] = {}
        self.load_errors: dict[str, list[Finding]] = {}
        self.sites: SitesBook = SitesBook.empty()
        self.home.mkdir(parents=True, exist_ok=True)
        self.missions_dir.mkdir(exist_ok=True)
        self.bt_dir.mkdir(exist_ok=True)

    # ----- missions ----------------------------------------------------------

    def load_all(self) -> None:
        self.missions.clear()
        self.load_errors.clear()
        for path in sorted(self.missions_dir.glob("*.json")):
            try:
                doc = json.loads(path.read_text("utf-8"))
                m = load_mission(doc)
                if m.name != path.stem:
                    log.warning("%s: mission name '%s' differs from file name; using the name inside the file", path.name, m.name)
                self.missions[m.name] = StoredMission(m, path, _mtime_iso(path))
            except MissionValidationError as e:
                self.load_errors[path.stem] = e.errors
                log.error("%s: %s", path.name, e)
            except (OSError, ValueError) as e:
                self.load_errors[path.stem] = [Finding("error", [], str(e))]
                log.error("%s: %s", path.name, e)
        self.load_sites()

    def get(self, name: str) -> Mission | None:
        s = self.missions.get(name)
        return s.mission if s else None

    def names(self) -> set[str]:
        return set(self.missions)

    def summaries(self) -> list[dict[str, Any]]:
        out = [s.mission.summary(s.updated_at) for s in self.missions.values()]
        for name, errs in self.load_errors.items():
            if name not in self.missions:
                out.append({"name": name, "title": name, "description": "", "version": 0, "errors": [e.as_dict() for e in errs], "triggers": [], "interrupts": [], "steps": 0, "sha256": "", "updated_at": None})
        return sorted(out, key=lambda m: m["name"])

    def save(self, doc: dict[str, Any]) -> Mission:
        m = load_mission(doc)
        path = self.missions_dir / f"{m.name}.json"
        _write_atomic(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        self.missions[m.name] = StoredMission(m, path, _mtime_iso(path))
        self.load_errors.pop(m.name, None)
        return m

    def delete(self, name: str) -> bool:
        s = self.missions.pop(name, None)
        if s is None:
            return False
        try:
            s.path.unlink()
        except FileNotFoundError:
            pass
        for f in self.bt_dir.glob(f"{name}__*.xml"):
            f.unlink(missing_ok=True)
        return True

    def raw(self, name: str) -> dict[str, Any] | None:
        s = self.missions.get(name)
        return s.mission.raw if s else None

    # ----- sites -------------------------------------------------------------

    def load_sites(self) -> SitesBook:
        if self.sites_path.exists():
            try:
                self.sites = SitesBook.from_dict(json.loads(self.sites_path.read_text("utf-8")))
            except (MissionValidationError, ValueError, OSError) as e:
                log.error("sites.json: %s", e)
                self.sites = SitesBook.empty()
        else:
            self.sites = SitesBook.empty()
        return self.sites

    def save_sites(self, doc: dict[str, Any]) -> SitesBook:
        book = SitesBook.from_dict(doc)
        _write_atomic(self.sites_path, json.dumps(book.to_dict(), indent=2, ensure_ascii=False) + "\n")
        self.sites = book
        return book

    # ----- connectors config -------------------------------------------------

    def load_connectors_config(self) -> dict[str, dict[str, Any]]:
        if not self.connectors_path.exists():
            return {}
        text = self.connectors_path.read_text("utf-8")
        try:
            if yaml is not None:
                doc = yaml.safe_load(text) or {}
            else:
                doc = json.loads(text)
        except Exception as e:  # noqa: BLE001
            log.error("connectors.yaml: %s", e)
            return {}
        conns = doc.get("connectors", doc) if isinstance(doc, dict) else {}
        return {str(k): dict(v or {}) for k, v in conns.items() if isinstance(v, dict) or v is None}

    # ----- behavior trees ----------------------------------------------------

    def write_bt(self, file_name: str, xml: str) -> Path:
        path = self.bt_dir / file_name
        if not path.exists() or path.read_text("utf-8") != xml:
            _write_atomic(path, xml)
        return path


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# run log


class RunLog:
    """SQLite history of runs and their events. Synchronous; writes are tiny."""

    def __init__(self, path: Path | str, keep_runs: int = 2000):
        self.path = str(path)
        self.keep_runs = keep_runs
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, mission TEXT NOT NULL, inputs TEXT, source TEXT, priority INTEGER, policy TEXT,
                status TEXT, created_at TEXT, started_at TEXT, finished_at TEXT, error TEXT, result TEXT, parent_run_id TEXT, seq INTEGER
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, t REAL, type TEXT, step_id TEXT, path TEXT, data TEXT
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
            CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at);
            """
        )
        self._seq = int(self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM runs").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def upsert_run(self, run: dict[str, Any]) -> None:
        with self._lock:
            row = self._db.execute("SELECT seq FROM runs WHERE id = ?", (run["id"],)).fetchone()
            seq = row[0] if row else self._next_seq()
            self._db.execute(
                """INSERT OR REPLACE INTO runs (id, mission, inputs, source, priority, policy, status, created_at, started_at, finished_at, error, result, parent_run_id, seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["id"],
                    run["mission"],
                    json.dumps(run.get("inputs"), ensure_ascii=False),
                    json.dumps(run.get("source"), ensure_ascii=False),
                    run.get("priority"),
                    run.get("policy"),
                    run.get("status"),
                    run.get("created_at"),
                    run.get("started_at"),
                    run.get("finished_at"),
                    run.get("error") or "",
                    json.dumps(run.get("result"), ensure_ascii=False, default=str),
                    run.get("parent_run_id"),
                    seq,
                ),
            )
            self._db.commit()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def add_event(self, run_id: str, event: dict[str, Any]) -> None:
        data = {k: v for k, v in event.items() if k not in ("type", "t", "run_id", "step_id", "path")}
        with self._lock:
            self._db.execute(
                "INSERT INTO events (run_id, t, type, step_id, path, data) VALUES (?,?,?,?,?,?)",
                (run_id, float(event.get("t") or time.time()), event["type"], event.get("step_id"), json.dumps(event.get("path")), json.dumps(data, ensure_ascii=False, default=str)),
            )
            self._db.commit()

    def list_runs(self, limit: int = 50, mission: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT id, mission, inputs, source, priority, policy, status, created_at, started_at, finished_at, error, result, parent_run_id FROM runs"
        args: list[Any] = []
        if mission:
            q += " WHERE mission = ?"
            args.append(mission)
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._row(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, mission, inputs, source, priority, policy, status, created_at, started_at, finished_at, error, result, parent_run_id FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                return None
            ev = self._db.execute("SELECT t, type, step_id, path, data FROM events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        run = self._row(row)
        run["events"] = [{"t": e[0], "type": e[1], "step_id": e[2], "path": json.loads(e[3]) if e[3] else None, **(json.loads(e[4]) if e[4] else {})} for e in ev]
        return run

    def prune(self) -> None:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            if n <= self.keep_runs:
                return
            cutoff = self._db.execute("SELECT seq FROM runs ORDER BY seq DESC LIMIT 1 OFFSET ?", (self.keep_runs,)).fetchone()
            if not cutoff:
                return
            self._db.execute("DELETE FROM events WHERE run_id IN (SELECT id FROM runs WHERE seq <= ?)", (cutoff[0],))
            self._db.execute("DELETE FROM runs WHERE seq <= ?", (cutoff[0],))
            self._db.commit()

    @staticmethod
    def _row(r: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": r[0],
            "mission": r[1],
            "inputs": json.loads(r[2]) if r[2] else {},
            "source": json.loads(r[3]) if r[3] else None,
            "priority": r[4],
            "policy": r[5],
            "status": r[6],
            "created_at": r[7],
            "started_at": r[8],
            "finished_at": r[9],
            "error": r[10],
            "result": json.loads(r[11]) if r[11] else None,
            "parent_run_id": r[12],
        }


def mission_hash(doc: dict[str, Any]) -> str:
    return mission_sha256(doc)
