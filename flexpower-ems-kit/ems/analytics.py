"""Consumption analysis on top of the 15-minute interval table."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime
from typing import Any

from .config import Config, Meter
from .db import INTERVAL_SECONDS, Database

PERIOD_LABELS = {
    "today": "Today", "yesterday": "Yesterday",
    "this_week": "This week", "last_week": "Last week",
    "this_month": "This month", "last_month": "Last month",
    "last_7_days": "Last 7 days", "last_30_days": "Last 30 days",
    "this_year": "This year",
}


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------
def _month_start(d: datetime) -> datetime:
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_months(d: datetime, n: int) -> datetime:
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1)


def resolve_period(name: str, cfg: Config, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return (start, end) as timezone-aware local datetimes; end is exclusive."""
    tz = cfg.tz
    now = (now or datetime.now(tz)).astimezone(tz)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    if name == "today":
        return today, today + timedelta(days=1)
    if name == "yesterday":
        return today - timedelta(days=1), today
    if name == "this_week":
        return week, week + timedelta(days=7)
    if name == "last_week":
        return week - timedelta(days=7), week
    if name == "this_month":
        return _month_start(now), _add_months(_month_start(now), 1)
    if name == "last_month":
        return _add_months(_month_start(now), -1), _month_start(now)
    if name == "last_7_days":
        return today - timedelta(days=6), today + timedelta(days=1)
    if name == "last_30_days":
        return today - timedelta(days=29), today + timedelta(days=1)
    if name == "this_year":
        return today.replace(month=1, day=1), today.replace(year=today.year + 1, month=1, day=1)
    raise ValueError(f"unknown period '{name}'")


def parse_range(cfg: Config, period: str | None, start: str | None, end: str | None,
                now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Either a named period or explicit YYYY-MM-DD dates (end date inclusive)."""
    if start and end:
        s = datetime.fromisoformat(start).replace(tzinfo=cfg.tz)
        e = datetime.fromisoformat(end).replace(tzinfo=cfg.tz) + timedelta(days=1)
        if e <= s:
            raise ValueError("end must be after start")
        return s, e, f"{s:%d %b %Y} – {(e - timedelta(days=1)):%d %b %Y}"
    period = period or "this_month"
    s, e = resolve_period(period, cfg, now)
    return s, e, PERIOD_LABELS.get(period, period)


def previous_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Same-length period immediately before; whole months map to the previous month."""
    if start == _month_start(start) and end == _add_months(start, 1):
        return _add_months(start, -1), start
    return start - (end - start), start


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def rate_at(cfg: Config, local: datetime) -> float:
    t = cfg.section("tariff")
    base = float(t.get("rate_per_kwh", 0) or 0)
    peak = t.get("peak")
    if not peak:
        return base
    if peak.get("weekdays_only", True) and local.weekday() >= 5:
        return base
    s, e = _parse_hhmm(peak.get("start", "00:00")), _parse_hhmm(peak.get("end", "00:00"))
    now_t = local.time()
    inside = s <= now_t < e if s < e else (now_t >= s or now_t < e)
    return float(peak.get("rate_per_kwh", base)) if inside else base


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _pct_change(cur: float, prev: float) -> float | None:
    return None if prev <= 0 else round((cur - prev) / prev * 100, 1)


def _site_meters(cfg: Config) -> tuple[list[Meter], list[Meter]]:
    mains = [m for m in cfg.meters if m.main]
    subs = [m for m in cfg.meters if not m.main]
    return mains, subs


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def summary(cfg: Config, db: Database, start: datetime, end: datetime, label: str = "",
            with_previous: bool = True) -> dict[str, Any]:
    tz = cfg.tz
    s_ts, e_ts = int(start.timestamp()), int(end.timestamp())
    rows = db.intervals(s_ts, e_ts)
    mains, subs = _site_meters(cfg)
    site_ids = {m.id for m in (mains or subs)}

    per: dict[str, dict[str, Any]] = {m.id: {"kwh": 0.0, "cost": 0.0, "kw": [], "peak_kw": 0.0,
                                               "peak_at": None, "night_kwh": 0.0} for m in cfg.meters}
    site_demand: dict[int, float] = defaultdict(float)
    for r in rows:
        p = per.get(r["meter_id"])
        if p is None:  # meter removed from config
            continue
        local = datetime.fromtimestamp(r["start"], tz)
        kwh = r["kwh"]
        avg_kw = kwh * 3600 / INTERVAL_SECONDS
        p["kwh"] += kwh
        p["cost"] += kwh * rate_at(cfg, local)
        p["kw"].append(avg_kw)
        peak = r["max_kw"] if r["max_kw"] is not None else avg_kw
        if peak > p["peak_kw"]:
            p["peak_kw"], p["peak_at"] = peak, r["start"]
        if local.hour >= 22 or local.hour < 6:
            p["night_kwh"] += kwh
        if r["meter_id"] in site_ids:
            site_demand[r["start"]] += avg_kw

    prev = None
    if with_previous:
        ps, pe = previous_range(start, end)
        now = datetime.now(tz)
        partial = start < now < end
        if partial:  # period still running: compare like-for-like up to the same point
            pe = min(pe, ps + (now - start))
        prev = summary(cfg, db, ps, pe, with_previous=False)
        prev_by_id = {m["id"]: m for m in prev["meters"] + prev["mains"]}

    def meter_row(m: Meter) -> dict[str, Any]:
        p = per[m.id]
        hours = len(p["kw"]) * INTERVAL_SECONDS / 3600
        avg_kw = p["kwh"] / hours if hours else 0.0
        base = _percentile(p["kw"], 0.10)
        row = {
            "id": m.id, "name": m.name, "group": m.group, "main": m.main,
            "kwh": round(p["kwh"], 2), "cost": round(p["cost"], 2),
            "co2_kg": round(p["kwh"] * cfg.co2_factor, 1),
            "peak_kw": round(p["peak_kw"], 2),
            "peak_at": datetime.fromtimestamp(p["peak_at"], tz).isoformat() if p["peak_at"] else None,
            "avg_kw": round(avg_kw, 2), "baseload_kw": round(base, 2),
            "baseload_pct": round(base / avg_kw * 100, 1) if avg_kw > 0 else 0.0,
            "night_pct": round(p["night_kwh"] / p["kwh"] * 100, 1) if p["kwh"] > 0 else 0.0,
            "hours_with_data": round(hours, 1),
        }
        if prev is not None:
            pk = prev_by_id.get(m.id, {}).get("kwh", 0.0)
            row["prev_kwh"] = pk
            row["change_pct"] = _pct_change(row["kwh"], pk)
        return row

    sub_rows = sorted((meter_row(m) for m in subs), key=lambda r: r["kwh"], reverse=True)
    main_rows = [meter_row(m) for m in mains]
    sub_total = sum(r["kwh"] for r in sub_rows)
    for r in sub_rows:
        r["share_pct"] = round(r["kwh"] / sub_total * 100, 1) if sub_total else 0.0

    site_rows = main_rows or sub_rows
    site_kwh = sum(r["kwh"] for r in site_rows)
    site_cost = sum(r["cost"] for r in site_rows)
    peak_ts = max(site_demand, key=site_demand.get) if site_demand else None

    groups: dict[str, dict[str, float]] = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0})
    for r in sub_rows:
        groups[r["group"]]["kwh"] += r["kwh"]
        groups[r["group"]]["cost"] += r["cost"]
    group_rows = sorted(({"group": g, "kwh": round(v["kwh"], 2), "cost": round(v["cost"], 2),
                          "share_pct": round(v["kwh"] / sub_total * 100, 1) if sub_total else 0.0}
                         for g, v in groups.items()), key=lambda r: r["kwh"], reverse=True)

    totals = {
        "kwh": round(site_kwh, 2), "cost": round(site_cost, 2),
        "co2_kg": round(site_kwh * cfg.co2_factor, 1),
        "avg_rate": round(site_cost / site_kwh, 3) if site_kwh else 0.0,
        "peak_demand_kw": round(site_demand[peak_ts], 2) if peak_ts else 0.0,
        "peak_demand_at": datetime.fromtimestamp(peak_ts, tz).isoformat() if peak_ts else None,
        "submetered_kwh": round(sub_total, 2),
        "unmetered_kwh": round(site_kwh - sub_total, 2) if main_rows else None,
    }
    if prev is not None:
        totals["prev_kwh"] = prev["totals"]["kwh"]
        totals["prev_cost"] = prev["totals"]["cost"]
        totals["change_pct"] = _pct_change(totals["kwh"], prev["totals"]["kwh"])
        totals["compare_partial"] = partial

    result = {
        "period": {"label": label, "start": start.isoformat(), "end": end.isoformat()},
        "currency": cfg.currency, "totals": totals,
        "mains": main_rows, "meters": sub_rows, "groups": group_rows,
    }
    if prev is not None:
        result["previous_period"] = prev["period"]
    result["insights"] = insights(cfg, result) if with_previous else []
    return result


def insights(cfg: Config, s: dict[str, Any]) -> list[str]:
    """Plain-language findings for the dashboard and emails."""
    out: list[str] = []
    cur = cfg.currency
    t = s["totals"]
    meters = [m for m in s["meters"] if m["kwh"] > 0]
    if not meters and t["kwh"] <= 0:
        return ["No consumption recorded in this period yet."]

    if t.get("change_pct") is not None:
        direction = "up" if t["change_pct"] > 0 else "down"
        ref = "the same point of the previous period" if t.get("compare_partial") else "the previous period"
        out.append(f"Site consumption is {direction} {abs(t['change_pct']):.1f}% on {ref} "
                   f"({t['kwh']:,.0f} kWh vs {t['prev_kwh']:,.0f} kWh).")

    if meters:
        top = meters[0]
        out.append(f"{top['name']} was the largest consumer: {top['kwh']:,.0f} kWh "
                   f"({top['share_pct']:.0f}% of sub-metered load, {cur} {top['cost']:,.0f}).")
        if len(meters) >= 3:
            top3 = sum(m["share_pct"] for m in meters[:3])
            out.append(f"The top 3 loads ({', '.join(m['name'] for m in meters[:3])}) account for {top3:.0f}% of sub-metered energy.")

    risers = [m for m in meters if m.get("change_pct") is not None and m["change_pct"] >= 15 and m["kwh"] - m["prev_kwh"] > 10]
    if risers:
        r = max(risers, key=lambda m: m["kwh"] - m["prev_kwh"])
        out.append(f"{r['name']} rose {r['change_pct']:.0f}% (+{r['kwh'] - r['prev_kwh']:,.0f} kWh) on the previous period — worth checking.")

    idle = [m for m in meters if m["baseload_kw"] >= 0.5 and m["peak_kw"] > 0 and m["baseload_kw"] / m["peak_kw"] >= 0.2]
    if idle:
        m = max(idle, key=lambda m: m["baseload_kw"])
        out.append(f"{m['name']} never drops below about {m['baseload_kw']:.1f} kW, even when idle "
                   f"({m['baseload_kw'] / m['peak_kw'] * 100:.0f}% of its peak). Check for equipment left running or leaks.")

    night = [m for m in meters if m["night_pct"] >= 25 and m["kwh"] > 20]
    if night:
        m = max(night, key=lambda m: m["night_pct"])
        out.append(f"{m['night_pct']:.0f}% of {m['name']}'s energy was used between 22:00 and 06:00.")

    if t.get("unmetered_kwh") is not None and t["kwh"] > 0:
        share = t["unmetered_kwh"] / t["kwh"] * 100
        if share >= 10:
            out.append(f"{share:.0f}% of the supply ({t['unmetered_kwh']:,.0f} kWh) is not covered by any sub-meter.")
        elif share < -3:
            out.append("Sub-meters add up to more than the main meter — check meter scaling or CT ratios.")

    if t.get("peak_demand_at"):
        at = datetime.fromisoformat(t["peak_demand_at"])
        out.append(f"Peak demand was {t['peak_demand_kw']:,.1f} kW on {at:%a %d %b at %H:%M}.")
    return out


# ---------------------------------------------------------------------------
# Time series & profiles
# ---------------------------------------------------------------------------
def _bucket_key(local: datetime, bucket: str) -> datetime:
    if bucket == "15min":
        return local
    if bucket == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "month":
        return _month_start(local)
    raise ValueError("bucket must be 15min, hour, day or month")


def auto_bucket(start: datetime, end: datetime) -> str:
    days = (end - start).total_seconds() / 86400
    return "15min" if days <= 1.01 else "hour" if days <= 3 else "day" if days <= 93 else "month"


def timeseries(cfg: Config, db: Database, start: datetime, end: datetime,
               bucket: str | None = None, by: str = "meter") -> dict[str, Any]:
    """kWh per bucket, one series per meter (or per group). Main meters are kept separate."""
    tz = cfg.tz
    bucket = bucket or auto_bucket(start, end)
    rows = db.intervals(int(start.timestamp()), int(end.timestamp()))
    meters = {m.id: m for m in cfg.meters}

    # Build the full list of bucket labels so gaps show as zero.
    labels: list[datetime] = []
    cur = _bucket_key(start, bucket)
    while cur < end:
        labels.append(cur)
        if bucket == "month":
            cur = _add_months(cur, 1)
        elif bucket == "day":
            cur = _bucket_key(cur + timedelta(days=1, hours=2), "day")  # +2h absorbs DST shifts
        else:  # step on absolute time so DST changes neither skip nor repeat
            seconds = 900 if bucket == "15min" else 3600
            cur = datetime.fromtimestamp(cur.timestamp() + seconds, tz)
    idx = {d: i for i, d in enumerate(labels)}

    series: dict[str, list[float]] = {}
    main_series: dict[str, list[float]] = {}
    for r in rows:
        m = meters.get(r["meter_id"])
        if m is None:
            continue
        key = _bucket_key(datetime.fromtimestamp(r["start"], tz), bucket)
        i = idx.get(key)
        if i is None:
            continue
        target = main_series if m.main else series
        name = m.name if (by == "meter" or m.main) else m.group
        target.setdefault(name, [0.0] * len(labels))[i] += r["kwh"]

    fmt = {"15min": "%H:%M", "hour": "%d %b %H:00", "day": "%a %d %b", "month": "%b %Y"}[bucket]
    rnd = lambda d: {k: [round(v, 3) for v in vals] for k, vals in d.items()}  # noqa: E731
    return {"bucket": bucket, "labels": [d.strftime(fmt) for d in labels],
            "iso": [d.isoformat() for d in labels], "series": rnd(series), "mains": rnd(main_series)}


def hourly_profile(cfg: Config, db: Database, start: datetime, end: datetime) -> dict[str, Any]:
    """Average site kW for each hour of the day, weekdays vs weekends."""
    tz = cfg.tz
    mains, subs = _site_meters(cfg)
    ids = [m.id for m in (mains or subs)]
    rows = db.intervals(int(start.timestamp()), int(end.timestamp()), ids)
    per_slot: dict[tuple[bool, int, int], float] = defaultdict(float)  # (weekend, day, hour) -> kWh
    for r in rows:
        local = datetime.fromtimestamp(r["start"], tz)
        per_slot[(local.weekday() >= 5, local.toordinal(), local.hour)] += r["kwh"]
    out = {}
    for weekend in (False, True):
        sums, counts = [0.0] * 24, [0] * 24
        for (we, _day, hour), kwh in per_slot.items():
            if we == weekend:
                sums[hour] += kwh
                counts[hour] += 1
        out["weekend" if weekend else "weekday"] = [round(sums[h] / counts[h], 2) if counts[h] else None for h in range(24)]
    return out


def live(cfg: Config, db: Database) -> dict[str, Any]:
    states = db.meter_states()
    now = time.time()
    stale = max(3 * cfg.poll_seconds, 180)
    meters = []
    site_kw = 0.0
    mains, subs = _site_meters(cfg)
    site_ids = {m.id for m in (mains or subs)}
    for m in cfg.meters:
        st = states.get(m.id)
        values = json.loads(st["last_values"]) if st and st["last_values"] else {}
        last_ok = st["last_ok_ts"] if st else None
        online = bool(last_ok and now - last_ok < stale)
        err = st["last_error"].split("|", 1)[1] if st and st["last_error"] else None
        if online and m.id in site_ids and values.get("power_kw") is not None:
            site_kw += values["power_kw"]
        meters.append({"id": m.id, "name": m.name, "group": m.group, "main": m.main,
                       "protocol": m.protocol, "online": online, "last_seen": last_ok,
                       "error": err, "values": values})
    return {"ts": int(now), "site_kw": round(site_kw, 2), "meters": meters,
            "online": sum(1 for m in meters if m["online"]), "total": len(meters)}
