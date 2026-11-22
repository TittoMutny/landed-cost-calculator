"""One table listing every value with its Modbus address and OPC UA NodeId,
for the SCADA / PCS 7 engineer setting up tags."""
from __future__ import annotations

import csv
import io

from ..config import Config
from .modbus_server import register_map
from .opcua_server import METER_VARS

_OPC_SITE = {"Site total power": "Site.TotalPower_kW", "Site energy today": "Site.Today_kWh",
             "Site energy this month": "Site.ThisMonth_kWh", "Meters online": "Site.MetersOnline",
             "Meters configured": "Site.MetersTotal", "Heartbeat counter": "Site.Heartbeat"}
_OPC_METER = dict(zip(["Power", "Energy counter", "Voltage", "Current", "Power factor", "Energy today",
                       "Energy this month"], [n for n, _ in METER_VARS]))
_OPC_METER["Online (1/0)"] = "Online"


def tag_rows(cfg: Config) -> list[dict]:
    ids = {f"{m.name} ({m.id})": m.id for m in cfg.meters}
    rows = []
    for r in register_map([(m.id, m.name) for m in cfg.meters]):
        if r["meter"] == "Site":
            node = _OPC_SITE.get(r["value"], "")
        else:
            leaf = _OPC_METER.get(r["value"])
            node = f"Meters.{ids[r['meter']]}.{leaf}" if leaf else ""
        rows.append({**r, "opcua": f"ns=<urn:flexpower:ems>;s={node}" if node else ""})
    return rows


def tags_csv(cfg: Config) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Meter", "Value", "Unit", "Modbus address (0-based, FC03/FC04)", "Modbus type", "OPC UA NodeId"])
    for r in tag_rows(cfg):
        w.writerow([r["meter"], r["value"], r["unit"], r["address"], r["type"], r["opcua"]])
    return buf.getvalue().encode("utf-8-sig")
