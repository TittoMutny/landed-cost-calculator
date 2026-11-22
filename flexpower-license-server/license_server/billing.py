"""Payments: turning money into subscription months, and the M-Pesa endpoints.

Every payment (M-Pesa STK, M-Pesa Paybill, or recorded by staff) is a row in
`payments`. Money is added to the customer's credit; whole months at the
customer's monthly price are taken from the credit and added to "paid until".
A payment that covers at least one month lifts a pause automatically.
"""
from __future__ import annotations

import calendar
import hmac
import json
import logging
import math
import time
from datetime import date
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .mpesa import (STILL_PROCESSING, Daraja, DarajaError, MpesaSettings, account_no, mask_phone,
                    normalize_phone, parse_account_no, parse_stk_callback)

log = logging.getLogger("license_server.billing")

PAYMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER, method TEXT NOT NULL, status TEXT NOT NULL,
  amount REAL, months INTEGER DEFAULT 0, phone TEXT, receipt TEXT UNIQUE, checkout_id TEXT UNIQUE,
  account_ref TEXT, result_desc TEXT, raw TEXT, requested_by TEXT,
  created INTEGER NOT NULL, updated INTEGER NOT NULL, last_query INTEGER
);
CREATE INDEX IF NOT EXISTS ix_payments_customer ON payments(customer_id, created);
CREATE INDEX IF NOT EXISTS ix_payments_status ON payments(status);
"""
ACCEPTED = {"ResultCode": 0, "ResultDesc": "Accepted"}


def migrate(con) -> None:
    con.executescript(PAYMENTS_SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(customers)")}
    if "monthly_price" not in cols:
        con.execute("ALTER TABLE customers ADD COLUMN monthly_price REAL")
    if "credit" not in cols:
        con.execute("ALTER TABLE customers ADD COLUMN credit REAL NOT NULL DEFAULT 0")


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, min(d.day, calendar.monthrange(d.year + y, m + 1)[1]))


def monthly_price(plans: dict, c) -> float | None:
    if c["monthly_price"]:
        return float(c["monthly_price"])
    p = (plans.get(c["plan"]) or {}).get("price_month")
    return float(p) if p else None


def credit_customer(con, plans: dict, event: Callable, cid: int, amount: float, actor: str,
                    note: str, fixed_months: int | None = None) -> dict[str, Any]:
    """Add money (or, for staff entries, a fixed number of months) and extend the subscription."""
    c = con.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if c is None:
        raise HTTPException(404, "Customer not found")
    credit = float(c["credit"] or 0)
    if fixed_months is not None:
        months = fixed_months
    else:
        price = monthly_price(plans, c)
        credit += float(amount or 0)
        months = int(credit // price) if price else 0
        if price:
            credit = round(credit - months * price, 2)
    paid_until = c["paid_until"]
    status = c["status"]
    if months > 0:
        paid_until = add_months(max(date.fromisoformat(c["paid_until"]), date.today()), months).isoformat()
        if status == "paused":  # paying lifts a pause, but never revives a cancelled subscription
            status = "active"
    con.execute("UPDATE customers SET credit=?, paid_until=?, status=?, "
                "pause_reason=CASE WHEN ?='active' THEN NULL ELSE pause_reason END WHERE id=?",
                (credit, paid_until, status, status, cid))
    detail = f"{note}: {months} month(s), paid until {paid_until}" + (f", credit {credit:g}" if credit else "")
    event(con, cid, actor, "payment", detail)
    return {"months": months, "paid_until": paid_until, "credit": credit, "status": status}


class PayBody(BaseModel):
    key: str = Field(max_length=64)
    machine_id: str = Field(max_length=128)
    phone: str = Field(max_length=20)
    months: int = Field(1, ge=1, le=24)


class PayStatusBody(BaseModel):
    key: str = Field(max_length=64)
    machine_id: str = Field(max_length=128)
    checkout_id: str = Field(max_length=100)


class StaffStkBody(BaseModel):
    phone: str = Field(max_length=20)
    months: int = Field(1, ge=1, le=24)


class AssignBody(BaseModel):
    customer_id: int


def install(app: FastAPI, cfg, store, *, admin_session: Callable, admin_write: Callable, client_ip: Callable,
            customer_for_key: Callable, daraja: Daraja | None = None) -> None:
    s = MpesaSettings(cfg.raw.get("mpesa"))
    api = daraja or Daraja(s)
    app.state.mpesa = s
    app.state.daraja = api

    def require_mpesa():
        problems = s.problems()
        if problems:
            raise HTTPException(503, "M-Pesa payments are not available: " + "; ".join(problems))

    def check_callback(request: Request, secret: str) -> None:
        if not s.callback_secret or not hmac.compare_digest(secret, s.callback_secret):
            log.warning("mpesa callback with wrong secret from %s", client_ip(request))
            raise HTTPException(404)
        if s.allowed_ips and client_ip(request) not in s.allowed_ips:
            log.warning("mpesa callback from unexpected address %s", client_ip(request))
            raise HTTPException(403)

    def start_stk(con, c, phone_raw: str, months: int, actor: str) -> dict[str, Any]:
        require_mpesa()
        if c["status"] == "cancelled":
            raise HTTPException(400, "This subscription is cancelled. Contact Flexpower.")
        price = monthly_price(cfg.plans, c)
        if not price:
            raise HTTPException(400, "No monthly price is set for this plan. Contact Flexpower.")
        try:
            phone = normalize_phone(phone_raw)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        now = int(time.time())
        recent = con.execute("SELECT status, created FROM payments WHERE customer_id=? AND method='mpesa_stk' "
                             "AND created > ? ORDER BY created DESC", (c["id"], now - 3600)).fetchall()
        if any(r["status"] == "pending" and now - r["created"] < 90 for r in recent):
            raise HTTPException(429, "A payment request was just sent. Check your phone, or try again in a minute.")
        if len(recent) >= 5:
            raise HTTPException(429, "Too many payment requests this hour. Please pay via Paybill or try later.")
        amount = max(1, math.ceil(months * price - float(c["credit"] or 0)))
        try:
            r = api.stk_push(phone, amount, account_no(c["id"]), "Flexpower EMS")
        except DarajaError as exc:
            raise HTTPException(502, f"M-Pesa: {exc}") from exc
        con.execute("INSERT INTO payments(customer_id, method, status, amount, months, phone, checkout_id, account_ref, "
                    "requested_by, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (c["id"], "mpesa_stk", "pending", amount, months, mask_phone(phone), r["CheckoutRequestID"],
                     account_no(c["id"]), actor, now, now))
        store.event(con, c["id"], actor, "mpesa_request", f"KES {amount} for {months} month(s) to {mask_phone(phone)}")
        return {"checkout_id": r["CheckoutRequestID"], "amount": amount, "currency": cfg.raw.get("currency", "KES"),
                "message": r.get("CustomerMessage") or "Check your phone and enter your M-Pesa PIN."}

    def complete_stk(con, p, receipt: str | None, paid_amount: float | None, desc: str, raw: Any) -> None:
        if p["status"] != "pending":
            return  # already handled (duplicate callback, or query beat the callback)
        if receipt and con.execute("SELECT 1 FROM payments WHERE receipt=?", (receipt,)).fetchone():
            return
        amount = float(p["amount"]) if paid_amount is None else min(float(p["amount"]), float(paid_amount))
        res = credit_customer(con, cfg.plans, store.event, p["customer_id"], amount, "mpesa",
                              f"M-Pesa {receipt or p['checkout_id']} KES {amount:g}")
        con.execute("UPDATE payments SET status='completed', receipt=?, amount=?, months=?, result_desc=?, raw=?, "
                    "updated=? WHERE id=?", (receipt, amount, res["months"], desc, json.dumps(raw)[:2000],
                                             int(time.time()), p["id"]))

    def fail_stk(con, p, desc: str, raw: Any) -> None:
        if p["status"] == "pending":
            con.execute("UPDATE payments SET status='failed', result_desc=?, raw=?, updated=? WHERE id=?",
                        (desc, json.dumps(raw)[:2000], int(time.time()), p["id"]))

    def reconcile(con, p) -> None:
        """Ask Safaricom about a pending STK payment whose callback hasn't arrived."""
        con.execute("UPDATE payments SET last_query=? WHERE id=?", (int(time.time()), p["id"]))
        try:
            q = api.stk_query(p["checkout_id"])
        except DarajaError as exc:
            if exc.code != STILL_PROCESSING:
                log.warning("stk query %s failed: %s", p["checkout_id"], exc)
            return
        if str(q.get("ResultCode")) == "0":
            complete_stk(con, p, None, None, q.get("ResultDesc", "confirmed by query"), q)
        elif q.get("ResultCode") is not None:
            fail_stk(con, p, q.get("ResultDesc", "failed"), q)

    def view(p) -> dict:
        return {k: p[k] for k in ("id", "customer_id", "method", "status", "amount", "months", "phone", "receipt",
                                  "account_ref", "result_desc", "created", "updated")}

    # ------------------------------------------------------------- EMS app (customer side)
    @app.post("/api/v1/pay")
    def pay(body: PayBody, request: Request):
        with store.lock, store.connect() as con:
            c = customer_for_key(con, body.key, body.machine_id, request)
            return start_stk(con, c, body.phone, body.months, "customer")

    @app.post("/api/v1/pay-status")
    def pay_status(body: PayStatusBody, request: Request):
        with store.lock, store.connect() as con:
            c = customer_for_key(con, body.key, body.machine_id, request)
            p = con.execute("SELECT * FROM payments WHERE checkout_id=? AND customer_id=?",
                            (body.checkout_id, c["id"])).fetchone()
            if p is None:
                raise HTTPException(404, "Unknown payment request")
            now = int(time.time())
            if p["status"] == "pending" and now - p["created"] > 25 and now - (p["last_query"] or 0) > 15:
                reconcile(con, p)
                p = con.execute("SELECT * FROM payments WHERE id=?", (p["id"],)).fetchone()
            if p["status"] == "pending" and now - p["created"] > 600:
                fail_stk(con, p, "No answer from M-Pesa", {})
                p = con.execute("SELECT * FROM payments WHERE id=?", (p["id"],)).fetchone()
            return {"status": p["status"], "receipt": p["receipt"], "amount": p["amount"], "months": p["months"],
                    "message": p["result_desc"] or ""}

    # ------------------------------------------------------------- Safaricom callbacks
    @app.post("/mpesa/stk-callback/{secret}")
    async def stk_callback(secret: str, request: Request):
        check_callback(request, secret)
        body = await request.json()
        cb = parse_stk_callback(body)
        with store.lock, store.connect() as con:
            p = con.execute("SELECT * FROM payments WHERE checkout_id=?", (cb["checkout_id"],)).fetchone()
            if p is None:
                log.warning("stk callback for unknown checkout %s", cb["checkout_id"])
                return ACCEPTED
            if cb["result_code"] != "0":
                fail_stk(con, p, cb["result_desc"], body)
                return ACCEPTED
            if s.verify_with_query:  # callbacks are unsigned: confirm with Safaricom before crediting
                try:
                    q = api.stk_query(cb["checkout_id"])
                    if str(q.get("ResultCode")) != "0":
                        log.warning("stk callback says paid but query says %s", q)
                        fail_stk(con, p, f"not confirmed by M-Pesa: {q.get('ResultDesc')}", body)
                        return ACCEPTED
                except DarajaError as exc:
                    log.warning("could not confirm stk callback by query (%s); accepting it", exc)
            complete_stk(con, p, cb["receipt"], cb["amount"], cb["result_desc"], body)
        return ACCEPTED

    @app.post("/mpesa/c2b/validation/{secret}")
    async def c2b_validation(secret: str, request: Request):
        check_callback(request, secret)
        body = await request.json()
        cid = parse_account_no(str(body.get("BillRefNumber", "")))
        with store.connect() as con:
            c = con.execute("SELECT status FROM customers WHERE id=?", (cid,)).fetchone() if cid else None
        if c is None or c["status"] == "cancelled":
            return {"ResultCode": "C2B00012", "ResultDesc": "Rejected"}  # invalid account number
        return {"ResultCode": "0", "ResultDesc": "Accepted"}

    @app.post("/mpesa/c2b/confirmation/{secret}")
    async def c2b_confirmation(secret: str, request: Request):
        check_callback(request, secret)
        body = await request.json()
        receipt = str(body.get("TransID") or "")
        try:
            amount = float(body.get("TransAmount") or 0)
        except ValueError:
            amount = 0.0
        ref = str(body.get("BillRefNumber") or "")
        now = int(time.time())
        with store.lock, store.connect() as con:
            if not receipt or con.execute("SELECT 1 FROM payments WHERE receipt=?", (receipt,)).fetchone():
                return ACCEPTED  # duplicate notice
            cid = parse_account_no(ref)
            c = con.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone() if cid else None
            phone = mask_phone(str(body.get("MSISDN") or ""))
            if c is None or amount <= 0:
                con.execute("INSERT INTO payments(customer_id, method, status, amount, phone, receipt, account_ref, "
                            "result_desc, raw, created, updated) VALUES (NULL,'mpesa_c2b','unmatched',?,?,?,?,?,?,?,?)",
                            (amount, phone, receipt, ref, f"no customer for account '{ref}'", json.dumps(body)[:2000], now, now))
                store.event(con, None, "mpesa", "unmatched_payment", f"{receipt} KES {amount:g} account '{ref}'")
                return ACCEPTED
            res = credit_customer(con, cfg.plans, store.event, c["id"], amount, "mpesa", f"M-Pesa Paybill {receipt} KES {amount:g}")
            con.execute("INSERT INTO payments(customer_id, method, status, amount, months, phone, receipt, account_ref, "
                        "raw, created, updated) VALUES (?,'mpesa_c2b','completed',?,?,?,?,?,?,?,?)",
                        (c["id"], amount, res["months"], phone, receipt, ref, json.dumps(body)[:2000], now, now))
        return ACCEPTED

    # ------------------------------------------------------------- admin
    @app.get("/admin/api/mpesa")
    def mpesa_info(sess: dict = Depends(admin_session)):
        return {"enabled": s.enabled, "environment": s.environment, "shortcode": s.shortcode,
                "c2b_shortcode": s.c2b_shortcode, "transaction_type": s.transaction_type, "problems": s.problems(),
                "currency": cfg.raw.get("currency", "KES"),
                "callbacks": {k: s.url(k).replace(s.callback_secret, "<secret>") if s.callback_secret else ""
                              for k in ("stk-callback", "c2b/confirmation", "c2b/validation")}}

    @app.post("/admin/api/mpesa/register-c2b")
    def register_c2b(actor: str = Depends(admin_write)):
        require_mpesa()
        try:
            r = api.register_c2b()
        except DarajaError as exc:
            raise HTTPException(502, f"M-Pesa: {exc}") from exc
        with store.connect() as con:
            store.event(con, None, actor, "mpesa_c2b_registered", s.c2b_shortcode)
        return r

    @app.get("/admin/api/payments")
    def payments(status: str | None = None, customer_id: int | None = None, sess: dict = Depends(admin_session)):
        sql, args = "SELECT * FROM payments WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if customer_id:
            sql += " AND customer_id=?"
            args.append(customer_id)
        with store.connect() as con:
            return [view(p) for p in con.execute(sql + " ORDER BY id DESC LIMIT 200", args)]

    @app.post("/admin/api/payments/{pid}/assign")
    def assign(pid: int, body: AssignBody, actor: str = Depends(admin_write)):
        with store.lock, store.connect() as con:
            p = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
            if p is None or p["status"] != "unmatched":
                raise HTTPException(400, "Only unmatched payments can be assigned")
            res = credit_customer(con, cfg.plans, store.event, body.customer_id, p["amount"], actor,
                                  f"M-Pesa {p['receipt']} KES {p['amount']:g} (assigned by staff)")
            con.execute("UPDATE payments SET customer_id=?, status='completed', months=?, updated=? WHERE id=?",
                        (body.customer_id, res["months"], int(time.time()), pid))
            return res

    @app.post("/admin/api/customers/{cid}/stk")
    def staff_stk(cid: int, body: StaffStkBody, actor: str = Depends(admin_write)):
        with store.lock, store.connect() as con:
            c = con.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
            if c is None:
                raise HTTPException(404, "Customer not found")
            return start_stk(con, c, body.phone, body.months, actor)

    app.state.reconcile = lambda: _reconcile_all(store, reconcile)


def _reconcile_all(store, reconcile) -> int:
    """Query every STK payment still pending after 2 minutes (call periodically)."""
    now, n = int(time.time()), 0
    with store.lock, store.connect() as con:
        for p in con.execute("SELECT * FROM payments WHERE status='pending' AND method='mpesa_stk' AND created < ? "
                             "AND created > ?", (now - 120, now - 86400)).fetchall():
            reconcile(con, p)
            n += 1
    return n


def billing_info(cfg, c) -> dict[str, Any]:
    """What an installation needs to show payment options (goes into the signed token)."""
    s = MpesaSettings(cfg.raw.get("mpesa"))
    return {"account_no": account_no(c["id"]), "paybill": s.c2b_shortcode if s.enabled else None,
            "price_month": monthly_price(cfg.plans, c), "currency": cfg.raw.get("currency", "KES"),
            "credit": float(c["credit"] or 0), "mpesa": s.enabled and not s.problems()}
