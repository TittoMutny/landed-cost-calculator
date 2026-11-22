"""Dashboard web server and JSON API.

Security model
  * Login with username/password creates a server-side session. The browser only
    holds a random token in an HttpOnly cookie (SameSite=Lax, Secure over HTTPS).
  * Every state-changing request (POST) must carry the session's CSRF token in an
    X-CSRF-Token header and must not be a cross-site request, so another website
    cannot act on behalf of a logged-in user.
  * Repeated failed logins lock the username and the client IP for a while.
  * With no users configured (only sensible behind Cloudflare Access) visitors get
    an automatic session, so the CSRF protection still applies.
"""
from __future__ import annotations

import logging
import math
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import analytics, reports
from .auth import hash_password, verify_password
from .config import Config
from .db import Database
from .security import LoginLimiter, Session, SessionStore

STATIC = Path(__file__).parent / "static"
COOKIE = "ems_session"
GUEST = "guest"
log = logging.getLogger("ems.web")

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; form-action 'self'; base-uri 'none'")


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class KeyBody(BaseModel):
    key: str = Field(min_length=8, max_length=64)


def create_app(cfg: Config, db: Database, integrations=None, license=None) -> FastAPI:
    app = FastAPI(title="Flexpower EMS Kit", docs_url=None, redoc_url=None, openapi_url=None)
    web = cfg.section("web")
    users = {u["username"]: u for u in (web.get("users") or []) if u.get("username")}
    login_required = bool(users)
    sessions = SessionStore(db, idle_seconds=int(float(web.get("session_idle_hours", 12)) * 3600),
                            max_seconds=int(float(web.get("session_max_days", 7)) * 86400))
    limiter = LoginLimiter(db, max_attempts=int(web.get("login_max_attempts", 5)), window_seconds=15 * 60,
                           lock_seconds=int(float(web.get("login_lockout_minutes", 15)) * 60))
    dummy_hash = hash_password(secrets.token_hex(8))  # same cost as a real check, for unknown usernames

    # ------------------------------------------------------------- helpers
    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        # cloudflared connects from this PC; the visitor's address is in CF-Connecting-IP
        if peer in ("127.0.0.1", "::1"):
            return request.headers.get("cf-connecting-ip") or peer
        return peer

    def is_https(request: Request) -> bool:
        setting = web.get("secure_cookies", "auto")
        if isinstance(setting, bool):
            return setting
        return (request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto", "").lower() == "https"
                or '"https"' in request.headers.get("cf-visitor", ""))

    def start_session(request: Request, response: Response, username: str) -> None:
        token, _ = sessions.create(username, client_ip(request))
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=is_https(request),
                            path="/", max_age=sessions.max)

    def user_for(session: Session) -> dict[str, Any] | None:
        if not login_required:
            return {"username": GUEST, "role": "admin"}
        u = users.get(session.username)  # removing a user from config revokes their sessions
        return {"username": u["username"], "role": u.get("role", "viewer")} if u else None

    def session_and_user(request: Request) -> tuple[Session, dict[str, Any]] | None:
        s = sessions.get(request.cookies.get(COOKIE))
        if s is None:
            return None
        u = user_for(s)
        return (s, u) if u else None

    def current_user(request: Request) -> dict[str, Any]:
        su = session_and_user(request)
        if su is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required")
        return su[1]

    def reject_cross_site(request: Request) -> None:
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request blocked")

    def admin_action(request: Request) -> dict[str, Any]:
        """For POSTs that change something: session + CSRF token + admin role."""
        su = session_and_user(request)
        if su is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required")
        session, user = su
        reject_cross_site(request)
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token")
        if user["role"] != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
        return user

    def meter_filter(meter: str | None) -> list[str] | None:
        if not meter:
            return None
        ids = [m for m in meter.split(",") if m]
        unknown = [m for m in ids if cfg.meter(m) is None]
        if unknown:
            raise HTTPException(400, f"unknown meter(s): {', '.join(unknown)}")
        return ids

    def rng(period: str | None, start: str | None, end: str | None) -> tuple[datetime, datetime, str]:
        try:
            return analytics.parse_range(cfg, period, start, end)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ------------------------------------------------------------- public routes
    @app.get("/static/{name}")
    def static_file(name: str):
        f = (STATIC / name).resolve()
        if f.parent != STATIC.resolve() or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/health")
    def health():
        lv = analytics.live(cfg, db)
        return {"ok": True, "meters_online": lv["online"], "meters_total": lv["total"]}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if session_and_user(request) is not None:
            return RedirectResponse("/", status.HTTP_303_SEE_OTHER)
        if not login_required:  # no users configured: hand out a guest session
            resp = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
            start_session(request, resp, GUEST)
            return resp
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    def login(body: LoginBody, request: Request):
        reject_cross_site(request)
        if not login_required:
            raise HTTPException(400, "Login is disabled; no users are configured")
        ip = client_ip(request)
        user_key, ip_key = f"user:{body.username.strip().lower()}", f"ip:{ip}"
        wait = limiter.locked_for([user_key, ip_key])
        if wait:
            log.warning("login blocked (locked) user=%r ip=%s", body.username, ip)
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"Too many failed attempts. Try again in {math.ceil(wait / 60)} minute(s).")
        u = users.get(body.username.strip())
        ok = verify_password(body.password, u["password_hash"] if u else dummy_hash)
        if not (u and ok):
            limiter.failure([user_key, ip_key])
            log.warning("login failed user=%r ip=%s", body.username, ip)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
        limiter.success([user_key])
        log.info("login ok user=%s ip=%s", u["username"], ip)
        resp = JSONResponse({"ok": True})
        start_session(request, resp, u["username"])
        return resp

    @app.post("/api/logout")
    def logout(request: Request):
        reject_cross_site(request)
        sessions.delete(request.cookies.get(COOKIE))
        resp = JSONResponse({"ok": True, "login_required": login_required})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ------------------------------------------------------------- dashboard
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if session_and_user(request) is None:
            return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    def get_config(request: Request, user: dict = Depends(current_user)):
        return {
            "site": cfg.site_name, "currency": cfg.currency, "timezone": str(cfg.tz),
            "poll_seconds": cfg.poll_seconds, "user": user, "login_required": login_required,
            "csrf": session_and_user(request)[0].csrf,
            "periods": analytics.PERIOD_LABELS,
            "meters": [{"id": m.id, "name": m.name, "group": m.group, "main": m.main,
                        "protocol": m.protocol} for m in cfg.meters],
            "reports": {k: {"enabled": bool(v.get("enabled")), "recipients": len(v.get("recipients") or [])}
                        for k, v in (cfg.section("reports") or {}).items()},
            "email_enabled": bool(cfg.section("email").get("enabled")),
            "license": license.state().public() if license is not None else None,
        }

    # ------------------------------------------------------------- subscription
    @app.get("/api/license")
    def get_license(user: dict = Depends(current_user)):
        return license.state().public() if license is not None else {"status": "none", "usable": True}

    @app.post("/api/license/activate")
    def activate_license(body: KeyBody, user: dict = Depends(admin_action)):
        if license is None:
            raise HTTPException(400, "Licensing is not enabled")
        try:
            st = license.activate(body.key)
        except Exception as exc:  # noqa: BLE001 - shown to the admin as-is
            raise HTTPException(400, f"Activation failed: {exc}") from exc
        log.info("licence activated by %s: %s", user["username"], st.status)
        return st.public()

    @app.post("/api/license/check")
    def check_license(user: dict = Depends(admin_action)):
        if license is None:
            raise HTTPException(400, "Licensing is not enabled")
        return license.check_now().public()

    @app.get("/api/live")
    def get_live(user: dict = Depends(current_user)):
        return analytics.live(cfg, db)

    @app.get("/api/summary")
    def get_summary(period: str | None = None, start: str | None = None, end: str | None = None,
                    user: dict = Depends(current_user)):
        s, e, label = rng(period, start, end)
        return analytics.summary(cfg, db, s, e, label)

    @app.get("/api/timeseries")
    def get_timeseries(period: str | None = None, start: str | None = None, end: str | None = None,
                       bucket: str | None = Query(None, pattern="^(15min|hour|day|month)$"),
                       by: str = Query("meter", pattern="^(meter|group)$"), meter: str | None = None,
                       user: dict = Depends(current_user)):
        s, e, _ = rng(period, start, end)
        return analytics.timeseries(cfg, db, s, e, bucket, by, meter_filter(meter))

    @app.get("/api/profile")
    def get_profile(period: str | None = None, start: str | None = None, end: str | None = None,
                    meter: str | None = None, user: dict = Depends(current_user)):
        s, e, _ = rng(period, start, end)
        return analytics.hourly_profile(cfg, db, s, e, meter_filter(meter))

    @app.get("/api/heatmap")
    def get_heatmap(period: str | None = None, start: str | None = None, end: str | None = None,
                    meter: str | None = None, user: dict = Depends(current_user)):
        s, e, _ = rng(period, start, end)
        if (e - s).days > 400:
            raise HTTPException(400, "range too long for a heatmap")
        return analytics.heatmap(cfg, db, s, e, meter_filter(meter))

    @app.get("/api/recent")
    def get_recent(minutes: int = Query(120, ge=5, le=1440), meter: str | None = None,
                   user: dict = Depends(current_user)):
        return analytics.recent_power(cfg, db, minutes, meter_filter(meter))

    @app.get("/api/export.csv")
    def export_csv(kind: str = Query("summary", pattern="^(summary|timeseries)$"),
                   period: str | None = None, start: str | None = None, end: str | None = None,
                   bucket: str | None = Query(None, pattern="^(15min|hour|day|month)$"),
                   user: dict = Depends(current_user)):
        s, e, label = rng(period, start, end)
        if kind == "summary":
            data = reports.summary_csv(analytics.summary(cfg, db, s, e, label))
        else:
            data = reports.timeseries_csv(analytics.timeseries(cfg, db, s, e, bucket))
        name = f"energy-{kind}-{s:%Y%m%d}-{e:%Y%m%d}.csv"
        return Response(data, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/reports/preview", response_class=HTMLResponse)
    def preview_report(kind: str = Query("monthly", pattern="^(daily|weekly|monthly)$"),
                       user: dict = Depends(current_user)):
        return reports.build_report(cfg, db, kind)["html"]

    @app.get("/api/integrations")
    def get_integrations(user: dict = Depends(current_user)):
        from .integrations.modbus_server import register_map

        status_list = integrations.status() if integrations else []
        modbus = any(s["type"] == "modbus_tcp" for s in status_list)
        return {"integrations": status_list,
                "modbus_map": register_map([(m.id, m.name) for m in cfg.meters]) if modbus else []}

    @app.get("/api/scada-tags.csv")
    def scada_tags(user: dict = Depends(current_user)):
        from .integrations.tags import tags_csv

        return Response(tags_csv(cfg), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="flexpower-scada-tags.csv"'})

    @app.get("/api/profinet/{name}")
    def profinet_file(name: str, user: dict = Depends(current_user)):
        from .integrations import profinet_options
        from .integrations.profinet_gateway import gateway_notes, s7_source

        opts = profinet_options(cfg)
        try:
            if name == "EMS_DB.awl":
                data = s7_source(cfg, opts).encode("cp1252", errors="replace")
            elif name == "gateway-settings.txt":
                data = gateway_notes(cfg, opts).encode("utf-8")
            else:
                raise HTTPException(404)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return Response(data, media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/reports/log")
    def report_log(user: dict = Depends(current_user)):
        return [dict(r) for r in db.recent_reports()]

    @app.post("/api/reports/send")
    def send_report(kind: str = Query(..., pattern="^(daily|weekly|monthly)$"), user: dict = Depends(admin_action)):
        log.info("report %s sent manually by %s", kind, user["username"])
        return reports.run_scheduled_report(cfg, db, kind, license=license)

    # Always reachable, whatever the subscription status (sign-in, licence screen, health checks).
    open_paths = {"/api/health", "/api/config", "/api/login", "/api/logout", "/api/license",
                  "/api/license/activate", "/api/license/check"}

    @app.middleware("http")
    async def security_headers(request, call_next):
        path = request.url.path
        if license is not None and path.startswith("/api/") and path not in open_paths:
            st = license.state()
            if not st.usable:
                # 402 Payment Required: the dashboard shows the "subscription paused" screen
                resp = JSONResponse({"detail": st.reason or f"Subscription {st.status}", "license": st.public()},
                                    status_code=402)
                resp.headers["Cache-Control"] = "no-store"
                return resp
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = CSP
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
