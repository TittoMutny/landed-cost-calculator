"""SQLite storage.

Two levels of data are kept:
  readings  - every polled value (pruned after raw_retention_days)
  intervals - kWh and peak kW per meter per 15 minutes (kept forever, used by
              the dashboard and reports)
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

INTERVAL_SECONDS = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts INTEGER NOT NULL, meter_id TEXT NOT NULL, quantity TEXT NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_readings_meter_ts ON readings(meter_id, ts);
CREATE INDEX IF NOT EXISTS ix_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS intervals (
    meter_id TEXT NOT NULL, start INTEGER NOT NULL, kwh REAL NOT NULL DEFAULT 0, max_kw REAL,
    PRIMARY KEY (meter_id, start)
);
CREATE INDEX IF NOT EXISTS ix_intervals_start ON intervals(start);

CREATE TABLE IF NOT EXISTS meter_state (
    meter_id TEXT PRIMARY KEY,
    last_ts INTEGER, last_energy REAL, last_power REAL,
    last_ok_ts INTEGER, last_error TEXT, last_values TEXT
);

CREATE TABLE IF NOT EXISTS report_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL,
    period_start INTEGER, period_end INTEGER, recipients TEXT, status TEXT, detail TEXT
);
"""

# A cumulative energy jump bigger than this between two polls is treated as a
# meter swap / bad read rather than real consumption.
MAX_KWH_JUMP = 100_000.0


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
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

    # ------------------------------------------------------------------ ingest
    def ingest(self, meter_id: str, ts: int, values: dict[str, float],
               max_gap_seconds: float = 3600) -> float:
        """Store one poll result and add its consumption to the 15-minute interval.

        Consumption comes from the cumulative energy_kwh register when the meter
        has one; otherwise power_kw is integrated over time. Returns the kWh
        attributed to this poll.
        """
        import json

        energy = values.get("energy_kwh")
        power = values.get("power_kw")
        with self._write_lock, self.connect() as con:
            con.executemany(
                "INSERT INTO readings(ts, meter_id, quantity, value) VALUES (?,?,?,?)",
                [(ts, meter_id, q, float(v)) for q, v in values.items()],
            )
            st = con.execute("SELECT * FROM meter_state WHERE meter_id=?", (meter_id,)).fetchone()
            delta = 0.0
            if st is not None and st["last_ts"] is not None and ts > st["last_ts"]:
                if energy is not None and st["last_energy"] is not None:
                    d = energy - st["last_energy"]
                    if 0 <= d < MAX_KWH_JUMP:  # negative = counter reset / meter replaced
                        delta = d
                elif energy is None and power is not None and st["last_power"] is not None:
                    dt = ts - st["last_ts"]
                    if dt <= max_gap_seconds:
                        delta = max(0.0, (power + st["last_power"]) / 2 * dt / 3600)

            start = ts - ts % INTERVAL_SECONDS
            con.execute(
                """INSERT INTO intervals(meter_id, start, kwh, max_kw) VALUES (?,?,?,?)
                   ON CONFLICT(meter_id, start) DO UPDATE SET
                     kwh = kwh + excluded.kwh,
                     max_kw = CASE WHEN excluded.max_kw IS NULL THEN max_kw
                                   WHEN max_kw IS NULL THEN excluded.max_kw
                                   ELSE MAX(max_kw, excluded.max_kw) END""",
                (meter_id, start, delta, power),
            )
            con.execute(
                """INSERT INTO meter_state(meter_id, last_ts, last_energy, last_power, last_ok_ts, last_error, last_values)
                   VALUES (?,?,?,?,?,NULL,?)
                   ON CONFLICT(meter_id) DO UPDATE SET
                     last_ts=excluded.last_ts,
                     last_energy=COALESCE(excluded.last_energy, last_energy),
                     last_power=excluded.last_power,
                     last_ok_ts=excluded.last_ok_ts, last_error=NULL,
                     last_values=excluded.last_values""",
                (meter_id, ts, energy, power, ts, json.dumps(values)),
            )
        return delta

    def record_error(self, meter_id: str, ts: int, error: str) -> None:
        with self._write_lock, self.connect() as con:
            con.execute(
                """INSERT INTO meter_state(meter_id, last_error) VALUES (?,?)
                   ON CONFLICT(meter_id) DO UPDATE SET last_error=excluded.last_error""",
                (meter_id, f"{ts}|{error[:500]}"),
            )

    # ------------------------------------------------------------------ queries
    def intervals(self, start: int, end: int, meter_ids: list[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT meter_id, start, kwh, max_kw FROM intervals WHERE start >= ? AND start < ?"
        args: list = [start, end]
        if meter_ids:
            sql += f" AND meter_id IN ({','.join('?' * len(meter_ids))})"
            args += meter_ids
        with self.connect() as con:
            return con.execute(sql + " ORDER BY start", args).fetchall()

    def meter_states(self) -> dict[str, sqlite3.Row]:
        with self.connect() as con:
            return {r["meter_id"]: r for r in con.execute("SELECT * FROM meter_state")}

    def prune_readings(self, older_than_ts: int) -> int:
        with self._write_lock, self.connect() as con:
            return con.execute("DELETE FROM readings WHERE ts < ?", (older_than_ts,)).rowcount

    def log_report(self, ts: int, kind: str, start: int, end: int, recipients: str,
                   status: str, detail: str = "") -> None:
        with self._write_lock, self.connect() as con:
            con.execute(
                "INSERT INTO report_log(ts, kind, period_start, period_end, recipients, status, detail) VALUES (?,?,?,?,?,?,?)",
                (ts, kind, start, end, recipients, status, detail[:1000]),
            )

    def recent_reports(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute("SELECT * FROM report_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
