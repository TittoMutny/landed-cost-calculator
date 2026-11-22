"""Plugins that publish EMS data to other systems (SCADA, PCS 7, historians...).

Every few seconds the IntegrationManager builds one snapshot of the site and
all meters and hands it to each enabled plugin. Plugins only *publish*: none of
them can change meters, settings or stored data.

Built-in plugin types (configured under `integrations:` in config.yaml):
    opcua             OPC UA server - PCS 7 OS / WinCC, Ignition, AVEVA, Kepware...
    profinet_gateway  compact Modbus TCP layout for a Modbus TCP -> PROFINET gateway (PCS 7 AS)
    modbus_tcp        read-only Modbus TCP server - any SCADA, PLCs

Your own plugin: subclass Integration, put the file in the `plugins/` folder
next to config.yaml, and configure  type: custom, class: "my_file:MyClass".
"""
from __future__ import annotations

import importlib
import logging
import sys
import threading
import time
from datetime import datetime
from typing import Any

from .. import analytics
from ..config import Config
from ..db import Database

log = logging.getLogger("ems.integrations")

# Quantities every meter exposes, in a fixed order (SCADA tags are built from this).
QUANTITIES = ["power_kw", "energy_kwh", "voltage_v", "current_a", "pf", "today_kwh", "month_kwh"]


class Integration:
    """Base class for a publishing plugin.

    `options` is the plugin's own block from config.yaml. Implement start(),
    stop() and publish(); publish() is called from a background thread with a
    fresh snapshot (see IntegrationManager.build_snapshot for its layout).
    """

    type_name = "custom"

    def __init__(self, cfg: Config, options: dict[str, Any]):
        self.cfg = cfg
        self.options = options
        self.name = options.get("name") or self.type_name
        self.last_error: str | None = None
        self.last_publish: int | None = None
        self.running = False

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def publish(self, snapshot: dict[str, Any]) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        """One line shown in the dashboard, e.g. the endpoint URL."""
        return ""

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type_name, "running": self.running,
                "endpoint": self.describe(), "last_publish": self.last_publish, "error": self.last_error}


def builtin_types() -> dict[str, type[Integration]]:
    from .modbus_server import ModbusTcpIntegration
    from .opcua_server import OpcUaIntegration
    from .profinet_gateway import ProfinetGatewayIntegration
    return {"opcua": OpcUaIntegration, "modbus_tcp": ModbusTcpIntegration,
            "profinet_gateway": ProfinetGatewayIntegration, "mqtt": _mqtt_class()}


def _mqtt_class():
    from .mqtt_publisher import MqttIntegration
    return MqttIntegration


def profinet_options(cfg: Config) -> dict[str, Any]:
    """The profinet_gateway block from config.yaml (enabled or not), or defaults."""
    for opts in cfg.raw.get("integrations") or []:
        if isinstance(opts, dict) and opts.get("type") == "profinet_gateway":
            return opts
    return {"type": "profinet_gateway"}


def load_plugin_class(cfg: Config, spec: str) -> type[Integration]:
    """Import "module:Class" from the plugins/ folder (or any installed package)."""
    if ":" not in spec:
        raise ValueError(f"custom plugin class must look like 'module:ClassName', got '{spec}'")
    plugins_dir = str(cfg.base_dir / "plugins")
    if plugins_dir not in sys.path:
        sys.path.insert(0, plugins_dir)
    module_name, class_name = spec.split(":", 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    if not (isinstance(cls, type) and issubclass(cls, Integration)):
        raise TypeError(f"{spec} is not a subclass of ems.integrations.Integration")
    return cls


class IntegrationManager:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.plugins: list[Integration] = []
        self.errors: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._publish_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.interval = 5.0
        types = builtin_types()
        for i, opts in enumerate(cfg.raw.get("integrations") or []):
            if not isinstance(opts, dict) or not opts.get("enabled", False):
                continue
            kind = str(opts.get("type", ""))
            try:
                cls = load_plugin_class(cfg, opts.get("class", "")) if kind == "custom" else types[kind]
                self.plugins.append(cls(cfg, opts))
            except KeyError:
                self._fail(i, kind, f"unknown integration type '{kind}' (use opcua, modbus_tcp, profinet_gateway or custom)")
            except Exception as exc:  # noqa: BLE001 - a broken plugin must not stop the EMS
                self._fail(i, kind, f"could not load: {exc}")
        intervals = [float(p.options.get("update_seconds", 5)) for p in self.plugins]
        self.interval = max(1.0, min(intervals)) if intervals else 5.0

    def _fail(self, i: int, kind: str, msg: str) -> None:
        log.error("integrations[%d] (%s): %s", i, kind, msg)
        self.errors.append({"name": f"integrations[{i}]", "type": kind, "running": False, "endpoint": "",
                            "last_publish": None, "error": msg})

    # ------------------------------------------------------------------ data
    def build_snapshot(self) -> dict[str, Any]:
        """Current values for the site and each meter (in config order)."""
        cfg, tz = self.cfg, self.cfg.tz
        now = datetime.now(tz)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month0 = day0.replace(day=1)
        with self.db.connect() as con:
            sums = {}
            for label, since in (("today_kwh", day0), ("month_kwh", month0)):
                for r in con.execute("SELECT meter_id, SUM(kwh) AS k FROM intervals WHERE start >= ? GROUP BY meter_id",
                                     (int(since.timestamp()),)):
                    sums.setdefault(r["meter_id"], {})[label] = r["k"] or 0.0
        live = analytics.live(cfg, self.db)
        ts = live["ts"]
        meters = []
        for i, lm in enumerate(live["meters"]):
            v = lm["values"]
            meters.append({
                "index": i, "id": lm["id"], "name": lm["name"], "group": lm["group"], "main": lm["main"],
                "online": lm["online"], "age_s": (ts - lm["last_seen"]) if lm["last_seen"] else None,
                "power_kw": v.get("power_kw"), "energy_kwh": v.get("energy_kwh"), "voltage_v": v.get("voltage_v"),
                "current_a": v.get("current_a"), "pf": v.get("pf"),
                "today_kwh": round(sums.get(lm["id"], {}).get("today_kwh", 0.0), 3),
                "month_kwh": round(sums.get(lm["id"], {}).get("month_kwh", 0.0), 3),
            })
        site_ids = {m.id for m in cfg.meters if m.main} or {m.id for m in cfg.meters}
        site = {
            "power_kw": live["site_kw"],
            "today_kwh": round(sum(m["today_kwh"] for m in meters if m["id"] in site_ids), 3),
            "month_kwh": round(sum(m["month_kwh"] for m in meters if m["id"] in site_ids), 3),
            "meters_online": live["online"], "meters_total": live["total"],
        }
        return {"ts": ts, "site": site, "meters": meters}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        for p in self.plugins:
            try:
                p.start()
                log.info("integration %s started %s", p.name, p.describe())
            except Exception as exc:  # noqa: BLE001
                p.running, p.last_error = False, str(exc)
                log.error("integration %s failed to start: %s", p.name, exc)
        if self.plugins:
            self._thread = threading.Thread(target=self._loop, name="integrations", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self.build_snapshot()
            except Exception as exc:  # noqa: BLE001
                log.exception("building integration snapshot failed: %s", exc)
                snap = None
            if snap is not None:
                self.publish(snap)
            self._stop.wait(self.interval)

    def publish(self, snap: dict[str, Any]) -> None:
        with self._publish_lock:  # plugins never see two publishes at once
            for p in self.plugins:
                if not p.running:
                    continue
                try:
                    p.publish(snap)
                    p.last_publish, p.last_error = int(time.time()), None
                except Exception as exc:  # noqa: BLE001
                    p.last_error = str(exc)
                    log.warning("integration %s publish failed: %s", p.name, exc)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        for p in self.plugins:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                log.exception("stopping %s", p.name)

    def status(self) -> list[dict[str, Any]]:
        return [p.status() for p in self.plugins] + self.errors
