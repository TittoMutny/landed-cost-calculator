"""Command line entry point.

    python -m ems run                      start collector + scheduler + dashboard
    python -m ems seed-demo --days 90      fill the database with simulated history
    python -m ems test-meter <meter_id>    read one meter once and print the values
    python -m ems report weekly --html out.html [--send]
    python -m ems hash-password            make a password hash for config.yaml
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import ConfigError, load_config
from .db import INTERVAL_SECONDS, Database

log = logging.getLogger("ems")


def setup_logging(cfg_dir: Path, verbose: bool = False) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    (cfg_dir / "data").mkdir(exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(cfg_dir / "data" / "ems.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("pymodbus").setLevel(logging.WARNING)


def cmd_run(args, cfg) -> None:
    import uvicorn

    from .collector import Collector
    from .scheduler import start_scheduler
    from .web import create_app

    db = Database(cfg.db_path)
    collector = Collector(cfg, db)
    collector.start()
    scheduler = start_scheduler(cfg, db)
    web = cfg.section("web")
    host, port = args.host or web.get("host", "127.0.0.1"), args.port or int(web.get("port", 8080))
    log.info("dashboard on http://%s:%s", host, port)
    try:
        uvicorn.run(create_app(cfg, db), host=host, port=port, log_level="warning")
    finally:
        scheduler.shutdown(wait=False)
        collector.stop()


def cmd_seed_demo(args, cfg) -> None:
    """Write simulated 15-minute history for every *simulated* meter."""
    from .collector import simulated_power

    db = Database(cfg.db_path)
    sims = [m for m in cfg.meters if m.protocol == "simulated"]
    if not sims:
        sys.exit("No simulated meters in config; nothing to seed.")
    rng = random.Random(42)
    end = int(time.time()) // INTERVAL_SECONDS * INTERVAL_SECONDS
    start = end - args.days * 86400
    subs = [m for m in sims if not m.main]
    mains = [m for m in sims if m.main]
    rows = []
    for ts in range(start, end, INTERVAL_SECONDS):
        local = datetime.fromtimestamp(ts, cfg.tz)
        total = 0.0
        for m in subs:
            kw = simulated_power(m, local, rng)
            total += kw
            rows.append((m.id, ts, kw / 4, kw * (1 + rng.random() * 0.15)))
        for m in mains:
            kw = total * 1.08 + 1.5
            rows.append((m.id, ts, kw / 4, kw * 1.05))
    with db.connect() as con:
        con.execute("DELETE FROM intervals WHERE start >= ? AND start < ? AND meter_id IN (%s)"
                    % ",".join("?" * len(sims)), [start, end] + [m.id for m in sims])
        con.executemany("INSERT INTO intervals(meter_id, start, kwh, max_kw) VALUES (?,?,?,?)", rows)
    print(f"Seeded {len(rows):,} intervals ({args.days} days) for {len(sims)} simulated meters into {cfg.db_path}")


def cmd_test_meter(args, cfg) -> None:
    from .collector import make_client, read_meter

    m = cfg.meter(args.meter_id)
    if m is None:
        sys.exit(f"Unknown meter '{args.meter_id}'. Known: {', '.join(x.id for x in cfg.meters)}")
    if m.protocol == "simulated":
        sys.exit("That meter is simulated; nothing to test.")
    print(f"Reading {m.name} ({m.connection_key}, unit {m.unit_id}) ...")
    client = make_client(m)
    try:
        values = read_meter(client, m)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"FAILED: {exc}")
    finally:
        client.close()
    for k, v in values.items():
        print(f"  {k:<14} {v}")


def cmd_report(args, cfg) -> None:
    from .reports import build_report, run_scheduled_report

    db = Database(cfg.db_path)
    if args.send:
        print(json.dumps(run_scheduled_report(cfg, db, args.kind, args.to or None), indent=2))
        return
    report = build_report(cfg, db, args.kind)
    out = Path(args.html or f"{args.kind}-report.html")
    out.write_text(report["html"], encoding="utf-8")
    for name, data in report["attachments"]:
        (out.parent / name).write_bytes(data)
    print(f"Wrote {out} and {len(report['attachments'])} CSV files. Subject: {report['subject']}")


def cmd_hash_password(args, cfg) -> None:
    from .auth import hash_password

    pw = getpass.getpass("New password: ")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match")
    if len(pw) < 10:
        sys.exit("Use at least 10 characters")
    print(hash_password(pw))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ems", description="Flexpower EMS Kit")
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--host")
    r.add_argument("--port", type=int)
    s = sub.add_parser("seed-demo")
    s.add_argument("--days", type=int, default=90)
    t = sub.add_parser("test-meter")
    t.add_argument("meter_id")
    rp = sub.add_parser("report")
    rp.add_argument("kind", choices=["daily", "weekly", "monthly"])
    rp.add_argument("--html", help="write the report here instead of emailing")
    rp.add_argument("--send", action="store_true", help="email it to the configured recipients")
    rp.add_argument("--to", nargs="*", help="override recipients when sending")
    sub.add_parser("hash-password")
    args = p.parse_args(argv)

    if args.cmd == "hash-password":
        return cmd_hash_password(args, None)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        sys.exit(f"Config error: {exc}")
    setup_logging(cfg.base_dir, args.verbose)
    {"run": cmd_run, "seed-demo": cmd_seed_demo, "test-meter": cmd_test_meter,
     "report": cmd_report}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
