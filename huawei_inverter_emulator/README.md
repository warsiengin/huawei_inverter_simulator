# Huawei Inverter Emulator add-on

This add-on runs the repository's Huawei SUN2000-330KTL-H1 simulator as a
Modbus TCP server. It is a software test device for integrations and
demonstrations—not a bridge to a physical inverter or a safety-certified
emulation of Huawei firmware.

The [repository README](../README.md) documents every implemented register,
the fixed-point gains, modeled status values, simulator behavior, and known
limitations. The original reference is
[`huawei_modbus_registers_required.pdf`](../huawei_modbus_registers_required.pdf).

## Install

1. In Home Assistant, open **Settings → Add-ons → Add-on store**.
2. Open the top-right menu and select **Repositories**.
3. Add `https://github.com/warsiengin/huawei_inverter_simulator`.
4. Find **Huawei Inverter Emulator**, install it, and open its configuration.
5. Choose a unit ID if needed, then start the add-on.

When adding the repository, Home Assistant reads `repository.yaml` at the
repository root and discovers the add-on in `huawei_inverter_emulator/`.

## Configure and connect

| Option | Default | Valid values | Purpose |
| --- | ---: | --- | --- |
| `unit_id` | `1` | Integer 1–247 | Unit identifier accepted by the Modbus server. Configure the client to use the same value. |
| `quiet` | `false` | `true` / `false` | Turn off the periodic telemetry summary in the add-on logs. Errors and startup information are still logged. |

The server binds to `0.0.0.0` inside the add-on and listens on container TCP
port **502**. Use the Home Assistant host's LAN IP address and the exposed
host-side port in your client. If host port 502 is already in use, change the
port mapping in the add-on's **Network** settings; the server continues to
listen on container port 502.

The emulator provides holding-register data at the exact addresses defined in
the PDF. Many clients use a zero-based address offset internally; configure
the client's address convention so it sends the register address shown in the
PDF. Multi-register 32-bit values are high-word-first.

The PDF's read-only labels describe the intended client interface. The
current holding-register datastore does not enforce read-only permissions
per address, so avoid writing to telemetry/status registers; model-owned
values are republished on the next update.

## What is simulated

- AC active and reactive power, daily peak, power factor, conversion
  efficiency, temperature, and insulation resistance.
- Device and state bitfields, startup/shutdown epoch timestamps, daily and
  lifetime energy yields.
- Writable grid code, failsafe power limit, system-time, and fast-scheduling
  settings.
- One-shot startup (`40200 = 1`) and shutdown (`40201 = 1`) control commands.

The power model uses a local-time 06:00–18:00 half-sine solar envelope and a
randomized cloud factor. It is a demonstration model, not a weather forecast.
Alarm registers and fault code remain clear; fast scheduling is stored but
does not affect the model. Energy values, commands, and operating state are
held in process memory and reset when the add-on restarts. No Home Assistant
entities or persistent history are created.

Refer to the [register table and behavior notes](../README.md#register-map) for
all addresses, data types, gains, read/write permissions, status values, and
limitations.

## Logs and troubleshooting

Open the add-on's **Log** tab to see startup status and periodic telemetry.
Each summary includes active/reactive/apparent power, power factor, efficiency,
temperature, insulation, device status, and daily yield. Set `quiet: true` to
hide those periodic summaries when logs should contain only lifecycle and error
messages.

| Symptom | Checks |
| --- | --- |
| Add-on will not start | Check the add-on log for configuration or image-build errors. Confirm that the selected architecture is supported by the add-on store. |
| Client cannot connect | Confirm the add-on is running, the client targets the Home Assistant host and exposed TCP port, and the host/network firewall permits that connection. |
| Modbus reports no response / wrong device | Match the client's unit ID to `unit_id`; check its holding-register address offset against the exact PDF addresses. |
| Port cannot be exposed | Check whether another service already uses host port 502 and assign an unused host port in **Network** settings. |
| Power stays at zero | Output follows local time and the simulated solar curve; outside its daylight window the model normally reports standby. A startup command can force a small irradiance floor. Check whether a shutdown command is latched. |
| Power is unexpectedly capped | Read register `42405`; its signed raw value is kW × 1000 and the applied limit is clamped to the 0–275 kW model range. |

## Developer notes

The add-on's `Dockerfile` uses the Home Assistant architecture-specific base
image from `build.yaml`, creates a Python virtual environment, and installs
`requirements.txt` (`pymodbus>=3.6,<3.8`). `run.sh` obtains the validated
`unit_id` and `quiet` values through `bashio`, binds the server to all
container interfaces, and replaces the shell with the Python process so its
exit status is reported correctly to the supervisor.

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
