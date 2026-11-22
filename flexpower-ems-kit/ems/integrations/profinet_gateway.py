"""PROFINET via a Modbus TCP -> PROFINET gateway.

A Windows PC can't be a PROFINET IO-Device without special hardware, so the
usual way to feed a PCS 7 / S7 controller is a small gateway (HMS Anybus,
Hilscher netTAP, Moxa MGate and similar):

    EMS PC --Modbus TCP--> gateway --PROFINET IO--> PCS 7 AS

The gateway polls this server and presents the data as PROFINET input data.
This plugin is a read-only Modbus TCP server like `modbus_tcp`, but with a
compact layout without gaps that maps byte-for-byte onto an S7 data block:

  Site, registers 0..11 (24 bytes)
    0   Heartbeat        INT  (+1 every update; use it as a watchdog)
    1   MetersOnline     INT
    2   TotalPower_kW    REAL
    4   Today_kWh        REAL
    6   ThisMonth_kWh    REAL
    8   MetersPublished  INT
    9   reserved         3 x WORD

  Meter k (k = 0, 1, ... in the order of `meters:`) at 12 + 16*k (32 bytes)
    +0  Status           WORD  bit 0 = online, bit 1 = values valid
    +1  Age_s            INT   seconds since the last reading (32767 = never)
    +2  Power_kW         REAL
    +4  Energy_kWh       REAL
    +6  Voltage_V        REAL
    +8  Current_A        REAL
    +10 PowerFactor      REAL
    +12 Today_kWh        REAL
    +14 ThisMonth_kWh    REAL

REALs are IEEE-754 big-endian (S7 byte order) with no word swap. Values that
are not available are 0.0 with the 'values valid' bit cleared, never NaN,
because NaN can disturb PLC arithmetic.
"""
from __future__ import annotations

from typing import Any

from .modbus_server import MAX_READ, ModbusTcpIntegration

SITE_REGS = 12
METER_REGS = 16

SITE_LAYOUT = [("Heartbeat", 0, "INT"), ("MetersOnline", 1, "INT"), ("TotalPower_kW", 2, "REAL"),
               ("Today_kWh", 4, "REAL"), ("ThisMonth_kWh", 6, "REAL"), ("MetersPublished", 8, "INT"),
               ("Reserved1", 9, "WORD"), ("Reserved2", 10, "WORD"), ("Reserved3", 11, "WORD")]
METER_LAYOUT = [("Status", 0, "WORD"), ("Age_s", 1, "INT"), ("Power_kW", 2, "REAL"), ("Energy_kWh", 4, "REAL"),
                ("Voltage_V", 6, "REAL"), ("Current_A", 8, "REAL"), ("PowerFactor", 10, "REAL"),
                ("Today_kWh", 12, "REAL"), ("ThisMonth_kWh", 14, "REAL")]
_SNAP_KEYS = {"Power_kW": "power_kw", "Energy_kWh": "energy_kwh", "Voltage_V": "voltage_v", "Current_A": "current_a",
              "PowerFactor": "pf", "Today_kWh": "today_kwh", "ThisMonth_kWh": "month_kwh"}

STATUS_ONLINE = 0x0001
STATUS_VALID = 0x0002


def selected_meters(cfg, options: dict[str, Any]) -> list:
    ids = options.get("meters")
    if not ids:
        return list(cfg.meters)
    by_id = {m.id: m for m in cfg.meters}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"profinet_gateway: unknown meter id(s) {unknown}")
    return [by_id[i] for i in ids]


def io_bytes(n_meters: int) -> int:
    return (SITE_REGS + METER_REGS * n_meters) * 2


def read_commands(n_meters: int) -> list[tuple[int, int]]:
    """Modbus read commands for the gateway (start, count), each <= 125 registers
    and cut on meter boundaries so no REAL is split between two commands."""
    total = SITE_REGS + METER_REGS * n_meters
    per = SITE_REGS + METER_REGS * ((MAX_READ - SITE_REGS) // METER_REGS)  # site + 7 meters = 124
    cmds, start = [], 0
    while start < total:
        count = min(per if start == 0 else (MAX_READ // METER_REGS) * METER_REGS, total - start)
        cmds.append((start, count))
        start += count
    return cmds


class ProfinetGatewayIntegration(ModbusTcpIntegration):
    type_name = "profinet_gateway"

    def __init__(self, cfg, options):
        options = dict(options)
        options["word_order"] = "big"  # S7 REAL byte order; the gateway must not swap
        self.meters = selected_meters(cfg, options)
        super().__init__(cfg, options)
        self.io_size = int(options.get("gateway_io_bytes", 0) or 0)

    def table_size(self) -> int:
        return SITE_REGS + METER_REGS * max(1, len(self.meters))

    def describe(self) -> str:
        return (f"modbus://{self.host}:{self.port} for a PROFINET gateway, {len(self.meters)} meters, "
                f"{io_bytes(len(self.meters))} bytes input data")

    def start(self) -> None:
        need = io_bytes(len(self.meters))
        if self.io_size and need > self.io_size:
            raise ValueError(f"{need} bytes needed but gateway_io_bytes is {self.io_size}; "
                             f"publish fewer meters (meters: [...]) or use a bigger gateway")
        super().start()

    def publish(self, snap: dict[str, Any]) -> None:
        t = self.table
        self.heartbeat = (self.heartbeat + 1) & 0x7FFF  # stays positive as an S7 INT
        by_id = {m["id"]: m for m in snap["meters"]}
        site = snap["site"]
        updates = [
            (0, [self.heartbeat]), (1, [min(site["meters_online"], 32767)]),
            (2, t.encode(site.get("power_kw") or 0.0, "float32")),
            (4, t.encode(site.get("today_kwh") or 0.0, "float32")),
            (6, t.encode(site.get("month_kwh") or 0.0, "float32")),
            (8, [len(self.meters)]),
        ]
        for k, meter in enumerate(self.meters):
            base = SITE_REGS + METER_REGS * k
            m = by_id.get(meter.id)
            online = bool(m and m["online"])
            status = (STATUS_ONLINE if online else 0) | (STATUS_VALID if online else 0)
            age = 32767 if not m or m["age_s"] is None else min(int(m["age_s"]), 32767)
            updates += [(base, [status]), (base + 1, [age])]
            for name, off, _ in METER_LAYOUT[2:]:
                key = _SNAP_KEYS[name]
                v = m.get(key) if m else None
                # kWh totals are still meaningful when the meter is offline; live values are not
                if v is None or (not online and key not in ("today_kwh", "month_kwh")):
                    v = 0.0
                updates.append((base + off, t.encode(float(v), "float32")))
        with t.lock:
            for addr, words in updates:
                t.regs[addr:addr + len(words)] = words


# ---------------------------------------------------------------------------
# Files for the PLC / gateway engineer
# ---------------------------------------------------------------------------
def _ident(text: str, max_len: int = 24) -> str:
    out = "".join(c if c.isalnum() else "_" for c in text).strip("_") or "Meter"
    return ("M_" + out if out[0].isdigit() else out)[:max_len]


def s7_source(cfg, options: dict[str, Any]) -> str:
    """STEP 7 (classic / PCS 7) STL source with a UDT and a DB matching the input data.
    Import it under Sources, review, then compile."""
    meters = selected_meters(cfg, options)
    udt = int(options.get("udt_number", 100))
    db = int(options.get("db_number", 100))
    comments = {"Status": "bit0 online, bit1 values valid", "Age_s": "seconds since last reading",
                "Heartbeat": "+1 every update - watchdog"}
    lines = [f"// Flexpower EMS Kit - PROFINET gateway data, {io_bytes(len(meters))} bytes",
             "// Copy the gateway input area into this DB with SFC 14 DPRD_DAT (consistent read).",
             "// REALs are big-endian (S7 order): the gateway must NOT swap bytes or words.", "",
             f"TYPE UDT {udt}", "TITLE = 'EMS meter'", "VERSION : 0.1", "", "  STRUCT"]
    for name, _, kind in METER_LAYOUT:
        c = f"\t//{comments[name]}" if name in comments else ""
        lines.append(f"   {name} : {kind} ;{c}")
    lines += ["  END_STRUCT ;", "END_TYPE", "", f"DATA_BLOCK DB {db}", f"TITLE = '{cfg.site_name[:40]} EMS'",
              "VERSION : 0.1", "", "  STRUCT"]
    for name, _, kind in SITE_LAYOUT:
        c = f"\t//{comments[name]}" if name in comments else ""
        lines.append(f"   Site_{name} : {kind} ;{c}")
    used: set[str] = set()
    for k, m in enumerate(meters):
        ident = _ident(m.id)
        while ident in used:
            ident = f"{ident[:20]}_{k}"
        used.add(ident)
        lines.append(f"   {ident} : UDT {udt} ;\t//{m.name} (offset {io_offset(k)}.0)")
    lines += ["  END_STRUCT ;", "BEGIN", "END_DATA_BLOCK", ""]
    return "\r\n".join(lines)


def io_offset(k: int) -> int:
    return (SITE_REGS + METER_REGS * k) * 2


def gateway_notes(cfg, options: dict[str, Any]) -> str:
    meters = selected_meters(cfg, options)
    host = options.get("host", "<EMS PC address>")
    port = int(options.get("port", 502))
    lines = ["Flexpower EMS Kit - Modbus TCP -> PROFINET gateway settings", "",
             f"Modbus TCP server (the EMS): {host}:{port}, any unit id, function code 03 (or 04)",
             f"Total input data: {io_bytes(len(meters))} bytes ({len(meters)} meters)",
             "Byte/word swap: OFF (data is already in S7 big-endian order)", "",
             "Read commands (cyclic, e.g. every 1000 ms):"]
    for i, (start, count) in enumerate(read_commands(len(meters)), 1):
        lines.append(f"  {i}. FC03 start register {start} count {count} -> gateway input byte {start * 2}..{(start + count) * 2 - 1}")
    lines += ["", "Input data layout (byte offsets):", "  0.0  Site block (24 bytes)"]
    for k, m in enumerate(meters):
        lines.append(f"  {io_offset(k)}.0  {m.name} ({m.id}) (32 bytes)")
    return "\n".join(lines) + "\n"
