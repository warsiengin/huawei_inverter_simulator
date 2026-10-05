#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Huawei SUN2000-330KTL-H1 PV inverter — Modbus/TCP simulator
============================================================

Implements `Huawei_Solar_Inverter_Modbus_Specification.pdf` over Modbus TCP.

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

# Nameplate ratings are model constraints and documentation values, not all
# independent Modbus signals. The PDF exposes only four PV input channels and
# does not assign registers to AC voltage, output current, or grid frequency.
MAX_DC_INPUT_VOLTAGE_V = 1500.0
MPP_VOLTAGE_MIN_V = 500.0
MPP_VOLTAGE_MAX_V = 1500.0
MAX_DC_INPUT_CURRENT_A = 65.0
PHYSICAL_DC_INPUT_COUNT = 6
PDF_PV_CHANNEL_COUNT = 4
NOMINAL_AC_VOLTAGE_V = 800.0
MAX_OUTPUT_CURRENT_A = 238.2
NOMINAL_FREQUENCIES_HZ = (50, 60)
AMBIENT_OPERATING_TEMP_MIN_C = -25.0
AMBIENT_OPERATING_TEMP_MAX_C = 60.0

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

R_PV1_VOLTAGE = 32016
R_PV1_CURRENT = 32017
R_PV2_VOLTAGE = 32018
R_PV2_CURRENT = 32019
R_PV3_VOLTAGE = 32020
R_PV3_CURRENT = 32021
R_PV4_VOLTAGE = 32022
R_PV4_CURRENT = 32023

R_INPUT_POWER = 32064        # I32 gain 1000  kW
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

# The supplied specification assigns write access only to these contiguous
# ranges. The startup/shutdown commands are write-only; all other documented
# signals are read-only except the system-time, grid-code, power-limit, and
# fast-scheduling registers.
WRITABLE_REGISTER_RANGES = (
    (R_SYSTEM_TIME, R_SYSTEM_TIME + 1),
    (R_CMD_STARTUP, R_CMD_SHUTDOWN),
    (R_GRID_CODE, R_GRID_CODE),
    (R_FAILSAFE_LIMIT, R_FAILSAFE_LIMIT + 1),
    (R_FAST_SCHED, R_FAST_SCHED),
)
WRITE_FUNCTION_CODES = frozenset((6, 16, 22))
SUPPORTED_REGISTER_FUNCTION_CODES = frozenset((3, 4, *WRITE_FUNCTION_CODES))
SPEC_READ_ONLY_REGISTER_RANGES = (
    (R_PV1_VOLTAGE, R_PV4_CURRENT),
    (R_INPUT_POWER, R_INPUT_POWER + 1),
    (R_PEAK_POWER, R_REACTIVE_POWER + 1),
    (R_POWER_FACTOR, R_POWER_FACTOR),
    (R_EFFICIENCY, R_SHUTDOWN_TIME + 1),
    (R_TOTAL_YIELD, R_TOTAL_YIELD + 1),
    (R_DAILY_YIELD, R_DAILY_YIELD + 1),
)

# Device-status values explicitly documented by the supplied specification.
ST_STANDBY_INIT = 0x0000
ST_STANDBY_INSULATION_CHECK = 0x0001
ST_ON_GRID = 0x0200
ST_SHUTDOWN_FAULT = 0x0300

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
        self.system_time_offset = 0.0
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

    def on_modbus_write(self, address: int, values: list[int]) -> None:
        """Apply a client write to the simulated system clock, if applicable.

        Register 40000 is a 32-bit Unix timestamp. Converting a client write
        into an offset preserves clock synchronization while subsequent
        simulation updates continue refreshing the register.
        """
        end_address = address + len(values) - 1
        if address <= R_SYSTEM_TIME + 1 and end_address >= R_SYSTEM_TIME:
            self.system_time_offset = self.rd_u32(R_SYSTEM_TIME) - time.time()

        if address <= R_FAILSAFE_LIMIT + 1 and end_address >= R_FAILSAFE_LIMIT:
            raw_limit = self.rd_i32(R_FAILSAFE_LIMIT)
            bounded_limit = min(max(raw_limit, 0), round(self.p_max * 1000))
            if bounded_limit != raw_limit:
                log.warning(
                    "Clamping failsafe limit register 42405 from %d to %d",
                    raw_limit,
                    bounded_limit,
                )
                self.wr_i32(R_FAILSAFE_LIMIT, bounded_limit)

        if address <= R_FAST_SCHED <= end_address:
            fast_scheduling = self.rd_u16(R_FAST_SCHED)
            if fast_scheduling not in (0, 1):
                normalized_value = int(bool(fast_scheduling))
                log.warning(
                    "Normalizing fast-scheduling register 45086 from %d to %d",
                    fast_scheduling,
                    normalized_value,
                )
                self.wr_u16(R_FAST_SCHED, normalized_value)

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
        self.wr_u32(R_INPUT_POWER, 0)
        for address in (
            R_PV1_VOLTAGE, R_PV1_CURRENT,
            R_PV2_VOLTAGE, R_PV2_CURRENT,
            R_PV3_VOLTAGE, R_PV3_CURRENT,
            R_PV4_VOLTAGE, R_PV4_CURRENT,
        ):
            self.wr_i16(address, 0)
        self.wr_u32(R_STARTUP_TIME, 0)
        self.wr_u32(R_SHUTDOWN_TIME, 0)
        log.info("Register map initialised for %s (unit id %d)", MODEL_NAME, unit_id)

    # ------------------------------------------------------------------ #
    #  Main simulation step
    # ------------------------------------------------------------------ #
    def update(self) -> None:
        """Advance one simulated second and publish a coherent register snapshot."""
        wall_time = time.time()
        now = wall_time + self.system_time_offset
        # Bound elapsed time to avoid a long pause (or a delayed first tick)
        # creating an unrealistic single-step energy jump.
        dt = min(max(wall_time - self.last_t, 0.0), 10.0)
        self.last_t = wall_time

        # ---- day roll-over -------------------------------------------------
        today = datetime.fromtimestamp(now).date()
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
        lt = datetime.fromtimestamp(now)
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
        # Sample both nameplate operating modes. Positive PF/Q denotes
        # lagging; negative PF/Q denotes leading. The 330 kVA ceiling and the
        # 238.2 A current rating are both applied at the 800 V three-phase
        # nameplate voltage, so the stricter effective S limit always wins.
        if p_ac > 0.5:
            pf_magnitude = random.uniform(0.8, 1.0)
            q_magnitude = p_ac * math.tan(math.acos(pf_magnitude))
            current_limited_s = (
                math.sqrt(3)
                * NOMINAL_AC_VOLTAGE_V
                * MAX_OUTPUT_CURRENT_A
                / 1000.0
            )
            s_limit = min(self.s_max, current_limited_s)
            max_q = math.sqrt(max(0.0, s_limit**2 - p_ac**2))
            q_ac = min(q_magnitude, max_q) * random.choice((-1, 1))

            s_actual = math.hypot(p_ac, q_ac)
            pf = (
                math.copysign(p_ac / s_actual, q_ac)
                if s_actual > 0 and q_ac != 0
                else 1.0
            )
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

        # Reconstruct DC input power from AC output and conversion efficiency,
        # then divide it evenly over the four PV channels named in the PDF.
        # The nameplate describes six physical inputs; the extra two have no
        # registers in the supplied map and are not fabricated here.
        dc_input_kw = p_ac / (eff_pct / 100.0) if eff_pct > 0 else 0.0
        pv_voltage_v = (
            MPP_VOLTAGE_MIN_V
            + (MPP_VOLTAGE_MAX_V - MPP_VOLTAGE_MIN_V) * dc
            if dc_input_kw > 0
            else 0.0
        )
        pv_current_a = (
            dc_input_kw * 1000.0 / (PDF_PV_CHANNEL_COUNT * pv_voltage_v)
            if pv_voltage_v > 0
            else 0.0
        )

        # ---- thermal -------------------------------------------------------
        # The PDF's temperature register is an internal-temperature reading;
        # the nameplate's -25 to +60 C range is ambient operating temperature
        # and is not a limit for that internal sensor. This load-dependent
        # synthetic reading is a first-order model, not
        # an ambient-weather or thermal-safety simulation.
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
        if p_ac > 0.5:
            status = ST_ON_GRID
        elif sun > 0.02 or self.forced_stop:
            status = ST_STANDBY_INSULATION_CHECK
        else:
            status = ST_STANDBY_INIT

        # ---- startup / shutdown timestamps ---------------------------------
        # Timestamps change only on transitions into or out of an on-grid
        # state, and are Unix epoch seconds as specified by the PDF.
        running = status == ST_ON_GRID
        was_running = self.last_status == ST_ON_GRID
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
        self.wr_i32(R_INPUT_POWER, int(round(dc_input_kw * 1000)))
        for voltage_register, current_register in (
            (R_PV1_VOLTAGE, R_PV1_CURRENT),
            (R_PV2_VOLTAGE, R_PV2_CURRENT),
            (R_PV3_VOLTAGE, R_PV3_CURRENT),
            (R_PV4_VOLTAGE, R_PV4_CURRENT),
        ):
            self.wr_i16(voltage_register, int(round(pv_voltage_v * 10)))
            self.wr_i16(current_register, int(round(pv_current_a * 100)))
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
        p_kw = self.rd_i32(R_ACTIVE_POWER) / 1000.0
        q_kvar = self.rd_i32(R_REACTIVE_POWER) / 1000.0
        s_kva = math.hypot(
            p_kw,
            q_kvar,
        )
        output_current_a = (
            s_kva * 1000.0
            / (math.sqrt(3) * NOMINAL_AC_VOLTAGE_V)
        )
        return (
            f"[{MODEL_NAME}] "
            f"P={p_kw:7.2f} kW  "
            f"Q={q_kvar:7.2f} kVar  "
            f"S={s_kva:7.2f} kVA  "
            f"Iac={output_current_a:6.2f} A @ "
            f"{NOMINAL_AC_VOLTAGE_V:.0f} V  "
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
class InverterRegisterContext(ModbusSlaveContext):
    """Holding-register context that enforces the specification's access modes."""

    def __init__(self, *args, **kwargs):
        self.write_callback = None
        super().__init__(*args, **kwargs)

    def validate(self, fc_as_hex, address, count=1):
        """Reject writes outside RW/WO regions and reads from WO command words."""
        if fc_as_hex not in SUPPORTED_REGISTER_FUNCTION_CODES:
            return False
        if not super().validate(fc_as_hex, address, count):
            return False

        start = address if self.zero_mode else address + 1
        end = start + count - 1

        if fc_as_hex in WRITE_FUNCTION_CODES:
            return any(
                allowed_start <= start and end <= allowed_end
                for allowed_start, allowed_end in WRITABLE_REGISTER_RANGES
            )

        if fc_as_hex == 4:
            return any(
                allowed_start <= start and end <= allowed_end
                for allowed_start, allowed_end in SPEC_READ_ONLY_REGISTER_RANGES
            )

        if fc_as_hex == 3:
            # FC 03 remains available for clients that read the entire map as
            # holding registers, but WO command words cannot be read.
            return not (start <= R_CMD_SHUTDOWN and end >= R_CMD_STARTUP)

        return True

    def setValues(self, fc_as_hex, address, values):
        """Forward validated protocol writes and notify the model of clock sync."""
        super().setValues(fc_as_hex, address, values)
        if fc_as_hex in WRITE_FUNCTION_CODES and self.write_callback is not None:
            adjusted_address = address if self.zero_mode else address + 1
            self.write_callback(adjusted_address, values)


def build_context(unit_id: int):
    """Build an address-zero-based holding-register map for one Modbus unit.

    A contiguous block is used so the sparse register addresses in the
    specification can be addressed directly by Modbus clients. Compatibility
    fallbacks support the pymodbus 3.6/3.7 constructor names used by the pinned
    add-on dependency.
    """
    block = ModbusSequentialDataBlock(0, [0] * (MAX_ADDR + 2))
    try:
        store = InverterRegisterContext(hr=block, ir=block, zero_mode=True)
    except TypeError:
        log.warning("zero_mode addressing is unsupported; "
                    "falling back to default addressing")
        store = InverterRegisterContext(hr=block, ir=block)

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
    context[args.unit].write_callback = sim.on_modbus_write
    sim.initialise(
        unit_id=args.unit,
        grid_code=args.grid_code,
        failsafe_limit_kw=args.failsafe_limit_kw,
        fast_scheduling=args.fast_scheduling,
    )

    log.info("Starting %s simulator on %s:%d (unit id %d)",
             MODEL_NAME, args.host, args.port, args.unit)
    log.info(
        "Nameplate ratings: DC max %.0f V; MPP %.0f-%.0f V; "
        "%d physical inputs at %.0f A max (%.0f A short-circuit each; "
        "%d PV channels in the supplied register map); "
        "%.0f V three-phase AC, %d/%d Hz; %.0f kW, %.0f kVA, %.1f A max; "
        "PF 0.8 leading to 0.8 lagging; ambient %.0f to %.0f C; "
        "non-isolated, IP66, class I, pollution degree III; "
        "nameplate communications MBUS/RS485 (emulated transport: Modbus TCP)",
        MAX_DC_INPUT_VOLTAGE_V,
        MPP_VOLTAGE_MIN_V,
        MPP_VOLTAGE_MAX_V,
        PHYSICAL_DC_INPUT_COUNT,
        MAX_DC_INPUT_CURRENT_A,
        115.0,
        PDF_PV_CHANNEL_COUNT,
        NOMINAL_AC_VOLTAGE_V,
        *NOMINAL_FREQUENCIES_HZ,
        sim.p_rated,
        sim.s_max,
        MAX_OUTPUT_CURRENT_A,
        AMBIENT_OPERATING_TEMP_MIN_C,
        AMBIENT_OPERATING_TEMP_MAX_C,
    )
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