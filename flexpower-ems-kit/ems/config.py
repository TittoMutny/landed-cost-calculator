"""Load and validate config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

REGISTER_TYPES = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2,
                  "uint64": 4, "int64": 4, "float32": 2, "float64": 4}


class ConfigError(ValueError):
    pass


@dataclass
class Register:
    name: str
    address: int
    type: str = "float32"
    function: str = "holding"
    scale: float = 1.0
    word_order: str = "big"

    @property
    def count(self) -> int:
        return REGISTER_TYPES[self.type]


@dataclass
class Meter:
    id: str
    name: str
    protocol: str
    group: str = "Ungrouped"
    main: bool = False
    unit_id: int = 1
    host: str | None = None
    port: int = 502
    serial_port: str | None = None
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1
    bytesize: int = 8
    timeout: float = 2.0
    registers: list[Register] = field(default_factory=list)
    sim_kw: float = 5.0
    sim_idle_kw: float = 0.5

    @property
    def connection_key(self) -> str:
        """Meters with the same key share one Modbus connection and are polled in turn."""
        if self.protocol == "tcp":
            return f"tcp://{self.host}:{self.port}"
        if self.protocol == "rtu":
            return f"rtu://{self.serial_port}"
        return f"sim://{self.id}"


@dataclass
class Config:
    raw: dict[str, Any]
    base_dir: Path
    meters: list[Meter]

    # convenient accessors -------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name) or {}

    @property
    def site_name(self) -> str:
        return self.section("site").get("name", "Flexpower EMS Kit")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.section("site").get("timezone", "UTC"))

    @property
    def currency(self) -> str:
        return self.section("site").get("currency", "")

    @property
    def co2_factor(self) -> float:
        return float(self.section("site").get("co2_kg_per_kwh", 0) or 0)

    @property
    def poll_seconds(self) -> float:
        return float(self.section("collector").get("poll_seconds", 60))

    @property
    def db_path(self) -> Path:
        p = Path(self.section("storage").get("database", "data/ems.sqlite3"))
        return p if p.is_absolute() else self.base_dir / p

    @property
    def smtp_password_file(self) -> Path:
        p = Path(self.section("email").get("password_file") or "data/smtp.secret")
        return p if p.is_absolute() else self.base_dir / p

    @property
    def smtp_password(self) -> str | None:
        """Environment variable first (handy for testing), then the encrypted file
        written by `python -m ems set-smtp-password`."""
        from .credentials import load_secret

        env = self.section("email").get("password_env")
        if env and os.environ.get(env):
            return os.environ[env]
        return load_secret(self.smtp_password_file)

    def meter(self, meter_id: str) -> Meter | None:
        return next((m for m in self.meters if m.id == meter_id), None)


def _parse_register(name: str, spec: dict[str, Any], where: str) -> Register:
    if "address" not in spec:
        raise ConfigError(f"{where}: register '{name}' has no address")
    reg = Register(
        name=name,
        address=int(spec["address"]),
        type=str(spec.get("type", "float32")).lower(),
        function=str(spec.get("function", "holding")).lower(),
        scale=float(spec.get("scale", 1.0)),
        word_order=str(spec.get("word_order", "big")).lower(),
    )
    if reg.type not in REGISTER_TYPES:
        raise ConfigError(f"{where}: register '{name}' type '{reg.type}' must be one of {sorted(REGISTER_TYPES)}")
    if reg.function not in ("holding", "input"):
        raise ConfigError(f"{where}: register '{name}' function must be 'holding' or 'input'")
    if reg.word_order not in ("big", "little"):
        raise ConfigError(f"{where}: register '{name}' word_order must be 'big' or 'little'")
    return reg


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}. Copy config.example.yaml to config.yaml first.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles = raw.get("profiles") or {}

    meters: list[Meter] = []
    seen: set[str] = set()
    for i, m in enumerate(raw.get("meters") or []):
        where = f"meters[{i}]"
        if not isinstance(m, dict) or "id" not in m:
            raise ConfigError(f"{where}: every meter needs an 'id'")
        mid = str(m["id"])
        if mid in seen:
            raise ConfigError(f"{where}: duplicate meter id '{mid}'")
        seen.add(mid)
        protocol = str(m.get("protocol", "tcp")).lower()
        if protocol not in ("tcp", "rtu", "simulated"):
            raise ConfigError(f"{where}: protocol must be tcp, rtu or simulated")
        if protocol == "tcp" and not m.get("host"):
            raise ConfigError(f"{where}: tcp meter '{mid}' needs 'host'")
        if protocol == "rtu" and not m.get("serial_port"):
            raise ConfigError(f"{where}: rtu meter '{mid}' needs 'serial_port'")

        reg_specs: dict[str, Any] = {}
        if m.get("profile"):
            prof = profiles.get(m["profile"])
            if prof is None:
                raise ConfigError(f"{where}: unknown profile '{m['profile']}'")
            reg_specs.update(prof.get("registers") or {})
        reg_specs.update(m.get("registers") or {})  # per-meter overrides
        if protocol != "simulated" and not reg_specs:
            raise ConfigError(f"{where}: meter '{mid}' needs a profile or registers")

        meters.append(Meter(
            id=mid,
            name=str(m.get("name", mid)),
            protocol=protocol,
            group=str(m.get("group", "Ungrouped")),
            main=bool(m.get("main", False)),
            unit_id=int(m.get("unit_id", 1)),
            host=m.get("host"),
            port=int(m.get("port", 502)),
            serial_port=m.get("serial_port"),
            baudrate=int(m.get("baudrate", 9600)),
            parity=str(m.get("parity", "N")).upper(),
            stopbits=int(m.get("stopbits", 1)),
            bytesize=int(m.get("bytesize", 8)),
            timeout=float(m.get("timeout", 2.0)),
            registers=[_parse_register(n, s, where) for n, s in reg_specs.items()],
            sim_kw=float(m.get("sim_kw", 5.0)),
            sim_idle_kw=float(m.get("sim_idle_kw", 0.5)),
        ))

    cfg = Config(raw=raw, base_dir=path.parent, meters=meters)
    cfg.tz  # validate timezone early
    return cfg
