"""Read-only Modbus TCP server so any SCADA or PLC can poll the EMS.

Only function codes 03 (holding) and 04 (input registers) are answered; both
return the same table. Every write function is refused with exception 01, so a
SCADA cannot change anything in the EMS.

Register map (zero-based addresses, 32-bit values use two registers):

  Site block, starting at 0
    0  total power kW          float32
    2  site energy today kWh   float32
    4  site energy month kWh   float32
    6  meters online           uint16
    7  meters configured       uint16
    8  heartbeat               uint16, +1 every update (detect a stalled EMS)
    9  reserved

  Meter n (n = 0, 1, 2... in config.yaml order) at 100 + 20*n
    +0  power kW               float32
    +2  energy counter kWh     float32
    +4  voltage V              float32
    +6  current A              float32
    +8  power factor           float32
    +10 energy today kWh       float32
    +12 energy this month kWh  float32
    +14 energy counter kWh x10 uint32 (full precision for large counters)
    +16 online                 uint16, 1 = online
    +17 data age seconds       uint16 (65535 = never)
    +18 reserved (2 registers)

Floats that are not available are NaN (0x7FC00000).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import struct
import threading
from typing import Any

from . import Integration

log = logging.getLogger("ems.integrations.modbus")

SITE_BASE = 0
METER_BASE = 100
METER_STRIDE = 20
MAX_READ = 125

SITE_FIELDS = [("power_kw", 0, "float32", "Site total power", "kW"), ("today_kwh", 2, "float32", "Site energy today", "kWh"),
               ("month_kwh", 4, "float32", "Site energy this month", "kWh"), ("meters_online", 6, "uint16", "Meters online", ""),
               ("meters_total", 7, "uint16", "Meters configured", ""), ("heartbeat", 8, "uint16", "Heartbeat counter", "")]
METER_FIELDS = [("power_kw", 0, "float32", "Power", "kW"), ("energy_kwh", 2, "float32", "Energy counter", "kWh"),
                ("voltage_v", 4, "float32", "Voltage", "V"), ("current_a", 6, "float32", "Current", "A"),
                ("pf", 8, "float32", "Power factor", ""), ("today_kwh", 10, "float32", "Energy today", "kWh"),
                ("month_kwh", 12, "float32", "Energy this month", "kWh"),
                ("energy_kwh_x10", 14, "uint32", "Energy counter x10", "0.1 kWh"),
                ("online", 16, "uint16", "Online (1/0)", ""), ("age_s", 17, "uint16", "Data age", "s")]


def register_map(meter_names: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Rows describing every register, for documentation and the dashboard."""
    rows = [{"address": SITE_BASE + off, "type": t, "meter": "Site", "value": desc, "unit": u}
            for _, off, t, desc, u in SITE_FIELDS]
    for n, (mid, name) in enumerate(meter_names):
        base = METER_BASE + METER_STRIDE * n
        rows += [{"address": base + off, "type": t, "meter": f"{name} ({mid})", "value": desc, "unit": u}
                 for _, off, t, desc, u in METER_FIELDS]
    return rows


class RegisterTable:
    def __init__(self, size: int, word_order: str = "big"):
        self.regs = [0] * size
        self.little = word_order == "little"
        self.lock = threading.Lock()

    def _words(self, raw: bytes) -> list[int]:
        words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        return words[::-1] if self.little else words

    def encode(self, value: Any, kind: str) -> list[int]:
        if kind == "float32":
            v = float("nan") if value is None or (isinstance(value, float) and math.isinf(value)) else float(value)
            return self._words(struct.pack(">f", v))
        if kind == "uint32":
            return self._words(struct.pack(">I", max(0, min(int(value or 0), 0xFFFFFFFF))))
        return [max(0, min(int(value or 0), 0xFFFF))]

    def read(self, start: int, count: int) -> list[int] | None:
        with self.lock:
            if start < 0 or start + count > len(self.regs):
                return None
            return self.regs[start:start + count]


class ModbusTcpIntegration(Integration):
    type_name = "modbus_tcp"

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        self.host = str(options.get("host", "0.0.0.0"))
        self.port = int(options.get("port", 502))
        self.max_clients = int(options.get("max_clients", 16))
        self.idle_timeout = float(options.get("idle_timeout_seconds", 300))
        self.allow = [ipaddress.ip_network(a, strict=False) for a in options.get("allow_ips") or []]
        self.table = RegisterTable(self.table_size(), str(options.get("word_order", "big")).lower())
        self.heartbeat = 0
        self.clients = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None

    def table_size(self) -> int:
        return METER_BASE + METER_STRIDE * max(1, len(self.cfg.meters))

    def describe(self) -> str:
        return f"modbus://{self.host}:{self.port}"

    def status(self):
        s = super().status()
        s["clients"] = self.clients
        return s

    # ------------------------------------------------------------- data
    def publish(self, snap: dict[str, Any]) -> None:
        t = self.table
        self.heartbeat = (self.heartbeat + 1) & 0xFFFF
        updates: list[tuple[int, list[int]]] = []
        site = dict(snap["site"], heartbeat=self.heartbeat)
        for key, off, kind, *_ in SITE_FIELDS:
            updates.append((SITE_BASE + off, t.encode(site.get(key), kind)))
        for m in snap["meters"]:
            base = METER_BASE + METER_STRIDE * m["index"]
            if base + METER_STRIDE > len(t.regs):
                continue
            vals = dict(m, online=1 if m["online"] else 0,
                        age_s=65535 if m["age_s"] is None else m["age_s"],
                        energy_kwh_x10=None if m["energy_kwh"] is None else round(m["energy_kwh"] * 10))
            for key, off, kind, *_ in METER_FIELDS:
                updates.append((base + off, t.encode(vals.get(key), kind)))
        with t.lock:
            for addr, words in updates:
                t.regs[addr:addr + len(words)] = words

    # ------------------------------------------------------------- protocol
    def handle_pdu(self, pdu: bytes) -> bytes:
        fc = pdu[0] if pdu else 0
        if fc not in (3, 4):
            return bytes([(fc | 0x80) & 0xFF, 0x01])  # illegal function (all writes land here)
        if len(pdu) != 5:
            return bytes([fc | 0x80, 0x03])
        start, count = struct.unpack(">HH", pdu[1:5])
        if not 1 <= count <= MAX_READ:
            return bytes([fc | 0x80, 0x03])  # illegal data value
        regs = self.table.read(start, count)
        if regs is None:
            return bytes([fc | 0x80, 0x02])  # illegal data address
        return bytes([fc, count * 2]) + b"".join(r.to_bytes(2, "big") for r in regs)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = ipaddress.ip_address(peer[0]) if peer else None
        if self.allow and (ip is None or not any(ip in n for n in self.allow)):
            log.warning("modbus: refused connection from %s (not in allow_ips)", peer)
            writer.close()
            return
        if self.clients >= self.max_clients:
            log.warning("modbus: refused %s, too many clients", peer)
            writer.close()
            return
        self.clients += 1
        try:
            while True:
                header = await asyncio.wait_for(reader.readexactly(7), self.idle_timeout)
                tid, proto, length, unit = struct.unpack(">HHHB", header)
                if proto != 0 or not 2 <= length <= 254:
                    break
                pdu = await asyncio.wait_for(reader.readexactly(length - 1), 10)
                resp = self.handle_pdu(pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            self.clients -= 1
            writer.close()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="modbus-server", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self._start_error:
            raise self._start_error
        self.running = True

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._server = self._loop.run_until_complete(asyncio.start_server(self._client, self.host, self.port))
        except Exception as exc:  # noqa: BLE001
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._server.close()
            self._loop.run_until_complete(self._server.wait_closed())
            self._loop.close()

    def stop(self) -> None:
        self.running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
