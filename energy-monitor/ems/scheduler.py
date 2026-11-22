"""Background jobs: emailed reports and raw-data housekeeping."""
from __future__ import annotations

import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config
from .db import Database
from .reports import run_scheduled_report

log = logging.getLogger("ems.scheduler")


def start_scheduler(cfg: Config, db: Database) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=cfg.tz)
    reports = cfg.section("reports")

    for kind in ("daily", "weekly", "monthly"):
        rc = reports.get(kind) or {}
        if not rc.get("enabled"):
            continue
        hh, mm = (rc.get("time") or "07:00").split(":")
        trig = {"hour": int(hh), "minute": int(mm)}
        if kind == "weekly":
            trig["day_of_week"] = rc.get("day_of_week", "mon")
        elif kind == "monthly":
            trig["day"] = int(rc.get("day_of_month", 1))
        sched.add_job(run_scheduled_report, CronTrigger(timezone=cfg.tz, **trig), args=[cfg, db, kind],
                      id=f"report_{kind}", misfire_grace_time=6 * 3600, coalesce=True)
        log.info("%s report scheduled (%s)", kind, trig)

    days = int(cfg.section("collector").get("raw_retention_days", 60))

    def prune():
        n = db.prune_readings(int(time.time()) - days * 86400)
        log.info("pruned %d raw readings older than %d days", n, days)

    sched.add_job(prune, CronTrigger(hour=3, minute=15, timezone=cfg.tz), id="prune", coalesce=True)
    sched.start()
    return sched
