#!/usr/bin/env bash
# Homelab Dashboard — MariaDB alles-in-één
#
# Op de MariaDB-server/LXC (database + gebruiker aanmaken):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)" -- --server
#
# Op de dashboard-container (credentials + test + herstart):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)"
#
# Non-interactief op dashboard (bestaande database):
#   HOMELAB_DB_HOST=10.0.10.17 HOMELAB_DB_PASS='jouw-wachtwoord' \
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)" -- --client

set -euo pipefail

REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${HOMELAB_APP_ROOT:-/opt/homelab-dashboard}"
CRED_DIR="${HOMELAB_CREDENTIALS_DIR:-/root/.homelab-db/credentials}"
SERVICE_JSON="${CRED_DIR}/service.json"
SERVICE_NAME="${HOMELAB_SERVICE_NAME:-homelab-dashboard}"

usage() {
  cat <<EOF
Gebruik:
  setup-database.sh --server    Op MariaDB-host: database + gebruiker
  setup-database.sh --client    Op dashboard: service.json + test + herstart
  setup-database.sh             Detecteert automatisch (--client als dashboard gevonden)

Omgevingsvariabelen (client, non-interactief):
  HOMELAB_DB_HOST      MariaDB host
  HOMELAB_DB_PORT      Poort (default 3306)
  HOMELAB_DB_NAME      Database (default homelab_dashboard)
  HOMELAB_DB_USER      Gebruiker (default homelab_dashboard)
  HOMELAB_DB_PASS      Wachtwoord (verplicht bij non-interactief)
EOF
}

fetch_helper() {
  local name="$1"
  local dest="$2"

  if [[ -f "$SCRIPT_DIR/$name" ]]; then
    cp "$SCRIPT_DIR/$name" "$dest"
    return 0
  fi

  curl -fsSL "${REPO_RAW}/scripts/${name}" -o "$dest"
}

detect_role() {
  if [[ -f "$APP_DIR/homelab_dashboard.py" ]] \
    || systemctl list-unit-files "${SERVICE_NAME}.service" &>/dev/null \
    || [[ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
    echo "client"
    return
  fi

  if command -v mysql &>/dev/null \
    && { systemctl is-active --quiet mariadb 2>/dev/null \
      || systemctl is-active --quiet mysql 2>/dev/null \
      || [[ -S /run/mysqld/mysqld.sock ]]; }; then
    echo "server"
    return
  fi

  echo "unknown"
}

run_server_setup() {
  local helper
  helper="$(mktemp /tmp/homelab-setup-mariadb.XXXXXX.sh)"
  trap 'rm -f "$helper"' RETURN
  fetch_helper "setup-mariadb.sh" "$helper"
  chmod +x "$helper"
  bash "$helper"
}

write_service_json() {
  local db_host db_port db_name db_user db_pass

  if [[ -n "${HOMELAB_DB_HOST:-}" && -n "${HOMELAB_DB_PASS:-}" ]]; then
    db_host="$HOMELAB_DB_HOST"
    db_port="${HOMELAB_DB_PORT:-3306}"
    db_name="${HOMELAB_DB_NAME:-homelab_dashboard}"
    db_user="${HOMELAB_DB_USER:-homelab_dashboard}"
    db_pass="$HOMELAB_DB_PASS"
    echo "Non-interactieve modus: service.json aanmaken..."
  else
    echo "=== Homelab Dashboard — MariaDB credentials ==="
    echo ""
    read -rp "MariaDB host [10.0.10.17]: " db_host
    db_host="${db_host:-10.0.10.17}"
    read -rp "MariaDB poort [3306]: " db_port
    db_port="${db_port:-3306}"
    read -rp "Database naam [homelab_dashboard]: " db_name
    db_name="${db_name:-homelab_dashboard}"
    read -rp "Database gebruiker [homelab_dashboard]: " db_user
    db_user="${db_user:-homelab_dashboard}"
    read -rsp "Database wachtwoord: " db_pass
    echo ""
    if [[ -z "$db_pass" ]]; then
      echo "Wachtwoord is verplicht."
      exit 1
    fi
  fi

  mkdir -p "$CRED_DIR"
  chmod 700 "$CRED_DIR"

  cat >"$SERVICE_JSON" <<EOF
{
  "host": "$db_host",
  "port": $db_port,
  "user": "$db_user",
  "password": "$db_pass",
  "database": "$db_name"
}
EOF
  chmod 600 "$SERVICE_JSON"
  echo "Opgeslagen: $SERVICE_JSON"
}

test_connection() {
  local helper
  helper="$(mktemp /tmp/homelab-test-db.XXXXXX.sh)"
  trap 'rm -f "$helper"' RETURN
  fetch_helper "test-db-connection.sh" "$helper"
  chmod +x "$helper"
  HOMELAB_APP_ROOT="$APP_DIR" HOMELAB_CREDENTIALS_DIR="$CRED_DIR" bash "$helper"
}

restart_dashboard() {
  if systemctl list-unit-files "${SERVICE_NAME}.service" &>/dev/null \
    || [[ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]]; then
    echo "Dashboard-service herstarten..."
    systemctl restart "$SERVICE_NAME"
    systemctl --no-pager --full status "$SERVICE_NAME" | head -5 || true
  else
    echo "Geen systemd-service gevonden — start handmatig: python homelab_dashboard.py"
  fi
}

run_client_setup() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Voer uit als root op de dashboard-container."
    exit 1
  fi

  write_service_json
  echo ""
  test_connection
  echo ""
  restart_dashboard
  echo ""
  echo "✅ MariaDB is geconfigureerd voor Homelab Dashboard."
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "Open: http://${ip:-<container-ip>}:8765/"
}

main() {
  local role="${1:-}"

  if [[ "$role" == "-h" || "$role" == "--help" ]]; then
    usage
    exit 0
  fi

  if [[ -z "$role" ]]; then
    role="$(detect_role)"
    if [[ "$role" == "unknown" ]]; then
      echo "Kon niet automatisch detecteren waar dit script draait."
      echo ""
      echo "  --server   op MariaDB-host"
      echo "  --client   op dashboard-container"
      exit 1
    fi
    echo "Modus gedetecteerd: $role"
    echo ""
  fi

  case "$role" in
    --server|-s|server)
      if [[ "$(id -u)" -ne 0 ]]; then
        echo "Voer uit als root op de MariaDB-server."
        exit 1
      fi
      run_server_setup
      ;;
    --client|-c|client)
      run_client_setup
      ;;
    *)
      echo "Onbekende optie: $role"
      usage
      exit 1
      ;;
  esac
}

main "${1:-}"