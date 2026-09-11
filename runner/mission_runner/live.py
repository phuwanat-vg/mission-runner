"""Live robot data for the map: costmap, laser scan, the path Nav2 is following
and the footprint.

Layers are subscribed on demand - a client asks for them over the WebSocket and
the relay subscribes to the matching ROS topics only while somebody is looking,
so an idle robot pays nothing. Everything is converted to map-frame world
coordinates and rate limited before it leaves the robot, so the editor can draw
it without knowing anything about TF.
"""

from __future__ import annotations

import base64
import logging
import math
import time
from typing import TYPE_CHECKING, Any

from .events import EventBus

if TYPE_CHECKING:
    from .backends.sim import SimBackend

log = logging.getLogger("mission.live")

#: layer -> minimum seconds between emissions
RATE_LIMIT = {"costmap": 1.0, "scan": 0.5, "plan": 0.5, "footprint": 0.5}
LAYERS = tuple(RATE_LIMIT)
MAX_SCAN_POINTS = 360
MAX_PLAN_POSES = 300


class LiveRelay:
    """Base: rate limiting and the layer set."""

    def __init__(self, events: EventBus):
        self.events = events
        self.layers: set[str] = set()
        self._last: dict[str, float] = {}

    def available(self) -> dict[str, bool]:
        return {layer: False for layer in LAYERS}

    def set_layers(self, layers: set[str]) -> None:
        wanted = {x for x in layers if x in LAYERS}
        if wanted == self.layers:
            return
        added, removed = wanted - self.layers, self.layers - wanted
        self.layers = wanted
        self._apply(added, removed)

    def _apply(self, added: set[str], removed: set[str]) -> None:
        pass

    def close(self) -> None:
        self.set_layers(set())

    def _allow(self, layer: str) -> bool:
        now = time.monotonic()
        if now - self._last.get(layer, 0.0) < RATE_LIMIT[layer]:
            return False
        self._last[layer] = now
        return True

    def emit(self, layer: str, **data: Any) -> None:
        if layer in self.layers and self._allow(layer):
            self.events.emit_threadsafe(f"live.{layer}", layer=layer, **data)


class SimLiveRelay(LiveRelay):
    """What a simulated robot can honestly show: its footprint and the path it
    is following. Costmap and scan need real sensors."""

    def __init__(self, events: EventBus, backend: SimBackend, footprint_m: float = 0.35):
        super().__init__(events)
        self.backend = backend
        self.footprint_m = footprint_m

    def available(self) -> dict[str, bool]:
        return {"costmap": False, "scan": False, "plan": True, "footprint": True}

    def tick(self) -> None:
        """Called by the runner's robot ticker."""
        if not self.layers:
            return
        st = self.backend.robot_state()
        if st.x is None or st.y is None:
            return
        if "footprint" in self.layers:
            yaw = math.radians(st.yaw_deg or 0.0)
            half = self.footprint_m
            corners = [(half, half * 0.7), (half, -half * 0.7), (-half, -half * 0.7), (-half, half * 0.7)]
            pts = [[round(st.x + cx * math.cos(yaw) - cy * math.sin(yaw), 3), round(st.y + cx * math.sin(yaw) + cy * math.cos(yaw), 3)] for cx, cy in corners]
            self.emit("footprint", points=pts, frame="map")


class RosLiveRelay(LiveRelay):
    """Subscribes to Nav2's topics while a client is watching."""

    def __init__(self, events: EventBus, node: Any, cb_group: Any, tf_buffer: Any, topics: dict[str, str] | None = None, map_frame: str = "map"):
        super().__init__(events)
        self.node = node
        self.cb_group = cb_group
        self.tf = tf_buffer
        self.map_frame = map_frame
        self.topics = {
            "costmap": "/local_costmap/costmap",
            "scan": "/scan",
            "plan": "/plan",
            "footprint": "/local_costmap/published_footprint",
            **(topics or {}),
        }
        self._subs: dict[str, Any] = {}

    def available(self) -> dict[str, bool]:
        return {layer: True for layer in LAYERS}

    def _apply(self, added: set[str], removed: set[str]) -> None:
        for layer in removed:
            sub = self._subs.pop(layer, None)
            if sub is not None:
                try:
                    self.node.destroy_subscription(sub)
                except Exception:  # noqa: BLE001
                    pass
        for layer in added:
            try:
                self._subs[layer] = self._subscribe(layer)
            except Exception as e:  # noqa: BLE001
                log.warning("live layer '%s' unavailable: %s", layer, e)

    def _subscribe(self, layer: str) -> Any:
        from nav_msgs.msg import OccupancyGrid, Path
        from geometry_msgs.msg import PolygonStamped
        from sensor_msgs.msg import LaserScan
        from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

        topic = self.topics[layer]
        if layer == "costmap":
            qos = QoSProfile(depth=1, reliability=QoSReliabilityPolicy.RELIABLE, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
            return self.node.create_subscription(OccupancyGrid, topic, self._on_costmap, qos, callback_group=self.cb_group)
        if layer == "scan":
            return self.node.create_subscription(LaserScan, topic, self._on_scan, 1, callback_group=self.cb_group)
        if layer == "plan":
            return self.node.create_subscription(Path, topic, self._on_plan, 1, callback_group=self.cb_group)
        return self.node.create_subscription(PolygonStamped, topic, self._on_footprint, 1, callback_group=self.cb_group)

    # ----- conversions ----------------------------------------------------------

    def _lookup(self, frame: str) -> tuple[float, float, float] | None:
        """map <- frame as (x, y, yaw)."""
        if not frame or frame.lstrip("/") == self.map_frame:
            return (0.0, 0.0, 0.0)
        try:
            import rclpy

            t = self.tf.lookup_transform(self.map_frame, frame.lstrip("/"), rclpy.time.Time())
        except Exception:  # noqa: BLE001 - TF not ready
            return None
        q = t.transform.rotation
        return (float(t.transform.translation.x), float(t.transform.translation.y), 2.0 * math.atan2(q.z, q.w))

    def _on_costmap(self, msg: Any) -> None:
        if "costmap" not in self.layers or not self._allow("costmap"):
            return
        from .mapimage import encode_png_gray

        info = msg.info
        w, h = int(info.width), int(info.height)
        if w * h == 0 or w * h > 4_000_000:
            return
        # Cost 0..100 and -1 unknown -> greyscale the editor can tint.
        table = bytes([0 if v < 0 else min(255, int(v * 255 / 100)) for v in range(-1, 101)])
        data = msg.data
        pixels = bytearray(w * h)
        for row in range(h):  # flip: OccupancyGrid row 0 is min_y, images start at max_y
            src = (h - 1 - row) * w
            pixels[row * w : (row + 1) * w] = bytes(table[min(100, max(-1, data[src + c])) + 1] for c in range(w))
        q = info.origin.orientation
        self.events.emit_threadsafe(
            "live.costmap",
            layer="costmap",
            png=base64.b64encode(encode_png_gray(bytes(pixels), w, h)).decode("ascii"),
            width=w,
            height=h,
            resolution=float(info.resolution),
            origin={"x": float(info.origin.position.x), "y": float(info.origin.position.y), "yaw_deg": math.degrees(2.0 * math.atan2(q.z, q.w))},
            bounds=[
                float(info.origin.position.x),
                float(info.origin.position.y),
                float(info.origin.position.x) + w * float(info.resolution),
                float(info.origin.position.y) + h * float(info.resolution),
            ],
            frame=msg.header.frame_id,
        )

    def _on_scan(self, msg: Any) -> None:
        if "scan" not in self.layers or not self._allow("scan"):
            return
        tf = self._lookup(msg.header.frame_id)
        if tf is None:
            return
        ox, oy, oyaw = tf
        ranges = msg.ranges
        n = len(ranges)
        if n == 0:
            return
        stride = max(1, n // MAX_SCAN_POINTS)
        points: list[list[float]] = []
        for i in range(0, n, stride):
            r = ranges[i]
            if not math.isfinite(r) or r < msg.range_min or r > msg.range_max:
                continue
            a = oyaw + msg.angle_min + i * msg.angle_increment
            points.append([round(ox + r * math.cos(a), 3), round(oy + r * math.sin(a), 3)])
        self.events.emit_threadsafe("live.scan", layer="scan", points=points, frame=self.map_frame, origin=[round(ox, 3), round(oy, 3)])

    def _on_plan(self, msg: Any) -> None:
        if "plan" not in self.layers or not self._allow("plan"):
            return
        tf = self._lookup(msg.header.frame_id)
        if tf is None:
            return
        ox, oy, oyaw = tf
        poses = msg.poses
        stride = max(1, len(poses) // MAX_PLAN_POSES)
        out: list[list[float]] = []
        for i in range(0, len(poses), stride):
            p = poses[i].pose.position
            x, y = float(p.x), float(p.y)
            out.append([round(ox + x * math.cos(oyaw) - y * math.sin(oyaw), 3), round(oy + x * math.sin(oyaw) + y * math.cos(oyaw), 3)])
        self.events.emit_threadsafe("live.plan", layer="plan", points=out, frame=self.map_frame)

    def _on_footprint(self, msg: Any) -> None:
        if "footprint" not in self.layers or not self._allow("footprint"):
            return
        tf = self._lookup(msg.header.frame_id)
        if tf is None:
            return
        ox, oy, oyaw = tf
        pts = [
            [round(ox + float(p.x) * math.cos(oyaw) - float(p.y) * math.sin(oyaw), 3), round(oy + float(p.x) * math.sin(oyaw) + float(p.y) * math.cos(oyaw), 3)]
            for p in msg.polygon.points
        ]
        self.events.emit_threadsafe("live.footprint", layer="footprint", points=pts, frame=self.map_frame)
