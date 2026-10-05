#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Huawei SUN2000-330KTL-H1 PV inverter — Modbus/TCP simulator
============================================================

Serves the register map from `huawei_modbus_registers_required.pdf`
over Modbus TCP on 192.168.100.15:502, unit id 1.

Based on nameplate specs:
  Model: SUN2000-330KTL-H1
  Max Input Voltage: 1500 Vd.c.
  MPP Voltage Range: 500 – 1500 Vd.c.
  Output Nominal Voltage: 800 Va.c.; 3 ~ + (PE)
  Rated Output Power: 275 kW
  Max Apparent Power: 330 kVA
  Max Output Current: 238.2 A
  Power Factor: 0.8 (lagging) – 0.8 (leading)
  Operating Temperature: -25 – +60 °C
  Communication: MBUS/RS485 (Simulated over Modbus/TCP)

Requirements
------------
    pip install "pymodbus>=3.6,<3.8"

Running on port 502 requires root / CAP_NET_BIND_SERVICE:
    sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python3))
    # or simply:  sudo python3 huawei_sim.py
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import math
import random
import time
from datetime import datetime

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

# --------------------------------------------------------------------------- #
#  Configuration defaults
# --------------------------------------------------------------------------- #
DEFAULT_HOST = "192.168.100.15"
DEFAULT_PORT = 502
DEFAULT_UNIT = 1

MODEL_NAME = "SUN2000-330KTL-H1"

# --------------------------------------------------------------------------- #
#  Register addresses
# --------------------------------------------------------------------------- #
# Addresses and value widths below follow the supplied register-map PDF.
# Multi-register values occupy consecutive 16-bit holding registers: the first
# register contains the high word and the next register contains the low word.
R_STATE1 = 32000
R_STATE2 = 32002
R_STATE3 = 32003
R_ALARM1 = 32008
R_ALARM2 = 32009
R_ALARM3 = 32010

R_PEAK_POWER = 32078          # I32 gain 1000  kW
R_ACTIVE_POWER = 32080        # I32 gain 1000  kW
R_REACTIVE_POWER = 32082      # I32 gain 1000  kVar
R_POWER_FACTOR = 32084        # I16 gain 1000
R_EFFICIENCY = 32086          # U16 gain 100
R_INTERNAL_TEMP = 32087       # I16 gain 10    degC
R_INSULATION = 32088          # U16 gain 1000  MOhm
R_DEVICE_STATUS = 32089       # U16 enum
R_FAULT_CODE = 32090          # U16
R_STARTUP_TIME = 32091        # U32 epoch
R_SHUTDOWN_TIME = 32093       # U32 epoch
R_TOTAL_YIELD = 32106         # U32 gain 100   kWh
R_DAILY_YIELD = 32114         # U32 gain 100   kWh

R_SYSTEM_TIME = 40000         # U32 epoch, RW
R_CMD_STARTUP = 40200         # U16, write 1
R_CMD_SHUTDOWN = 40201        # U16, write 1
R_GRID_CODE = 42000           # U16, RW
R_FAILSAFE_LIMIT = 42405      # I32 gain 1000, kW, RW
R_FAST_SCHED = 45086          # U16, RW

MAX_ADDR = 45086

# Device status codes written to register 32089. The emulator currently emits
# the standby, on-grid, power-limited, and command-shutdown states; the PDF
# documents additional states that are reserved for future model extensions.
ST_STANDBY_INIT = 0x0000
ST_STANDBY_IRRADIATION = 0x0002
ST_STANDBY_GRID_DETECT = 0x0003
ST_STARTING = 0x0100
ST_ON_GRID = 0x0200
ST_ON_GRID_LIMITED = 0x0201
ST_SHUTDOWN_FAULT = 0x0300
ST_SHUTDOWN_COMMAND = 0x0301

log = logging.getLogger("huawei-sim")


# --------------------------------------------------------------------------- #
#  Small helpers to pack/unpack 16- and 32-bit values into holding registers
# --------------------------------------------------------------------------- #
def _u16(v: int) -> int:
    """Encode a Python integer as the low 16 bits used by one Modbus register."""
    return int(v) & 0xFFFF


def _u32_words(v: int) -> list[int]:
    """Encode a value into Modbus big-endian register order (high word first)."""
    v = int(v) & 0xFFFFFFFF
    return [(v >> 16) & 0xFFFF, v & 0xFFFF]


# --------------------------------------------------------------------------- #
#  Simulator
# --------------------------------------------------------------------------- #
class InverterSim:
    """Electrical/thermal model published through the PDF's holding-register map.

    Model calculations use engineering units (kW, kVar, degrees Celsius, and
    kWh). The register publication step at the end of :meth:`update` applies
    each signal's documented gain so Modbus clients receive scaled integers.
    """

    PERIOD = 1.0                # seconds between register refreshes

    def __init__(self, block: ModbusSequentialDataBlock):
        self.block = block

        # ---- nameplate (SUN2000-330KTL-H1) ----
        self.p_rated = 275.0     # kW AC nominal (Rated Output Power)
        self.p_max = 275.0       # kW AC maximum active power
        self.s_max = 330.0       # kVA AC maximum apparent power

        # ---- internal state ----
        self.cloud = 1.0
        self.temp_c = 25.0
        self.insulation_mohm = 25.0
        self.daily_energy_kwh = 0.0
        # Demonstration starting point, not persisted between process restarts.
        self.total_energy_kwh = 1_482_530.00
        self.peak_today_kw = 0.0
        self.startup_ts = 0
        self.shutdown_ts = 0
        self.forced_start = False
        self.forced_stop = False
        self.last_t = time.time()
        self.today = datetime.now().date()
        self.last_status = None

    # ------------------------------------------------------------------ #
    #  Raw register access
    # ------------------------------------------------------------------ #
    # These helpers deal only in raw register values. They do not apply a
    # signal's engineering-unit gain (for example, active power is stored as
    # kW * 1000 and must be divided by 1000 by the consumer).
    def rd_u16(self, addr: int) -> int:
        return self.block.getValues(addr, 1)[0] & 0xFFFF

    def rd_i16(self, addr: int) -> int:
        """Read one register and interpret its two's-complement signed value."""
        v = self.rd_u16(addr)
        return v - 0x10000 if v & 0x8000 else v

    def rd_u32(self, addr: int) -> int:
        """Read two consecutive registers as an unsigned high-word-first value."""
        hi, lo = self.block.getValues(addr, 2)
        return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)

    def rd_i32(self, addr: int) -> int:
        """Read two consecutive registers as a signed two's-complement value."""
        v = self.rd_u32(addr)
        return v - 0x1_0000_0000 if v & 0x8000_0000 else v

    # The matching write helpers intentionally accept raw values too, keeping
    # scaling at the point where the emulator maps an engineering value to a
    # particular PDF-defined signal.
    def wr_u16(self, addr: int, value: int) -> None:
        self.block.setValues(addr, [_u16(value)])

    def wr_i16(self, addr: int, value: int) -> None:
        self.block.setValues(addr, [_u16(value)])

    def wr_u32(self, addr: int, value: int) -> None:
        self.block.setValues(addr, _u32_words(value))

    def wr_i32(self, addr: int, value: int) -> None:
        self.block.setValues(addr, _u32_words(value))

    # ------------------------------------------------------------------ #
    #  One-time setup
    # ------------------------------------------------------------------ #
    def initialise(
        self,
        unit_id: int = DEFAULT_UNIT,
        grid_code: int = 0,
        failsafe_limit_kw: float = 275.0,
        fast_scheduling: bool = False,
    ) -> None:
        """Populate configured writable settings and a fault-free standby snapshot.

        ``failsafe_limit_kw`` is accepted in engineering units and converted to
        the PDF's signed kW * 1000 register representation. The limit is
        clamped to the simulator's rated active-power range.
        """
        failsafe_limit_kw = min(max(failsafe_limit_kw, 0.0), self.p_max)
        self.wr_u16(R_GRID_CODE, grid_code)
        self.wr_u16(R_FAST_SCHED, int(fast_scheduling))
        self.wr_i32(R_FAILSAFE_LIMIT, round(failsafe_limit_kw * 1000))
        self.wr_u32(R_SYSTEM_TIME, int(time.time()))
        self.wr_u16(R_FAULT_CODE, 0)
        for reg in (R_ALARM1, R_ALARM2, R_ALARM3,
                    R_CMD_STARTUP, R_CMD_SHUTDOWN):
            self.wr_u16(reg, 0)
        self.wr_u16(R_STATE1, 0x0001)                     # standby
        self.wr_u16(R_STATE2, 0x0007)                     # unlocked, PV on, DSP ok
        self.wr_u32(R_STATE3, 0x0000)
        self.wr_u32(R_TOTAL_YIELD, int(self.total_energy_kwh * 100))
        self.wr_u32(R_DAILY_YIELD, 0)
        self.wr_u32(R_STARTUP_TIME, 0)
        self.wr_u32(R_SHUTDOWN_TIME, 0)
        log.info("Register map initialised for %s (unit id %d)", MODEL_NAME, unit_id)

    # ------------------------------------------------------------------ #
    #  Main simulation step
    # ------------------------------------------------------------------ #
    def update(self) -> None:
        """Advance one simulated second and publish a coherent register snapshot."""
        now = time.time()
        # Bound elapsed time to avoid a long pause (or a delayed first tick)
        # creating an unrealistic single-step energy jump.
        dt = min(max(now - self.last_t, 0.0), 10.0)
        self.last_t = now

        # ---- day roll-over -------------------------------------------------
        today = datetime.now().date()
        if today != self.today:
            log.info("Day roll-over: daily yield reset (was %.2f kWh)",
                     self.daily_energy_kwh)
            self.today = today
            self.daily_energy_kwh = 0.0
            self.peak_today_kw = 0.0

        # ---- remote commands ----------------------------------------------
        # Command registers are treated as one-shot triggers: acknowledge the
        # write by clearing it, then latch the resulting operating mode until
        # the opposite command is received. A shutdown command takes precedence
        # over solar availability later in this update.
        if self.rd_u16(R_CMD_STARTUP):
            self.wr_u16(R_CMD_STARTUP, 0)
            self.forced_start = True
            self.forced_stop = False
            log.info(">>> STARTUP command received (40200)")

        if self.rd_u16(R_CMD_SHUTDOWN):
            self.wr_u16(R_CMD_SHUTDOWN, 0)
            self.forced_stop = True
            self.forced_start = False
            log.info(">>> SHUTDOWN command received (40201)")

        # ---- solar irradiance model ---------------------------------------
        # Approximate daylight with a half sine between 06:00 and 18:00 local
        # time. This is deliberately a simple deterministic daily envelope,
        # with bounded random cloud cover added below for changing output.
        lt = datetime.now()
        hours = lt.hour + lt.minute / 60.0 + lt.second / 3600.0
        if 6.0 <= hours <= 18.0:
            sun = math.sin(math.pi * (hours - 6.0) / 12.0)
        else:
            sun = 0.0
        sun = max(0.0, sun)

        # slowly drifting cloud cover
        # A bounded random walk avoids implausibly abrupt irradiance changes
        # while preventing the cloud factor from reaching zero.
        self.cloud += random.uniform(-0.04, 0.04)
        self.cloud = min(1.0, max(0.55, self.cloud))

        dc = sun * self.cloud

        if self.forced_start:
            dc = max(dc, 0.10)
        if self.forced_stop:
            dc = 0.0

        # ---- failsafe active power limit (register 42405) ------------------
        # The register contains signed kW scaled by 1000. Clamp its effective
        # value to the physical model range even if a client writes an
        # out-of-range limit.
        limit_kw = self.rd_i32(R_FAILSAFE_LIMIT) / 1000.0
        limit_kw = min(max(limit_kw, 0.0), self.p_max)

        # ---- AC active power -----------------------------------------------
        # Convert the normalized irradiance to available nameplate output,
        # add a small measurement/model variation, then enforce the failsafe.
        p_potential = self.p_max * dc
        p_ac = p_potential * (1.0 + random.uniform(-0.01, 0.01))
        p_ac = max(0.0, p_ac)
        limited = p_ac > limit_kw
        p_ac = min(p_ac, limit_kw)

        # ---- Reactive power / Power Factor --------------------------------
        # Nameplate: 0.8 lagging to 0.8 leading, but we operate near 1.0
        # for a realistic high-efficiency scenario unless heavily loaded.
        # Limit Q so that Apparent Power (S) <= 330 kVA
        # The sign convention is intentionally non-directional here: this
        # example model produces non-negative reactive power rather than
        # modeling leading/lagging setpoints.
        if p_ac > 0.5:
            pf = random.uniform(0.995, 1.0)
            q_ac = p_ac * math.tan(math.acos(pf))
            
            # Enforce S_max = 330 kVA
            max_q = math.sqrt(max(0, self.s_max**2 - p_ac**2))
            q_ac = min(q_ac, max_q)
            
            # Recalculate actual PF based on capped Q
            s_actual = math.hypot(p_ac, q_ac)
            pf = p_ac / s_actual if s_actual > 0 else 1.0
        else:
            pf = 1.0
            q_ac = 0.0

        # ---- efficiency ----------------------------------------------------
        # At negligible power efficiency is reported as zero because the
        # model does not simulate inverter standby conversion losses.
        if p_ac > 0.5 and dc > 0.01:
            eff_pct = 98.0 + 1.0 * min(1.0, dc * 2.2) + random.uniform(-0.15, 0.15)
            eff_pct = min(eff_pct, 99.0)
        else:
            eff_pct = 0.0

        # ---- thermal -------------------------------------------------------
        # Ambient range: -25 to +60 C. Cabinet internal is higher under load.
        # Move partway toward a load-dependent target per tick and add small
        # sensor noise; this is a first-order approximation, not a thermal CFD
        # or ambient-weather model.
        target_temp = 25.0 + 35.0 * (p_ac / self.p_max) if p_ac > 0.5 else 22.0
        self.temp_c += (target_temp - self.temp_c) * 0.05
        self.temp_c += random.uniform(-0.15, 0.15)

        # ---- insulation resistance (slow random walk) ----------------------
        # Keep the synthetic insulation measurement within a plausible
        # display range; this random walk does not generate insulation alarms.
        self.insulation_mohm += random.uniform(-0.3, 0.3)
        self.insulation_mohm = min(50.0, max(5.0, self.insulation_mohm))

        # ---- device status -------------------------------------------------
        # Report command shutdown distinctly from normal standby. A low-power
        # cutoff (0.5 kW) keeps status changes from chattering near zero.
        if self.forced_stop:
            status = ST_SHUTDOWN_COMMAND
        elif p_ac > 0.5:
            status = ST_ON_GRID_LIMITED if limited else ST_ON_GRID
        elif sun > 0.02:
            status = ST_STANDBY_GRID_DETECT
        elif sun > 0:
            status = ST_STANDBY_IRRADIATION
        else:
            status = ST_STANDBY_INIT

        # ---- startup / shutdown timestamps ---------------------------------
        # Timestamps change only on transitions into or out of an on-grid
        # state, and are Unix epoch seconds as specified by the PDF.
        running = status in (ST_ON_GRID, ST_ON_GRID_LIMITED)
        was_running = self.last_status in (ST_ON_GRID, ST_ON_GRID_LIMITED)
        if running and not was_running:
            self.startup_ts = int(now)
            log.info("Inverter transitioned to ON-GRID")
        if was_running and not running:
            self.shutdown_ts = int(now)
            log.info("Inverter transitioned to STANDBY/SHUTDOWN (status 0x%04X)",
                     status)
        self.last_status = status

        # ---- energy accumulation -------------------------------------------
        # kW * elapsed seconds / 3600 gives kWh. The bounded dt above limits
        # the amount added after a scheduling delay.
        self.daily_energy_kwh += p_ac * dt / 3600.0
        self.total_energy_kwh += p_ac * dt / 3600.0
        self.peak_today_kw = max(self.peak_today_kw, p_ac)

        # ---- state bitfields ------------------------------------------------
        # State 1 is published separately from device status. It describes
        # operating flags, while register 32089 carries the enumerated status.
        if running:
            state1 = 0x0006                      # bit1 grid-connected, bit2 normal
            if limited:
                state1 |= 0x0008                 # bit3 derating (power rationing)
        elif status == ST_SHUTDOWN_COMMAND:
            state1 = 0x0040                      # bit6 stop due to command
        else:
            state1 = 0x0001                      # bit0 standby

        # ================================================================ #
        #  Publish everything to the Modbus register space
        # ================================================================ #
        # Apply the PDF's fixed-point gains here: power and PF use *1000,
        # efficiency uses *100, temperature uses *10, insulation and energy
        # use *1000 and *100 respectively. Epoch timestamps have gain 1.
        self.wr_i32(R_ACTIVE_POWER, int(round(p_ac * 1000)))
        self.wr_i32(R_REACTIVE_POWER, int(round(q_ac * 1000)))
        self.wr_i32(R_PEAK_POWER, int(round(self.peak_today_kw * 1000)))
        self.wr_i16(R_POWER_FACTOR, int(round(pf * 1000)))
        self.wr_u16(R_EFFICIENCY, int(round(eff_pct * 100)))
        self.wr_i16(R_INTERNAL_TEMP, int(round(self.temp_c * 10)))
        self.wr_u16(R_INSULATION, int(round(self.insulation_mohm * 1000)))
        self.wr_u16(R_DEVICE_STATUS, status)
        self.wr_u16(R_FAULT_CODE, 0)
        self.wr_u32(R_STARTUP_TIME, self.startup_ts)
        self.wr_u32(R_SHUTDOWN_TIME, self.shutdown_ts)
        self.wr_u32(R_DAILY_YIELD, int(round(self.daily_energy_kwh * 100)))
        self.wr_u32(R_TOTAL_YIELD, int(round(self.total_energy_kwh * 100)))
        self.wr_u32(R_SYSTEM_TIME, int(now))

        self.wr_u16(R_STATE1, state1)
        self.wr_u16(R_STATE2, 0x0007)            # unlocked | PV connected | DSP ok
        self.wr_u32(R_STATE3, 0x0000)            # on-grid, off-grid switch disabled
        self.wr_u16(R_ALARM1, 0)
        self.wr_u16(R_ALARM2, 0)
        self.wr_u16(R_ALARM3, 0)

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Refresh model registers continuously; log and survive tick errors."""
        while True:
            try:
                self.update()
            except Exception:
                log.exception("Simulation step failed")
            await asyncio.sleep(self.PERIOD)

    # ------------------------------------------------------------------ #
    def dump(self) -> str:
        """Format a concise engineering-unit snapshot for the add-on log."""
        s_kva = math.hypot(
            self.rd_i32(R_ACTIVE_POWER) / 1000.0,
            self.rd_i32(R_REACTIVE_POWER) / 1000.0
        )
        return (
            f"[{MODEL_NAME}] "
            f"P={self.rd_i32(R_ACTIVE_POWER)/1000:7.2f} kW  "
            f"Q={self.rd_i32(R_REACTIVE_POWER)/1000:7.2f} kVar  "
            f"S={s_kva:7.2f} kVA  "
            f"PF={self.rd_i16(R_POWER_FACTOR)/1000:5.3f}  "
            f"eff={self.rd_u16(R_EFFICIENCY)/100:5.2f}%  "
            f"T={self.rd_i16(R_INTERNAL_TEMP)/10:5.1f}C  "
            f"Riso={self.rd_u16(R_INSULATION)/1000:5.2f}MOhm  "
            f"status=0x{self.rd_u16(R_DEVICE_STATUS):04X}  "
            f"Eday={self.rd_u32(R_DAILY_YIELD)/100:8.2f} kWh"
        )


# --------------------------------------------------------------------------- #
#  Server plumbing
# --------------------------------------------------------------------------- #
def build_context(unit_id: int):
    """Build an address-zero-based holding-register map for one Modbus unit.

    A contiguous block is used so the sparse register addresses in the
    specification can be addressed directly by Modbus clients. Compatibility
    fallbacks support the pymodbus 3.6/3.7 constructor names used by the pinned
    add-on dependency.
    """
    block = ModbusSequentialDataBlock(0, [0] * (MAX_ADDR + 2))
    try:
        store = ModbusSlaveContext(hr=block, zero_mode=True)
    except TypeError:
        log.warning("ModbusSlaveContext(zero_mode=...) unsupported; "
                    "falling back to default addressing")
        store = ModbusSlaveContext(hr=block)

    try:
        context = ModbusServerContext(devices={unit_id: store}, single=False)
    except TypeError:
        context = ModbusServerContext(slaves={unit_id: store}, single=False)

    return context, block


async def periodic_log(sim: InverterSim, interval: float = 10.0) -> None:
    """Emit a human-readable power/temperature/yield snapshot at a fixed rate."""
    while True:
        await asyncio.sleep(interval)
        log.info("%s", sim.dump())


async def amain(args) -> None:
    """Initialize the model and run its background tasks beside the TCP server."""
    context, block = build_context(args.unit)
    sim = InverterSim(block)
    sim.initialise(
        unit_id=args.unit,
        grid_code=args.grid_code,
        failsafe_limit_kw=args.failsafe_limit_kw,
        fast_scheduling=args.fast_scheduling,
    )

    log.info("Starting %s simulator on %s:%d (unit id %d)",
             MODEL_NAME, args.host, args.port, args.unit)
    if args.advertised_ip:
        address = f"[{args.advertised_ip}]" if args.advertised_ip.version == 6 \
            else str(args.advertised_ip)
        log.info("Client connection address: %s:%d", address, args.port)
    log.info(
        "Initial writable registers: 40000 system_time=%d; "
        "42000 grid_code=%d; 42405 power_limit=%.3f kW (raw=%d); "
        "45086 fast_scheduling=%d",
        sim.rd_u32(R_SYSTEM_TIME),
        sim.rd_u16(R_GRID_CODE),
        sim.rd_i32(R_FAILSAFE_LIMIT) / 1000.0,
        sim.rd_i32(R_FAILSAFE_LIMIT),
        sim.rd_u16(R_FAST_SCHED),
    )

    asyncio.create_task(sim.run())
    if not args.quiet:
        asyncio.create_task(periodic_log(sim))

    await StartAsyncTcpServer(context=context, address=(args.host, args.port))


def main() -> None:
    """Parse standalone CLI options and translate listener errors for operators."""
    global args

    p = argparse.ArgumentParser(
        description=f"Huawei {MODEL_NAME} Modbus/TCP inverter simulator")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"bind address (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"TCP port (default {DEFAULT_PORT})")
    p.add_argument("--unit", type=int, default=DEFAULT_UNIT,
                   help=f"Modbus unit/slave id (default {DEFAULT_UNIT})")
    p.add_argument("--advertised-ip", type=ipaddress.ip_address, default="",
                   help="optional client-facing host IP to include in startup logs")
    p.add_argument("--grid-code", type=int, default=0,
                   help="initial grid code for holding register 42000 (default 0)")
    p.add_argument("--failsafe-limit-kw", type=float, default=275.0,
                   help="initial active power limit in kW for register 42405")
    p.add_argument("--fast-scheduling", action="store_true",
                   help="enable register 45086 at startup")
    p.add_argument("--quiet", action="store_true",
                   help="disable the periodic telemetry log line")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="enable pymodbus debug logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("pymodbus").setLevel(logging.WARNING)

    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.info("Simulator stopped by user")
    except PermissionError:
        log.error("Permission denied binding to %s:%d — port 502 needs root "
                  "or CAP_NET_BIND_SERVICE.", args.host, args.port)
        raise SystemExit(1)
    except OSError as exc:
        log.error("Cannot bind %s:%d -> %s\n"
                  "Is the IP configured on this machine and the port free?",
                  args.host, args.port, exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()