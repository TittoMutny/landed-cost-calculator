"""Weekly / monthly consumption reports: HTML email body + CSV attachments."""
from __future__ import annotations

import csv
import io
import logging
import smtplib
import ssl
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import escape
from typing import Any

from . import analytics
from .config import Config
from .db import Database

log = logging.getLogger("ems.reports")

REPORT_PERIOD = {"weekly": "last_week", "monthly": "last_month", "daily": "yesterday"}


def report_range(cfg: Config, kind: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    period = REPORT_PERIOD[kind]
    start, end = analytics.resolve_period(period, cfg, now)
    if kind == "monthly":
        label = f"{start:%B %Y}"
    elif kind == "weekly":
        label = f"Week of {start:%d %b} – {(end - timedelta(days=1)):%d %b %Y}"
    else:
        label = f"{start:%A %d %B %Y}"
    return start, end, label


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def summary_csv(s: dict[str, Any]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    cur = s["currency"]
    w.writerow(["Meter", "Group", "Main meter", "kWh", f"Cost ({cur})", "Share %", "Peak kW", "Peak at",
                "Average kW", "Base load kW", "Night use %", "Previous kWh", "Change %", "CO2 kg"])
    for m in s["mains"] + s["meters"]:
        w.writerow([m["name"], m["group"], "yes" if m["main"] else "", m["kwh"], m["cost"], m.get("share_pct", ""),
                    m["peak_kw"], m["peak_at"] or "", m["avg_kw"], m["baseload_kw"], m["night_pct"],
                    m.get("prev_kwh", ""), "" if m.get("change_pct") is None else m["change_pct"], m["co2_kg"]])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens it as UTF-8


def timeseries_csv(ts: dict[str, Any]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    names = list(ts["mains"]) + list(ts["series"])
    w.writerow(["Period start"] + [f"{n} (kWh)" for n in names])
    for i, iso in enumerate(ts["iso"]):
        w.writerow([iso] + [(ts["mains"].get(n) or ts["series"].get(n))[i] for n in names])
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def _fmt(v: float | None, d: int = 0) -> str:
    return "–" if v is None else f"{v:,.{d}f}"


def _change(v: float | None) -> str:
    if v is None:
        return '<span style="color:#888">–</span>'
    color = "#c0392b" if v > 0 else "#1e8449"
    arrow = "▲" if v > 0 else "▼"
    return f'<span style="color:{color}">{arrow} {abs(v):.1f}%</span>'


def render_html(cfg: Config, s: dict[str, Any], title: str, daily: dict[str, Any] | None = None) -> str:
    cur = escape(s["currency"])
    t = s["totals"]
    url = (cfg.section("email").get("dashboard_url") or "").strip()
    max_kwh = max([m["kwh"] for m in s["meters"]] + [1])

    rows = []
    for m in s["meters"]:
        bar = int(m["kwh"] / max_kwh * 100)
        rows.append(f"""
        <tr>
          <td style="padding:6px 8px;border-bottom:1px solid #eee">{escape(m['name'])}<br>
              <span style="color:#888;font-size:12px">{escape(m['group'])}</span></td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;width:35%">
              <div style="background:#2e86de;height:10px;width:{bar}%;border-radius:2px"></div></td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right">{_fmt(m['kwh'])}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right">{_fmt(m.get('share_pct'), 1)}%</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right">{_fmt(m['cost'])}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right">{_fmt(m['peak_kw'], 1)}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right">{_change(m.get('change_pct'))}</td>
        </tr>""")

    group_rows = "".join(
        f'<tr><td style="padding:4px 8px">{escape(g["group"])}</td>'
        f'<td style="padding:4px 8px;text-align:right">{_fmt(g["kwh"])} kWh</td>'
        f'<td style="padding:4px 8px;text-align:right">{g["share_pct"]:.1f}%</td>'
        f'<td style="padding:4px 8px;text-align:right">{cur} {_fmt(g["cost"])}</td></tr>'
        for g in s["groups"])

    daily_html = ""
    if daily and daily["labels"]:
        totals = [sum(vals[i] for vals in (daily["mains"] or daily["series"]).values()) for i in range(len(daily["labels"]))]
        mx = max(totals + [1])
        bars = "".join(
            f'<td style="vertical-align:bottom;text-align:center;padding:0 1px">'
            f'<div title="{_fmt(v)} kWh" style="background:#54a0ff;width:100%;height:{max(1, int(v / mx * 80))}px"></div>'
            f'<div style="font-size:9px;color:#888;margin-top:2px">{escape(lbl.split(" ")[1] if " " in lbl else lbl)}</div></td>'
            for lbl, v in zip(daily["labels"], totals))
        daily_html = f"""
        <h3 style="margin:24px 0 8px">Daily consumption (kWh)</h3>
        <table style="width:100%;border-collapse:collapse;height:100px"><tr>{bars}</tr></table>"""

    tiles = [
        ("Energy", f"{_fmt(t['kwh'])} kWh", _change(t.get("change_pct")) + " vs previous"),
        ("Cost", f"{cur} {_fmt(t['cost'])}", f"avg {cur} {t['avg_rate']:.2f}/kWh"),
        ("Peak demand", f"{_fmt(t['peak_demand_kw'], 1)} kW",
         datetime.fromisoformat(t["peak_demand_at"]).strftime("%a %d %b %H:%M") if t["peak_demand_at"] else ""),
        ("CO₂", f"{_fmt(t['co2_kg'] / 1000, 2)} t", ""),
    ]
    tile_html = "".join(
        f'<td style="padding:12px;background:#f5f7fa;border-radius:6px;width:25%">'
        f'<div style="color:#666;font-size:12px">{a}</div>'
        f'<div style="font-size:20px;font-weight:600;margin:4px 0">{b}</div>'
        f'<div style="font-size:12px">{c}</div></td>' for a, b, c in tiles)

    insight_html = "".join(f"<li style='margin-bottom:6px'>{escape(i)}</li>" for i in s["insights"])
    link = f'<p><a href="{escape(url)}" style="color:#2e86de">Open the live dashboard →</a></p>' if url else ""

    return f"""<!doctype html><html><body style="margin:0;background:#eef1f5">
<div style="max-width:760px;margin:0 auto;padding:24px;font-family:Segoe UI,Arial,sans-serif;color:#222;background:#fff">
  <div style="color:#888;font-size:13px">{escape(cfg.site_name)}</div>
  <h2 style="margin:4px 0 16px">{escape(title)}</h2>
  <table style="width:100%;border-collapse:separate;border-spacing:6px"><tr>{tile_html}</tr></table>
  <h3 style="margin:24px 0 8px">Key findings</h3>
  <ul style="padding-left:20px;line-height:1.45">{insight_html}</ul>
  {daily_html}
  <h3 style="margin:24px 0 8px">Consumption by machine</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr style="background:#f5f7fa;text-align:left">
      <th style="padding:6px 8px">Meter</th><th></th>
      <th style="padding:6px 8px;text-align:right">kWh</th><th style="padding:6px 8px;text-align:right">Share</th>
      <th style="padding:6px 8px;text-align:right">{cur}</th><th style="padding:6px 8px;text-align:right">Peak kW</th>
      <th style="padding:6px 8px;text-align:right">vs prev</th>
    </tr>
    {''.join(rows)}
  </table>
  <h3 style="margin:24px 0 8px">By area</h3>
  <table style="width:100%;border-collapse:collapse;font-size:14px">{group_rows}</table>
  {link}
  <p style="color:#999;font-size:12px;margin-top:24px">Full figures are attached as CSV files.
  Period: {escape(s['period']['start'][:10])} to {escape((datetime.fromisoformat(s['period']['end']) - timedelta(days=1)).date().isoformat())}.<br>Sent by Flexpower EMS Kit.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# Build & send
# ---------------------------------------------------------------------------
def build_report(cfg: Config, db: Database, kind: str, now: datetime | None = None) -> dict[str, Any]:
    start, end, label = report_range(cfg, kind, now)
    s = analytics.summary(cfg, db, start, end, label)
    daily = analytics.timeseries(cfg, db, start, end, "day" if kind != "daily" else "hour")
    title = f"{kind.capitalize()} energy report — {label}"
    stem = f"energy-{kind}-{start:%Y-%m-%d}"
    return {
        "kind": kind, "start": start, "end": end, "subject": f"{cfg.site_name}: {title}",
        "html": render_html(cfg, s, title, daily if kind != "daily" else None),
        "attachments": [(f"{stem}-by-meter.csv", summary_csv(s)),
                        (f"{stem}-{daily['bucket']}.csv", timeseries_csv(daily))],
        "summary": s,
    }


def send_email(cfg: Config, recipients: list[str], subject: str, html: str,
               attachments: list[tuple[str, bytes]] = ()) -> None:
    e = cfg.section("email")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = e.get("from_addr") or e.get("username")
    msg["To"] = ", ".join(recipients)
    msg.set_content("This report is best viewed in an email client that shows HTML.")
    msg.add_alternative(html, subtype="html")
    for name, data in attachments:
        msg.add_attachment(data, maintype="text", subtype="csv", filename=name)

    host, port = e.get("smtp_host"), int(e.get("smtp_port", 587))
    security = (e.get("security") or "starttls").lower()
    ctx = ssl.create_default_context()
    smtp_cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    kwargs = {"context": ctx} if security == "ssl" else {}
    with smtp_cls(host, port, timeout=30, **kwargs) as smtp:
        if security == "starttls":
            smtp.starttls(context=ctx)
        if e.get("username") and cfg.smtp_password:
            smtp.login(e["username"], cfg.smtp_password)
        smtp.send_message(msg)


def run_scheduled_report(cfg: Config, db: Database, kind: str, recipients: list[str] | None = None,
                         license=None) -> dict[str, Any]:
    if license is not None:
        st = license.state()
        if not st.feature("reports"):
            why = "email reports are not included in this plan" if st.usable else f"subscription {st.status}"
            db.log_report(int(time.time()), kind, 0, 0, "", "skipped", why)
            log.info("%s report skipped: %s", kind, why)
            return {"status": "skipped", "reason": why}
    rcfg = cfg.section("reports").get(kind) or {}
    recipients = recipients or list(rcfg.get("recipients") or [])
    report = build_report(cfg, db, kind)
    start, end = int(report["start"].timestamp()), int(report["end"].timestamp())
    if not cfg.section("email").get("enabled"):
        db.log_report(int(time.time()), kind, start, end, ",".join(recipients), "skipped", "email disabled in config")
        log.info("%s report built but email is disabled", kind)
        return {"status": "skipped", "reason": "email disabled in config", "subject": report["subject"]}
    if not recipients:
        db.log_report(int(time.time()), kind, start, end, "", "skipped", "no recipients")
        return {"status": "skipped", "reason": "no recipients configured", "subject": report["subject"]}
    try:
        send_email(cfg, recipients, report["subject"], report["html"], report["attachments"])
    except Exception as exc:  # noqa: BLE001
        log.exception("sending %s report failed", kind)
        db.log_report(int(time.time()), kind, start, end, ",".join(recipients), "failed", str(exc))
        return {"status": "failed", "reason": str(exc), "subject": report["subject"]}
    db.log_report(int(time.time()), kind, start, end, ",".join(recipients), "sent")
    log.info("%s report sent to %s", kind, recipients)
    return {"status": "sent", "recipients": recipients, "subject": report["subject"]}
