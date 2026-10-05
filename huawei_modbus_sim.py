import asyncio
import random
import time

from pymodbus.server import StartAsyncTcpServer
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusSlaveContext,
    ModbusServerContext,
)

SLAVE_ID = 1
REG_COUNT = 65536
HOST = "0.0.0.0"
PORT = 502


def set_u16(store, addr, value):
    store.setValues(3, addr, [value & 0xFFFF])


def set_i16(store, addr, value):
    store.setValues(3, addr, [value & 0xFFFF])


def set_u32(store, addr, value):
    value = value & 0xFFFFFFFF
    store.setValues(3, addr, [(value >> 16) & 0xFFFF, value & 0xFFFF])


def set_i32(store, addr, value):
    value = value & 0xFFFFFFFF
    store.setValues(3, addr, [(value >> 16) & 0xFFFF, value & 0xFFFF])


def init_registers(store):
    # -----------------------------
    # Static / identity
    # -----------------------------
    set_u16(store, 32071, 2)      # Number of PV strings
    set_u16(store, 32072, 2)      # Number of MPP trackers
    set_u32(store, 32073, 5000)   # Rated power 5 kW  -> raw 5000
    set_u32(store, 32075, 5000)   # Pmax 5 kW
    set_u32(store, 32077, 5000)   # Smax 5 kVA

    # -----------------------------
    # PV strings
    # Formula: PVn voltage = 32014 + 2n, PVn current = 32015 + 2n
    # -----------------------------
    set_i16(store, 32016, 4000)   # PV1 voltage 400.0 V  -> raw 4000
    set_i16(store, 32017, 850)    # PV1 current 8.50 A   -> raw 850
    set_i16(store, 32018, 3950)   # PV2 voltage 395.0 V  -> raw 3950
    set_i16(store, 32019, 800)    # PV2 current 8.00 A   -> raw 800

    # -----------------------------
    # Grid / AC
    # -----------------------------
    set_u16(store, 32066, 2300)   # Power grid voltage 230.0 V -> raw 2300
    set_u16(store, 32069, 2300)   # Phase A voltage
    set_u16(store, 32070, 2310)   # Phase B voltage
    # Note: PDF has a conflict at 32071: "Number of PV strings" and "Phase C voltage".
    # We keep 32071 as Number of PV strings above, so do not use Phase C here.

    set_u16(store, 32085, 5000)   # Grid frequency 50.00 Hz -> raw 5000
    set_u16(store, 32086, 9850)   # Efficiency 98.50 % -> raw 9850
    set_i16(store, 32087, 350)    # Internal temp 35.0 °C -> raw 350
    set_u16(store, 32088, 1500)   # Insulation 1.500 MΩ -> raw 1500

    # -----------------------------
    # Status / faults
    # -----------------------------
    set_u16(store, 32089, 0x0200) # Device status: On-grid
    set_u16(store, 32090, 0)      # Fault code: none

    now = int(time.time())
    set_u32(store, 32091, now - 3600)  # Startup time
    set_u32(store, 32093, now - 1800)  # Shutdown time

    # -----------------------------
    # Energy / power
    # -----------------------------
    set_u32(store, 32064, 3200)     # DC input power 3.2 kW -> raw 3200
    set_u32(store, 32080, 3000)     # Active power 3.0 kW -> raw 3000
    set_u32(store, 32082, 0)        # Reactive power 0 kVar
    set_i16(store, 32084, 1000)     # PF 1.000 -> raw 1000

    set_u32(store, 32106, 1234567)  # Total yield 12345.67 kWh -> raw 1234567
    set_u32(store, 32114, 1234)     # Daily yield 12.34 kWh -> raw 1234


async def update_loop(store):
    """Simulate slowly changing live values."""
    while True:
        active_kw = random.uniform(0.0, 5.0)
        dc_kw = active_kw * random.uniform(0.95, 1.05)

        set_u32(store, 32080, int(active_kw * 1000))
        set_u32(store, 32064, int(dc_kw * 1000))

        # Very rough phase A current at 230 V
        current_a = (active_kw * 1000) / 230.0
        set_u32(store, 32072, int(current_a * 1000))

        await asyncio.sleep(5)


async def main():
    di = ModbusSequentialDataBlock(0, [0] * REG_COUNT)
    co = ModbusSequentialDataBlock(0, [0] * REG_COUNT)
    hr = ModbusSequentialDataBlock(0, [0] * REG_COUNT)
    ir = ModbusSequentialDataBlock(0, [0] * REG_COUNT)

    store = ModbusSlaveContext(di=di, co=co, hr=hr, ir=ir)
    context = ModbusServerContext(slaves={SLAVE_ID: store}, single=False)

    init_registers(store)
    asyncio.create_task(update_loop(store))

    print(f"Modbus TCP simulator listening on {HOST}:{PORT}, slave {SLAVE_ID}")
    await StartAsyncTcpServer(context, address=(HOST, PORT))


if __name__ == "__main__":
    asyncio.run(main())