"""Project files (``project/1``): the maps with their points and lanes, the
missions and the project settings, in one JSON document.

Mission Builder saves them as ``.mproj`` files. The runner imports and exports
them over HTTP (``/api/project``) and from the command line
(``mission_runner project import|export``), so a project reaches a robot with or
without a network.

Importing is all or nothing: every mission is validated against the project's
own sites before anything is written.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .model import SCHEMA_DIR, Finding, MissionValidationError, SitesBook, check_mission_cycles, validate_mission
from .types import now_iso

if TYPE_CHECKING:
    from .model import Mission
    from .store import MissionStore

__all__ = ["PROJECT_SCHEMA", "export_project", "check_project", "write_project"]

PROJECT_SCHEMA: dict[str, Any] = json.loads((SCHEMA_DIR / "project.schema.json").read_text("utf-8"))
_validator = Draft202012Validator(PROJECT_SCHEMA)


def export_project(store: MissionStore, name: str = "", settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """The store's sites and missions as a project document."""
    missions = [s.mission.raw for s in sorted(store.missions.values(), key=lambda s: s.mission.name)]
    return {
        "schema": "project/1",
        "name": name or "robot",
        "updated_at": now_iso(),
        "settings": dict(settings or {}),
        "sites": store.sites.to_dict(),
        "missions": missions,
    }


def check_project(
    doc: Any,
    *,
    connectors: set[str] | None = None,
    capabilities: dict[str, dict[str, Any]] | None = None,
    trigger_capabilities: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[Finding], list[Finding], SitesBook | None, list[Mission]]:
    """Validate a whole project. Returns ``(errors, warnings, sites, missions)``;
    paths of mission findings are prefixed with ``missions/<name>``."""
    errors: list[Finding] = []
    warnings: list[Finding] = []
    if not isinstance(doc, dict):
        return [Finding("error", [], "a project must be a JSON object")], [], None, []
    for e in sorted(_validator.iter_errors(doc), key=lambda e: list(e.path)):
        errors.append(Finding("error", list(e.path), e.message))
    if errors:
        return errors, warnings, None, []

    try:
        book = SitesBook.from_dict(doc["sites"])
    except MissionValidationError as e:
        return [Finding("error", ["sites", *f.path], f.message) for f in e.errors], warnings, None, []

    docs = doc["missions"]
    names = [str(m.get("name", "")) for m in docs]
    seen: set[str] = set()
    for i, n in enumerate(names):
        if n in seen:
            errors.append(Finding("error", ["missions", i, "name"], f"mission '{n}' appears more than once"))
        seen.add(n)

    parsed: list[Mission] = []
    for m_doc in docs:
        name = str(m_doc.get("name", "?"))
        errs, warns, m = validate_mission(
            m_doc,
            sites=book,
            missions=set(names),
            connectors=connectors,
            capabilities=capabilities,
            trigger_capabilities=trigger_capabilities,
        )
        errors.extend(Finding(f.level, ["missions", name, *f.path], f.message, f.step_id) for f in errs)
        warnings.extend(Finding(f.level, ["missions", name, *f.path], f.message, f.step_id) for f in warns)
        if m is not None:
            parsed.append(m)
    if not errors:
        errors.extend(check_mission_cycles({m.name: m for m in parsed}))
    return errors, warnings, book, parsed


def write_project(store: MissionStore, doc: dict[str, Any], book: SitesBook, *, replace: bool = False, protect: set[str] | frozenset[str] = frozenset()) -> dict[str, list[str]]:
    """Write a checked project into the store. With ``replace``, missions that
    are not in the project are deleted, except those in ``protect`` (a running
    mission). Returns the names saved, deleted and kept because protected."""
    store.save_sites(book.to_dict())
    saved: list[str] = []
    for m_doc in doc["missions"]:
        saved.append(store.save(m_doc).name)
    deleted: list[str] = []
    kept: list[str] = []
    if replace:
        wanted = set(saved)
        for name in sorted(store.names() - wanted):
            if name in protect:
                kept.append(name)
                continue
            if store.delete(name):
                deleted.append(name)
    return {"saved": saved, "deleted": deleted, "kept": kept}
