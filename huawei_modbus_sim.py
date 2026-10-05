#!/usr/bin/env python3
"""Backward-compatible launcher for the specification-compliant simulator.

The original prototype in this file exposed a separate 5 kW register model.
Keeping two implementations caused the CLI and Home Assistant add-on to serve
different values and specifications. Both repository entry points now start
the same canonical simulator.
"""

from huawei_inverter_emulator.inverter_emulator import main


if __name__ == "__main__":
    main()