#!/bin/bash
# kresus: prepares the volume (salt, config, Woob sources) then starts Kresus.
# Any other command is run as is (e.g. `sure-bridge.py accounts`).
set -euo pipefail

DATA=${KRESUS_DIR:-/var/lib/kresus}
CONFIG=$DATA/config.ini

prepare() {
  # Encryption salt for the bank credentials: generated once, kept in the volume.
  # Losing it makes the stored credentials unreadable: back it up with the volume.
  if [ ! -f "$CONFIG" ]; then
    salt=${KRESUS_SALT:-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}
    cat > "$CONFIG" <<INI
[kresus]
datadir=$DATA
host=${HOST:-0.0.0.0}
port=${PORT:-9876}
salt=$salt

[db]
type=sqlite
sqlite_path=$DATA/kresus.sqlite
INI
    chmod 600 "$CONFIG"
    echo "[entrypoint] config.ini created with a random salt in $CONFIG: back up the volume"
  fi

  # Woob modules: the master checkout embedded in the image (no release download).
  printf 'file:///opt/woob/modules\n' > "$KRESUS_WOOB_SOURCES_LIST"

  # The boursedirect/esalia modules write their exports to /var/lib/kresus.
  mkdir -p "$DATA"
}

if [ "${1:-}" = "kresus" ]; then
  prepare
  echo "[entrypoint] $(cat /opt/woob/.build-ref); sources: $(cat "$KRESUS_WOOB_SOURCES_LIST")"
  exec kresus --config "$CONFIG"
fi
exec "$@"
