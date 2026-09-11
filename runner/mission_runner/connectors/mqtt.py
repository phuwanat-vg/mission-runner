"""MQTT connector (paho-mqtt 1.x or 2.x). Works against a local mosquitto on
the robot as well as a cloud broker; reconnects on its own."""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from ..model import EventSource
from ..types import StepFailed
from .base import Armed, Connector, EventCallback

log = logging.getLogger("mission.mqtt")

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None  # type: ignore[assignment]


class MqttConnector(Connector):
    type = "mqtt"
    source_types = ("mqtt.subscribe",)

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        super().__init__(name, config)
        self._client: Any = None
        self._connected = False
        self._lock = threading.Lock()
        #: topic filter -> list of callbacks
        self._subs: dict[str, list[EventCallback]] = {}

    @property
    def available(self) -> bool:
        return mqtt is not None

    @property
    def unavailable_reason(self) -> str:
        return "" if mqtt is not None else "python package 'paho-mqtt' is not installed"

    @property
    def connected(self) -> bool:
        return self._connected

    def _credentials(self) -> tuple[str | None, str | None]:
        c = self.config
        user = c.get("username") or (os.environ.get(str(c["username_env"])) if c.get("username_env") else None)
        pw = c.get("password") or (os.environ.get(str(c["password_env"])) if c.get("password_env") else None)
        return user, pw

    async def start(self) -> None:
        if mqtt is None:
            log.warning("connector %s: paho-mqtt not installed", self.name)
            return
        c = self.config
        client_id = str(c.get("client_id") or f"mission-runner-{os.getpid()}")
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=bool(c.get("clean_session", True)))  # type: ignore[attr-defined]
        except AttributeError:  # paho 1.x
            client = mqtt.Client(client_id=client_id, clean_session=bool(c.get("clean_session", True)))
        user, pw = self._credentials()
        if user:
            client.username_pw_set(user, pw)
        if bool(c.get("tls", False)):
            client.tls_set()
            if c.get("tls_insecure"):
                client.tls_insecure_set(True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        will = c.get("will")
        if isinstance(will, dict) and will.get("topic"):
            client.will_set(str(will["topic"]), str(will.get("payload", "OFFLINE")), int(will.get("qos", 1)), bool(will.get("retain", True)))
        self._client = client
        host = str(c.get("host", "localhost"))
        port = int(c.get("port", 8883 if c.get("tls") else 1883))
        try:
            client.connect_async(host, port, int(c.get("keepalive", 60)))
            client.loop_start()
        except Exception as e:  # noqa: BLE001
            log.warning("connector %s: connect to %s:%s failed: %s (will retry)", self.name, host, port, e)

    async def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._connected = False

    # ----- paho callbacks (paho thread) ----------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: Any, *args: Any) -> None:
        code = getattr(rc, "value", rc)
        ok = code == 0 or str(rc) == "Success"
        self._connected = bool(ok)
        log.info("connector %s: %s", self.name, "connected" if ok else f"connect failed ({rc})")
        if ok:
            with self._lock:
                for topic in self._subs:
                    client.subscribe(topic, qos=int(self.config.get("qos", 1)))
            online = self.config.get("online")
            if isinstance(online, dict) and online.get("topic"):
                client.publish(str(online["topic"]), str(online.get("payload", "ONLINE")), int(online.get("qos", 1)), bool(online.get("retain", True)))

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self._connected = False
        log.warning("connector %s: disconnected", self.name)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        payload = _parse_payload(msg.payload)
        payload["topic"] = msg.topic
        with self._lock:
            targets = [cb for filt, cbs in self._subs.items() if mqtt.topic_matches_sub(filt, msg.topic) for cb in cbs]  # type: ignore[union-attr]
        for cb in targets:
            try:
                cb(dict(payload))
            except Exception:  # noqa: BLE001
                log.exception("mqtt callback failed")

    # ----- connector API -------------------------------------------------------------

    async def arm(self, source: EventSource, callback: EventCallback) -> Armed:
        if source.type != "mqtt.subscribe":
            raise StepFailed(f"mqtt cannot arm '{source.type}'")
        topic = str(source.params.get("topic", "")).strip()
        if not topic:
            raise StepFailed("mqtt.subscribe needs a topic")
        with self._lock:
            first = topic not in self._subs
            self._subs.setdefault(topic, []).append(callback)
        if first and self._client is not None and self._connected:
            self._client.subscribe(topic, qos=int(self.config.get("qos", 1)))

        def disarm() -> None:
            with self._lock:
                cbs = self._subs.get(topic, [])
                if callback in cbs:
                    cbs.remove(callback)
                empty = not cbs
                if empty:
                    self._subs.pop(topic, None)
            if empty and self._client is not None and self._connected:
                try:
                    self._client.unsubscribe(topic)
                except Exception:  # noqa: BLE001
                    pass

        return Armed(disarm, source.summary())

    async def publish(self, topic: str, payload: Any, qos: int = 1, retain: bool = False) -> None:
        if self._client is None:
            raise StepFailed(f"connector '{self.name}' is not started")
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False)
        elif payload is None:
            data = ""
        elif isinstance(payload, bool):
            data = "true" if payload else "false"
        else:
            data = str(payload)
        info = self._client.publish(topic, data, qos=qos, retain=retain)
        rc = getattr(info, "rc", 0)
        if rc != 0 and not self._connected:
            raise StepFailed(f"mqtt '{self.name}' is not connected (queued publish failed, rc={rc})")


def _parse_payload(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"bytes": raw.hex()}
    text_stripped = text.strip()
    if text_stripped[:1] in ("{", "[", '"') or text_stripped in ("true", "false", "null") or _looks_numeric(text_stripped):
        try:
            value = json.loads(text_stripped)
            if isinstance(value, dict):
                return value
            return {"value": value, "text": text}
        except ValueError:
            pass
    return {"text": text}


def _looks_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False
