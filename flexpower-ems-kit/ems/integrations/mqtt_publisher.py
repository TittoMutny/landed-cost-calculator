"""MQTT publisher: sends EMS values to an MQTT broker (Mosquitto, HiveMQ, EMQX,
Ignition MQTT Engine, Node-RED, AWS IoT, Azure IoT Hub...).

Topics (prefix defaults to "flexpower/<site>"):

  <prefix>/status                 "online" / "offline" (retained; set to
                                  "offline" by the broker if the EMS disappears)
  <prefix>/site                   {"ts":..., "power_kw":..., "today_kwh":..., ...}
  <prefix>/meters/<meter id>      {"ts":..., "name":..., "online":..., "power_kw":..., ...}

With `format: values` or `both`, each value also goes to its own topic, e.g.
  <prefix>/meters/compressor/power_kw   "6.72"
which suits simple subscribers (Node-RED, SCADA MQTT drivers, Home Assistant).

The plugin only publishes. It never subscribes, so nothing sent to the broker
can change the EMS.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import QUANTITIES, Integration

log = logging.getLogger("ems.integrations.mqtt")

SITE_KEYS = ["power_kw", "today_kwh", "month_kwh", "meters_online", "meters_total"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-") or "site"


def topic_safe(text: str) -> str:
    # '+', '#' and '/' have special meaning in MQTT topics
    return re.sub(r"[+#/\s]+", "_", text)


class MqttIntegration(Integration):
    type_name = "mqtt"

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        self.host = str(options.get("host", "localhost"))
        self.tls = bool(options.get("tls", True))
        self.port = int(options.get("port", 8883 if self.tls else 1883))
        self.prefix = str(options.get("topic_prefix") or f"flexpower/{slug(cfg.site_name)}").rstrip("/")
        self.fmt = str(options.get("format", "json")).lower()
        if self.fmt not in ("json", "values", "both"):
            raise ValueError("mqtt format must be json, values or both")
        self.qos = int(options.get("qos", 1))
        if self.qos not in (0, 1, 2):
            raise ValueError("mqtt qos must be 0, 1 or 2")
        self.retain = bool(options.get("retain", True))
        self.only_changes = bool(options.get("only_changes", False))
        self.client_id = str(options.get("client_id") or f"flexpower-ems-{slug(cfg.site_name)}")
        self.username = options.get("username")
        self.connected = False
        self.client = None
        self._last: dict[str, str] = {}
        self._lock = threading.Lock()

    def describe(self) -> str:
        scheme = "mqtts" if self.tls else "mqtt"
        return f"{scheme}://{self.host}:{self.port}  topics {self.prefix}/#"

    def status(self):
        s = super().status()
        s["connected"] = self.connected
        return s

    def _password(self) -> str | None:
        import os

        from ..credentials import load_secret

        env = self.options.get("password_env")
        if env and os.environ.get(env):
            return os.environ[env]
        p = Path(self.options.get("password_file") or "data/mqtt.secret")
        p = p if p.is_absolute() else self.cfg.base_dir / p
        return load_secret(p)

    def _path(self, key: str) -> str | None:
        v = self.options.get(key)
        if not v:
            return None
        p = Path(v)
        return str(p if p.is_absolute() else self.cfg.base_dir / p)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id, clean_session=True)
        if self.username:
            pw = self._password()
            if pw is None:
                raise ValueError("mqtt: username set but no password; run `python -m ems set-mqtt-password`")
            c.username_pw_set(self.username, pw)
        if self.tls:
            c.tls_set(ca_certs=self._path("ca_file"), certfile=self._path("client_cert"),
                      keyfile=self._path("client_key"), cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            if self.options.get("tls_insecure"):  # skips hostname check; testing only
                c.tls_insecure_set(True)
        c.will_set(f"{self.prefix}/status", "offline", qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        self.client = c
        c.connect_async(self.host, self.port, keepalive=int(self.options.get("keepalive", 60)))
        c.loop_start()  # background thread; reconnects automatically
        self.running = True

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            self.connected = False
            self.last_error = f"broker refused connection: {reason_code}"
            log.warning("mqtt: %s", self.last_error)
            return
        self.connected = True
        self.last_error = None
        with self._lock:
            self._last.clear()  # resend everything after a reconnect
        client.publish(f"{self.prefix}/status", "online", qos=1, retain=True)
        log.info("mqtt: connected to %s:%s", self.host, self.port)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        if reason_code != 0:
            log.warning("mqtt: disconnected (%s), reconnecting", reason_code)

    def stop(self) -> None:
        if self.client is not None:
            try:
                if self.connected:
                    self.client.publish(f"{self.prefix}/status", "offline", qos=1, retain=True).wait_for_publish(3)
                self.client.disconnect()
            finally:
                self.client.loop_stop()
        self.running = False

    # ------------------------------------------------------------- data
    def messages(self, snap: dict[str, Any]) -> list[tuple[str, str]]:
        ts = datetime.fromtimestamp(snap["ts"], timezone.utc).isoformat().replace("+00:00", "Z")
        out: list[tuple[str, str]] = []
        site = {"ts": ts, "site": self.cfg.site_name, **{k: snap["site"].get(k) for k in SITE_KEYS}}
        meters = []
        for m in snap["meters"]:
            meters.append((topic_safe(m["id"]), {
                "ts": ts, "id": m["id"], "name": m["name"], "group": m["group"], "main": m["main"],
                "online": m["online"], "age_s": m["age_s"], **{q: m.get(q) for q in QUANTITIES}}))
        if self.fmt in ("json", "both"):
            out.append((f"{self.prefix}/site", json.dumps(site, separators=(",", ":"))))
            out += [(f"{self.prefix}/meters/{mid}", json.dumps(body, separators=(",", ":"))) for mid, body in meters]
        if self.fmt in ("values", "both"):
            out += [(f"{self.prefix}/site/{k}", self._plain(site[k])) for k in SITE_KEYS]
            for mid, body in meters:
                out += [(f"{self.prefix}/meters/{mid}/{k}", self._plain(body[k])) for k in ["online", *QUANTITIES]]
        return out

    @staticmethod
    def _plain(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    def publish(self, snap: dict[str, Any]) -> None:
        if not self.connected:
            raise ConnectionError(self.last_error or f"not connected to {self.host}:{self.port}")
        msgs = self.messages(snap)
        for topic, payload in msgs:
            if self.only_changes:
                # JSON payloads carry a timestamp, so compare without it
                key = payload.split(',"', 1)[1] if payload.startswith('{"ts"') else payload
                with self._lock:
                    if self._last.get(topic) == key:
                        continue
                    self._last[topic] = key
            self.client.publish(topic, payload, qos=self.qos, retain=self.retain)
