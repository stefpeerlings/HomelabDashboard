#!/usr/bin/env bash
# MariaDB lokaal in de dashboard-LXC (standaard bij installatie).

set -euo pipefail

CRED_DIR="${HOMELAB_CREDENTIALS_DIR:-/root/.homelab-db/credentials}"
SERVICE_JSON="${CRED_DIR}/service.json"
DB_NAME="${HOMELAB_DB_NAME:-homelab_dashboard}"
DB_USER="${HOMELAB_DB_USER:-homelab_dashboard}"
DB_HOST="${HOMELAB_DB_HOST:-127.0.0.1}"
DB_PORT="${HOMELAB_DB_PORT:-3306}"
QUIET="${HOMELAB_QUIET:-0}"

log() {
  if [[ "$QUIET" == "1" ]]; then
    echo "$(date -Iseconds) $*" >>"${HOMELAB_INSTALL_LOG:-/tmp/homelab-lxc-install.log}"
  else
    echo "$@"
  fi
}

generate_password() {
  if [[ -n "${HOMELAB_DB_PASS:-}" ]]; then
    echo "$HOMELAB_DB_PASS"
    return
  fi
  openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 24
}

service_json_ready() {
  [[ -f "$SERVICE_JSON" ]] || return 1
  python3 - <<'PY' "$SERVICE_JSON"
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
cfg = json.loads(path.read_text(encoding="utf-8"))
pw = cfg.get("password")
if pw in (None, "", "VUL_AAN"):
    raise SystemExit(1)
PY
}

test_existing_connection() {
  [[ -f "$SERVICE_JSON" ]] || return 1
  local py="python3"
  local app_root="${HOMELAB_APP_ROOT:-/opt/homelab-dashboard}"
  [[ -x "$app_root/.venv/bin/python" ]] && py="$app_root/.venv/bin/python"
  SERVICE_JSON="$SERVICE_JSON" "$py" - <<'PY'
import json, os
from pathlib import Path
try:
    import pymysql
except ImportError:
    raise SystemExit(1)
path = Path(os.environ["SERVICE_JSON"])
cfg = json.loads(path.read_text(encoding="utf-8"))
conn = pymysql.connect(
    host=cfg["host"],
    port=int(cfg.get("port", 3306)),
    user=cfg["user"],
    password=cfg["password"],
    database=cfg["database"],
    connect_timeout=5,
)
conn.close()
PY
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Voer uit als root."
  exit 1
fi

if [[ "${HOMELAB_DB_MODE:-local}" == "remote" ]]; then
  log "HOMELAB_DB_MODE=remote — lokale MariaDB overgeslagen"
  exit 0
fi

if service_json_ready && test_existing_connection 2>/dev/null; then
  log "MariaDB-credentials bestaan al en werken"
  exit 0
fi

log "MariaDB lokaal installeren..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq mariadb-server

systemctl enable mariadb
systemctl start mariadb

DB_PASS="$(generate_password)"

mysql -u root <<EOF
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';
ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost';
FLUSH PRIVILEGES;
EOF

mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR"

cat >"$SERVICE_JSON" <<EOF
{
  "host": "$DB_HOST",
  "port": $DB_PORT,
  "user": "$DB_USER",
  "password": "$DB_PASS",
  "database": "$DB_NAME"
}
EOF
chmod 600 "$SERVICE_JSON"

log "MariaDB lokaal klaar ($DB_HOST:$DB_PORT/$DB_NAME)"