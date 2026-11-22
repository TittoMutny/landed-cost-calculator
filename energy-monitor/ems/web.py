"""Dashboard web server and JSON API."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import analytics, reports
from .auth import verify_password
from .config import Config
from .db import Database

STATIC = Path(__file__).parent / "static"


def create_app(cfg: Config, db: Database) -> FastAPI:
    app = FastAPI(title="Energy Monitor", docs_url=None, redoc_url=None)
    users = {u["username"]: u for u in (cfg.section("web").get("users") or []) if u.get("username")}
    basic = HTTPBasic(auto_error=False)

    def current_user(creds: HTTPBasicCredentials | None = Depends(basic)) -> dict[str, Any]:
        if not users:  # login disabled (e.g. Cloudflare Access handles it)
            return {"username": "anonymous", "role": "admin"}
        if creds is not None:
            u = users.get(creds.username)
            # compare against a dummy hash when the user is unknown to keep timing similar
            ok = verify_password(creds.password, u["password_hash"] if u else "pbkdf2_sha256$1$AA==$AA==")
            if u and ok:
                return {"username": u["username"], "role": u.get("role", "viewer")}
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required",
                            headers={"WWW-Authenticate": 'Basic realm="Energy Monitor"'})

    def admin(user: dict = Depends(current_user)) -> dict:
        if user["role"] != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
        return user

    def rng(period: str | None, start: str | None, end: str | None) -> tuple[datetime, datetime, str]:
        try:
            return analytics.parse_range(cfg, period, start, end)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

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

    @app.get("/", response_class=HTMLResponse)
    def index(user: dict = Depends(current_user)):
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def get_config(user: dict = Depends(current_user)):
        return {
            "site": cfg.site_name, "currency": cfg.currency, "timezone": str(cfg.tz),
            "poll_seconds": cfg.poll_seconds, "user": user,
            "periods": analytics.PERIOD_LABELS,
            "meters": [{"id": m.id, "name": m.name, "group": m.group, "main": m.main,
                        "protocol": m.protocol} for m in cfg.meters],
            "reports": {k: {"enabled": bool(v.get("enabled")), "recipients": len(v.get("recipients") or [])}
                        for k, v in (cfg.section("reports") or {}).items()},
            "email_enabled": bool(cfg.section("email").get("enabled")),
        }

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
                       by: str = Query("meter", pattern="^(meter|group)$"),
                       user: dict = Depends(current_user)):
        s, e, _ = rng(period, start, end)
        return analytics.timeseries(cfg, db, s, e, bucket, by)

    @app.get("/api/profile")
    def get_profile(period: str | None = None, start: str | None = None, end: str | None = None,
                    user: dict = Depends(current_user)):
        s, e, _ = rng(period, start, end)
        return analytics.hourly_profile(cfg, db, s, e)

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

    @app.get("/api/reports/log")
    def report_log(user: dict = Depends(current_user)):
        return [dict(r) for r in db.recent_reports()]

    @app.post("/api/reports/send")
    def send_report(kind: str = Query(..., pattern="^(daily|weekly|monthly)$"), user: dict = Depends(admin)):
        return reports.run_scheduled_report(cfg, db, kind)

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    return app
