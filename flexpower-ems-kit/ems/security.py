"""Login sessions and brute-force lockout, stored in SQLite so they survive restarts."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from .db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL,
    created INTEGER NOT NULL, last_seen INTEGER NOT NULL, ip TEXT
);
CREATE TABLE IF NOT EXISTS login_failures (
    key TEXT PRIMARY KEY, count INTEGER NOT NULL, first_ts INTEGER NOT NULL, locked_until INTEGER
);
"""


def _hash(token: str) -> str:
    # Only a hash of the cookie value is stored, so a copied database can't be used to log in.
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    username: str
    csrf: str
    created: int


class SessionStore:
    def __init__(self, db: Database, idle_seconds: int, max_seconds: int):
        self.db = db
        self.idle = idle_seconds
        self.max = max_seconds
        with db.connect() as con:
            con.executescript(SCHEMA)

    def create(self, username: str, ip: str | None = None) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.db.connect() as con:
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (_hash(token), username, csrf, now, now, ip))
            con.execute("DELETE FROM sessions WHERE last_seen < ? OR created < ?", (now - self.idle, now - self.max))
        return token, Session(username, csrf, now)

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = int(time.time())
        h = _hash(token)
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM sessions WHERE token_hash=?", (h,)).fetchone()
            if row is None:
                return None
            if now - row["last_seen"] > self.idle or now - row["created"] > self.max:
                con.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
                return None
            if now - row["last_seen"] >= 60:  # limit writes to once a minute per session
                con.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, h))
        return Session(row["username"], row["csrf"], row["created"])

    def delete(self, token: str | None) -> None:
        if token:
            with self.db.connect() as con:
                con.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))

    def delete_user(self, username: str) -> None:
        with self.db.connect() as con:
            con.execute("DELETE FROM sessions WHERE username=?", (username,))


class LoginLimiter:
    """Locks a username, and separately a client IP, after too many failed logins."""

    def __init__(self, db: Database, max_attempts: int, window_seconds: int, lock_seconds: int):
        self.db = db
        self.max_attempts = max_attempts
        self.window = window_seconds
        self.lock = lock_seconds
        with db.connect() as con:
            con.executescript(SCHEMA)

    def locked_for(self, keys: list[str]) -> int:
        """Seconds until every given key is unlocked (0 = not locked)."""
        now = int(time.time())
        with self.db.connect() as con:
            rows = con.execute(
                f"SELECT locked_until FROM login_failures WHERE key IN ({','.join('?' * len(keys))})", keys
            ).fetchall()
        return max([0] + [r["locked_until"] - now for r in rows if r["locked_until"] and r["locked_until"] > now])

    def failure(self, keys: list[str]) -> None:
        now = int(time.time())
        with self.db.connect() as con:
            for k in keys:
                row = con.execute("SELECT * FROM login_failures WHERE key=?", (k,)).fetchone()
                if row is None or now - row["first_ts"] > self.window or (row["locked_until"] and row["locked_until"] <= now):
                    count, first = 1, now
                else:
                    count, first = row["count"] + 1, row["first_ts"]
                locked = now + self.lock if count >= self.max_attempts else None
                con.execute("INSERT OR REPLACE INTO login_failures VALUES (?,?,?,?)", (k, count, first, locked))

    def success(self, keys: list[str]) -> None:
        with self.db.connect() as con:
            con.execute(f"DELETE FROM login_failures WHERE key IN ({','.join('?' * len(keys))})", keys)
