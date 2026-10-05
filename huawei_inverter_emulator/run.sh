#!/usr/bin/with-contenv bashio
set -e

args=(
  --host 0.0.0.0
  --port 502
  --unit "$(bashio::config 'unit_id')"
  --grid-code "$(bashio::config 'grid_code')"
  --failsafe-limit-kw "$(bashio::config 'failsafe_limit_kw')"
)

client_ip="$(bashio::config 'client_ip')"
if [ -n "${client_ip}" ]; then
  args+=(--advertised-ip "${client_ip}")
fi

if [ "$(bashio::config 'fast_scheduling')" = "true" ]; then
  args+=(--fast-scheduling)
fi

if [ "$(bashio::config 'quiet')" = "true" ]; then
  args+=(--quiet)
fi

exec python3 /app/inverter_emulator.py "${args[@]}"
