import base64
import io
import json

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from license_server.app import ServerConfig, create_app, hash_password
from license_server.crypto import generate_key
from license_server.mpesa import (STILL_PROCESSING, Daraja, DarajaError, MpesaSettings, normalize_phone,
                                  parse_account_no)

SECRET = "s" * 24


class FakeDaraja(Daraja):
    """Stands in for Safaricom: records STK pushes and answers queries as told."""

    def __init__(self, s):
        super().__init__(s)
        self.pushes = []
        self.query_result: dict | str = STILL_PROCESSING
        self.n = 0

    def stk_push(self, phone, amount, account_ref, description):
        self.n += 1
        self.pushes.append({"phone": phone, "amount": amount, "ref": account_ref})
        return {"MerchantRequestID": f"m{self.n}", "CheckoutRequestID": f"ws_CO_{self.n}", "ResponseCode": "0",
                "CustomerMessage": "Success. Request accepted for processing"}

    def stk_query(self, checkout_id):
        if self.query_result == STILL_PROCESSING:
            raise DarajaError("The transaction is being processed", STILL_PROCESSING)
        return self.query_result

    def register_c2b(self):
        return {"ResponseCode": "0", "ResponseDescription": "Success"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"MPESA_CONSUMER_KEY": "ck", "MPESA_CONSUMER_SECRET": "cs", "MPESA_PASSKEY": "pk",
                 "MPESA_CALLBACK_SECRET": SECRET}.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "license-server.yaml").write_text(f"""
admins:
  - {{username: staff, password_hash: "{hash_password('staff-pass-1234', 1000)}"}}
currency: KES
plans:
  basic: {{max_meters: 10, price_month: 5000, features: {{reports: true, integrations: false}}}}
mpesa:
  enabled: true
  shortcode: "174379"
  c2b_shortcode: "600000"
  public_base_url: "https://lic.example"
  allowed_ips: []
  reconcile_in_background: false
""")
    cfg = ServerConfig(tmp_path / "license-server.yaml")
    generate_key(cfg.key_path)
    fake = FakeDaraja(MpesaSettings(cfg.raw["mpesa"]))
    c = TestClient(create_app(cfg, daraja=fake))
    c.post("/admin/api/login", json={"username": "staff", "password": "staff-pass-1234"}).raise_for_status()
    h = {"X-CSRF-Token": c.get("/admin/api/me").json()["csrf"]}
    cust = c.post("/admin/api/customers", json={"name": "Acme", "plan": "basic",
                                               "paid_until": (date.today() - timedelta(days=40)).isoformat()}, headers=h).json()
    c.post("/api/v1/activate", json={"key": cust["license_key"], "machine_id": "pc-1"})
    return c, h, cust, fake


def stk_callback(checkout_id, code=0, amount=5000, receipt="QAB1CD2EF3"):
    cb = {"MerchantRequestID": "m", "CheckoutRequestID": checkout_id, "ResultCode": code,
          "ResultDesc": "The service request is processed successfully." if code == 0 else "Request cancelled by user"}
    if code == 0:
        cb["CallbackMetadata"] = {"Item": [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
                                           {"Name": "TransactionDate", "Value": 20261122101500}, {"Name": "PhoneNumber", "Value": 254712345678}]}
    return {"Body": {"stkCallback": cb}}


def status(c, cust):
    return c.post("/api/v1/check", json={"key": cust["license_key"], "machine_id": "pc-1"}).json()["status"]


def test_helpers():
    assert normalize_phone("0712 345 678") == normalize_phone("+254712345678") == normalize_phone("712345678") == "254712345678"
    assert normalize_phone("0112345678") == "254112345678"
    with pytest.raises(ValueError):
        normalize_phone("0201234567")
    assert parse_account_no("flx0007") == 7 and parse_account_no(" FLX-12 ") == 12 and parse_account_no("7") is None


def test_daraja_request_format(monkeypatch):
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def opener(req, timeout):
        sent.append(req)
        if "oauth" in req.full_url:
            return Resp(json.dumps({"access_token": "tok", "expires_in": "3599"}).encode())
        return Resp(json.dumps({"ResponseCode": "0", "CheckoutRequestID": "ws_1", "MerchantRequestID": "m"}).encode())

    monkeypatch.setenv("MPESA_PASSKEY", "pk")
    monkeypatch.setenv("MPESA_CALLBACK_SECRET", SECRET)
    d = Daraja(MpesaSettings({"enabled": True, "shortcode": "174379", "public_base_url": "https://lic.example"}), opener)
    d.stk_push("254712345678", 5000, "FLX0007-LONGREF", "Flexpower EMS subscription")
    assert sent[0].get_header("Authorization").startswith("Basic ")
    body = json.loads(sent[1].data)
    assert sent[1].full_url == "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    assert body["Amount"] == 5000 and body["PartyA"] == "254712345678" and body["AccountReference"] == "FLX0007-LONG"
    assert body["CallBackURL"] == f"https://lic.example/mpesa/stk-callback/{SECRET}"
    assert base64.b64decode(body["Password"]).decode() == f"174379pk{body['Timestamp']}"
    assert len(body["TransactionDesc"]) <= 13
    d.stk_push("254712345678", 1, "FLX1", "x")
    assert sum("oauth" in r.full_url for r in sent) == 1  # token cached


def test_stk_payment_unlocks_paused_customer(env):
    c, h, cust, fake = env
    assert status(c, cust) == "paused"  # 40 days unpaid
    r = c.post("/api/v1/pay", json={"key": cust["license_key"], "machine_id": "pc-1", "phone": "0712345678", "months": 2})
    assert r.status_code == 200, r.text
    assert r.json()["amount"] == 10000 and fake.pushes[0] == {"phone": "254712345678", "amount": 10000, "ref": "FLX0001"}
    cid = r.json()["checkout_id"]
    q = {"key": cust["license_key"], "machine_id": "pc-1", "checkout_id": cid}
    assert c.post("/api/v1/pay-status", json=q).json()["status"] == "pending"

    fake.query_result = {"ResultCode": "0", "ResultDesc": "The service request is processed successfully."}
    assert c.post(f"/mpesa/stk-callback/{SECRET}", json=stk_callback(cid, amount=10000)).json()["ResultCode"] == 0
    st = c.post("/api/v1/pay-status", json=q).json()
    assert st["status"] == "completed" and st["receipt"] == "QAB1CD2EF3" and st["months"] == 2
    assert status(c, cust) == "active"
    detail = c.get(f"/admin/api/customers/{cust['id']}").json()
    assert date.fromisoformat(detail["paid_until"]) >= date.today() + timedelta(days=58)

    # the same callback again must not credit twice
    c.post(f"/mpesa/stk-callback/{SECRET}", json=stk_callback(cid, amount=10000))
    assert c.get(f"/admin/api/customers/{cust['id']}").json()["paid_until"] == detail["paid_until"]


def test_cancelled_by_user_and_query_fallback(env):
    c, h, cust, fake = env
    pay = {"key": cust["license_key"], "machine_id": "pc-1", "phone": "0712345678", "months": 1}
    cid = c.post("/api/v1/pay", json=pay).json()["checkout_id"]
    c.post(f"/mpesa/stk-callback/{SECRET}", json=stk_callback(cid, code=1032))
    q = {"key": cust["license_key"], "machine_id": "pc-1", "checkout_id": cid}
    assert c.post("/api/v1/pay-status", json=q).json()["status"] == "failed"
    assert status(c, cust) == "paused"

    # a cancelled attempt doesn't block trying again straight away
    assert c.post("/api/v1/pay", json=pay).status_code == 200


def test_lost_callback_is_reconciled_by_query(env, tmp_path):
    c, h, cust, fake = env
    pay = {"key": cust["license_key"], "machine_id": "pc-1", "phone": "0712345678", "months": 1}
    cid = c.post("/api/v1/pay", json=pay).json()["checkout_id"]
    import sqlite3
    db = sqlite3.connect(tmp_path / "data" / "licenses.sqlite3")
    db.execute("UPDATE payments SET created = created - 60 WHERE checkout_id=?", (cid,))
    db.commit()
    q = {"key": cust["license_key"], "machine_id": "pc-1", "checkout_id": cid}
    assert c.post("/api/v1/pay-status", json=q).json()["status"] == "pending"  # still processing at Safaricom
    db.execute("UPDATE payments SET last_query = 0 WHERE checkout_id=?", (cid,))
    db.commit()
    fake.query_result = {"ResultCode": "0", "ResultDesc": "The service request is processed successfully."}
    assert c.post("/api/v1/pay-status", json=q).json()["status"] == "completed"
    assert status(c, cust) == "active"


def test_unconfirmed_callback_is_not_credited(env):
    c, h, cust, fake = env
    cid = c.post("/api/v1/pay", json={"key": cust["license_key"], "machine_id": "pc-1", "phone": "0712345678"}).json()["checkout_id"]
    fake.query_result = {"ResultCode": "1032", "ResultDesc": "Request cancelled by user"}  # Safaricom disagrees
    c.post(f"/mpesa/stk-callback/{SECRET}", json=stk_callback(cid))
    assert status(c, cust) == "paused"


def test_callback_security(env, tmp_path):
    c, h, cust, fake = env
    assert c.post("/mpesa/stk-callback/wrong-secret", json=stk_callback("x")).status_code == 404
    assert c.post("/mpesa/c2b/confirmation/wrong-secret", json={}).status_code == 404
    c.app.state.mpesa.allowed_ips = ["196.201.214.200"]
    assert c.post(f"/mpesa/stk-callback/{SECRET}", json=stk_callback("x")).status_code == 403
    # CF-Connecting-IP is only trusted when the request comes from cloudflared on this machine,
    # so a direct caller can't spoof Safaricom's address with the header
    assert c.post(f"/mpesa/stk-callback/{SECRET}", json=stk_callback("x"),
                  headers={"CF-Connecting-IP": "196.201.214.200"}).status_code == 403


def test_paybill_c2b_with_credit_and_unmatched(env):
    c, h, cust, fake = env
    base = {"TransactionType": "Pay Bill", "TransTime": "20261122101500", "BusinessShortCode": "600000",
            "MSISDN": "254712345678", "FirstName": "Jane"}
    v = c.post(f"/mpesa/c2b/validation/{SECRET}", json={**base, "BillRefNumber": "FLX9999", "TransAmount": "100"})
    assert v.json()["ResultCode"] == "C2B00012"
    assert c.post(f"/mpesa/c2b/validation/{SECRET}", json={**base, "BillRefNumber": "flx0001"}).json()["ResultCode"] == "0"

    # KES 7,500 at 5,000/month = 1 month + 2,500 credit
    c.post(f"/mpesa/c2b/confirmation/{SECRET}", json={**base, "TransID": "RKT1", "TransAmount": "7500", "BillRefNumber": "flx0001"})
    d = c.get(f"/admin/api/customers/{cust['id']}").json()
    assert d["credit"] == 2500 and status(c, cust) == "active"
    # duplicate notice ignored; another 2,500 completes a second month
    c.post(f"/mpesa/c2b/confirmation/{SECRET}", json={**base, "TransID": "RKT1", "TransAmount": "7500", "BillRefNumber": "flx0001"})
    c.post(f"/mpesa/c2b/confirmation/{SECRET}", json={**base, "TransID": "RKT2", "TransAmount": "2500", "BillRefNumber": "FLX0001"})
    d2 = c.get(f"/admin/api/customers/{cust['id']}").json()
    assert d2["credit"] == 0 and d2["paid_until"] > d["paid_until"]

    # wrong account number -> unmatched; staff assigns it
    c.post(f"/mpesa/c2b/confirmation/{SECRET}", json={**base, "TransID": "RKT3", "TransAmount": "5000", "BillRefNumber": "ACME"})
    un = c.get("/admin/api/payments?status=unmatched").json()
    assert len(un) == 1 and un[0]["receipt"] == "RKT3" and un[0]["phone"] == "25471****678"
    r = c.post(f"/admin/api/payments/{un[0]['id']}/assign", json={"customer_id": cust["id"]}, headers=h)
    assert r.json()["months"] == 1
    assert c.get("/admin/api/payments?status=unmatched").json() == []


def test_pay_requires_valid_key_and_limits_requests(env):
    c, h, cust, fake = env
    bad = {"key": "FLX-NOPE", "machine_id": "pc-1", "phone": "0712345678"}
    assert c.post("/api/v1/pay", json=bad).status_code == 403
    assert c.post("/api/v1/pay", json={**bad, "key": cust["license_key"], "machine_id": "pc-9"}).status_code == 403
    ok = {"key": cust["license_key"], "machine_id": "pc-1", "phone": "0712345678"}
    assert c.post("/api/v1/pay", json={**ok, "phone": "12345"}).status_code == 400
    assert c.post("/api/v1/pay", json=ok).status_code == 200
    assert c.post("/api/v1/pay", json=ok).status_code == 429  # one pending request at a time
    # token tells the app how to pay
    tok = c.post("/api/v1/check", json={"key": cust["license_key"], "machine_id": "pc-1"}).json()["token"]
    body = tok.split(".")[0]
    p = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert p["billing"] == {"account_no": "FLX0001", "paybill": "600000", "price_month": 5000.0,
                            "currency": "KES", "credit": 0.0, "mpesa": True}


def test_staff_can_request_payment_and_see_history(env):
    c, h, cust, fake = env
    r = c.post(f"/admin/api/customers/{cust['id']}/stk", json={"phone": "0712345678", "months": 3}, headers=h)
    assert r.status_code == 200 and fake.pushes[-1]["amount"] == 15000
    assert c.post(f"/admin/api/customers/{cust['id']}/stk", json={"phone": "0712345678"}).status_code == 403  # CSRF
    hist = c.get(f"/admin/api/payments?customer_id={cust['id']}").json()
    assert hist[0]["method"] == "mpesa_stk" and hist[0]["status"] == "pending"
    info = c.get("/admin/api/mpesa").json()
    assert info["enabled"] and info["problems"] == [] and SECRET not in json.dumps(info)
    assert c.post("/admin/api/mpesa/register-c2b", headers=h).json()["ResponseCode"] == "0"
