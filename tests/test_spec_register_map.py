import asyncio
import time
import unittest

from pymodbus.pdu.register_write_message import (
    WriteMultipleRegistersRequest,
    WriteSingleRegisterRequest,
)
from pymodbus.pdu.register_read_message import (
    ReadHoldingRegistersRequest,
    ReadInputRegistersRequest,
)

from huawei_inverter_emulator.inverter_emulator import (
    InverterSim,
    R_CMD_SHUTDOWN,
    R_CMD_STARTUP,
    R_ACTIVE_POWER,
    R_DEVICE_STATUS,
    R_EFFICIENCY,
    R_FAILSAFE_LIMIT,
    R_FAST_SCHED,
    R_GRID_CODE,
    R_INPUT_POWER,
    R_PV1_CURRENT,
    R_PV1_VOLTAGE,
    R_PV2_CURRENT,
    R_PV2_VOLTAGE,
    R_PV3_CURRENT,
    R_PV3_VOLTAGE,
    R_PV4_CURRENT,
    R_PV4_VOLTAGE,
    R_STARTUP_TIME,
    R_SYSTEM_TIME,
    ST_ON_GRID,
    ST_STANDBY_INSULATION_CHECK,
    ST_STANDBY_INIT,
    WRITABLE_REGISTER_RANGES,
    build_context,
)


class SpecificationRegisterMapTests(unittest.TestCase):
    def setUp(self):
        self.context, self.block = build_context(unit_id=1)
        self.slave = self.context[1]
        self.simulator = InverterSim(self.block)
        self.slave.write_callback = self.simulator.on_modbus_write
        self.simulator.initialise(unit_id=1)

    def test_specified_input_power_and_four_pv_strings_are_published(self):
        self.simulator.forced_start = True
        self.simulator.update()

        self.assertEqual(self.simulator.rd_u16(R_DEVICE_STATUS), ST_ON_GRID)
        dc_kw = self.simulator.rd_i32(R_INPUT_POWER) / 1000
        ac_kw = self.simulator.rd_i32(R_ACTIVE_POWER) / 1000
        efficiency = self.simulator.rd_u16(R_EFFICIENCY) / 10000
        self.assertGreater(dc_kw, ac_kw)
        self.assertAlmostEqual(dc_kw, ac_kw / efficiency, delta=0.002)

        voltage_registers = (
            R_PV1_VOLTAGE,
            R_PV2_VOLTAGE,
            R_PV3_VOLTAGE,
            R_PV4_VOLTAGE,
        )
        current_registers = (
            R_PV1_CURRENT,
            R_PV2_CURRENT,
            R_PV3_CURRENT,
            R_PV4_CURRENT,
        )
        pv_power_kw = 0.0
        for voltage_register, current_register in zip(
            voltage_registers, current_registers
        ):
            voltage_v = self.simulator.rd_i16(voltage_register) / 10
            current_a = self.simulator.rd_i16(current_register) / 100
            self.assertGreaterEqual(voltage_v, 500)
            self.assertLessEqual(voltage_v, 1500)
            self.assertGreater(current_a, 0)
            pv_power_kw += voltage_v * current_a / 1000

        self.assertAlmostEqual(pv_power_kw, dc_kw, delta=0.1)

    def test_standby_and_shutdown_only_publish_documented_status_values(self):
        self.simulator.forced_stop = True
        self.simulator.update()
        self.assertIn(
            self.simulator.rd_u16(R_DEVICE_STATUS),
            (ST_STANDBY_INIT, ST_STANDBY_INSULATION_CHECK),
        )

        self.block.setValues(R_CMD_SHUTDOWN, [1])
        self.simulator.update()
        self.assertEqual(self.simulator.rd_u16(R_CMD_SHUTDOWN), 0)
        self.assertEqual(
            self.simulator.rd_u16(R_DEVICE_STATUS),
            ST_STANDBY_INSULATION_CHECK,
        )

    def test_write_access_matches_read_write_and_write_only_ranges(self):
        self.assertFalse(self.slave.validate(6, R_INPUT_POWER, 2))
        self.assertFalse(self.slave.validate(16, R_PV1_CURRENT, 1))
        self.assertTrue(self.slave.validate(6, R_GRID_CODE, 1))
        self.assertTrue(self.slave.validate(16, R_FAILSAFE_LIMIT, 2))
        self.assertTrue(self.slave.validate(6, R_CMD_STARTUP, 1))
        self.assertFalse(self.slave.validate(3, R_CMD_STARTUP, 1))
        self.assertTrue(self.slave.validate(4, R_PV1_VOLTAGE, 8))
        self.assertFalse(self.slave.validate(4, R_GRID_CODE, 1))
        self.assertFalse(self.slave.validate(23, R_GRID_CODE, 1))
        for start, end in WRITABLE_REGISTER_RANGES:
            self.assertTrue(self.slave.validate(16, start, end - start + 1))

    def test_modbus_rejects_read_only_and_write_only_register_operations(self):
        read_only_write = WriteSingleRegisterRequest(address=R_INPUT_POWER, value=0)
        response = asyncio.run(read_only_write.update_datastore(self.slave))
        self.assertEqual(response.function_code, 0x86)
        self.assertEqual(response.exception_code, 2)

        write_only_read = asyncio.run(
            ReadHoldingRegistersRequest(
                address=R_CMD_STARTUP, count=1
            ).update_datastore(self.slave)
        )
        self.assertEqual(write_only_read.function_code, 0x83)
        self.assertEqual(write_only_read.exception_code, 2)

        read_only_read = asyncio.run(
            ReadInputRegistersRequest(
                address=R_PV1_VOLTAGE, count=8
            ).update_datastore(self.slave)
        )
        self.assertEqual(read_only_read.function_code, 4)
        self.assertEqual(len(read_only_read.registers), 8)

        writable_limit = WriteMultipleRegistersRequest(
            address=R_FAILSAFE_LIMIT,
            values=[123456 >> 16, 123456 & 0xFFFF],
        )
        response = asyncio.run(writable_limit.update_datastore(self.slave))
        self.assertEqual(response.function_code, 16)
        self.assertEqual(self.simulator.rd_i32(R_FAILSAFE_LIMIT), 123456)

    def test_system_time_write_is_retained_across_model_updates(self):
        requested_epoch = int(time.time()) - 120
        self.simulator.forced_start = True
        response = asyncio.run(
            WriteMultipleRegistersRequest(
                address=R_SYSTEM_TIME,
                values=[requested_epoch >> 16, requested_epoch & 0xFFFF],
            ).update_datastore(self.slave)
        )
        self.assertEqual(response.function_code, 16)

        self.simulator.update()
        self.assertAlmostEqual(
            self.simulator.rd_u32(R_SYSTEM_TIME),
            requested_epoch,
            delta=1,
        )
        self.assertAlmostEqual(
            self.simulator.rd_u32(R_STARTUP_TIME),
            requested_epoch,
            delta=1,
        )

    def test_initial_writable_settings_use_specified_gains(self):
        self.simulator.initialise(
            unit_id=1,
            grid_code=19,
            failsafe_limit_kw=123.456,
            fast_scheduling=True,
        )
        self.assertEqual(self.simulator.rd_u16(R_GRID_CODE), 19)
        self.assertEqual(self.simulator.rd_i32(R_FAILSAFE_LIMIT), 123456)
        self.assertEqual(self.simulator.rd_u16(R_FAST_SCHED), 1)

    def test_writable_value_ranges_are_normalized_to_spec(self):
        asyncio.run(
            WriteMultipleRegistersRequest(
                address=R_FAILSAFE_LIMIT,
                values=[0x7FFF, 0xFFFF],
            ).update_datastore(self.slave)
        )
        self.assertEqual(self.simulator.rd_i32(R_FAILSAFE_LIMIT), 275000)

        asyncio.run(
            WriteSingleRegisterRequest(
                address=R_FAST_SCHED,
                value=7,
            ).update_datastore(self.slave)
        )
        self.assertEqual(self.simulator.rd_u16(R_FAST_SCHED), 1)


if __name__ == "__main__":
    unittest.main()
