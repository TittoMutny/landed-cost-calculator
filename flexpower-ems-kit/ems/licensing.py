"""Subscription licence for Flexpower EMS Kit.

The app checks in with the Flexpower licence server every few hours and gets a
token signed with Flexpower's private key. The token says whether this
installation may run (active / overdue / paused / cancelled / invalid), the
plan limits, and until when the answer is valid while offline.

What a status means here:
  active, overdue  everything works (overdue shows a payment reminder)
  paused           dashboard, email reports and SCADA/MQTT feeds are locked;
                   meters keep recording so no data is lost
  cancelled,
  invalid, unlicensed   locked, and real meters are no longer recorded
  demo             no key, only simulated meters: fully usable for demos

A token is accepted only if its signature matches FLEXPOWER_PUBLIC_KEY, it was
issued for this PC, it hasn't expired and the PC clock isn't set back.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config

log = logging.getLogger("ems.license")

# Set by `python -m license_server init --ems <this folder>`. Not configurable on
# purpose: a customer who could swap it could sign their own licences.
FLEXPOWER_PUBLIC_KEY = "REPLACE_WITH_FLEXPOWER_PUBLIC_KEY"
DEFAULT_SERVER = "https://license.flexpower.example"

USABLE = {"active", "overdue", "demo"}
KEEP_RECORDING = USABLE | {"paused"}
CLOCK_TOLERANCE = 600  # seconds the PC clock may be behind the server's


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_token(token: str, public_key_b64: str) -> dict[str, Any]:
    """Return the payload if the signature is valid; raise ValueError otherwise."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        body, sig = token.split(".")
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        pub.verify(_b64d(sig), body.encode())
        return json.loads(_b64d(body))
    except InvalidSignature as exc:
        raise ValueError("licence signature is not valid") from exc
    except Exception as exc:  # noqa: BLE001 - malformed token, bad key...
        raise ValueError(f"licence token is malformed: {exc}") from exc


def _os_machine_id() -> str | None:
    """The operating system's own permanent ID: Windows MachineGuid or Linux machine-id.
    Network adapter addresses are deliberately not used: they change when Wi-Fi /
    Ethernet / VPN adapters come and go, and Python invents a random one if none is found."""
    try:
        if os.name == "nt":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0])
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            if Path(p).exists() and Path(p).read_text().strip():
                return Path(p).read_text().strip()
    except OSError:
        pass
    return None


def machine_id(data_dir: Path | None = None) -> str:
    """Stable identifier for this PC, sent to the licence server to tie a key to one PC."""
    raw = _os_machine_id()
    if raw is None:  # no OS id available: create one and keep it with the data
        f = (data_dir or Path("data")) / "machine-id"
        try:
            raw = f.read_text().strip()
        except OSError:
            raw = ""
        if not raw:
            raw = uuid.uuid4().hex
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(raw)
    return hashlib.sha256(f"flexpower-ems|{raw}".encode()).hexdigest()[:32]


@dataclass
class LicenseState:
    status: str
    reason: str = ""
    customer: str = ""
    plan: str = ""
    paid_until: str | None = None
    pause_on: str | None = None
    max_meters: int | None = None
    features: dict[str, bool] = field(default_factory=dict)
    support: dict[str, str] = field(default_factory=dict)
    expires: int | None = None
    last_check: int | None = None
    last_error: str | None = None
    key_hint: str = ""

    @property
    def usable(self) -> bool:
        return self.status in USABLE

    def feature(self, name: str) -> bool:
        return self.status == "demo" or (self.usable and bool(self.features.get(name, False)))

    def public(self) -> dict[str, Any]:
        """What the dashboard may show (no key, no machine id)."""
        return {"status": self.status, "usable": self.usable, "reason": self.reason, "customer": self.customer,
                "plan": self.plan, "paid_until": self.paid_until, "pause_on": self.pause_on,
                "max_meters": self.max_meters, "features": self.features, "support": self.support,
                "last_check": self.last_check, "last_error": self.last_error, "key_hint": self.key_hint}


class LicenseManager:
    def __init__(self, cfg: Config, public_key: str | None = None):
        self.cfg = cfg
        lic = cfg.section("license")
        self.server = str(lic.get("server_url") or DEFAULT_SERVER).rstrip("/")
        self.public_key = public_key or FLEXPOWER_PUBLIC_KEY
        self.path = cfg.base_dir / "data" / "license.json"
        self.machine = machine_id(cfg.base_dir / "data")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._saved = self._load()

    # ------------------------------------------------------------- storage
    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._saved), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def key(self) -> str | None:
        return self._saved.get("key")

    # ------------------------------------------------------------- state
    def _demo_possible(self) -> bool:
        return bool(self.cfg.meters) and all(m.protocol == "simulated" for m in self.cfg.meters)

    def state(self, now: float | None = None) -> LicenseState:
        now = time.time() if now is None else now
        with self._lock:
            saved = dict(self._saved)
        hint = (saved.get("key") or "")[-4:]
        common = {"last_check": saved.get("last_check"), "last_error": saved.get("last_error"), "key_hint": hint}
        token = saved.get("token")
        if not saved.get("key") or not token:
            if self._demo_possible():
                return LicenseState("demo", "Demo mode: simulated meters only. Activate a licence to connect real meters.", **common)
            return LicenseState("unlicensed", "No licence activated on this PC.", **common)
        try:
            p = verify_token(token, self.public_key)
        except ValueError as exc:
            return LicenseState("invalid", str(exc), **common)
        base = dict(customer=p.get("customer", ""), plan=p.get("plan", ""), paid_until=p.get("paid_until"),
                    pause_on=p.get("pause_on"), max_meters=p.get("max_meters"), features=p.get("features") or {},
                    support=p.get("support") or {}, expires=p.get("exp"), **common)
        if p.get("machine") != self.machine:
            return LicenseState("invalid", "This licence was issued for a different PC.", **base)
        if now + CLOCK_TOLERANCE < p.get("iat", 0) or now + CLOCK_TOLERANCE < saved.get("max_seen_iat", 0):
            return LicenseState("invalid", "The PC clock is behind the licence server. Correct the date and time.", **base)
        status = p.get("status", "invalid")
        if status in ("active", "overdue") and now > p.get("exp", 0):
            reason = ("Payment overdue: subscription paused." if p.get("pause_on") and
                      time.strftime("%Y-%m-%d", time.gmtime(now)) >= p["pause_on"]
                      else "Could not reach the Flexpower licence server for too long. Check the internet connection.")
            return LicenseState("paused", reason, **base)
        return LicenseState(status, p.get("reason", ""), **base)

    def allowed_meter_ids(self) -> set[str]:
        """Meters that may be polled: within the plan limit, and only while recording is allowed."""
        st = self.state()
        if st.status == "demo":
            return {m.id for m in self.cfg.meters if m.protocol == "simulated"}
        if st.status not in KEEP_RECORDING:
            return set()
        limit = st.max_meters if st.max_meters is not None else len(self.cfg.meters)
        return {m.id for m in self.cfg.meters[:limit]}

    # ------------------------------------------------------------- server calls
    def _post(self, path: str, key: str) -> tuple[int, dict[str, Any]]:
        body = json.dumps({"key": key, "machine_id": self.machine, "hostname": socket.gethostname()[:128],
                           "version": __version__, "meters": len(self.cfg.meters)}).encode()
        req = urllib.request.Request(self.server + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": f"FlexpowerEMS/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"detail": f"HTTP {e.code}"}

    def _apply(self, code: int, resp: dict[str, Any], key: str, activating: bool = False) -> LicenseState:
        token = resp.get("token")
        if not token:
            raise RuntimeError(resp.get("detail") or f"licence server answered HTTP {code}")
        payload = verify_token(token, self.public_key)  # never store an unsigned / forged answer
        if payload.get("machine") != self.machine:
            raise RuntimeError("licence server answer was not for this PC")
        if activating and payload.get("status") == "invalid":
            # a mistyped key must not replace a working licence
            raise RuntimeError(payload.get("reason") or "licence key rejected")
        with self._lock:
            self._saved.update(key=key, token=token, last_check=int(time.time()), last_error=None,
                               max_seen_iat=max(self._saved.get("max_seen_iat", 0), payload.get("iat", 0)))
            self._save()
        st = self.state()
        log.info("licence: %s %s", st.status, st.reason)
        return st

    def activate(self, key: str) -> LicenseState:
        key = key.strip().upper()
        code, resp = self._post("/api/v1/activate", key)
        return self._apply(code, resp, key, activating=True)

    def check_now(self) -> LicenseState:
        if not self.key:
            return self.state()
        try:
            code, resp = self._post("/api/v1/check", self.key)
            return self._apply(code, resp, self.key)
        except Exception as exc:  # noqa: BLE001 - offline: keep the cached (signed) answer
            with self._lock:
                self._saved["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                self._save()
            log.warning("licence check failed: %s", exc)
            return self.state()

    # ------------------------------------------------------------- background
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="license", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            st = self.check_now()
            hours = 6.0
            try:
                hours = float(verify_token(self._saved["token"], self.public_key).get("check_hours", 6)) if self._saved.get("token") else 6.0
            except (ValueError, KeyError):
                pass
            wait = 15 * 60 if st.last_error or not st.usable else hours * 3600
            self._stop.wait(wait)

    def stop(self) -> None:
        self._stop.set()
