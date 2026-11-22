"""OPC UA server: the natural way to bring EMS values into PCS 7 OS / WinCC,
Ignition, AVEVA, Kepware and most modern SCADA.

Address space (namespace "urn:flexpower:ems"), all variables read-only:

  Objects/FlexpowerEMS/
    Site/       TotalPower_kW, Today_kWh, ThisMonth_kWh, MetersOnline, MetersTotal, Heartbeat, LastUpdate
    Meters/<meter id>/
                Name, Group, Online, Power_kW, Energy_kWh, Voltage_V, Current_A,
                PowerFactor, Today_kWh, ThisMonth_kWh, LastReading

NodeIds are predictable strings, e.g.  ns=2;s=Meters.compressor.Power_kW
(the namespace index is normally 2; clients should resolve it from the URI).
When a meter is offline its measured values carry the OPC UA status
BadCommunicationError, so the SCADA shows them as bad quality.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import Integration

log = logging.getLogger("ems.integrations.opcua")
NAMESPACE = "urn:flexpower:ems"

SITE_VARS = [("TotalPower_kW", "power_kw", "Double"), ("Today_kWh", "today_kwh", "Double"),
             ("ThisMonth_kWh", "month_kwh", "Double"), ("MetersOnline", "meters_online", "UInt32"),
             ("MetersTotal", "meters_total", "UInt32")]
METER_VARS = [("Power_kW", "power_kw"), ("Energy_kWh", "energy_kwh"), ("Voltage_V", "voltage_v"),
              ("Current_A", "current_a"), ("PowerFactor", "pf"), ("Today_kWh", "today_kwh"), ("ThisMonth_kWh", "month_kwh")]
MEASURED = {"power_kw", "energy_kwh", "voltage_v", "current_a", "pf"}  # bad quality when the meter is offline


def _make_user_manager(users: dict[str, str], allow_anonymous: bool):
    from asyncua.crypto.permission_rules import User, UserRole

    from ..auth import verify_password

    class EmsUserManager:
        """Everyone who gets in is a plain reader; nobody is ever an OPC UA admin."""

        def get_user(self, iserver, username=None, password=None, certificate=None):
            if username:
                h = users.get(username)
                if h and verify_password(password or "", h):
                    return User(role=UserRole.User, name=username)
                log.warning("opcua: rejected login for user %r", username)
                return None
            return User(role=UserRole.User, name="anonymous") if allow_anonymous else None

    return EmsUserManager()


class OpcUaIntegration(Integration):
    type_name = "opcua"

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        self.endpoint = str(options.get("endpoint", "opc.tcp://0.0.0.0:4840/flexpower/"))
        self.security = str(options.get("security", "sign_encrypt")).lower()  # none | sign_encrypt | both
        self.users = {u["username"]: u["password_hash"] for u in options.get("users") or [] if u.get("username")}
        self.allow_anonymous = bool(options.get("allow_anonymous", not self.users))
        self.cert_dir = Path(options.get("certificate_dir") or cfg.base_dir / "data" / "opcua")
        if not self.cert_dir.is_absolute():
            self.cert_dir = cfg.base_dir / self.cert_dir
        self.trusted_dir = options.get("trusted_clients_dir")
        self.ns = 2
        self._latest: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping: asyncio.Event | None = None
        self._start_error: Exception | None = None
        self._nodes: dict[str, Any] = {}
        self.heartbeat = 0

    def describe(self) -> str:
        return f"{self.endpoint} (security: {self.security}, {'anonymous allowed' if self.allow_anonymous else 'login required'})"

    def publish(self, snap: dict[str, Any]) -> None:
        with self._lock:
            self._latest = snap

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.security not in ("none", "sign_encrypt", "both"):
            raise ValueError("opcua security must be none, sign_encrypt or both")
        if not self.allow_anonymous and not self.users:
            raise ValueError("opcua: allow_anonymous is false but no users are configured")
        self._thread = threading.Thread(target=self._run, name="opcua-server", daemon=True)
        self._thread.start()
        self._ready.wait(30)
        if self._start_error:
            raise self._start_error
        if not self._ready.is_set():
            raise TimeoutError("OPC UA server did not start within 30 s")
        self.running = True

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:  # noqa: BLE001
            if not self._ready.is_set():
                self._start_error = exc
                self._ready.set()
            else:
                self.last_error = str(exc)
                self.running = False
                log.exception("OPC UA server stopped: %s", exc)
        finally:
            self._loop.close()

    def stop(self) -> None:
        self.running = False
        if self._loop and self._stopping:
            self._loop.call_soon_threadsafe(self._stopping.set)
        if self._thread:
            self._thread.join(timeout=10)

    # ------------------------------------------------------------- server
    async def _setup_security(self, server) -> None:
        from asyncua import ua

        policies = []
        if self.security in ("sign_encrypt", "both"):
            from asyncua.crypto.cert_gen import setup_self_signed_certificate
            from cryptography.x509.oid import ExtendedKeyUsageOID

            self.cert_dir.mkdir(parents=True, exist_ok=True)
            key, cert = self.cert_dir / "server_key.pem", self.cert_dir / "server_cert.der"
            host = socket.gethostname()
            await setup_self_signed_certificate(key, cert, "urn:flexpower:ems:server", host,
                                                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH],
                                                {"countryName": "KE", "organizationName": "Flexpower EMS Kit",
                                                 "commonName": f"Flexpower EMS Kit@{host}"})
            await server.load_certificate(str(cert))
            await server.load_private_key(str(key))
            policies += [ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt, ua.SecurityPolicyType.Basic256Sha256_Sign]
            if self.trusted_dir:
                from asyncua.crypto.truststore import TrustStore
                from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions

                d = Path(self.trusted_dir)
                d = d if d.is_absolute() else self.cfg.base_dir / d
                d.mkdir(parents=True, exist_ok=True)
                store = TrustStore([d], [])
                await store.load()
                # Only client certificates copied into trusted_clients_dir are accepted.
                opts = (CertificateValidatorOptions.TIME_RANGE | CertificateValidatorOptions.TRUSTED
                        | CertificateValidatorOptions.PEER_CLIENT)
                server.set_certificate_validator(CertificateValidator(opts, store))
        if self.security in ("none", "both"):
            policies.append(ua.SecurityPolicyType.NoSecurity)
        server.set_security_policy(policies)
        ids = (["Anonymous"] if self.allow_anonymous else []) + (["Username"] if self.users else [])
        server.set_security_IDs(ids)

    async def _main(self) -> None:
        from asyncua import Server, ua

        server = Server(user_manager=_make_user_manager(self.users, self.allow_anonymous))
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("Flexpower EMS Kit")
        await server.set_application_uri("urn:flexpower:ems:server")
        await self._setup_security(server)
        self.ns = await server.register_namespace(NAMESPACE)
        ns = self.ns

        def nid(path: str):
            return ua.NodeId(path, ns)

        root = await server.nodes.objects.add_folder(nid("FlexpowerEMS"), ua.QualifiedName("FlexpowerEMS", ns))
        site = await root.add_folder(nid("Site"), ua.QualifiedName("Site", ns))
        for name, _key, vtype in SITE_VARS:
            self._nodes[f"site.{name}"] = await site.add_variable(nid(f"Site.{name}"), ua.QualifiedName(name, ns),
                                                                   0.0 if vtype == "Double" else 0, getattr(ua.VariantType, vtype))
        self._nodes["site.Heartbeat"] = await site.add_variable(nid("Site.Heartbeat"), ua.QualifiedName("Heartbeat", ns), 0, ua.VariantType.UInt32)
        self._nodes["site.LastUpdate"] = await site.add_variable(nid("Site.LastUpdate"), ua.QualifiedName("LastUpdate", ns),
                                                                  datetime.now(timezone.utc), ua.VariantType.DateTime)
        meters = await root.add_folder(nid("Meters"), ua.QualifiedName("Meters", ns))
        for m in self.cfg.meters:
            f = await meters.add_folder(nid(f"Meters.{m.id}"), ua.QualifiedName(m.id, ns))
            await f.add_variable(nid(f"Meters.{m.id}.Name"), ua.QualifiedName("Name", ns), m.name, ua.VariantType.String)
            await f.add_variable(nid(f"Meters.{m.id}.Group"), ua.QualifiedName("Group", ns), m.group, ua.VariantType.String)
            self._nodes[f"{m.id}.Online"] = await f.add_variable(nid(f"Meters.{m.id}.Online"), ua.QualifiedName("Online", ns), False, ua.VariantType.Boolean)
            for name, _key in METER_VARS:
                self._nodes[f"{m.id}.{name}"] = await f.add_variable(nid(f"Meters.{m.id}.{name}"), ua.QualifiedName(name, ns), 0.0, ua.VariantType.Double)
            self._nodes[f"{m.id}.LastReading"] = await f.add_variable(nid(f"Meters.{m.id}.LastReading"), ua.QualifiedName("LastReading", ns),
                                                                       datetime(1970, 1, 1, tzinfo=timezone.utc), ua.VariantType.DateTime)
        # Variables are created read-only (no set_writable), so clients cannot change them.

        self._stopping = asyncio.Event()
        async with server:
            self._ready.set()
            while not self._stopping.is_set():
                with self._lock:
                    snap, self._latest = self._latest, None
                if snap is not None:
                    await self._write(snap)
                try:
                    await asyncio.wait_for(self._stopping.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass

    async def _write(self, snap: dict[str, Any]) -> None:
        from asyncua import ua

        now = datetime.now(timezone.utc)

        def dv(value, vtype, good=True):
            status = ua.StatusCode(ua.StatusCodes.Good if good else ua.StatusCodes.BadCommunicationError)
            return ua.DataValue(ua.Variant(value, vtype), StatusCode=status, SourceTimestamp=now, ServerTimestamp=now)

        for name, key, vtype in SITE_VARS:
            v = snap["site"].get(key) or 0
            await self._nodes[f"site.{name}"].write_value(dv(float(v) if vtype == "Double" else int(v), getattr(ua.VariantType, vtype)))
        self.heartbeat = (self.heartbeat + 1) & 0xFFFFFFFF
        await self._nodes["site.Heartbeat"].write_value(dv(self.heartbeat, ua.VariantType.UInt32))
        await self._nodes["site.LastUpdate"].write_value(dv(now, ua.VariantType.DateTime))
        for m in snap["meters"]:
            if f"{m['id']}.Online" not in self._nodes:
                continue
            await self._nodes[f"{m['id']}.Online"].write_value(dv(bool(m["online"]), ua.VariantType.Boolean))
            for name, key in METER_VARS:
                v = m.get(key)
                good = v is not None and (m["online"] or key not in MEASURED)
                await self._nodes[f"{m['id']}.{name}"].write_value(dv(float(v or 0.0), ua.VariantType.Double, good))
            if m["age_s"] is not None:
                seen = datetime.fromtimestamp(snap["ts"] - m["age_s"], timezone.utc)
                await self._nodes[f"{m['id']}.LastReading"].write_value(dv(seen, ua.VariantType.DateTime))
