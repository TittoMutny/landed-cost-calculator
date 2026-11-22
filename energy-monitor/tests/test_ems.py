import socket
import struct
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from ems import analytics, reports
from ems.auth import hash_password, verify_password
from ems.collector import decode_registers, make_client, plan_blocks, read_meter
from ems.config import ConfigError, Register, load_config
from ems.db import Database


def write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


BASE = """
site: {name: Test, timezone: UTC, currency: USD, co2_kg_per_kwh: 0.5}
tariff:
  rate_per_kwh: 0.10
  peak: {rate_per_kwh: 0.20, start: "08:00", end: "18:00", weekdays_only: true}
storage: {database: data/t.sqlite3}
collector: {poll_seconds: 60}
"""


@pytest.fixture
def cfg(tmp_path):
    return load_config(write_config(tmp_path, BASE + """
meters:
  - {id: main, name: Main, protocol: simulated, main: true}
  - {id: a, name: Machine A, group: Line 1, protocol: simulated}
  - {id: b, name: Machine B, group: Line 2, protocol: simulated}
"""))


# ------------------------------------------------------------------ decoding
def regs_of(fmt, value):
    raw = struct.pack(fmt, value)
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


@pytest.mark.parametrize("typ,fmt,val", [
    ("float32", ">f", 1234.5), ("float64", ">d", -98765.4321), ("uint16", ">H", 65000),
    ("int16", ">h", -1200), ("uint32", ">I", 4_000_000_000), ("int32", ">i", -2_000_000),
    ("uint64", ">Q", 12_345_678_901_234), ("int64", ">q", -5),
])
def test_decode_big_endian(typ, fmt, val):
    assert decode_registers(regs_of(fmt, val), typ) == pytest.approx(val)


def test_decode_little_word_order():
    regs = regs_of(">f", 230.25)
    assert decode_registers(list(reversed(regs)), "float32", "little") == pytest.approx(230.25)


def test_plan_blocks_merges_nearby_and_splits_functions():
    regs = [Register("a", 0, "float32", "input"), Register("b", 6, "float32", "input"),
            Register("c", 300, "float32", "input"), Register("d", 0, "uint16", "holding")]
    blocks = {(fn, start, count) for fn, start, count, _ in plan_blocks(regs)}
    assert blocks == {("input", 0, 8), ("input", 300, 2), ("holding", 0, 1)}


# ------------------------------------------------------------------ config
def test_config_rejects_unknown_profile(tmp_path):
    with pytest.raises(ConfigError, match="unknown profile"):
        load_config(write_config(tmp_path, BASE + "meters:\n  - {id: x, protocol: tcp, host: 1.2.3.4, profile: nope}\n"))


def test_config_profile_with_override(tmp_path):
    c = load_config(write_config(tmp_path, BASE + """
profiles:
  p: {registers: {energy_kwh: {address: 72, function: input}, power_kw: {address: 52, function: input}}}
meters:
  - {id: x, protocol: rtu, serial_port: COM3, profile: p, registers: {energy_kwh: {address: 342, function: input}}}
"""))
    regs = {r.name: r for r in c.meters[0].registers}
    assert regs["energy_kwh"].address == 342 and regs["power_kw"].address == 52
    assert c.meters[0].connection_key == "rtu://COM3"


# ------------------------------------------------------------------ storage
def test_ingest_energy_deltas_and_counter_reset(cfg):
    db = Database(cfg.db_path)
    t0 = 1_700_000_100 - 1_700_000_100 % 900
    assert db.ingest("a", t0, {"energy_kwh": 100.0, "power_kw": 5}) == 0  # first reading: baseline only
    assert db.ingest("a", t0 + 60, {"energy_kwh": 100.5, "power_kw": 7}) == pytest.approx(0.5)
    assert db.ingest("a", t0 + 120, {"energy_kwh": 3.0, "power_kw": 6}) == 0  # meter reset ignored
    assert db.ingest("a", t0 + 180, {"energy_kwh": 3.25, "power_kw": 6}) == pytest.approx(0.25)
    rows = db.intervals(t0, t0 + 900)
    assert len(rows) == 1 and rows[0]["kwh"] == pytest.approx(0.75) and rows[0]["max_kw"] == 7


def test_ingest_integrates_power_without_energy_register(cfg):
    db = Database(cfg.db_path)
    db.ingest("b", 1_000_000_800, {"power_kw": 10})
    assert db.ingest("b", 1_000_000_800 + 360, {"power_kw": 10}) == pytest.approx(1.0)  # 10 kW for 6 min


# ------------------------------------------------------------------ analytics
def seed(db, meter, start_ts, hours, kw):
    rows = [(meter, start_ts + i * 900, kw / 4, kw) for i in range(hours * 4)]
    with db.connect() as con:
        con.executemany("INSERT INTO intervals(meter_id,start,kwh,max_kw) VALUES (?,?,?,?)", rows)


def test_summary_tou_cost_shares_and_unmetered(cfg):
    db = Database(cfg.db_path)
    day = datetime(2026, 3, 2, tzinfo=cfg.tz)  # a Monday
    ts = int(day.timestamp())
    seed(db, "a", ts, 24, 3.0)   # 72 kWh
    seed(db, "b", ts, 24, 1.0)   # 24 kWh
    seed(db, "main", ts, 24, 4.5)  # 108 kWh
    s = analytics.summary(cfg, db, day, day.replace(day=3), "test")
    t = s["totals"]
    assert t["kwh"] == pytest.approx(108)
    assert t["unmetered_kwh"] == pytest.approx(12)
    # 10 peak hours at 0.20, 14 off-peak at 0.10 on the main meter
    assert t["cost"] == pytest.approx(4.5 * 10 * 0.20 + 4.5 * 14 * 0.10)
    assert [m["id"] for m in s["meters"]] == ["a", "b"]
    assert s["meters"][0]["share_pct"] == 75.0
    assert s["meters"][0]["baseload_kw"] == pytest.approx(3.0)
    assert any("Machine A was the largest" in i for i in s["insights"])


def test_timeseries_hourly_and_previous_month(cfg):
    db = Database(cfg.db_path)
    day = datetime(2026, 3, 2, tzinfo=cfg.tz)
    seed(db, "a", int(day.timestamp()), 2, 4.0)
    ts = analytics.timeseries(cfg, db, day, day.replace(day=3), "hour")
    assert len(ts["labels"]) == 24 and ts["series"]["Machine A"][:3] == [4.0, 4.0, 0.0]
    ts = analytics.timeseries(cfg, db, day, day.replace(day=3), "hour", by="group")
    assert "Line 1" in ts["series"]
    s, e = analytics.previous_range(datetime(2026, 3, 1, tzinfo=cfg.tz), datetime(2026, 4, 1, tzinfo=cfg.tz))
    assert (s.month, e.month, s.day) == (2, 3, 1)


def test_report_builds_html_and_csv(cfg):
    db = Database(cfg.db_path)
    now = datetime(2026, 4, 3, 9, tzinfo=cfg.tz)
    seed(db, "a", int(datetime(2026, 3, 1, tzinfo=cfg.tz).timestamp()), 24 * 31, 2.0)
    r = reports.build_report(cfg, db, "monthly", now)
    assert "March 2026" in r["subject"]
    assert "Machine A" in r["html"]
    names = [n for n, _ in r["attachments"]]
    assert names[0].endswith("by-meter.csv")
    assert b"Machine A" in r["attachments"][0][1]
    assert r["summary"]["meters"][0]["kwh"] == pytest.approx(2.0 * 24 * 31)


# ------------------------------------------------------------------ auth + web
def test_password_hash_roundtrip():
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "garbage")


def test_web_requires_login_and_roles(tmp_path):
    from fastapi.testclient import TestClient
    from ems.web import create_app

    body = BASE + f"""
web:
  users:
    - {{username: boss, password_hash: "{hash_password('owner-pass-123')}", role: viewer}}
    - {{username: admin, password_hash: "{hash_password('admin-pass-123')}", role: admin}}
meters:
  - {{id: a, name: Machine A, protocol: simulated}}
"""
    c = load_config(write_config(tmp_path, body))
    client = TestClient(create_app(c, Database(c.db_path)))
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", auth=("boss", "nope")).status_code == 401
    r = client.get("/api/summary?period=last_7_days", auth=("boss", "owner-pass-123"))
    assert r.status_code == 200 and "totals" in r.json()
    assert client.get("/", auth=("boss", "owner-pass-123")).status_code == 200
    assert client.get("/static/chart.umd.js").status_code == 200
    assert client.get("/static/..%2Fweb.py").status_code == 404
    assert client.post("/api/reports/send?kind=weekly", auth=("boss", "owner-pass-123")).status_code == 403
    r = client.post("/api/reports/send?kind=weekly", auth=("admin", "admin-pass-123"))
    assert r.json()["status"] == "skipped"  # email disabled in this config
    r = client.get("/api/export.csv?kind=timeseries&period=yesterday", auth=("boss", "owner-pass-123"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")


# ------------------------------------------------------------------ real Modbus TCP
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_read_meter_over_modbus_tcp(tmp_path):
    from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
    from pymodbus.server import ServerStop, StartTcpServer

    ir = [0] * 120
    ir[72:74] = regs_of(">f", 54321.5)        # energy_kwh, input registers, float32
    ir[52:54] = regs_of(">f", 7500.0)         # power in W -> scale 0.001
    hr = [0] * 20
    hr[10:12] = regs_of(">I", 4_000_000)[::-1]  # uint32 little word order
    # pymodbus data blocks are 1-based internally: protocol address 0 = block index 1
    device = ModbusDeviceContext(ir=ModbusSequentialDataBlock(1, ir), hr=ModbusSequentialDataBlock(1, hr))
    ctx = ModbusServerContext(devices={3: device}, single=False)
    port = free_port()
    th = threading.Thread(target=StartTcpServer, kwargs={"context": ctx, "address": ("127.0.0.1", port)}, daemon=True)
    th.start()
    try:
        c = load_config(write_config(tmp_path, BASE + f"""
meters:
  - id: m1
    protocol: tcp
    host: 127.0.0.1
    port: {port}
    unit_id: 3
    registers:
      energy_kwh: {{address: 72, function: input, type: float32}}
      power_kw: {{address: 52, function: input, type: float32, scale: 0.001}}
      counter: {{address: 10, function: holding, type: uint32, word_order: little}}
"""))
        meter = c.meters[0]
        client = make_client(meter)
        for _ in range(50):  # wait for the server to listen
            if client.connect():
                break
            time.sleep(0.1)
        values = read_meter(client, meter)
        client.close()
        assert values == {"energy_kwh": pytest.approx(54321.5), "power_kw": pytest.approx(7.5), "counter": 4_000_000}
    finally:
        ServerStop()
