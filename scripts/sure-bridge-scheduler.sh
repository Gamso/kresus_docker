#!/bin/bash
# Runs `crypto-ledger.py sync` (if an exchange key is set) then `sure-bridge.py sync` once a day at SURE_SYNC_TIME
# (HH:MM, container TZ).
#   SURE_SYNC_TIME=07:30   daily send time
#   SURE_RUN_ON_START=1    also send once when the container starts
set -uo pipefail
TIME=${SURE_SYNC_TIME:-07:30}
ENV_FILE=/etc/sure-bridge.env

if [ ! -s "$ENV_FILE" ]; then
  echo "[sure-bridge] $ENV_FILE missing or empty: copy config/sure-bridge.env.example to config/sure-bridge.env"
  exec sleep infinity
fi

run() {
  echo "[sure-bridge] $(date '+%F %T') sync"
  # crypto exchanges first (when a key is set), so that the bridge sends their latest export
  if grep -Eq '^(BINANCE|KRAKEN)_API_KEY=.+' "$ENV_FILE"; then
    crypto-ledger.py sync || echo "[sure-bridge] crypto-ledger failed (exit code $?)"
  fi
  sure-bridge.py sync || echo "[sure-bridge] failed (exit code $?)"
}

[ "${SURE_RUN_ON_START:-0}" = "1" ] && run
while true; do
  now=$(date +%s)
  next=$(date -d "today $TIME" +%s)
  [ "$next" -le "$now" ] && next=$(date -d "tomorrow $TIME" +%s)
  echo "[sure-bridge] next send: $(date -d "@$next" '+%F %T')"
  sleep $((next - now))
  run
done
