"""Turns zones into Nav2 costmap filter masks.

Nav2 reads filter masks with the same `map_server` machinery as a map: a
`.pgm` plus a `.yaml`. Two masks are produced from a map's zones:

* **keepout** (`mode: trinary`) - every `keepout` zone is painted occupied, so
  the planner refuses to route through it.
* **speed limit** (`mode: percent`) - every `speed_limit` zone is painted with
  the percentage of the robot's maximum speed allowed inside it.

Point the `costmap_filter_info_server` at the mask that fits the filter you
enabled; the file paths come back from `POST /api/maps/{name}/filters`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .model import Zone

__all__ = ["MaskSpec", "MaskResult", "rasterize", "render_keepout", "render_speed_limit", "write_mask"]

FREE = 254
OCCUPIED = 0


@dataclass(slots=True)
class MaskSpec:
    """Where the mask lives in the world."""

    min_x: float
    min_y: float
    width: int
    height: int
    resolution: float

    @classmethod
    def from_bounds(cls, bounds: tuple[float, float, float, float], resolution: float = 0.05, margin_m: float = 1.0) -> MaskSpec:
        min_x, min_y, max_x, max_y = bounds
        min_x -= margin_m
        min_y -= margin_m
        max_x += margin_m
        max_y += margin_m
        width = max(4, min(4000, int(round((max_x - min_x) / resolution))))
        height = max(4, min(4000, int(round((max_y - min_y) / resolution))))
        return cls(min_x, min_y, width, height, resolution)

    def world_to_cell(self, x: float, y: float) -> tuple[float, float]:
        """Column, row in mask pixels. Row 0 is the top, i.e. max_y."""
        col = (x - self.min_x) / self.resolution
        row = self.height - (y - self.min_y) / self.resolution
        return col, row

    @property
    def origin(self) -> tuple[float, float, float]:
        return (self.min_x, self.min_y, 0.0)


@dataclass(slots=True)
class MaskResult:
    pgm: bytes
    yaml: str
    spec: MaskSpec
    zones: list[str]
    kind: str


def rasterize(pixels: bytearray, spec: MaskSpec, polygon: list[tuple[float, float]], value: int) -> None:
    """Scanline-fill a world-coordinate polygon into ``pixels`` (row-major, row 0 = top)."""
    if len(polygon) < 3:
        return
    cells = [spec.world_to_cell(x, y) for x, y in polygon]
    top = max(0, int(min(r for _c, r in cells)))
    bottom = min(spec.height - 1, int(max(r for _c, r in cells)) + 1)
    n = len(cells)
    for row in range(top, bottom + 1):
        y = row + 0.5
        crossings: list[float] = []
        for i in range(n):
            c1, r1 = cells[i]
            c2, r2 = cells[(i + 1) % n]
            if (r1 > y) != (r2 > y):
                t = (y - r1) / (r2 - r1)
                crossings.append(c1 + t * (c2 - c1))
        crossings.sort()
        for i in range(0, len(crossings) - 1, 2):
            start = max(0, int(crossings[i] + 0.5))
            end = min(spec.width - 1, int(crossings[i + 1] - 0.5))
            if end < start:
                # Sliver thinner than a cell: still mark the cell it passes through.
                mid = int((crossings[i] + crossings[i + 1]) / 2)
                if 0 <= mid < spec.width:
                    pixels[row * spec.width + mid] = value
                continue
            base = row * spec.width
            for col in range(start, end + 1):
                pixels[base + col] = value


def _pgm(pixels: bytes, width: int, height: int, comment: str) -> bytes:
    return b"P5\n# %s\n%d %d\n255\n" % (comment.encode("ascii", "replace"), width, height) + pixels


def _yaml(image_name: str, spec: MaskSpec, mode: str) -> str:
    ox, oy, oyaw = spec.origin
    return (
        f"image: {image_name}\n"
        f"resolution: {spec.resolution}\n"
        f"origin: [{ox:.4f}, {oy:.4f}, {oyaw:.4f}]\n"
        "negate: 0\n"
        "occupied_thresh: 0.65\n"
        "free_thresh: 0.196\n"
        f"mode: {mode}\n"
    )


def zone_bounds(zones: Iterable[Zone], fallback: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    boxes = [z.bounds() for z in zones]
    if not boxes:
        return fallback
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def render_keepout(zones: Iterable[Zone], spec: MaskSpec, image_name: str = "keepout_mask.pgm") -> MaskResult:
    """Keep-out zones as an occupied area. ``mode: trinary``."""
    keepouts = [z for z in zones if z.kind == "keepout"]
    pixels = bytearray([FREE]) * (spec.width * spec.height)
    for z in keepouts:
        rasterize(pixels, spec, z.polygon, OCCUPIED)
    return MaskResult(
        _pgm(bytes(pixels), spec.width, spec.height, f"mission_runner keepout mask: {', '.join(z.name for z in keepouts) or 'none'}"),
        _yaml(image_name, spec, "trinary"),
        spec,
        [z.name for z in keepouts],
        "keepout",
    )


def render_speed_limit(zones: Iterable[Zone], spec: MaskSpec, max_speed_mps: float, image_name: str = "speed_mask.pgm") -> MaskResult:
    """Speed-limit zones as percentages of ``max_speed_mps``. ``mode: percent``.

    In percent mode Nav2 reads the cell value as "percent of maximum speed",
    where a free (254) cell means no limit."""
    limited = [z for z in zones if z.kind == "speed_limit" and z.speed_mps]
    pixels = bytearray([FREE]) * (spec.width * spec.height)
    for z in limited:
        percent = max(1.0, min(100.0, (float(z.speed_mps or 0.0) / max(0.01, max_speed_mps)) * 100.0))
        # map_server percent mode: value = 100 - (cell / 254 * 100), so invert.
        value = int(round(254 * (100.0 - percent) / 100.0))
        rasterize(pixels, spec, z.polygon, max(0, min(254, value)))
    return MaskResult(
        _pgm(bytes(pixels), spec.width, spec.height, f"mission_runner speed mask (max {max_speed_mps} m/s): {', '.join(z.name for z in limited) or 'none'}"),
        _yaml(image_name, spec, "percent"),
        spec,
        [z.name for z in limited],
        "speed_limit",
    )


def write_mask(result: MaskResult, directory: Path, stem: str) -> dict[str, str]:
    """Write ``<stem>.pgm`` and ``<stem>.yaml`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    pgm_path = directory / f"{stem}.pgm"
    yaml_path = directory / f"{stem}.yaml"
    pgm_path.write_bytes(result.pgm)
    mode = "trinary" if result.kind == "keepout" else "percent"
    yaml_path.write_text(_yaml(pgm_path.name, result.spec, mode), encoding="utf-8")
    return {"yaml": str(yaml_path), "image": str(pgm_path), "zones": ", ".join(result.zones), "kind": result.kind}
