#!/bin/bash
# Runs `sure-bridge.py sync` once a day at SURE_SYNC_TIME (HH:MM, container TZ).
#   SURE_SYNC_TIME=07:30   daily send time
#   SURE_RUN_ON_START=1    also send once when the container starts
set -uo pipefail
TIME=${SURE_SYNC_TIME:-07:30}
ENV_FILE=/etc/sure-bridge.env

if [ ! -s "$ENV_FILE" ]; then
  echo "[sure-bridge] $ENV_FILE missing or empty: copy config/sure-bridge.env.example to config/sure-bridge.env"
  exec sleep infinity
fi

run() { echo "[sure-bridge] $(date '+%F %T') sync"; sure-bridge.py sync || echo "[sure-bridge] failed (exit code $?)"; }

[ "${SURE_RUN_ON_START:-0}" = "1" ] && run
while true; do
  now=$(date +%s)
  next=$(date -d "today $TIME" +%s)
  [ "$next" -le "$now" ] && next=$(date -d "tomorrow $TIME" +%s)
  echo "[sure-bridge] next send: $(date -d "@$next" '+%F %T')"
  sleep $((next - now))
  run
done
