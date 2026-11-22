import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "flexpower-license-server"))
from license_server.crypto import public_key_b64, sign_token  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from ems.config import load_config  # noqa: E402
from ems.db import Database  # noqa: E402
from ems.licensing import LicenseManager  # noqa: E402

KEY = Ed25519PrivateKey.generate()
PUB = public_key_b64(KEY)

REAL = """
site: {name: Test, timezone: UTC, currency: USD}
storage: {database: data/t.sqlite3}
collector: {poll_seconds: 60}
meters:
  - {id: m1, name: Meter 1, protocol: tcp, host: 127.0.0.1, registers: {power_kw: {address: 0}}}
  - {id: m2, name: Meter 2, protocol: tcp, host: 127.0.0.1, registers: {power_kw: {address: 0}}}
  - {id: m3, name: Meter 3, protocol: tcp, host: 127.0.0.1, registers: {power_kw: {address: 0}}}
"""
SIM = """
site: {name: Test, timezone: UTC, currency: USD}
storage: {database: data/t.sqlite3}
meters:
  - {id: s1, name: Sim, protocol: simulated}
"""


def cfg_for(tmp_path, body=REAL, extra=""):
    p = tmp_path / "config.yaml"
    p.write_text(body + extra, encoding="utf-8")
    return load_config(p)


def install(lm: LicenseManager, key=KEY, **over):
    now = int(time.time())
    payload = {"v": 1, "iat": now, "exp": now + 7 * 86400, "machine": lm.machine, "status": "active", "reason": "",
               "customer": "Acme", "plan": "pro", "paid_until": "2099-01-01", "pause_on": None, "max_meters": 2,
               "features": {"reports": True, "integrations": True}, "support": {"name": "Flexpower", "phone": "+254 700"}}
    payload.update(over)
    lm._saved = {"key": "FLX-TEST-TEST-TEST-ABCD", "token": sign_token(key, payload), "max_seen_iat": payload["iat"]}
    return lm


def test_states_from_signed_tokens(tmp_path):
    lm = LicenseManager(cfg_for(tmp_path), public_key=PUB)
    assert lm.state().status == "unlicensed"
    install(lm)
    st = lm.state()
    assert st.status == "active" and st.usable and st.feature("integrations") and st.key_hint == "ABCD"
    assert lm.allowed_meter_ids() == {"m1", "m2"}  # plan limit: first 2 meters

    install(lm, status="overdue", reason="pay soon")
    assert lm.state().usable and lm.state().status == "overdue"

    install(lm, status="paused", reason="Invoice unpaid")
    st = lm.state()
    assert not st.usable and st.reason == "Invoice unpaid"
    assert lm.allowed_meter_ids() == {"m1", "m2"}  # still recording while paused

    install(lm, status="cancelled")
    assert lm.allowed_meter_ids() == set()  # nothing recorded once cancelled


def test_forged_and_misused_tokens_are_rejected(tmp_path):
    lm = LicenseManager(cfg_for(tmp_path), public_key=PUB)
    install(lm, key=Ed25519PrivateKey.generate())  # signed by someone else
    assert lm.state().status == "invalid"
    install(lm)
    body, sig = lm._saved["token"].split(".")
    lm._saved["token"] = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig  # edited payload
    assert lm.state().status == "invalid"
    install(lm, machine="another-pc")
    assert "different PC" in lm.state().reason
    install(lm, exp=int(time.time()) - 60)  # offline too long
    st = lm.state()
    assert st.status == "paused" and "licence server" in st.reason
    install(lm, iat=int(time.time()) + 86400)  # PC clock set back a day
    assert "clock" in lm.state().reason


def test_demo_mode_with_only_simulated_meters(tmp_path):
    lm = LicenseManager(cfg_for(tmp_path, SIM), public_key=PUB)
    st = lm.state()
    assert st.status == "demo" and st.usable and st.feature("integrations")
    assert lm.allowed_meter_ids() == {"s1"}


def test_enforcement_in_web_reports_integrations_collector(tmp_path):
    from fastapi.testclient import TestClient

    from ems.collector import Collector
    from ems.integrations import IntegrationManager
    from ems.reports import run_scheduled_report
    from ems.web import create_app

    cfg = cfg_for(tmp_path)
    db = Database(cfg.db_path)
    lm = install(LicenseManager(cfg, public_key=PUB), status="paused", reason="Invoice unpaid")
    client = TestClient(create_app(cfg, db, None, lm))
    client.get("/login")  # guest session (no users configured)
    r = client.get("/api/summary?period=today")
    assert r.status_code == 402 and r.json()["license"]["status"] == "paused"
    conf = client.get("/api/config").json()
    assert conf["license"]["status"] == "paused" and conf["license"]["support"]["name"] == "Flexpower"
    assert "key" not in conf["license"] and "machine" not in str(conf["license"])
    assert client.get("/api/health").status_code == 200

    assert run_scheduled_report(cfg, db, "weekly", license=lm)["status"] == "skipped"

    mgr = IntegrationManager(cfg, db, lm)

    class Spy:
        running, last_error, name = True, None, "spy"
        calls = 0

        def publish(self, snap):
            Spy.calls += 1
    spy = Spy()
    mgr.plugins = [spy]
    mgr.publish({"ts": 0, "site": {}, "meters": []})
    assert Spy.calls == 0 and "paused" in spy.last_error

    polled = []
    col = Collector(cfg, db, license=lm)
    col.poll_once = Collector.poll_once.__get__(col)

    class FakeClient:
        connected = True

        def read_holding_registers(self, *a, **k):
            polled.append(1)
            raise IOError("stop here")

        def close(self):
            pass
    col.poll_once(cfg.meters, FakeClient())
    assert len(polled) == 2  # paused: still records, but only the 2 meters in the plan

    install(lm)  # active again
    assert client.get("/api/summary?period=today").status_code == 200
    install(lm, features={"reports": False, "integrations": False})
    assert run_scheduled_report(cfg, db, "weekly", license=lm)["reason"].startswith("email reports are not included")


# ------------------------------------------------------------------ end to end with a real licence server
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def license_server(tmp_path):
    import uvicorn
    from license_server.app import ServerConfig, create_app, hash_password
    from license_server.crypto import load_key

    d = tmp_path / "srv"
    d.mkdir()
    (d / "license-server.yaml").write_text(f"""
admins:
  - {{username: staff, password_hash: "{hash_password('staff-pass-1234', 1000)}"}}
support: {{name: Flexpower, phone: "+254 700 000 000"}}
""")
    cfg = ServerConfig(d / "license-server.yaml")
    cfg.key_path.parent.mkdir(parents=True)
    cfg.key_path.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    assert public_key_b64(load_key(cfg.key_path)) == PUB
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


def test_end_to_end_activate_pause_resume(tmp_path, license_server):
    import httpx

    admin = httpx.Client(base_url=license_server)
    admin.post("/admin/api/login", json={"username": "staff", "password": "staff-pass-1234"}).raise_for_status()
    h = {"X-CSRF-Token": admin.get("/admin/api/me").json()["csrf"]}
    cust = admin.post("/admin/api/customers", json={"name": "Acme", "plan": "basic", "max_meters": 2}, headers=h).json()

    cfg = cfg_for(tmp_path, extra=f"license: {{server_url: '{license_server}'}}\n")
    lm = LicenseManager(cfg, public_key=PUB)
    with pytest.raises(RuntimeError, match="Unknown licence key"):
        lm.activate("FLX-WRONG-KEY")
    st = lm.activate(cust["license_key"])
    assert st.status == "active" and st.max_meters == 2 and not st.feature("integrations")
    assert LicenseManager(cfg, public_key=PUB).state().status == "active"  # survives restart (saved token)

    admin.post(f"/admin/api/customers/{cust['id']}/pause", json={"reason": "Invoice 7 unpaid"}, headers=h)
    st = lm.check_now()
    assert st.status == "paused" and st.reason == "Invoice 7 unpaid"

    admin.post(f"/admin/api/customers/{cust['id']}/payment", json={"months": 1, "reference": "MPESA123"}, headers=h)
    assert lm.check_now().status == "active"

    # server unreachable: the cached signed answer keeps working, and the error is recorded
    lm.server = "http://127.0.0.1:1"
    st = lm.check_now()
    assert st.status == "active" and st.last_error


def test_machine_id_is_stable(tmp_path, monkeypatch):
    import subprocess
    import ems.licensing as L

    code = "import sys; sys.path.insert(0, '.'); from ems.licensing import machine_id; print(machine_id())"
    runs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=Path(__file__).resolve().parents[1]).stdout for _ in range(2)}
    assert len(runs) == 1  # same id in separate processes
    monkeypatch.setattr(L, "_os_machine_id", lambda: None)  # no OS id: falls back to a saved one
    a = L.machine_id(tmp_path)
    assert a == L.machine_id(tmp_path) and (tmp_path / "machine-id").exists()


def test_dashboard_activation_endpoint(tmp_path, license_server):
    import httpx
    from fastapi.testclient import TestClient

    from ems.web import create_app

    admin = httpx.Client(base_url=license_server)
    admin.post("/admin/api/login", json={"username": "staff", "password": "staff-pass-1234"}).raise_for_status()
    h = {"X-CSRF-Token": admin.get("/admin/api/me").json()["csrf"]}
    key = admin.post("/admin/api/customers", json={"name": "Web Co", "plan": "pro"}, headers=h).json()["license_key"]
    cfg = cfg_for(tmp_path, extra=f"license: {{server_url: '{license_server}'}}\n")
    db = Database(cfg.db_path)
    client = TestClient(create_app(cfg, db, None, LicenseManager(cfg, public_key=PUB)))
    client.get("/login")
    csrf = client.get("/api/config").json()["csrf"]
    assert client.get("/api/summary").status_code == 402  # unlicensed
    r = client.post("/api/license/activate", json={"key": "FLX-AAAA-BBBB-CCCC-DDDD"}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400 and "Unknown licence key" in r.json()["detail"]
    assert client.post("/api/license/activate", json={"key": key}).status_code == 403  # CSRF token required
    r = client.post("/api/license/activate", json={"key": key}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert client.get("/api/summary").status_code == 200


# ------------------------------------------------------------------ M-Pesa through the dashboard
@pytest.fixture
def mpesa_server(tmp_path, monkeypatch):
    import uvicorn
    from license_server.app import ServerConfig, create_app, hash_password
    from license_server.mpesa import Daraja, MpesaSettings

    for k, v in {"MPESA_CONSUMER_KEY": "ck", "MPESA_CONSUMER_SECRET": "cs", "MPESA_PASSKEY": "pk",
                 "MPESA_CALLBACK_SECRET": "c" * 24}.items():
        monkeypatch.setenv(k, v)
    d = tmp_path / "srv2"
    d.mkdir()
    (d / "license-server.yaml").write_text(f"""
admins:
  - {{username: staff, password_hash: "{hash_password('staff-pass-1234', 1000)}"}}
plans:
  basic: {{max_meters: 10, price_month: 5000, features: {{reports: true, integrations: false}}}}
mpesa: {{enabled: true, shortcode: "174379", c2b_shortcode: "600000", public_base_url: "https://lic.example",
        allowed_ips: [], verify_stk_with_query: false, reconcile_in_background: false}}
""")
    cfg = ServerConfig(d / "license-server.yaml")
    cfg.key_path.parent.mkdir(parents=True)
    cfg.key_path.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))

    class Fake(Daraja):
        pushes = []

        def stk_push(self, phone, amount, ref, desc):
            Fake.pushes.append((phone, amount, ref))
            return {"ResponseCode": "0", "CheckoutRequestID": f"ws_CO_{len(Fake.pushes)}", "CustomerMessage": "ok"}

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, daraja=Fake(MpesaSettings(cfg.raw["mpesa"]))),
                                           host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", Fake
    server.should_exit = True
    t.join(5)


def test_paused_customer_pays_with_mpesa_from_dashboard(tmp_path, mpesa_server):
    import httpx
    from fastapi.testclient import TestClient
    from datetime import date, timedelta

    from ems.web import create_app

    url, fake = mpesa_server
    admin = httpx.Client(base_url=url)
    admin.post("/admin/api/login", json={"username": "staff", "password": "staff-pass-1234"}).raise_for_status()
    h = {"X-CSRF-Token": admin.get("/admin/api/me").json()["csrf"]}
    cust = admin.post("/admin/api/customers", json={"name": "Pay Co", "plan": "basic",
                      "paid_until": (date.today() - timedelta(days=30)).isoformat()}, headers=h).json()

    cfg = cfg_for(tmp_path, extra=f"license: {{server_url: '{url}'}}\n")
    lm = LicenseManager(cfg, public_key=PUB)
    assert lm.activate(cust["license_key"]).status == "paused"
    client = TestClient(create_app(cfg, Database(cfg.db_path), None, lm))
    client.get("/login")
    conf = client.get("/api/config").json()
    assert conf["license"]["billing"]["account_no"] == "FLX0001" and conf["license"]["billing"]["mpesa"]
    assert client.get("/api/summary").status_code == 402

    r = client.post("/api/license/pay", json={"phone": "0712 345 678", "months": 1}, headers={"X-CSRF-Token": conf["csrf"]})
    assert r.status_code == 200, r.text
    assert fake.pushes[-1] == ("254712345678", 5000, "FLX0001")
    cid = r.json()["checkout_id"]
    assert client.get(f"/api/license/pay/{cid}").json()["status"] == "pending"

    # Safaricom confirms the payment
    httpx.post(f"{url}/mpesa/stk-callback/{'c' * 24}", json={"Body": {"stkCallback": {
        "CheckoutRequestID": cid, "ResultCode": 0, "ResultDesc": "ok",
        "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 5000}, {"Name": "MpesaReceiptNumber", "Value": "SKL1MN2OP3"}]}}}})
    s = client.get(f"/api/license/pay/{cid}").json()
    assert s["status"] == "completed" and s["license"]["status"] == "active"
    assert client.get("/api/summary").status_code == 200  # unlocked without anyone at Flexpower doing anything
    assert client.post("/api/license/pay", json={"phone": "0712345678"}).status_code == 403  # CSRF required
