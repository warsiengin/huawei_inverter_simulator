#!/usr/bin/with-contenv bashio
set -e

args=(
  --host 0.0.0.0
  --port 502
  --unit "$(bashio::config 'unit_id')"
)

if [ "$(bashio::config 'quiet')" = "true" ]; then
  args+=(--quiet)
fi

exec python3 /app/inverter_emulator.py "${args[@]}"
