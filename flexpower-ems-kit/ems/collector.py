"""Poll energy meters over Modbus TCP / RTU (or a built-in simulator)."""
from __future__ import annotations

import logging
import math
import random
import struct
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Callable

from .config import Config, Meter, Register
from .db import Database

log = logging.getLogger("ems.collector")

_FORMATS = {"uint16": ">H", "int16": ">h", "uint32": ">I", "int32": ">i",
            "uint64": ">Q", "int64": ">q", "float32": ">f", "float64": ">d"}

MAX_BLOCK = 100   # registers per request (Modbus limit is 125)
MAX_HOLE = 8      # unused registers we are willing to read to merge two blocks


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
def decode_registers(regs: list[int], reg_type: str, word_order: str = "big") -> float:
    """Turn raw 16-bit registers into a number. Byte order inside a word is
    always big-endian (Modbus standard); word order is configurable."""
    words = list(regs) if word_order == "big" else list(reversed(regs))
    raw = b"".join(int(w & 0xFFFF).to_bytes(2, "big") for w in words)
    return struct.unpack(_FORMATS[reg_type], raw)[0]


def plan_blocks(registers: list[Register]) -> list[tuple[str, int, int, list[Register]]]:
    """Group registers into as few read requests as possible.
    Returns (function, start_address, count, registers)."""
    blocks: list[tuple[str, int, int, list[Register]]] = []
    by_fn: dict[str, list[Register]] = defaultdict(list)
    for r in registers:
        by_fn[r.function].append(r)
    for fn, regs in by_fn.items():
        regs.sort(key=lambda r: r.address)
        cur: list[Register] = []
        start = end = 0
        for r in regs:
            r_end = r.address + r.count
            if cur and r.address - end <= MAX_HOLE and max(end, r_end) - start <= MAX_BLOCK:
                cur.append(r)
                end = max(end, r_end)
            else:
                if cur:
                    blocks.append((fn, start, end - start, cur))
                cur, start, end = [r], r.address, r_end
        if cur:
            blocks.append((fn, start, end - start, cur))
    return blocks


# ---------------------------------------------------------------------------
# Modbus access
# ---------------------------------------------------------------------------
def make_client(meter: Meter):
    from pymodbus.client import ModbusSerialClient, ModbusTcpClient

    if meter.protocol == "tcp":
        return ModbusTcpClient(meter.host, port=meter.port, timeout=meter.timeout, retries=1)
    return ModbusSerialClient(
        meter.serial_port, baudrate=meter.baudrate, parity=meter.parity,
        stopbits=meter.stopbits, bytesize=meter.bytesize, timeout=meter.timeout, retries=1,
    )


def read_meter(client, meter: Meter) -> dict[str, float]:
    """Read every configured register of one meter. Raises on communication errors."""
    if not client.connected and not client.connect():
        raise ConnectionError(f"cannot connect to {meter.connection_key}")
    values: dict[str, float] = {}
    for fn, start, count, regs in plan_blocks(meter.registers):
        reader = client.read_input_registers if fn == "input" else client.read_holding_registers
        rr = reader(start, count=count, device_id=meter.unit_id)
        if rr.isError():
            raise IOError(f"unit {meter.unit_id} {fn} @{start} x{count}: {rr}")
        for r in regs:
            off = r.address - start
            v = decode_registers(rr.registers[off:off + r.count], r.type, r.word_order) * r.scale
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                continue
            values[r.name] = round(float(v), 6)
    return values


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
def simulated_power(meter: Meter, local: datetime, rng: random.Random | None = None) -> float:
    """Plausible factory load: production Mon-Sat 07-19 with a lunch dip,
    idle load the rest of the time."""
    rng = rng or random
    hour = local.hour + local.minute / 60
    working = local.weekday() < 6 and 7 <= hour < 19
    if working:
        level = meter.sim_kw * (0.65 + 0.35 * rng.random())
        if 12 <= hour < 13:
            level *= 0.45
        if meter.group.lower() == "building":  # HVAC/lighting follow the afternoon heat
            level *= 0.8 + 0.4 * math.sin(math.pi * (hour - 7) / 12)
    else:
        level = meter.sim_idle_kw * (0.8 + 0.4 * rng.random())
    return round(level, 3)


class Simulator:
    """Keeps cumulative kWh counters for simulated meters."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        states = db.meter_states()
        self.energy = {m.id: (states[m.id]["last_energy"] if m.id in states and states[m.id]["last_energy"] else 1000.0)
                       for m in cfg.meters if m.protocol == "simulated"}
        self.last_ts: dict[str, float] = {}

    def read(self, meters: list[Meter], ts: float) -> dict[str, dict[str, float]]:
        local = datetime.fromtimestamp(ts, self.cfg.tz)
        out: dict[str, dict[str, float]] = {}
        subs_kw = 0.0
        ordered = sorted(meters, key=lambda m: m.main)  # sub-meters first, main last
        for m in ordered:
            kw = subs_kw * 1.08 + 1.5 if m.main else simulated_power(m, local)
            if not m.main:
                subs_kw += kw
            dt = ts - self.last_ts.get(m.id, ts)
            self.last_ts[m.id] = ts
            self.energy[m.id] += kw * dt / 3600
            volts = 400 + random.uniform(-6, 6)
            pf = round(random.uniform(0.86, 0.97), 3)
            out[m.id] = {
                "energy_kwh": round(self.energy[m.id], 4),
                "power_kw": kw,
                "voltage_v": round(volts, 1),
                "current_a": round(kw * 1000 / (math.sqrt(3) * volts * pf), 2),
                "pf": pf,
            }
        return out


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------
class Collector:
    """One thread per connection (TCP endpoint, serial port, or the simulator).
    Meters sharing an RS-485 bus are polled one after another on that thread."""

    def __init__(self, cfg: Config, db: Database, on_reading: Callable[[str, dict], None] | None = None,
                 license=None):
        self.cfg = cfg
        self.db = db
        self.on_reading = on_reading
        self.license = license  # LicenseManager; None = no licence checks (tests)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.simulator = Simulator(cfg, db)

    def groups(self) -> dict[str, list[Meter]]:
        g: dict[str, list[Meter]] = defaultdict(list)
        for m in self.cfg.meters:
            g["sim://all" if m.protocol == "simulated" else m.connection_key].append(m)
        return g

    def start(self) -> None:
        for key, meters in self.groups().items():
            t = threading.Thread(target=self._run, args=(key, meters), name=f"poll {key}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("collector started: %d meters on %d connections", len(self.cfg.meters), len(self._threads))

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)

    def _run(self, key: str, meters: list[Meter]) -> None:
        client = None if key.startswith("sim://") else make_client(meters[0])
        period = self.cfg.poll_seconds
        next_tick = time.time()
        try:
            while not self._stop.is_set():
                self.poll_once(meters, client)
                next_tick += period
                if next_tick < time.time():  # fell behind (slow bus); don't burst
                    next_tick = time.time() + period
                self._stop.wait(max(0.0, next_tick - time.time()))
        finally:
            if client is not None:
                client.close()

    def poll_once(self, meters: list[Meter], client=None) -> None:
        now = time.time()
        if self.license is not None:  # plan meter limit; nothing at all when cancelled / unlicensed
            allowed = self.license.allowed_meter_ids()
            meters = [m for m in meters if m.id in allowed]
            if not meters:
                return
        if meters and meters[0].protocol == "simulated":
            for mid, values in self.simulator.read(meters, now).items():
                self._store(mid, int(now), values)
            return
        for m in meters:
            ts = int(time.time())
            try:
                values = read_meter(client, m)
            except Exception as exc:  # noqa: BLE001 - one bad meter must not stop the bus
                log.warning("meter %s read failed: %s", m.id, exc)
                self.db.record_error(m.id, ts, str(exc))
                try:
                    client.close()  # force reconnect next time
                except Exception:  # noqa: BLE001
                    pass
                continue
            self._store(m.id, ts, values)

    def _store(self, meter_id: str, ts: int, values: dict[str, float]) -> None:
        if not values:
            return
        self.db.ingest(meter_id, ts, values, max_gap_seconds=max(3 * self.cfg.poll_seconds, 300))
        if self.on_reading:
            self.on_reading(meter_id, values)
