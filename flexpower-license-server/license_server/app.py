"""Flexpower licence server.

Two audiences:
  * EMS installations call POST /api/v1/activate and /api/v1/check and get a
    signed token saying whether they may run (active / overdue / paused /
    cancelled / invalid), their plan limits and how long the answer is valid.
  * Flexpower staff use /admin to add customers, record payments and pause or
    resume subscriptions.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .crypto import load_key, public_key_b64, sign_token

log = logging.getLogger("license_server")
STATIC = Path(__file__).parent / "static"
COOKIE = "flx_admin"
STATUSES = ("active", "paused", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, email TEXT, phone TEXT, plan TEXT NOT NULL,
  max_meters INTEGER NOT NULL, features TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', pause_reason TEXT,
  paid_until TEXT NOT NULL, grace_days INTEGER NOT NULL DEFAULT 7,
  key_hash TEXT UNIQUE NOT NULL, key_hint TEXT NOT NULL,
  machine_id TEXT, hostname TEXT, activated_at INTEGER,
  last_seen INTEGER, last_ip TEXT, app_version TEXT, meters_count INTEGER,
  notes TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, customer_id INTEGER,
  actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_customer ON events(customer_id, ts);
CREATE TABLE IF NOT EXISTS admin_sessions (
  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL,
  created INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (key TEXT PRIMARY KEY, count INTEGER NOT NULL, first_ts INTEGER NOT NULL);
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def new_license_key() -> str:
    raw = base64.b32encode(secrets.token_bytes(10)).decode()  # 16 chars, 80 bits
    return "FLX-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def normalize_key(key: str) -> str:
    return key.strip().upper()


def hash_password(password: str, iterations: int = 260_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def effective_status(c: dict, today: date) -> tuple[str, str, date | None]:
    """(status, reason, pause_on). Overdue customers keep working until the grace period ends."""
    if c["status"] == "cancelled":
        return "cancelled", "Subscription cancelled", None
    if c["status"] == "paused":
        return "paused", c["pause_reason"] or "Subscription paused by Flexpower", None
    paid = date.fromisoformat(c["paid_until"])
    pause_on = paid + timedelta(days=int(c["grace_days"]) + 1)
    if today >= pause_on:
        return "paused", f"Payment overdue since {paid.isoformat()}", None
    if today > paid:
        return "overdue", f"Payment overdue. Access pauses on {pause_on.isoformat()} unless paid.", pause_on
    return "active", "", pause_on


# ---------------------------------------------------------------------------
# server config and storage
# ---------------------------------------------------------------------------
class ServerConfig:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        base = self.path.parent
        r = self.raw
        self.db_path = base / r.get("database", "data/licenses.sqlite3")
        self.key_path = base / r.get("signing_key", "data/signing_key.pem")
        self.offline_days = int(r.get("offline_days", 7))
        self.check_hours = float(r.get("check_hours", 6))
        self.support = r.get("support") or {}
        self.admins = {a["username"]: a["password_hash"] for a in r.get("admins") or [] if a.get("username")}
        self.plans = r.get("plans") or {
            "basic": {"max_meters": 10, "features": {"reports": True, "integrations": False}},
            "pro": {"max_meters": 50, "features": {"reports": True, "integrations": True}},
            "enterprise": {"max_meters": 500, "features": {"reports": True, "integrations": True}},
        }
        self.secure_cookies = r.get("secure_cookies", "auto")
        self.trust_proxy_ip = bool(r.get("trust_cf_connecting_ip", True))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def event(self, con, customer_id: int | None, actor: str, action: str, detail: str = "") -> None:
        con.execute("INSERT INTO events(ts, customer_id, actor, action, detail) VALUES (?,?,?,?,?)",
                    (int(time.time()), customer_id, actor, action, detail[:500]))


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------
class ClientBody(BaseModel):
    key: str = Field(max_length=64)
    machine_id: str = Field(max_length=128)
    hostname: str = Field("", max_length=128)
    version: str = Field("", max_length=32)
    meters: int = Field(0, ge=0, le=100_000)


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class CustomerBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field("", max_length=200)
    phone: str = Field("", max_length=50)
    plan: str = Field("basic", max_length=50)
    max_meters: int | None = Field(None, ge=1, le=100_000)
    features: dict[str, bool] | None = None
    paid_until: str | None = None
    grace_days: int = Field(7, ge=0, le=90)
    notes: str = Field("", max_length=2000)


class CustomerPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    email: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=50)
    plan: str | None = Field(None, max_length=50)
    max_meters: int | None = Field(None, ge=1, le=100_000)
    features: dict[str, bool] | None = None
    paid_until: str | None = None
    grace_days: int | None = Field(None, ge=0, le=90)
    notes: str | None = Field(None, max_length=2000)


class PauseBody(BaseModel):
    reason: str = Field("Subscription paused by Flexpower", max_length=300)


class PaymentBody(BaseModel):
    months: int = Field(1, ge=1, le=60)
    amount: str = Field("", max_length=50)
    reference: str = Field("", max_length=100)


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------
def create_app(cfg: ServerConfig) -> FastAPI:
    app = FastAPI(title="Flexpower licence server", docs_url=None, redoc_url=None, openapi_url=None)
    store = Store(cfg.db_path)
    key = load_key(cfg.key_path)
    app.state.public_key = public_key_b64(key)

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if cfg.trust_proxy_ip and peer in ("127.0.0.1", "::1"):
            return request.headers.get("cf-connecting-ip") or peer
        return peer

    def too_many(con, k: str, limit: int, window: int) -> bool:
        row = con.execute("SELECT * FROM failures WHERE key=?", (k,)).fetchone()
        return bool(row and row["count"] >= limit and time.time() - row["first_ts"] < window)

    def fail(con, k: str, window: int) -> None:
        now = int(time.time())
        row = con.execute("SELECT * FROM failures WHERE key=?", (k,)).fetchone()
        if row is None or now - row["first_ts"] > window:
            con.execute("INSERT OR REPLACE INTO failures VALUES (?,?,?)", (k, 1, now))
        else:
            con.execute("UPDATE failures SET count=count+1 WHERE key=?", (k,))

    # ------------------------------------------------------------- tokens
    def token_for(c: dict | None, machine_id: str, status: str | None = None, reason: str = "") -> dict:
        now = int(time.time())
        today = datetime.now(timezone.utc).date()
        payload: dict[str, Any] = {"v": 1, "iat": now, "machine": machine_id, "support": cfg.support,
                                   "check_hours": cfg.check_hours}
        if c is None:
            payload.update(status=status or "invalid", reason=reason, exp=now + 86400 * 365)
        else:
            st, why, pause_on = effective_status(c, today)
            exp = now + cfg.offline_days * 86400
            if pause_on is not None:  # an offline PC still pauses on schedule
                exp = min(exp, int(datetime.combine(pause_on, datetime.min.time(), timezone.utc).timestamp()))
            if st in ("paused", "cancelled"):
                exp = now + 86400 * 365  # a pause stays in force until the server says otherwise
            payload.update(status=st, reason=why, exp=exp, lid=c["id"], customer=c["name"], plan=c["plan"],
                           paid_until=c["paid_until"], pause_on=pause_on.isoformat() if pause_on else None,
                           max_meters=c["max_meters"], features=json.loads(c["features"]))
        return {"token": sign_token(key, payload), "status": payload["status"], "reason": payload["reason"]}

    def lookup(con, body: ClientBody, request: Request, activating: bool) -> JSONResponse:
        ip = client_ip(request)
        if too_many(con, f"ip:{ip}", 20, 3600):
            raise HTTPException(429, "Too many invalid licence keys from this address. Try again later.")
        c = con.execute("SELECT * FROM customers WHERE key_hash=?", (sha256(normalize_key(body.key)),)).fetchone()
        if c is None:
            fail(con, f"ip:{ip}", 3600)
            return JSONResponse(token_for(None, body.machine_id, "invalid", "Unknown licence key"), status_code=403)
        c = dict(c)
        if c["machine_id"] and c["machine_id"] != body.machine_id:
            store.event(con, c["id"], "system", "rejected", f"key used on another PC ({body.hostname}, {ip})")
            return JSONResponse(token_for(None, body.machine_id, "invalid",
                                          "This licence is already activated on another PC. Contact Flexpower to move it."),
                                status_code=409)
        if not c["machine_id"]:
            if not activating:
                return JSONResponse(token_for(None, body.machine_id, "invalid", "Licence not activated on this PC"),
                                    status_code=409)
            con.execute("UPDATE customers SET machine_id=?, hostname=?, activated_at=? WHERE id=?",
                        (body.machine_id, body.hostname, int(time.time()), c["id"]))
            store.event(con, c["id"], "system", "activated", f"{body.hostname} ({ip})")
            c["machine_id"] = body.machine_id
        con.execute("UPDATE customers SET last_seen=?, last_ip=?, app_version=?, meters_count=?, hostname=? WHERE id=?",
                    (int(time.time()), ip, body.version, body.meters, body.hostname or c["hostname"], c["id"]))
        return JSONResponse(token_for(c, body.machine_id))

    @app.post("/api/v1/activate")
    def activate(body: ClientBody, request: Request):
        with store.lock, store.connect() as con:
            return lookup(con, body, request, activating=True)

    @app.post("/api/v1/check")
    def check(body: ClientBody, request: Request):
        with store.lock, store.connect() as con:
            return lookup(con, body, request, activating=False)

    @app.get("/api/v1/public-key")
    def public_key():
        return {"ed25519": app.state.public_key}

    @app.get("/health")
    def health():
        return {"ok": True}

    # ------------------------------------------------------------- admin auth
    def is_https(request: Request) -> bool:
        if isinstance(cfg.secure_cookies, bool):
            return cfg.secure_cookies
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" \
            or '"https"' in request.headers.get("cf-visitor", "")

    def admin_session(request: Request) -> dict:
        tok = request.cookies.get(COOKIE)
        if not tok:
            raise HTTPException(401, "Login required")
        now = int(time.time())
        with store.connect() as con:
            row = con.execute("SELECT * FROM admin_sessions WHERE token_hash=?", (sha256(tok),)).fetchone()
            if row is None or now - row["last_seen"] > 8 * 3600 or now - row["created"] > 7 * 86400 \
                    or row["username"] not in cfg.admins:
                raise HTTPException(401, "Login required")
            con.execute("UPDATE admin_sessions SET last_seen=? WHERE token_hash=?", (now, sha256(tok)))
        return dict(row)

    def admin_write(request: Request) -> str:
        s = admin_session(request)
        if request.headers.get("sec-fetch-site") == "cross-site" or \
                not secrets.compare_digest(request.headers.get("x-csrf-token", ""), s["csrf"]):
            raise HTTPException(403, "CSRF check failed")
        return s["username"]

    @app.get("/")
    def root():
        return RedirectResponse("/admin")

    @app.get("/admin")
    def admin_page():
        return FileResponse(STATIC / "admin.html", headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/login")
    def admin_login(body: LoginBody, request: Request):
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site request blocked")
        ip = client_ip(request)
        denied: HTTPException | None = None
        with store.lock, store.connect() as con:  # raise only after the block so failures are committed
            if any(too_many(con, k, 5, 900) for k in (f"admin-ip:{ip}", f"admin-user:{body.username.lower()}")):
                denied = HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
            else:
                h = cfg.admins.get(body.username)
                ok = verify_password(body.password, h or hash_password("x", 1000))
                if not (h and ok):
                    fail(con, f"admin-ip:{ip}", 900)
                    fail(con, f"admin-user:{body.username.lower()}", 900)
                    store.event(con, None, body.username[:50], "admin_login_failed", ip)
                    denied = HTTPException(401, "Invalid username or password")
        if denied:
            raise denied
        with store.lock, store.connect() as con:
            con.execute("DELETE FROM failures WHERE key=?", (f"admin-user:{body.username.lower()}",))
            tok, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(24), int(time.time())
            con.execute("INSERT INTO admin_sessions VALUES (?,?,?,?,?)", (sha256(tok), body.username, csrf, now, now))
            store.event(con, None, body.username, "admin_login", ip)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, tok, httponly=True, samesite="strict", secure=is_https(request), path="/admin")
        return resp

    @app.post("/admin/api/logout")
    def admin_logout(request: Request):
        tok = request.cookies.get(COOKIE)
        if tok:
            with store.connect() as con:
                con.execute("DELETE FROM admin_sessions WHERE token_hash=?", (sha256(tok),))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/admin")
        return resp

    @app.get("/admin/api/me")
    def admin_me(s: dict = Depends(admin_session)):
        return {"username": s["username"], "csrf": s["csrf"], "plans": cfg.plans, "public_key": app.state.public_key}

    # ------------------------------------------------------------- admin data
    def view(c: sqlite3.Row) -> dict:
        d = dict(c)
        d.pop("key_hash", None)
        d["features"] = json.loads(d["features"])
        st, why, pause_on = effective_status(d, datetime.now(timezone.utc).date())
        d.update(effective=st, effective_reason=why, pause_on=pause_on.isoformat() if pause_on else None)
        return d

    def get(con, cid: int) -> sqlite3.Row:
        c = con.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if c is None:
            raise HTTPException(404, "Customer not found")
        return c

    def check_date(s: str) -> str:
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError as exc:
            raise HTTPException(400, "Dates must be YYYY-MM-DD") from exc

    @app.get("/admin/api/customers")
    def list_customers(s: dict = Depends(admin_session)):
        with store.connect() as con:
            return [view(c) for c in con.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE")]

    @app.get("/admin/api/customers/{cid}")
    def customer(cid: int, s: dict = Depends(admin_session)):
        with store.connect() as con:
            c = view(get(con, cid))
            c["events"] = [dict(e) for e in con.execute(
                "SELECT ts, actor, action, detail FROM events WHERE customer_id=? ORDER BY id DESC LIMIT 50", (cid,))]
        return c

    @app.post("/admin/api/customers")
    def create_customer(body: CustomerBody, actor: str = Depends(admin_write)):
        plan = cfg.plans.get(body.plan)
        if plan is None:
            raise HTTPException(400, f"Unknown plan '{body.plan}'")
        features = {**plan.get("features", {}), **(body.features or {})}
        paid = check_date(body.paid_until) if body.paid_until else (date.today() + timedelta(days=30)).isoformat()
        lic = new_license_key()
        with store.lock, store.connect() as con:
            cur = con.execute(
                """INSERT INTO customers(name, email, phone, plan, max_meters, features, paid_until, grace_days,
                   key_hash, key_hint, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (body.name, body.email, body.phone, body.plan, body.max_meters or int(plan.get("max_meters", 10)),
                 json.dumps(features), paid, body.grace_days, sha256(lic), lic[-4:], body.notes, int(time.time())))
            store.event(con, cur.lastrowid, actor, "created", f"plan {body.plan}, paid until {paid}")
            c = view(get(con, cur.lastrowid))
        c["license_key"] = lic  # shown once; only a hash is stored
        return c

    @app.patch("/admin/api/customers/{cid}")
    def edit_customer(cid: int, body: CustomerPatch, actor: str = Depends(admin_write)):
        changes = body.model_dump(exclude_none=True)
        if "plan" in changes and changes["plan"] not in cfg.plans:
            raise HTTPException(400, f"Unknown plan '{changes['plan']}'")
        if "paid_until" in changes:
            changes["paid_until"] = check_date(changes["paid_until"])
        if "features" in changes:
            changes["features"] = json.dumps(changes["features"])
        if not changes:
            raise HTTPException(400, "Nothing to change")
        with store.lock, store.connect() as con:
            get(con, cid)
            con.execute(f"UPDATE customers SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?", [*changes.values(), cid])
            store.event(con, cid, actor, "edited", ", ".join(f"{k}={v}" for k, v in changes.items())[:500])
            return view(get(con, cid))

    def set_status(cid: int, actor: str, status: str, reason: str | None, action: str) -> dict:
        with store.lock, store.connect() as con:
            get(con, cid)
            con.execute("UPDATE customers SET status=?, pause_reason=? WHERE id=?", (status, reason, cid))
            store.event(con, cid, actor, action, reason or "")
            return view(get(con, cid))

    @app.post("/admin/api/customers/{cid}/pause")
    def pause(cid: int, body: PauseBody, actor: str = Depends(admin_write)):
        return set_status(cid, actor, "paused", body.reason, "paused")

    @app.post("/admin/api/customers/{cid}/resume")
    def resume(cid: int, actor: str = Depends(admin_write)):
        return set_status(cid, actor, "active", None, "resumed")

    @app.post("/admin/api/customers/{cid}/cancel")
    def cancel(cid: int, actor: str = Depends(admin_write)):
        return set_status(cid, actor, "cancelled", "Subscription cancelled", "cancelled")

    @app.post("/admin/api/customers/{cid}/payment")
    def payment(cid: int, body: PaymentBody, actor: str = Depends(admin_write)):
        with store.lock, store.connect() as con:
            c = get(con, cid)
            start = max(date.fromisoformat(c["paid_until"]), date.today())
            y, m = divmod(start.month - 1 + body.months, 12)
            day = min(start.day, calendar.monthrange(start.year + y, m + 1)[1])
            new = date(start.year + y, m + 1, day).isoformat()
            # a payment lifts a pause (but not a cancellation)
            status = "active" if c["status"] == "paused" else c["status"]
            con.execute("UPDATE customers SET paid_until=?, status=?, pause_reason=CASE WHEN ?='active' THEN NULL ELSE pause_reason END WHERE id=?",
                        (new, status, status, cid))
            store.event(con, cid, actor, "payment",
                        f"{body.months} month(s) {body.amount} {body.reference}".strip() + f", paid until {new}")
            return view(get(con, cid))

    @app.post("/admin/api/customers/{cid}/reset-activation")
    def reset_activation(cid: int, actor: str = Depends(admin_write)):
        with store.lock, store.connect() as con:
            c = get(con, cid)
            con.execute("UPDATE customers SET machine_id=NULL, activated_at=NULL WHERE id=?", (cid,))
            store.event(con, cid, actor, "activation_reset", c["hostname"] or "")
            return view(get(con, cid))

    @app.post("/admin/api/customers/{cid}/new-key")
    def new_key(cid: int, actor: str = Depends(admin_write)):
        lic = new_license_key()
        with store.lock, store.connect() as con:
            get(con, cid)
            con.execute("UPDATE customers SET key_hash=?, key_hint=?, machine_id=NULL, activated_at=NULL WHERE id=?",
                        (sha256(lic), lic[-4:], cid))
            store.event(con, cid, actor, "new_key", "old key revoked")
            c = view(get(con, cid))
        c["license_key"] = lic
        return c

    @app.middleware("http")
    async def headers(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                   "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        return resp

    return app
