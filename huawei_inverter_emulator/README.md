# Huawei Inverter Emulator add-on

This add-on runs the repository's Huawei SUN2000-330KTL-H1 simulator as a
Modbus TCP server. It is a software test device for integrations and
demonstrations—not a bridge to a physical inverter or a safety-certified
emulation of Huawei firmware.

The [repository README](../README.md) documents every specified register,
the fixed-point gains, modeled status values, simulator behavior, and known
limitations. The original reference is
[`Huawei_Solar_Inverter_Modbus_Specification.pdf`](../Huawei_Solar_Inverter_Modbus_Specification.pdf).

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on store**.
2. Open the menu in the upper-right corner and select **Repositories**.
3. Add `https://github.com/warsiengin/huawei_inverter_simulator`.
4. Find **Huawei Inverter Emulator**, install it, and open its configuration.
5. Choose a unit ID if needed, then start the add-on.

When adding the repository, Home Assistant reads `repository.yaml` at the
repository root and discovers the add-on in `huawei_inverter_emulator/`.

## Configure and connect

| Option | Default | Valid values | Purpose |
| --- | ---: | --- | --- |
| `client_ip` | Empty | Valid IPv4 or IPv6 address | Optional Home Assistant host address for reference. When set, it is included in the startup log as the client connection address. It does not change the container bind address. |
| `unit_id` | `1` | Integer 1–247 | Modbus unit identifier. Configure the client to use the same value. |
| `grid_code` | `0` | Integer 0–65535 | Initial value for writable register `42000`. The emulator does not interpret grid-code behavior. |
| `failsafe_limit_kw` | `275` | Number 0–275 | Initial active-power limit for writable register `42405`, in kW. Stored using a gain of 1000. |
| `fast_scheduling` | `false` | `true` / `false` | Initial enable state of writable register `45086`. This setting is retained but does not alter the simulated power model. |
| `quiet` | `false` | `true` / `false` | Suppress periodic telemetry summaries in the add-on log. Startup and error messages remain available. |

The server binds to `0.0.0.0` inside the add-on and listens on container TCP
port **502**. Set `client_ip` to the Home Assistant host's LAN address to have
that address included in the add-on startup log. This field is informational:
Home Assistant controls the host-side port mapping, and the add-on continues
to bind to all interfaces inside its container. If host port 502 is already in
use, change the published port in the add-on's **Network** settings.

The startup log identifies the configured client IP (when provided) and
reports the initial values of all writable configuration registers:
`40000` (system time), `42000` (grid code), `42405` (failsafe limit), and
`45086` (fast scheduling). The grid-code, failsafe-limit, and fast-scheduling
options initialize their corresponding registers whenever the add-on starts.
Modbus clients may update these registers while it is running; runtime changes
are held in memory and replaced by the configured initial values after a
restart. System time (`40000`) is initialized and refreshed automatically
from the system clock; it is not a user-supplied option.
A client write to `40000` synchronizes the simulator clock offset, which also
affects its timestamps and daylight cycle.
Because this is a 32-bit value, clients should write both registers together
with one multiple-register request (FC 16).

The failsafe power limit is clamped to 0–275 kW when a client writes a value
outside that model range. Fast scheduling accepts only its specified `0`
(disabled) and `1` (enabled) states; other nonzero values are normalized to
`1`. The example grid-code IDs documented by the reference include `0`
(Germany), `1` (China), `2` (France), `13` (Italy), and `19` (Australia).
Grid codes are retained but do not modify the simplified electrical model.

The emulator provides holding-register data at the exact addresses defined in
the PDF. Many clients use a zero-based address offset internally; configure
the client's address convention so it sends the register address shown in the
PDF. Read-only telemetry is available through both holding-register and
input-register read functions. Multi-register 32-bit values are high-word-first.

The server exposes the specified RO telemetry through holding registers
(FC 03) and input registers (FC 04). Its datastore enforces RO/RW/WO access:
RO writes and reads of WO startup/shutdown registers are rejected with
`Illegal Address`. Writable addresses accept FC 06, FC 16, and FC 22; FC 23
(combined read/write multiple registers) is rejected because it does not
permit separate validation of its read and write ranges.

## What is simulated

- AC active and reactive power, daily peak, power factor, conversion
  efficiency, temperature, and insulation resistance.
- Total DC input power and modeled voltage/current readings for all four PV
  strings listed in the specification.
- Active/apparent power, output current, MPP voltage, and PV-channel current
  are constrained by nameplate ratings. The synthetic PF and reactive power
  exercise leading and lagging operation; positive values indicate lagging
  and negative values indicate leading.
- Device and state bitfields, startup/shutdown epoch timestamps, daily and
  lifetime energy yields.
- Writable grid code, failsafe power limit, system-time, and fast-scheduling
  settings.
- One-shot startup (`40200 = 1`) and shutdown (`40201 = 1`) control commands.

The power model uses a local-time 06:00–18:00 half-sine solar envelope and a
randomized cloud factor. It is a demonstration model, not a weather forecast.
Input power is inferred from AC power and efficiency; the input measurements
are shared evenly among the four simulated PV strings. Alarm registers and
fault code remain clear; fast scheduling is stored but does not affect the
model. Energy values, commands, and operating state are
held in process memory and reset when the add-on restarts. No Home Assistant
entities or persistent history are created.

The nameplate describes six physical DC inputs, but the supplied Modbus PDF
defines only four PV voltage/current channels; only those four are published.
Nameplate ratings without PDF register addresses—including 800 V AC output,
50/60 Hz, ambient temperature, and output current—are not invented as
register telemetry. Output current is derived from apparent power at 800 V in
log summaries. The −25 to +60 °C value is the nameplate ambient operating
range, not a bound for the PDF's internal-temperature measurement. Short-
circuit current, enclosure, topology, pollution degree, protection class, and
MBUS/RS485 communication ratings are documented characteristics only; the
software does not emulate those physical properties or protections.

Refer to the [register table and behavior notes](../README.md#register-map) for
all addresses, data types, gains, read/write permissions, status values, and
limitations.

## Logs and troubleshooting

Open the add-on's **Log** tab to review startup status and periodic telemetry.
Each summary reports active, reactive, and apparent power; derived AC output
current at 800 V; power factor (positive lagging, negative leading);
efficiency; temperature; insulation resistance; device status; and daily
yield. Set `quiet: true` to suppress periodic summaries while retaining
lifecycle and error messages.

| Symptom | Recommended checks |
| --- | --- |
| Add-on does not start | Review the add-on log for configuration or image-build errors. Confirm that the host architecture is supported. |
| Client cannot connect | Confirm that the add-on is running, that the client targets the Home Assistant host and published TCP port, and that network firewall rules allow the connection. |
| Modbus reports no response or an unexpected device | Match the client unit ID to `unit_id` and verify the client's register-address convention against the PDF. |
| Host port cannot be published | Check whether another service uses host port 502. If necessary, assign an unused published host port in **Network** settings. |
| Active power is zero | The model follows local time and its solar curve. Outside daylight hours it normally reports standby. A startup command forces a minimum irradiance level. Check whether a shutdown command has been issued. |
| Active power is limited | Review `failsafe_limit_kw` and register `42405`. The register uses signed raw units of kW × 1000 and the applied limit is bounded to 0–275 kW. |

## Developer notes

The add-on's `Dockerfile` uses the Home Assistant architecture-specific base
image from `build.yaml`, creates a Python virtual environment, and installs
`requirements.txt` (`pymodbus>=3.6,<3.8`). `run.sh` obtains validated settings
through `bashio`, binds the server to all container interfaces, and replaces
the shell with the Python process so its exit status is reported correctly to
the supervisor.

Build locally from the repository root (requires a running Docker Linux
container engine):

```console
docker build \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.22 \
  --tag huawei-inverter-emulator:local \
  huawei_inverter_emulator
```

To check the standalone entry point and its options outside the image, install
the declared pymodbus range and run:

```console
python -m pip install "pymodbus>=3.6,<3.8"
python inverter_emulator.py --help
```
