import base64
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from license_server.app import ServerConfig, create_app, effective_status, hash_password
from license_server.crypto import generate_key


def decode(token):
    body = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


@pytest.fixture
def srv(tmp_path):
    (tmp_path / "license-server.yaml").write_text(f"""
admins:
  - {{username: staff, password_hash: "{hash_password('staff-pass-1234', 1000)}"}}
support: {{name: Flexpower, phone: "+254 700 000 000"}}
offline_days: 7
""")
    cfg = ServerConfig(tmp_path / "license-server.yaml")
    generate_key(cfg.key_path)
    client = TestClient(create_app(cfg))
    return client


def login(c):
    assert c.post("/admin/api/login", json={"username": "staff", "password": "staff-pass-1234"}).status_code == 200
    return c.get("/admin/api/me").json()["csrf"]


def new_customer(c, csrf, **kw):
    body = {"name": "Acme Steel", "plan": "pro", **kw}
    r = c.post("/admin/api/customers", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    return r.json()


def client_call(c, path, key, machine="pc-1"):
    return c.post(path, json={"key": key, "machine_id": machine, "hostname": "PLANT-PC", "version": "0.1.0", "meters": 6})


def test_effective_status_rules():
    today = date(2026, 3, 10)
    base = {"status": "active", "pause_reason": None, "grace_days": 7}
    assert effective_status({**base, "paid_until": "2026-03-31"}, today)[0] == "active"
    st, why, pause_on = effective_status({**base, "paid_until": "2026-03-05"}, today)
    assert st == "overdue" and pause_on == date(2026, 3, 13)
    assert effective_status({**base, "paid_until": "2026-03-02"}, today)[0] == "paused"  # 7 days grace used up
    assert effective_status({**base, "status": "paused", "paid_until": "2030-01-01"}, today)[0] == "paused"
    assert effective_status({**base, "status": "cancelled", "paid_until": "2030-01-01"}, today)[0] == "cancelled"


def test_admin_requires_login_and_csrf(srv):
    assert srv.get("/admin/api/customers").status_code == 401
    assert srv.post("/admin/api/login", json={"username": "staff", "password": "nope"}).status_code == 401
    csrf = login(srv)
    assert srv.post("/admin/api/customers", json={"name": "X"}).status_code == 403  # no CSRF token
    assert srv.post("/admin/api/customers", json={"name": "X"}, headers={"X-CSRF-Token": csrf}).status_code == 200


def test_admin_lockout(srv):
    for _ in range(5):
        srv.post("/admin/api/login", json={"username": "staff", "password": "bad"})
    assert srv.post("/admin/api/login", json={"username": "staff", "password": "staff-pass-1234"}).status_code == 429


def test_activation_check_pause_resume_payment(srv):
    csrf = login(srv)
    h = {"X-CSRF-Token": csrf}
    c = new_customer(srv, csrf)
    key = c["license_key"]
    assert key.startswith("FLX-") and c["max_meters"] == 50 and c["features"]["integrations"] is True
    assert "license_key" not in srv.get(f"/admin/api/customers/{c['id']}").json()  # shown once only

    assert client_call(srv, "/api/v1/check", key).status_code == 409  # must activate first
    r = client_call(srv, "/api/v1/activate", key)
    assert r.status_code == 200 and r.json()["status"] == "active"
    p = decode(r.json()["token"])
    assert p["machine"] == "pc-1" and p["max_meters"] == 50 and p["support"]["name"] == "Flexpower"
    assert p["exp"] - p["iat"] <= 7 * 86400

    # same key on another PC is refused
    r = client_call(srv, "/api/v1/activate", key, machine="pc-2")
    assert r.status_code == 409 and decode(r.json()["token"])["status"] == "invalid"

    # pause -> next check says paused (signed), resume -> active again
    srv.post(f"/admin/api/customers/{c['id']}/pause", json={"reason": "Invoice 42 unpaid"}, headers=h)
    r = client_call(srv, "/api/v1/check", key)
    assert r.json()["status"] == "paused" and decode(r.json()["token"])["reason"] == "Invoice 42 unpaid"
    srv.post(f"/admin/api/customers/{c['id']}/resume", headers=h)
    assert client_call(srv, "/api/v1/check", key).json()["status"] == "active"

    # overdue past grace -> auto paused; a payment fixes it and extends from today
    srv.patch(f"/admin/api/customers/{c['id']}", json={"paid_until": (date.today() - timedelta(days=30)).isoformat()}, headers=h)
    assert client_call(srv, "/api/v1/check", key).json()["status"] == "paused"
    r = srv.post(f"/admin/api/customers/{c['id']}/payment", json={"months": 3, "reference": "QWE123"}, headers=h).json()
    assert r["effective"] == "active" and r["paid_until"] > (date.today() + timedelta(days=85)).isoformat()
    assert client_call(srv, "/api/v1/check", key).json()["status"] == "active"

    detail = srv.get(f"/admin/api/customers/{c['id']}").json()
    actions = [e["action"] for e in detail["events"]]
    assert {"created", "activated", "paused", "resumed", "payment", "rejected"} <= set(actions)
    assert detail["hostname"] == "PLANT-PC" and detail["meters_count"] == 6


def test_overdue_token_expires_at_pause_date(srv):
    csrf = login(srv)
    c = new_customer(srv, csrf, paid_until=(date.today() - timedelta(days=2)).isoformat(), grace_days=3)
    r = client_call(srv, "/api/v1/activate", c["license_key"]).json()
    p = decode(r["token"])
    assert p["status"] == "overdue" and p["pause_on"] == (date.today() + timedelta(days=2)).isoformat()
    assert p["exp"] - p["iat"] <= 2 * 86400  # offline PC still pauses on the right day


def test_reset_activation_and_new_key(srv):
    csrf = login(srv)
    h = {"X-CSRF-Token": csrf}
    c = new_customer(srv, csrf)
    client_call(srv, "/api/v1/activate", c["license_key"])
    srv.post(f"/admin/api/customers/{c['id']}/reset-activation", headers=h)
    assert client_call(srv, "/api/v1/activate", c["license_key"], machine="pc-2").json()["status"] == "active"
    new = srv.post(f"/admin/api/customers/{c['id']}/new-key", headers=h).json()["license_key"]
    assert client_call(srv, "/api/v1/check", c["license_key"], machine="pc-2").status_code == 403  # old key dead
    assert client_call(srv, "/api/v1/activate", new, machine="pc-3").json()["status"] == "active"


def test_unknown_keys_are_rate_limited(srv):
    for i in range(20):
        assert client_call(srv, "/api/v1/activate", f"FLX-NOPE-{i}").status_code == 403
    assert client_call(srv, "/api/v1/activate", "FLX-NOPE-X").status_code == 429


def test_cancel_and_plan_validation(srv):
    csrf = login(srv)
    h = {"X-CSRF-Token": csrf}
    assert srv.post("/admin/api/customers", json={"name": "Y", "plan": "gold"}, headers=h).status_code == 400
    c = new_customer(srv, csrf, plan="basic")
    assert c["features"] == {"reports": True, "integrations": False} and c["max_meters"] == 10
    client_call(srv, "/api/v1/activate", c["license_key"])
    srv.post(f"/admin/api/customers/{c['id']}/cancel", headers=h)
    assert client_call(srv, "/api/v1/check", c["license_key"]).json()["status"] == "cancelled"
    # payment doesn't silently revive a cancelled subscription
    r = srv.post(f"/admin/api/customers/{c['id']}/payment", json={"months": 1}, headers=h).json()
    assert r["effective"] == "cancelled"
