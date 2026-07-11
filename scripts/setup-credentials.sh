#!/usr/bin/env bash
# Interactief MariaDB-credentials bestand aanmaken voor Homelab Dashboard.

set -euo pipefail

CRED_DIR="${HOMELAB_CREDENTIALS_DIR:-/root/.homelab-db/credentials}"
TARGET="$CRED_DIR/service.json"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Voer uit als root."
  exit 1
fi

mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR"

read -rp "MariaDB host [mariadb.lan]: " DB_HOST
DB_HOST="${DB_HOST:-mariadb.lan}"
read -rp "MariaDB poort [3306]: " DB_PORT
DB_PORT="${DB_PORT:-3306}"
read -rp "Database naam [homelab_dashboard]: " DB_NAME
DB_NAME="${DB_NAME:-homelab_dashboard}"
read -rp "Database gebruiker [homelab_dashboard]: " DB_USER
DB_USER="${DB_USER:-homelab_dashboard}"
read -rsp "Database wachtwoord: " DB_PASS
echo ""

cat >"$TARGET" <<EOF
{
  "host": "$DB_HOST",
  "port": $DB_PORT,
  "user": "$DB_USER",
  "password": "$DB_PASS",
  "database": "$DB_NAME"
}
EOF
chmod 600 "$TARGET"
echo "Opgeslagen: $TARGET"