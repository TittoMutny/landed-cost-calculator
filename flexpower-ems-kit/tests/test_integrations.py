import asyncio
import socket
import struct
import time
from datetime import datetime
from pathlib import Path

import pytest

from ems.auth import hash_password
from ems.config import load_config
from ems.db import Database
from ems.integrations import IntegrationManager
from ems.integrations.modbus_server import METER_BASE, METER_STRIDE, ModbusTcpIntegration, register_map

BASE = """
site: {name: Test, timezone: UTC, currency: USD}
storage: {database: data/t.sqlite3}
collector: {poll_seconds: 60}
meters:
  - {id: main, name: Main, protocol: simulated, main: true}
  - {id: a, name: Machine A, group: Line 1, protocol: simulated}
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make(tmp_path, extra=""):
    p = tmp_path / "config.yaml"
    p.write_text(BASE + extra, encoding="utf-8")
    cfg = load_config(p)
    db = Database(cfg.db_path)
    now = int(time.time())
    db.ingest("main", now - 60, {"energy_kwh": 1000.0, "power_kw": 10.0})
    db.ingest("main", now, {"energy_kwh": 1002.5, "power_kw": 12.5, "voltage_v": 400.0, "pf": 0.95})
    db.ingest("a", now, {"energy_kwh": 50.0, "power_kw": 4.0})
    return cfg, db


def floats(regs, i):
    return struct.unpack(">f", struct.pack(">HH", regs[i], regs[i + 1]))[0]


def test_snapshot_contents(tmp_path):
    cfg, db = make(tmp_path)
    snap = IntegrationManager(cfg, db).build_snapshot()
    main, a = snap["meters"]
    assert main["power_kw"] == 12.5 and main["online"] and main["today_kwh"] == pytest.approx(2.5)
    assert a["index"] == 1 and a["power_kw"] == 4.0
    assert snap["site"]["power_kw"] == 12.5  # site total comes from the main meter only
    assert snap["site"]["today_kwh"] == pytest.approx(2.5) and snap["site"]["meters_total"] == 2


def test_modbus_server_serves_values_and_refuses_writes(tmp_path):
    from pymodbus.client import ModbusTcpClient

    port = free_port()
    cfg, db = make(tmp_path, f"integrations:\n  - {{type: modbus_tcp, enabled: true, host: 127.0.0.1, port: {port}}}\n")
    mgr = IntegrationManager(cfg, db)
    mgr.start()
    try:
        mgr.publish(mgr.build_snapshot())
        mgr.publish(mgr.build_snapshot())
        c = ModbusTcpClient("127.0.0.1", port=port)
        assert c.connect()
        site = c.read_holding_registers(0, count=10, device_id=1).registers
        assert floats(site, 0) == pytest.approx(12.5) and site[6] == 2 and site[7] == 2 and site[8] >= 2
        a = c.read_input_registers(METER_BASE + METER_STRIDE, count=20, device_id=1).registers  # FC04 = same data
        assert floats(a, 0) == pytest.approx(4.0) and floats(a, 2) == pytest.approx(50.0) and a[16] == 1
        assert (a[14] << 16 | a[15]) == 500  # energy x10 as uint32
        import math
        assert math.isnan(floats(a, 4))  # voltage not measured on this meter -> NaN
        assert c.write_register(0, 123, device_id=1).isError()          # writes are refused
        assert c.read_holding_registers(0, count=10, device_id=1).registers[0] == site[0]
        assert c.read_holding_registers(10_000, count=2, device_id=1).isError()  # beyond the table
        c.close()
        assert mgr.status()[0]["running"] and mgr.status()[0]["last_publish"]
    finally:
        mgr.stop()


def test_modbus_allow_ips_and_little_word_order(tmp_path):
    from pymodbus.client import ModbusTcpClient

    port = free_port()
    cfg, db = make(tmp_path)
    p = ModbusTcpIntegration(cfg, {"host": "127.0.0.1", "port": port, "allow_ips": ["10.0.0.0/8"], "word_order": "little"})
    p.start()
    try:
        regs = p.table.encode(1.5, "float32")
        assert struct.unpack(">f", struct.pack(">HH", regs[1], regs[0]))[0] == 1.5
        c = ModbusTcpClient("127.0.0.1", port=port, retries=0, timeout=1)
        c.connect()
        with pytest.raises(Exception):
            r = c.read_holding_registers(0, count=1, device_id=1)
            assert not r.isError()
        c.close()
    finally:
        p.stop()


def test_register_map_layout():
    rows = register_map([("main", "Main"), ("a", "Machine A")])
    a_power = next(r for r in rows if r["meter"] == "Machine A (a)" and r["value"] == "Power")
    assert a_power["address"] == METER_BASE + METER_STRIDE and a_power["type"] == "float32"


def test_custom_plugin_from_plugins_folder(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "my_plugin.py").write_text(Path(__file__).parent.parent.joinpath("plugins/example_csv_export.py").read_text())
    cfg, db = make(tmp_path, """integrations:
  - {type: custom, enabled: true, class: "my_plugin:CsvExport", path: out/live.csv}
  - {type: nonsense, enabled: true}
  - {type: custom, enabled: true, class: "my_plugin:Nope"}
""")
    mgr = IntegrationManager(cfg, db)
    mgr.start()
    mgr.publish(mgr.build_snapshot())
    mgr.stop()
    text = (tmp_path / "out" / "live.csv").read_text()
    assert "Machine A" in text and "power_kw" in text
    errors = [s for s in mgr.status() if s["error"]]
    assert len(errors) == 2 and "unknown integration type" in errors[0]["error"]


def opc_cfg(tmp_path, port, extra=""):
    return make(tmp_path, f"""integrations:
  - type: opcua
    enabled: true
    endpoint: "opc.tcp://127.0.0.1:{port}/flexpower/"
    security: none
{extra}""")


def test_opcua_server_publishes_readonly_tags(tmp_path):
    from asyncua import Client, ua

    port = free_port()
    cfg, db = opc_cfg(tmp_path, port, "    allow_anonymous: true\n")
    mgr = IntegrationManager(cfg, db)
    mgr.start()
    try:
        assert mgr.status()[0]["running"], mgr.status()
        mgr.publish(mgr.build_snapshot())
        time.sleep(1.5)

        async def check():
            async with Client(f"opc.tcp://127.0.0.1:{port}/flexpower/") as c:
                ns = await c.get_namespace_index("urn:flexpower:ems")
                power = c.get_node(ua.NodeId("Meters.a.Power_kW", ns))
                assert await power.read_value() == pytest.approx(4.0)
                assert await c.get_node(ua.NodeId("Meters.main.Online", ns)).read_value() is True
                assert await c.get_node(ua.NodeId("Site.TotalPower_kW", ns)).read_value() == pytest.approx(12.5)
                assert await c.get_node(ua.NodeId("Meters.a.Name", ns)).read_value() == "Machine A"
                volts = await c.get_node(ua.NodeId("Meters.main.Voltage_V", ns)).read_data_value()
                assert volts.StatusCode.is_good() and volts.Value.Value == 400.0
                with pytest.raises(ua.UaStatusCodeError):
                    await power.write_value(99.0)  # read-only for every client
        asyncio.run(check())
    finally:
        mgr.stop()


def test_opcua_requires_valid_login(tmp_path):
    from asyncua import Client, ua

    port = free_port()
    cfg, db = opc_cfg(tmp_path, port, f"""    allow_anonymous: false
    users:
      - {{username: scada, password_hash: "{hash_password('scada-pass-123')}"}}
""")
    mgr = IntegrationManager(cfg, db)
    mgr.start()
    try:
        assert mgr.status()[0]["running"], mgr.status()
        url = f"opc.tcp://127.0.0.1:{port}/flexpower/"

        async def check():
            for user, pw in ((None, None), ("scada", "wrong")):
                c = Client(url)
                if user:
                    c.set_user(user); c.set_password(pw)
                with pytest.raises(Exception):
                    async with c:
                        pass
            c = Client(url)
            c.set_user("scada"); c.set_password("scada-pass-123")
            async with c:
                ns = await c.get_namespace_index("urn:flexpower:ems")
                assert await c.get_node(ua.NodeId("Meters.a.Group", ns)).read_value() == "Line 1"
        asyncio.run(check())
    finally:
        mgr.stop()


def test_opcua_sign_encrypt_generates_certificate(tmp_path):
    port = free_port()
    cfg, db = make(tmp_path, f"""integrations:
  - {{type: opcua, enabled: true, endpoint: "opc.tcp://127.0.0.1:{port}/flexpower/", security: sign_encrypt, allow_anonymous: true}}
""")
    mgr = IntegrationManager(cfg, db)
    mgr.start()
    try:
        assert mgr.status()[0]["running"], mgr.status()
        assert (tmp_path / "data" / "opcua" / "server_cert.der").exists()

        async def endpoints():
            from asyncua import Client
            c = Client(f"opc.tcp://127.0.0.1:{port}/flexpower/")
            return [e.SecurityPolicyUri for e in await c.connect_and_get_server_endpoints()]
        uris = asyncio.run(endpoints())
        assert any("Basic256Sha256" in u for u in uris) and not any(u.endswith("#None") for u in uris)
    finally:
        mgr.stop()


# ------------------------------------------------------------------ PROFINET gateway layout
def test_profinet_gateway_layout_and_status(tmp_path):
    from pymodbus.client import ModbusTcpClient
    from ems.integrations.profinet_gateway import METER_REGS, SITE_REGS

    port = free_port()
    cfg, db = make(tmp_path, f"""integrations:
  - {{type: profinet_gateway, enabled: true, host: 127.0.0.1, port: {port}, meters: [a, main]}}
""")
    # make meter "main" look offline: last reading 1 hour ago
    with db.connect() as con:
        con.execute("UPDATE meter_state SET last_ok_ts = last_ok_ts - 3600 WHERE meter_id='main'")
    mgr = IntegrationManager(cfg, db)
    mgr.start()
    try:
        mgr.publish(mgr.build_snapshot())
        c = ModbusTcpClient("127.0.0.1", port=port)
        assert c.connect()
        regs = c.read_holding_registers(0, count=SITE_REGS + 2 * METER_REGS, device_id=1).registers
        raw = b"".join(r.to_bytes(2, "big") for r in regs)  # exactly what lands in the PLC input area
        heartbeat, online = struct.unpack(">hh", raw[0:4])
        assert heartbeat >= 1 and online == 1 and struct.unpack(">h", raw[16:18])[0] == 2
        # meter 0 = "a" (order from `meters:`), at byte 24
        status, age = struct.unpack(">Hh", raw[24:28])
        power, energy, volts = struct.unpack(">fff", raw[28:40])
        assert status == 0b11 and 0 <= age < 5 and power == pytest.approx(4.0) and energy == pytest.approx(50.0)
        assert volts == 0.0  # not measured -> 0.0, never NaN
        # meter 1 = "main", offline: status 0, live values zeroed, today's kWh kept
        m = 24 + 32
        status, = struct.unpack(">H", raw[m:m + 2])
        power, = struct.unpack(">f", raw[m + 4:m + 8])
        today, = struct.unpack(">f", raw[m + 24:m + 28])
        assert status == 0 and power == 0.0 and today == pytest.approx(2.5)
        assert c.write_register(0, 1, device_id=1).isError()
        c.close()
    finally:
        mgr.stop()


def test_profinet_read_commands_and_s7_source(tmp_path):
    from ems.integrations.profinet_gateway import gateway_notes, io_bytes, read_commands, s7_source

    assert read_commands(6) == [(0, 108)]
    cmds = read_commands(20)  # 12 + 320 registers
    assert cmds[0] == (0, 124) and all(c <= 125 for _, c in cmds)
    assert sum(c for _, c in cmds) == 12 + 16 * 20
    assert all((s - 12) % 16 == 0 for s, _ in cmds[1:])  # never split a meter between commands
    assert io_bytes(20) == 664
    cfg, _ = make(tmp_path)
    src = s7_source(cfg, {"udt_number": 7, "db_number": 42})
    assert "TYPE UDT 7" in src and "DATA_BLOCK DB 42" in src
    assert "   a : UDT 7 ;" in src and "(offset 56.0)" in src and "END_DATA_BLOCK" in src
    notes = gateway_notes(cfg, {"host": "10.0.0.5"})
    assert "10.0.0.5:502" in notes and "swap: OFF" in notes


def test_profinet_gateway_size_check(tmp_path):
    cfg, db = make(tmp_path, """integrations:
  - {type: profinet_gateway, enabled: true, host: 127.0.0.1, port: 1, gateway_io_bytes: 64}
  - {type: profinet_gateway, enabled: true, meters: [nope]}
""")
    mgr = IntegrationManager(cfg, db)
    mgr.start()
    errs = [s["error"] for s in mgr.status()]
    mgr.stop()
    assert any("88 bytes needed" in (e or "") for e in errs)
    assert any("unknown meter" in (e or "") for e in errs)
