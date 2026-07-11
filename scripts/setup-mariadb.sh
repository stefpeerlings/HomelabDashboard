#!/usr/bin/env bash
# Maakt database + gebruiker aan op een MariaDB-server voor Homelab Dashboard.
# Uitvoeren op de MariaDB-host/LXC (niet op de dashboard-container).

set -euo pipefail

DB_NAME="${HOMELAB_DB_NAME:-homelab_dashboard}"
DB_USER="${HOMELAB_DB_USER:-homelab_dashboard}"
DB_HOST_PATTERN="${HOMELAB_DB_HOST_PATTERN:-10.0.%}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Voer uit als root op de MariaDB-server."
  exit 1
fi

if ! command -v mysql &>/dev/null; then
  echo "mysql client niet gevonden. Installeer mariadb-client of mysql-client."
  exit 1
fi

echo "=== Homelab Dashboard — MariaDB setup ==="
echo "Database: $DB_NAME"
echo "Gebruiker:  $DB_USER"
echo "Toegang vanaf hosts: $DB_HOST_PATTERN"
echo ""

read -rsp "MariaDB root wachtwoord (leeg = socket auth als root): " MYSQL_ROOT_PASS
echo ""

read -rsp "Nieuw wachtwoord voor $DB_USER: " DB_PASS
echo ""
read -rsp "Bevestig wachtwoord: " DB_PASS2
echo ""

if [[ "$DB_PASS" != "$DB_PASS2" ]]; then
  echo "Wachtwoorden komen niet overeen."
  exit 1
fi

if [[ ${#DB_PASS} -lt 12 ]]; then
  echo "Gebruik minimaal 12 tekens voor het database-wachtwoord."
  exit 1
fi

SQL="$(mktemp)"
trap 'rm -f "$SQL"' EXIT

cat >"$SQL" <<EOF
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS '${DB_USER}'@'${DB_HOST_PATTERN}' IDENTIFIED BY '${DB_PASS}';
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';

GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'${DB_HOST_PATTERN}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost';

FLUSH PRIVILEGES;
EOF

if [[ -n "$MYSQL_ROOT_PASS" ]]; then
  mysql -u root -p"$MYSQL_ROOT_PASS" <"$SQL"
else
  mysql -u root <"$SQL"
fi

echo ""
echo "✅ MariaDB klaar."
MARIADB_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo '<mariadb-ip>')"

echo ""
echo "Volgende stap op de dashboard-container:"
echo "  bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)\""
echo ""
echo "Of non-interactief:"
echo "  HOMELAB_DB_HOST=${MARIADB_IP} HOMELAB_DB_PASS='<jouw-wachtwoord>' \\"
echo "    bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)\" -- --client"
echo ""