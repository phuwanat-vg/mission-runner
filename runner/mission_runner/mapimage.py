"""Reads a ROS map (``.yaml`` + ``.pgm``/``.png``) and hands it to the editor as
a PNG plus the metadata needed to place it in world coordinates.

Only the standard library is used: PGM is parsed by hand and a greyscale PNG is
written with ``zlib``, so the robot needs no image library. When no map file
exists (``--sim``, or a map that was never saved) :func:`synthetic_map` draws a
room around the known sites so the editor still has a floor to show.
"""

from __future__ import annotations

import math
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["MapImage", "load_map", "synthetic_map", "encode_png_gray"]


@dataclass
class MapImage:
    """A map ready for the editor. ``png`` is greyscale, row 0 = top."""

    png: bytes
    width: int
    height: int
    resolution: float
    origin: tuple[float, float, float]
    source: str  # file path, or "synthetic"

    def meta(self) -> dict[str, Any]:
        """World placement of the image: ``bounds`` is [min_x, min_y, max_x, max_y]."""
        ox, oy, _yaw = self.origin
        return {
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "origin": {"x": ox, "y": oy, "yaw_deg": math.degrees(self.origin[2])},
            "bounds": [ox, oy, ox + self.width * self.resolution, oy + self.height * self.resolution],
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# PNG


def encode_png_gray(pixels: bytes, width: int, height: int) -> bytes:
    """Minimal 8-bit greyscale PNG encoder (no dependencies)."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        raw += pixels[y * width : (y + 1) * width]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# PGM / YAML


def _read_pgm(path: Path) -> tuple[bytes, int, int]:
    data = path.read_bytes()
    if not data.startswith((b"P5", b"P2")):
        raise ValueError(f"{path.name}: not a binary or ASCII PGM")
    binary = data.startswith(b"P5")
    # Header: magic, width, height, maxval - with '#' comments anywhere between.
    pos = 2
    fields: list[int] = []
    while len(fields) < 3:
        while pos < len(data) and data[pos : pos + 1].isspace():
            pos += 1
        if data[pos : pos + 1] == b"#":
            while pos < len(data) and data[pos : pos + 1] not in (b"\n", b"\r"):
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos : pos + 1].isspace():
            pos += 1
        fields.append(int(data[start:pos]))
    width, height, maxval = fields
    pos += 1  # single whitespace after maxval
    if binary:
        if maxval > 255:
            raise ValueError(f"{path.name}: 16-bit PGM is not supported")
        pixels = data[pos : pos + width * height]
    else:
        values = [int(v) for v in data[pos:].split()][: width * height]
        pixels = bytes(min(255, v) for v in values)
    if len(pixels) < width * height:
        raise ValueError(f"{path.name}: truncated ({len(pixels)} of {width * height} pixels)")
    return pixels, width, height


_YAML_NUM = r"[-+0-9.eE]+"


def _parse_map_yaml(text: str) -> dict[str, Any]:
    """Small parser for the flat map.yaml ROS writes (no PyYAML needed)."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("["):
            out[key] = [float(v) for v in re.findall(_YAML_NUM, value)]
        elif re.fullmatch(_YAML_NUM, value):
            out[key] = float(value)
        else:
            out[key] = value.strip("'\"")
    return out


def load_map(yaml_path: str | Path) -> MapImage:
    """Load a ROS map. Raises OSError / ValueError when it cannot be read."""
    path = Path(yaml_path).expanduser()
    doc = _parse_map_yaml(path.read_text("utf-8"))
    image = doc.get("image")
    if not image:
        raise ValueError(f"{path.name}: no 'image' field")
    img_path = Path(str(image))
    if not img_path.is_absolute():
        img_path = path.parent / img_path
    resolution = float(doc.get("resolution", 0.05))
    origin = doc.get("origin") or [0.0, 0.0, 0.0]
    negate = bool(int(doc.get("negate", 0)))

    if img_path.suffix.lower() == ".png":
        png = img_path.read_bytes()
        width, height = struct.unpack(">II", png[16:24])
        return MapImage(png, width, height, resolution, (float(origin[0]), float(origin[1]), float(origin[2] if len(origin) > 2 else 0.0)), str(img_path))

    pixels, width, height = _read_pgm(img_path)
    if negate:
        pixels = bytes(255 - p for p in pixels)
    return MapImage(
        encode_png_gray(pixels, width, height),
        width,
        height,
        resolution,
        (float(origin[0]), float(origin[1]), float(origin[2] if len(origin) > 2 else 0.0)),
        str(img_path),
    )


# ---------------------------------------------------------------------------
# synthetic


def synthetic_map(points: list[tuple[float, float]], resolution: float = 0.05, margin_m: float = 2.0) -> MapImage:
    """A plain room that contains ``points``, so the editor has a floor to draw
    on before a real map is recorded. Free space is white, the wall is grey."""
    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        min_x, max_x = min(xs) - margin_m, max(xs) + margin_m
        min_y, max_y = min(ys) - margin_m, max(ys) + margin_m
    else:
        min_x, max_x, min_y, max_y = -6.0, 6.0, -6.0, 6.0
    width = max(16, min(2000, int((max_x - min_x) / resolution)))
    height = max(16, min(2000, int((max_y - min_y) / resolution)))
    wall = max(1, int(0.1 / resolution))
    row_free = bytes([254]) * width
    pixels = bytearray()
    for y in range(height):
        if y < wall or y >= height - wall:
            pixels += bytes([120]) * width
        else:
            row = bytearray(row_free)
            row[:wall] = bytes([120]) * wall
            row[-wall:] = bytes([120]) * wall
            pixels += row
    return MapImage(encode_png_gray(bytes(pixels), width, height), width, height, resolution, (min_x, min_y, 0.0), "synthetic")
