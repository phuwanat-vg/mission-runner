"""Mission and sites model: parsing, structural validation (JSON Schema) and
semantic validation (ids, sites, connectors, expressions, cycles)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .expressions import ExpressionError, collect_expressions
from .expressions import parse as parse_expression

__all__ = [
    "MISSION_SCHEMA",
    "SITES_SCHEMA",
    "STEP_TYPES",
    "EVENT_TYPES",
    "POLICIES",
    "RESERVED_NAMES",
    "Finding",
    "MissionValidationError",
    "OnFail",
    "Step",
    "EventSource",
    "TriggerSpec",
    "InputDef",
    "Mission",
    "Site",
    "Zone",
    "Edge",
    "RouteLeg",
    "MapDef",
    "SitesBook",
    "load_mission",
    "validate_mission",
    "check_mission_cycles",
    "mission_sha256",
    "canonical_json",
]

SCHEMA_DIR = Path(__file__).parent / "schema"
MISSION_SCHEMA: dict[str, Any] = json.loads((SCHEMA_DIR / "mission.schema.json").read_text("utf-8"))
SITES_SCHEMA: dict[str, Any] = json.loads((SCHEMA_DIR / "sites.schema.json").read_text("utf-8"))
_mission_validator = Draft202012Validator(MISSION_SCHEMA)
_sites_validator = Draft202012Validator(SITES_SCHEMA)

NAME_RE = re.compile(MISSION_SCHEMA["properties"]["name"]["pattern"])
STEP_TYPES: frozenset[str] = frozenset(MISSION_SCHEMA["$defs"]["step"]["allOf"][1]["properties"]["type"]["enum"])
EVENT_TYPES: frozenset[str] = frozenset(MISSION_SCHEMA["$defs"]["eventSource"]["properties"]["type"]["enum"])
POLICIES: tuple[str, ...] = tuple(MISSION_SCHEMA["$defs"]["policy"]["enum"])
RESERVED_NAMES: frozenset[str] = frozenset(
    {"last", "payload", "robot", "mission", "run_id", "current_map", "sites", "zones", "in_zone", "zone_of", "inputs", "True", "False", "None"}
)

STEP_BASE_KEYS = frozenset({"id", "type", "name", "enabled", "out", "timeout_s", "on_fail"})
TRIGGER_BASE_KEYS = frozenset({"id", "type", "name", "enabled", "policy", "priority", "when", "edge", "debounce_s", "set", "run"})
CONTAINER_KEYS: dict[str, tuple[str, ...]] = {"if": ("then", "else"), "loop": ("body",)}
GLOBAL_MISSION = "global"

# Step types that read a pose / list of poses (for site validation and codegen).
POSE_PARAMS: dict[str, tuple[str, ...]] = {
    "nav.set_initial_pose": ("pose",),
    "nav.go_to_pose": ("pose",),
    "nav.compute_path": ("goal", "start"),
    "nav.dock": ("dock_pose",),
}
POSE_LIST_PARAMS: dict[str, tuple[str, ...]] = {
    "nav.go_through_poses": ("poses",),
    "nav.follow_waypoints": ("poses",),
    "nav.follow_path": ("points",),
    "nav.compute_path_through_poses": ("goals",),
}
#: Params that name a site directly (not a pose object).
SITE_NAME_PARAMS: dict[str, tuple[str, ...]] = {"nav.follow_route": ("to", "from")}
CONNECTOR_STEP_TYPES = frozenset({"mqtt.publish", "modbus.write"})
CONNECTOR_EVENT_TYPES = frozenset({"mqtt.subscribe", "modbus.poll"})


# ---------------------------------------------------------------------------
# findings


@dataclass(slots=True)
class Finding:
    level: str  # error | warning
    path: list[str | int]
    message: str
    step_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"level": self.level, "path": "/".join(str(p) for p in self.path), "message": self.message}
        if self.step_id:
            d["step_id"] = self.step_id
        return d

    def __str__(self) -> str:
        where = "/".join(str(p) for p in self.path)
        return f"{where}: {self.message}" if where else self.message


class MissionValidationError(ValueError):
    def __init__(self, errors: list[Finding]):
        super().__init__("; ".join(str(e) for e in errors[:5]) + (" ..." if len(errors) > 5 else ""))
        self.errors = errors


# ---------------------------------------------------------------------------
# model


@dataclass
class OnFail:
    retry: int = 0
    retry_delay_s: float = 0.0
    before_retry: list[Step] = field(default_factory=list)
    then: str = "abort"


@dataclass
class Step:
    id: str
    type: str
    name: str
    enabled: bool
    out: str | None
    timeout_s: float | None
    on_fail: OnFail | None
    params: dict[str, Any]
    path: tuple[int | str, ...]
    children: dict[str, list[Step]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.type

    def container(self, key: str) -> list[Step]:
        return self.children.get(key, [])


@dataclass
class EventSource:
    type: str
    params: dict[str, Any]
    when: str | None = None
    edge: str = "any"
    debounce_s: float = 0.0

    @property
    def connector(self) -> str | None:
        c = self.params.get("connector")
        return str(c) if c else None

    def summary(self) -> str:
        p = self.params
        t = self.type
        if t == "ros.topic":
            s = f"topic {p.get('topic')}"
        elif t == "mqtt.subscribe":
            s = f"mqtt {p.get('connector')}:{p.get('topic')}"
        elif t == "http.webhook":
            s = f"POST /hooks/{p.get('path')}"
        elif t == "timer.cron":
            s = f"cron {p.get('cron')}"
        elif t == "timer.interval":
            s = f"every {p.get('seconds')} s"
        elif t == "timer.boot":
            s = f"boot +{p.get('delay_s', 0)} s"
        elif t == "gpio.input":
            s = f"gpio {p.get('pin')} {p.get('gpio_edge', 'falling')}"
        elif t == "modbus.poll":
            s = f"modbus {p.get('connector')} {p.get('kind', 'coil')} {p.get('address')}"
        elif t == "mission.done":
            s = f"after {p.get('mission')} ({p.get('result', 'any')})"
        else:
            s = t
        if self.when:
            s += f" when {self.when}"
        if self.edge == "rising":
            s += " (rising)"
        return s


@dataclass
class TriggerSpec(EventSource):
    id: str = ""
    name: str = ""
    enabled: bool = True
    policy: str = "queue"
    priority: int = 50
    set: dict[str, str] = field(default_factory=dict)
    run: str | None = None  # interrupts only

    def as_source(self) -> EventSource:
        return EventSource(self.type, self.params, self.when, self.edge, self.debounce_s)


@dataclass(slots=True)
class InputDef:
    name: str
    type: str
    label: str = ""
    description: str = ""
    default: Any = None
    required: bool = False


@dataclass
class Mission:
    name: str
    title: str
    description: str
    version: int
    policy: str
    priority: int
    inputs: dict[str, InputDef]
    vars: dict[str, Any]
    triggers: list[TriggerSpec]
    interrupts: list[TriggerSpec]
    flow: list[Step]
    on_abort: list[Step]
    raw: dict[str, Any]
    sha256: str

    @property
    def is_global(self) -> bool:
        return self.name == GLOBAL_MISSION

    def iter_steps(self, steps: Iterable[Step] | None = None) -> Iterator[Step]:
        """Depth-first over flow, on_abort, containers and before_retry lists."""
        roots = list(steps) if steps is not None else [*self.flow, *self.on_abort]
        stack = list(reversed(roots))
        while stack:
            s = stack.pop()
            yield s
            nested: list[Step] = []
            for key in CONTAINER_KEYS.get(s.type, ()):
                nested.extend(s.container(key))
            if s.on_fail:
                nested.extend(s.on_fail.before_retry)
            stack.extend(reversed(nested))

    def step_by_id(self, step_id: str) -> Step | None:
        for s in self.iter_steps():
            if s.id == step_id:
                return s
        return None

    def trigger_summaries(self) -> list[str]:
        return [t.summary() for t in self.triggers if t.enabled]

    def summary(self, updated_at: str | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "policy": self.policy,
            "priority": self.priority,
            "inputs": {k: {"type": v.type, "default": v.default, "label": v.label, "required": v.required} for k, v in self.inputs.items()},
            "triggers": self.trigger_summaries(),
            "interrupts": [f"{t.summary()} -> {t.run}" for t in self.interrupts if t.enabled],
            "steps": sum(1 for _ in self.iter_steps(self.flow)),
            "sha256": self.sha256,
            "updated_at": updated_at,
        }


# ---------------------------------------------------------------------------
# sites


@dataclass(slots=True)
class Site:
    name: str
    x: float
    y: float
    yaw_deg: float = 0.0
    kind: str = "station"
    dock_id: str | None = None
    dock_type: str | None = None
    notes: str = ""

    def as_pose(self, frame: str = "map") -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "yaw_deg": self.yaw_deg, "frame": frame}

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"x": self.x, "y": self.y, "yaw_deg": self.yaw_deg, "kind": self.kind}
        if self.dock_id:
            d["dock_id"] = self.dock_id
        if self.dock_type:
            d["dock_type"] = self.dock_type
        if self.notes:
            d["notes"] = self.notes
        return d


@dataclass(slots=True)
class Zone:
    """A named area on a map: a keep-out, a speed cap, or just a place with a name."""

    name: str
    polygon: list[tuple[float, float]]
    kind: str = "work"
    speed_mps: float | None = None
    color: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "polygon": [[x, y] for x, y in self.polygon]}
        if self.speed_mps is not None:
            d["speed_mps"] = self.speed_mps
        if self.color:
            d["color"] = self.color
        if self.notes:
            d["notes"] = self.notes
        return d

    def contains(self, x: float, y: float) -> bool:
        """Even-odd ray casting; points exactly on an edge count as inside."""
        poly = self.polygon
        inside = False
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if (y1 > y) != (y2 > y):
                t = (y - y1) / (y2 - y1)
                if x < x1 + t * (x2 - x1):
                    inside = not inside
        return inside

    def bounds(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass(slots=True)
class Edge:
    """A lane between two sites. Missions that use ``nav.follow_route`` may only
    drive along edges, so the robot follows the routes you drew."""

    frm: str
    to: str
    bidirectional: bool = True
    speed_mps: float | None = None
    blocked: bool = False
    cost: float = 1.0
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"from": self.frm, "to": self.to}
        if not self.bidirectional:
            d["bidirectional"] = False
        if self.speed_mps is not None:
            d["speed_mps"] = self.speed_mps
        if self.blocked:
            d["blocked"] = True
        if self.cost != 1.0:
            d["cost"] = self.cost
        if self.notes:
            d["notes"] = self.notes
        return d

    def key(self) -> tuple[str, str]:
        return (self.frm, self.to) if self.bidirectional is False else tuple(sorted((self.frm, self.to)))  # type: ignore[return-value]


@dataclass
class RouteLeg:
    """One hop of a planned route."""

    frm: str
    to: str
    length_m: float
    speed_mps: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"from": self.frm, "to": self.to, "length_m": round(self.length_m, 3), "speed_mps": self.speed_mps}


@dataclass
class MapDef:
    name: str
    file: str = ""
    frame: str = "map"
    sites: dict[str, Site] = field(default_factory=dict)
    zones: dict[str, Zone] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def zones_at(self, x: float, y: float) -> list[Zone]:
        return [z for z in self.zones.values() if z.contains(x, y)]

    # ----- route graph ----------------------------------------------------------

    def neighbours(self) -> dict[str, list[tuple[str, Edge]]]:
        """Adjacency of the drivable graph (blocked edges and edges whose end
        points no longer exist are left out)."""
        adj: dict[str, list[tuple[str, Edge]]] = {name: [] for name in self.sites}
        for e in self.edges:
            if e.blocked or e.frm not in self.sites or e.to not in self.sites or e.frm == e.to:
                continue
            adj.setdefault(e.frm, []).append((e.to, e))
            if e.bidirectional:
                adj.setdefault(e.to, []).append((e.frm, e))
        return adj

    def nearest_site(self, x: float, y: float, max_distance_m: float | None = None, only_on_graph: bool = False) -> Site | None:
        """The site closest to a point, optionally restricted to sites that have
        at least one drivable edge."""
        adj = self.neighbours() if only_on_graph else None
        best: tuple[float, Site] | None = None
        for s in self.sites.values():
            if adj is not None and not adj.get(s.name):
                continue
            d = math.hypot(s.x - x, s.y - y)
            if best is None or d < best[0]:
                best = (d, s)
        if best is None or (max_distance_m is not None and best[0] > max_distance_m):
            return None
        return best[1]

    def plan_route(self, start: str, goal: str) -> list[RouteLeg] | None:
        """Cheapest path over the graph, or None when the two are not connected.
        Dijkstra on edge length x cost; graphs here are tens of nodes."""
        if start == goal:
            return []
        if start not in self.sites or goal not in self.sites:
            return None
        adj = self.neighbours()
        dist: dict[str, float] = {start: 0.0}
        prev: dict[str, tuple[str, Edge]] = {}
        visited: set[str] = set()
        queue: list[tuple[float, str]] = [(0.0, start)]
        while queue:
            queue.sort()
            d, node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            if node == goal:
                break
            here = self.sites[node]
            for nxt, edge in adj.get(node, ()):
                if nxt in visited:
                    continue
                there = self.sites[nxt]
                step = math.hypot(there.x - here.x, there.y - here.y) * max(0.01, edge.cost)
                nd = d + step
                if nd < dist.get(nxt, math.inf):
                    dist[nxt] = nd
                    prev[nxt] = (node, edge)
                    queue.append((nd, nxt))
        if goal not in prev:
            return None
        legs: list[RouteLeg] = []
        cur = goal
        while cur != start:
            before, edge = prev[cur]
            a, b = self.sites[before], self.sites[cur]
            legs.append(RouteLeg(before, cur, math.hypot(b.x - a.x, b.y - a.y), edge.speed_mps))
            cur = before
        legs.reverse()
        return legs


class SitesBook:
    """Named locations per map (sites.json)."""

    def __init__(self, maps: dict[str, MapDef] | None = None, default_map: str | None = None):
        self.maps: dict[str, MapDef] = maps or {}
        self.default_map: str | None = default_map or (next(iter(self.maps)) if self.maps else None)

    @classmethod
    def empty(cls) -> SitesBook:
        return cls({}, None)

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> SitesBook:
        errors = sorted(_sites_validator.iter_errors(doc), key=lambda e: list(e.path))
        if errors:
            raise MissionValidationError([Finding("error", list(e.path), e.message) for e in errors])
        maps: dict[str, MapDef] = {}
        for mname, m in (doc.get("maps") or {}).items():
            sites = {
                sname: Site(
                    sname,
                    float(s["x"]),
                    float(s["y"]),
                    float(s.get("yaw_deg", 0.0)),
                    str(s.get("kind", "station")),
                    s.get("dock_id"),
                    s.get("dock_type"),
                    str(s.get("notes", "")),
                )
                for sname, s in (m.get("sites") or {}).items()
            }
            zones = {
                zname: Zone(
                    zname,
                    [(float(p[0]), float(p[1])) for p in z["polygon"]],
                    str(z.get("kind", "work")),
                    float(z["speed_mps"]) if z.get("speed_mps") is not None else None,
                    str(z.get("color", "")),
                    str(z.get("notes", "")),
                )
                for zname, z in (m.get("zones") or {}).items()
            }
            edges = [
                Edge(
                    str(e["from"]),
                    str(e["to"]),
                    bool(e.get("bidirectional", True)),
                    float(e["speed_mps"]) if e.get("speed_mps") is not None else None,
                    bool(e.get("blocked", False)),
                    float(e.get("cost", 1.0)),
                    str(e.get("notes", "")),
                )
                for e in (m.get("edges") or [])
            ]
            maps[mname] = MapDef(mname, str(m.get("file", "")), str(m.get("frame", "map")), sites, zones, edges)
        return cls(maps, doc.get("default_map"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "sites/1",
            "default_map": self.default_map,
            "maps": {
                m.name: {
                    "file": m.file,
                    "frame": m.frame,
                    "sites": {s.name: s.as_dict() for s in m.sites.values()},
                    **({"edges": [e.as_dict() for e in m.edges]} if m.edges else {}),
                    **({"zones": {z.name: z.as_dict() for z in m.zones.values()}} if m.zones else {}),
                }
                for m in self.maps.values()
            },
        }

    def map(self, name: str | None) -> MapDef | None:
        if name is None:
            return None
        return self.maps.get(name)

    def lookup(self, map_name: str | None, site: str) -> Site | None:
        m = self.map(map_name)
        return m.sites.get(site) if m else None

    def maps_with_site(self, site: str) -> list[str]:
        return [m.name for m in self.maps.values() if site in m.sites]

    def map_file(self, name_or_path: str) -> str:
        m = self.maps.get(name_or_path)
        return m.file if m and m.file else name_or_path

    def set_site(self, map_name: str, site: Site) -> None:
        m = self.maps.setdefault(map_name, MapDef(map_name))
        m.sites[site.name] = site
        if self.default_map is None:
            self.default_map = map_name


# ---------------------------------------------------------------------------
# parsing


def canonical_json(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def mission_sha256(doc: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def _auto_id(path: tuple[int | str, ...]) -> str:
    return "_".join(str(p) for p in path)


def _parse_steps(items: Any, path: tuple[int | str, ...]) -> list[Step]:
    steps: list[Step] = []
    for i, raw in enumerate(items or []):
        if not isinstance(raw, dict):
            continue
        p = (*path, i)
        stype = str(raw.get("type", ""))
        on_fail = None
        of = raw.get("on_fail")
        if isinstance(of, dict):
            on_fail = OnFail(
                retry=int(of.get("retry", 0) or 0),
                retry_delay_s=float(of.get("retry_delay_s", 0) or 0),
                before_retry=_parse_steps(of.get("before_retry"), (*p, "on_fail", "before_retry")),
                then=str(of.get("then", "abort")),
            )
        params = {k: v for k, v in raw.items() if k not in STEP_BASE_KEYS}
        children: dict[str, list[Step]] = {}
        for key in CONTAINER_KEYS.get(stype, ()):
            children[key] = _parse_steps(raw.get(key), (*p, key))
            params.pop(key, None)
        timeout = raw.get("timeout_s")
        step = Step(
            id=str(raw.get("id") or _auto_id(p)),
            type=stype,
            name=str(raw.get("name", "") or ""),
            enabled=bool(raw.get("enabled", True)),
            out=str(raw["out"]) if raw.get("out") else None,
            timeout_s=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else None,
            on_fail=on_fail,
            params=params,
            path=p,
            children=children,
            raw=raw,
        )
        steps.append(step)
    return steps


def _parse_trigger(raw: dict[str, Any], default_policy: str, default_priority: int, is_interrupt: bool, index: int) -> TriggerSpec:
    params = {k: v for k, v in raw.items() if k not in TRIGGER_BASE_KEYS}
    policy = str(raw.get("policy") or ("interrupt_and_resume" if is_interrupt else default_policy))
    return TriggerSpec(
        type=str(raw.get("type", "")),
        params=params,
        when=str(raw["when"]) if raw.get("when") else None,
        edge=str(raw.get("edge", "any")),
        debounce_s=float(raw.get("debounce_s", 0) or 0),
        id=str(raw.get("id") or f"{'i' if is_interrupt else 't'}{index}"),
        name=str(raw.get("name", "") or ""),
        enabled=bool(raw.get("enabled", True)),
        policy=policy,
        priority=int(raw.get("priority", default_priority)),
        set={str(k): str(v) for k, v in (raw.get("set") or {}).items()},
        run=str(raw["run"]) if raw.get("run") else None,
    )


def load_mission(doc: dict[str, Any]) -> Mission:
    """Parse a mission document. Raises :class:`MissionValidationError` on
    schema violations (semantic checks are in :func:`validate_mission`)."""
    if not isinstance(doc, dict):
        raise MissionValidationError([Finding("error", [], "mission must be a JSON object")])
    errors = sorted(_mission_validator.iter_errors(doc), key=lambda e: (len(list(e.path)), list(e.path)))
    if errors:
        raise MissionValidationError([Finding("error", list(e.path), _schema_message(e)) for e in _dedupe(errors)])
    policy = str(doc.get("policy", "queue"))
    priority = int(doc.get("priority", 50))
    inputs = {
        str(k): InputDef(str(k), str(v.get("type", "string")), str(v.get("label", "")), str(v.get("description", "")), v.get("default"), bool(v.get("required", False)))
        for k, v in (doc.get("inputs") or {}).items()
    }
    return Mission(
        name=str(doc["name"]),
        title=str(doc.get("title") or doc["name"]),
        description=str(doc.get("description", "")),
        version=int(doc.get("version", 1)),
        policy=policy,
        priority=priority,
        inputs=inputs,
        vars=dict(doc.get("vars") or {}),
        triggers=[_parse_trigger(t, policy, priority, False, i) for i, t in enumerate(doc.get("triggers") or [])],
        interrupts=[_parse_trigger(t, "interrupt_and_resume", priority, True, i) for i, t in enumerate(doc.get("interrupts") or [])],
        flow=_parse_steps(doc.get("flow"), ("flow",)),
        on_abort=_parse_steps(doc.get("on_abort"), ("on_abort",)),
        raw=doc,
        sha256=mission_sha256(doc),
    )


def _dedupe(errors: list[Any]) -> list[Any]:
    """jsonschema reports every failed branch of allOf/oneOf; keep the most specific per path."""
    seen: dict[str, Any] = {}
    for e in errors:
        key = "/".join(str(p) for p in e.path)
        if key not in seen:
            seen[key] = e
    return list(seen.values())


def _schema_message(e: Any) -> str:
    msg = str(e.message)
    if e.validator == "oneOf" and "is not valid under any of the given schemas" in msg:
        return "invalid value (expected a pose object, a site name or an expression)" if "pose" in str(e.schema_path) else "invalid value"
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return msg


# ---------------------------------------------------------------------------
# semantic validation


def _is_site_ref(value: Any) -> str | None:
    """Return the site name when ``value`` statically refers to a site."""
    if isinstance(value, str):
        if value.startswith("$") or "${" in value:
            return None
        return value
    if isinstance(value, dict) and "site" in value and isinstance(value["site"], str):
        s = value["site"]
        return None if s.startswith("$") or "${" in s else s
    return None


def validate_mission(
    doc: dict[str, Any],
    *,
    sites: SitesBook | None = None,
    missions: set[str] | None = None,
    connectors: set[str] | None = None,
    capabilities: dict[str, dict[str, Any]] | None = None,
    trigger_capabilities: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[Finding], list[Finding], Mission | None]:
    """Full validation. Returns ``(errors, warnings, mission)``; ``mission`` is
    ``None`` when there are schema errors."""
    try:
        m = load_mission(doc)
    except MissionValidationError as e:
        return e.errors, [], None

    errors: list[Finding] = []
    warnings: list[Finding] = []

    def err(path: tuple[Any, ...], msg: str, step_id: str | None = None) -> None:
        errors.append(Finding("error", list(path), msg, step_id))

    def warn(path: tuple[Any, ...], msg: str, step_id: str | None = None) -> None:
        warnings.append(Finding("warning", list(path), msg, step_id))

    if not NAME_RE.match(m.name):
        err(("name",), "invalid mission name")

    if m.is_global and m.flow:
        warn(("flow",), "the global mission's flow is ignored; only its interrupts are used")
    if not m.flow and not m.is_global:
        warn(("flow",), "mission has no steps")

    # variables
    for name in [*m.inputs, *m.vars]:
        if name in RESERVED_NAMES:
            err(("inputs" if name in m.inputs else "vars", name), f"'{name}' is a reserved name")

    # steps
    seen_ids: dict[str, tuple[Any, ...]] = {}
    has_change_map = any(s.type == "nav.change_map" for s in m.iter_steps())
    for step in m.iter_steps():
        sid = step.id
        path = step.path
        if sid in seen_ids:
            err(path, f"duplicate step id '{sid}'", sid)
        seen_ids[sid] = path
        if step.type not in STEP_TYPES:
            err((*path, "type"), f"unknown step type '{step.type}'", sid)
            continue
        if step.out and step.out in RESERVED_NAMES:
            err((*path, "out"), f"'{step.out}' is a reserved name", sid)
        if step.type == "set" and step.params.get("var") in RESERVED_NAMES:
            err((*path, "var"), f"'{step.params.get('var')}' is a reserved name", sid)
        if step.type == "break" and not any(p in ("body",) for p in path):
            err(path, "'break' must be inside a loop", sid)
        if step.type == "loop" and "count" in step.params and "while" in step.params:
            err(path, "loop takes either 'count' or 'while', not both", sid)
        if step.type == "ask_user":
            opts = step.params.get("options") or ["Continue", "Stop"]
            dflt = step.params.get("default")
            if dflt is not None and dflt not in opts:
                err((*path, "default"), f"default '{dflt}' is not one of the options", sid)
            if step.timeout_s is None and (m.triggers or m.interrupts) and not m.is_global:
                warn(path, "ask_user without timeout_s can block an unattended robot forever", sid)
        if step.type == "run_mission":
            target = str(step.params.get("mission", ""))
            if target == m.name:
                err((*path, "mission"), "a mission cannot run itself", sid)
            elif missions is not None and target not in missions:
                err((*path, "mission"), f"unknown mission '{target}'", sid)
        if step.type == "wait_event":
            src = step.params.get("source") or {}
            if src.get("type") not in EVENT_TYPES:
                err((*path, "source", "type"), f"unknown event source '{src.get('type')}'", sid)
            _check_expr(src.get("when"), (*path, "source", "when"), sid, err)
            if src.get("type") in CONNECTOR_EVENT_TYPES and connectors is not None and src.get("connector") not in connectors:
                warn((*path, "source", "connector"), f"connector '{src.get('connector')}' is not configured on the robot", sid)
        if step.type in CONNECTOR_STEP_TYPES and connectors is not None and step.params.get("connector") not in connectors:
            warn((*path, "connector"), f"connector '{step.params.get('connector')}' is not configured on the robot", sid)
        if step.type == "if":
            _check_expr(step.params.get("condition"), (*path, "condition"), sid, err)
        if step.type == "loop":
            _check_expr(step.params.get("while"), (*path, "while"), sid, err)
        if not step.enabled:
            warn(path, "step is disabled", sid)
        if capabilities is not None:
            cap = capabilities.get(step.type)
            if cap is not None and not cap.get("available", True):
                warn(path, f"'{step.type}' is not available on this robot: {cap.get('reason', '')}".rstrip(": "), sid)
        # expressions inside params
        for expr in collect_expressions(step.params):
            try:
                parse_expression(expr)
            except ExpressionError as e:
                err(path, str(e), sid)
        # sites
        if sites is not None:
            refs: list[tuple[tuple[Any, ...], Any]] = []
            for key in POSE_PARAMS.get(step.type, ()):
                if key in step.params:
                    refs.append(((*path, key), step.params[key]))
            for key in POSE_LIST_PARAMS.get(step.type, ()):
                for i, v in enumerate(step.params.get(key) or []):
                    refs.append(((*path, key, i), v))
            for key in SITE_NAME_PARAMS.get(step.type, ()):
                if step.params.get(key):
                    refs.append(((*path, key), step.params[key]))
            if step.type == "nav.follow_route":
                for i, v in enumerate(step.params.get("through") or []):
                    refs.append(((*path, "through", i), v))
            for rpath, value in refs:
                site = _is_site_ref(value)
                if site is None:
                    continue
                where = sites.maps_with_site(site)
                if not where:
                    err(rpath, f"site '{site}' does not exist in any map", sid)
                elif sites.default_map and sites.default_map not in where and not has_change_map:
                    warn(rpath, f"site '{site}' is not in the default map '{sites.default_map}' (found in {', '.join(where)})", sid)
        if step.type == "nav.follow_route" and sites is not None:
            mapdef = sites.map(sites.default_map)
            to = step.params.get("to")
            if mapdef is not None and isinstance(to, str) and not to.startswith("$") and "${" not in to and to in mapdef.sites:
                if not mapdef.edges:
                    warn(path, f"map '{mapdef.name}' has no route edges yet; nav.follow_route will fail unless on_no_route is 'direct'", sid)
                elif not mapdef.neighbours().get(to):
                    warn((*path, "to"), f"site '{to}' is not connected to the route graph", sid)
        if step.type == "nav.change_map" and sites is not None:
            target = str(step.params.get("map", ""))
            if target and target not in sites.maps and not target.endswith(".yaml"):
                warn((*path, "map"), f"map '{target}' is not defined in sites.json", sid)

    # triggers and interrupts
    for kind, specs in (("triggers", m.triggers), ("interrupts", m.interrupts)):
        seen_t: set[str] = set()
        for i, t in enumerate(specs):
            path = (kind, i)
            if t.id in seen_t:
                err(path, f"duplicate {kind[:-1]} id '{t.id}'")
            seen_t.add(t.id)
            if t.type not in EVENT_TYPES:
                err((*path, "type"), f"unknown event source '{t.type}'")
            _check_expr(t.when, (*path, "when"), None, err)
            for k, v in t.set.items():
                _check_expr(v, (*path, "set", k), None, err)
                if k not in m.inputs and k not in m.vars:
                    warn((*path, "set", k), f"'{k}' is not a declared input; it will still be set as a variable")
            if t.policy not in POLICIES:
                err((*path, "policy"), f"unknown policy '{t.policy}'")
            if t.type in CONNECTOR_EVENT_TYPES and connectors is not None and t.connector not in connectors:
                warn((*path, "connector"), f"connector '{t.connector}' is not configured on the robot")
            if kind == "interrupts":
                if not t.run:
                    err((*path, "run"), "interrupt needs 'run'")
                elif t.run == m.name:
                    err((*path, "run"), "an interrupt cannot run its own mission")
                elif missions is not None and t.run not in missions:
                    err((*path, "run"), f"unknown mission '{t.run}'")
            if t.type == "mission.done" and missions is not None and t.params.get("mission") not in missions:
                warn((*path, "mission"), f"unknown mission '{t.params.get('mission')}'")
            if trigger_capabilities is not None:
                cap = trigger_capabilities.get(t.type)
                if cap is not None and not cap.get("available", True):
                    warn(path, f"'{t.type}' is not available on this robot: {cap.get('reason', '')}".rstrip(": "))

    # required inputs without default need a trigger 'set' or a caller
    for name, inp in m.inputs.items():
        if inp.required and inp.default is None:
            if not any(name in t.set for t in m.triggers) and m.triggers:
                warn(("inputs", name), f"required input '{name}' is not set by any trigger")

    return errors, warnings, m


def _check_expr(expr: Any, path: tuple[Any, ...], step_id: str | None, err: Any) -> None:
    if expr is None or expr == "":
        return
    try:
        parse_expression(str(expr))
    except ExpressionError as e:
        err(path, str(e), step_id)


def check_mission_cycles(missions: dict[str, Mission]) -> list[Finding]:
    """Detect run_mission / interrupt cycles across a set of missions."""
    graph: dict[str, set[str]] = {}
    for name, m in missions.items():
        targets: set[str] = set()
        for s in m.iter_steps():
            if s.type == "run_mission" and isinstance(s.params.get("mission"), str):
                targets.add(s.params["mission"])
        graph[name] = targets
    findings: list[Finding] = []
    state: dict[str, int] = {}

    def visit(n: str, stack: list[str]) -> None:
        state[n] = 1
        for t in graph.get(n, ()):
            if state.get(t) == 1:
                findings.append(Finding("error", ["flow"], f"run_mission cycle: {' -> '.join([*stack, n, t])}"))
            elif state.get(t, 0) == 0 and t in graph:
                visit(t, [*stack, n])
        state[n] = 2

    for n in graph:
        if state.get(n, 0) == 0:
            visit(n, [])
    return findings
