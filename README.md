# Huawei SUN2000-330KTL-H1 inverter emulator

This repository provides a software-only Modbus TCP simulator for the Huawei
SUN2000-330KTL-H1 inverter model, along with a Home Assistant add-on package.
It is intended for integration development, register-map testing, and
demonstrations. It does **not** connect to, control, or reproduce the full
firmware and safety behavior of a physical inverter.

The source of truth for register addresses, data types, gains, status values,
and documented controls is
[`huawei_modbus_registers_required.pdf`](./huawei_modbus_registers_required.pdf).
The Python simulator implements the modeled subset described below.

## Home Assistant add-on

The add-on definition and its standalone installation/configuration guide are
in [`huawei_inverter_emulator/`](./huawei_inverter_emulator/). It packages the
same emulator used by the command-line entry point.

### Add this repository to Home Assistant

1. Open **Settings → Add-ons → Add-on store**.
2. Select the top-right menu, then **Repositories**.
3. Add `https://github.com/warsiengin/huawei_inverter_simulator`.
4. Find **Huawei Inverter Emulator**, install it, and open its configuration.
5. Set the unit ID if needed, then start the add-on.

The add-on listens on **TCP port 502** and publishes that port to the Home
Assistant host. In the add-on's **Network** settings, change the host-side
port if another service already uses 502. Use the Home Assistant host's LAN
address and configured port in your Modbus client. Set `client_ip` if you want
the host address included in the add-on's startup log. This field does not
change the container bind address, which remains `0.0.0.0`.

The add-on startup log also lists the initial raw values and engineering
units for writable registers `40000`, `42000`, `42405`, and `45086`. Grid code,
failsafe limit, and fast-scheduling values are initialized from the add-on
options; system time is maintained automatically from the host clock.

### Add-on options

| Option | Default | Description |
| --- | ---: | --- |
| `client_ip` | Empty | Optional Home Assistant host IP address displayed in the add-on startup log; informational only. |
| `unit_id` | `1` | Modbus unit identifier. Valid range: 1–247. |
| `grid_code` | `0` | Initial value for writable register `42000`; currently not interpreted by the model. |
| `failsafe_limit_kw` | `275` | Initial value for writable register `42405`, in kW; valid range 0–275. |
| `fast_scheduling` | `false` | Initial value for writable register `45086`; retained but not used by the power model. |
| `quiet` | `false` | Suppresses periodic telemetry summaries. Startup and error messages remain available. |

See the [add-on guide](./huawei_inverter_emulator/README.md) for configuration,
registers, troubleshooting, and developer build instructions.

## Run outside Home Assistant

The root-level `inverter_emulator.py` is a compatibility entry point; it
imports and starts the packaged implementation in
[`huawei_inverter_emulator/inverter_emulator.py`](./huawei_inverter_emulator/inverter_emulator.py).

Install the supported pymodbus range and run the simulator:

```console
python -m pip install "pymodbus>=3.6,<3.8"
python inverter_emulator.py --host 0.0.0.0 --port 502 --unit 1
```

Available options:

```text
--host HOST    Listener bind address (standalone default: 192.168.100.15)
--port PORT    TCP port (default: 502)
--unit UNIT    Modbus unit/slave ID (default: 1)
--quiet        Disable the periodic telemetry log line
-v, --verbose  Enable pymodbus debug logging
```

Port 502 is a privileged port on many operating systems. When running
standalone, use an unprivileged port such as `1502` if necessary, or configure
the host's service permissions appropriately. Do not run the process with
elevated privileges unless required by the host's port-binding policy.

The default standalone bind address is `192.168.100.15`; use `--host 0.0.0.0`
to accept LAN connections on any available interface, or provide a specific
local interface address. The Home Assistant add-on always supplies
`--host 0.0.0.0`.

## Register map

The server exposes the specified addresses as Modbus holding registers. Signed
and unsigned 32-bit values use two consecutive 16-bit registers, high word
first. Values are raw fixed-point integers: divide by the listed gain to obtain
the engineering value (for example, raw active power `12345 / 1000 = 12.345 kW`).
Use the exact addresses from the table; do not apply client-side one-based
address adjustment unless that client explicitly requires it.

| Address | Signal | Type | Gain | Access | Simulator behavior |
| ---: | --- | --- | ---: | --- | --- |
| `32000` | State 1 | Bitfield 16 | 1 | Read-only | Standby, grid-connected, normal, derating, or command-stop flags. |
| `32002` | State 2 | Bitfield 16 | 1 | Read-only | Reports unlocked, PV-connected, and DSP-ready flags. |
| `32003` | State 3 | Bitfield 32 | 1 | Read-only | Reports on-grid mode; off-grid switch remains disabled. |
| `32008`–`32010` | Alarm 1–3 | Bitfield 16 | 1 | Read-only | Initialized and published as zero; alarms are not synthesized. |
| `32078` | Peak active power of current day | I32 | 1000 | Read-only | Maximum modeled AC active power today, in kW. |
| `32080` | Active power | I32 | 1000 | Read-only | Modeled AC active power, in kW. |
| `32082` | Reactive power | I32 | 1000 | Read-only | Modeled reactive power, in kVar. |
| `32084` | Power factor | I16 | 1000 | Read-only | Modeled power factor. |
| `32086` | Efficiency | U16 | 100 | Read-only | Modeled conversion efficiency, in percent. |
| `32087` | Internal temperature | I16 | 10 | Read-only | Synthetic cabinet temperature, in °C. |
| `32088` | Insulation resistance | U16 | 1000 | Read-only | Synthetic insulation resistance, in MΩ. |
| `32089` | Device status | U16 | 1 | Read-only | Standby, on-grid, power-limited, or command-shutdown enum. |
| `32090` | Fault code | U16 | 1 | Read-only | Zero; fault conditions are not simulated. |
| `32091` | Startup time | U32 | 1 | Read-only | Epoch seconds for the most recent transition to on-grid. |
| `32093` | Shutdown time | U32 | 1 | Read-only | Epoch seconds for the most recent transition out of on-grid. |
| `32106` | Accumulated energy yield | U32 | 100 | Read-only | Lifetime yield, in kWh. Starts from an example value and is volatile. |
| `32114` | Daily energy yield | U32 | 100 | Read-only | Yield since the current process-local day began, in kWh. |
| `40000` | System time | U32 | 1 | Read/write | Unix epoch seconds; refreshed from the simulator's system clock. |
| `40200` | Startup | U16 | 1 | Write command | Writing `1` requests forced startup; the command is cleared after processing. |
| `40201` | Shutdown | U16 | 1 | Write command | Writing `1` requests forced shutdown; the command is cleared after processing. |
| `42000` | Grid code | U16 | 1 | Read/write | Initialized to `0` (VDE-AR-N-4105); other codes are not interpreted. |
| `42405` | Failsafe active power limit | I32 | 1000 | Read/write | AC output limit in kW; initialized to the 275 kW model maximum and clamped to 0–275 kW when applied. |
| `45086` | Fast power scheduling | U16 | 1 | Read/write | Initialized to `0`; retained as a setting but not used by the power model. |

The status enumeration and alarm bit definitions are reproduced in the supplied
PDF. Although the simulator publishes the specified alarm registers, it does
not generate faults or alarms. The device-status values it currently models
are standby initialization (`0x0000`), standby irradiation (`0x0002`), standby
grid detection (`0x0003`), on-grid (`0x0200`), on-grid limited (`0x0201`), and
shutdown by command (`0x0301`).

The table's access column describes the intended access from the PDF, but the
current pymodbus holding-register datastore does not enforce a separate
read-only permission for each address. Do not write to telemetry/status
registers: the simulation republishes its model-owned values on each update.
Likewise, writes to grid code and fast scheduling are retained as register
values but do not change the simulated grid behavior.

### Simulator behavior and limitations

- A one-second update loop models a daylight curve using local time: a
  half-sine irradiance envelope between 06:00 and 18:00, multiplied by a
  bounded random cloud factor. There is no weather service or location input.
- Output is limited by the writable failsafe limit and the model's 275 kW
  active-power maximum. The nameplate apparent-power ceiling is 330 kVA.
- Reactive power, power factor, efficiency, temperature, and insulation
  readings are generated as plausible synthetic telemetry, not electrical or
  thermal measurements.
- Writing `1` to startup or shutdown is a one-shot command. Startup forces a
  minimum irradiance floor; shutdown forces power to zero until startup is
  requested. A shutdown command takes precedence over the solar model.
- Energy totals, cloud cover, peak power, timestamps, and command state live in
  memory. They reset when the add-on or standalone process restarts; no
  persistence or Home Assistant entities are created.
- The emulator implements Modbus TCP only. It does not provide Modbus RTU,
  MBUS, RS485, physical-inverter safety interlocks, or a full Huawei firmware
  implementation.

## Development

The add-on build context is `huawei_inverter_emulator/`. It contains
`config.yaml`, `build.yaml`, and `Dockerfile`, as expected by Home Assistant's
local add-on build process. With Docker Desktop's Linux container engine
available, an example local build is:

```console
docker build \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.22 \
  --tag huawei-inverter-emulator:local \
  huawei_inverter_emulator
```

The image installs `pymodbus>=3.6,<3.8` and starts the emulator through
`run.sh`, which reads the add-on's validated options using `bashio`.
