#!/usr/bin/env bash
#
# Homelab Dashboard - LXC installatie/update script
#
# In een LXC container (Debian 12/13):
#   bash <(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-install.sh)
#
# Update:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-install.sh)" -- --update
#
# Externe MariaDB i.p.v. lokaal:
#   HOMELAB_DB_MODE=remote bash lxc-install.sh

set -euo pipefail

APP_DIR="${HOMELAB_DIR:-/opt/homelab-dashboard}"
REPO_URL="${HOMELAB_REPO:-https://github.com/stefpeerlings/HomelabDashboard.git}"
REPO_BRANCH="${HOMELAB_BRANCH:-main}"
SERVICE_NAME="homelab-dashboard"
CREDENTIALS_DIR="${HOMELAB_CREDENTIALS_DIR:-/root/.homelab-db/credentials}"
HTTP_PORT="${HOMELAB_HTTP_PORT:-8765}"
WS_PORT="${HOMELAB_WS_PORT:-8766}"
DB_MODE="${HOMELAB_DB_MODE:-local}"
UPDATE_MODE=false
QUIET=false
INSTALL_LOG="${HOMELAB_INSTALL_LOG:-/tmp/homelab-lxc-install.log}"

if [[ "${HOMELAB_QUIET:-}" == "1" ]]; then
  QUIET=true
fi

if [[ "${1:-}" == "--update" || "${1:-}" == "-u" ]]; then
  UPDATE_MODE=true
fi

step() {
  if [[ "$QUIET" == true ]]; then
    echo "$(date -Iseconds) $*" >>"$INSTALL_LOG"
  else
    echo "$@"
  fi
}

run_quiet() {
  if [[ "$QUIET" == true ]]; then
    "$@" >>"$INSTALL_LOG" 2>&1
  else
    "$@"
  fi
}

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Dit script moet als root worden uitgevoerd (sudo bash lxc-install.sh)"
  exit 1
fi

step "=== Homelab Dashboard LXC Installer ==="
[[ "$QUIET" != true ]] && echo ""

if [[ -f "homelab_dashboard.py" && -f "requirements.txt" ]]; then
  APP_DIR="$(pwd)"
fi

clone_or_update_repo() {
  if [[ -d "$APP_DIR/.git" ]]; then
    step "Repository bijwerken..."
    run_quiet git -C "$APP_DIR" pull origin "$REPO_BRANCH"
  else
    step "Repository clonen..."
    mkdir -p "$APP_DIR"
    run_quiet git clone -b "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
  fi
}

install_dependencies() {
  step "[1/7] Systeempackages installeren..."
  export DEBIAN_FRONTEND=noninteractive
  run_quiet apt-get update -qq
  run_quiet apt-get install -y -qq python3 python3-venv python3-pip git curl openssh-client openssl
}

setup_venv() {
  step "[2/7] Python virtualenv voorbereiden..."
  cd "$APP_DIR"
  if [[ -d ".venv" && ! -f ".venv/bin/activate" ]]; then
    rm -rf .venv
  fi
  if [[ ! -d ".venv" ]]; then
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
}

install_python_packages() {
  step "[3/7] Python dependencies installeren..."
  run_quiet pip install --upgrade pip -q
  run_quiet pip install -r requirements.txt -q
}

prepare_dirs() {
  step "[4/7] Mappen en credentials voorbereiden..."
  mkdir -p "$CREDENTIALS_DIR" /etc/homelab-dashboard
  chmod 700 "$CREDENTIALS_DIR"

  if [[ "$DB_MODE" == "remote" && ! -f "$CREDENTIALS_DIR/service.json" ]]; then
    cp "$APP_DIR/config/service.json.example" "$CREDENTIALS_DIR/service.json"
    chmod 600 "$CREDENTIALS_DIR/service.json"
    step "Externe MariaDB: vul service.json in of gebruik setup-database.sh"
  fi

  if [[ ! -f "$CREDENTIALS_DIR/smtp.json" ]]; then
    cp "$APP_DIR/config/smtp.json.example" "$CREDENTIALS_DIR/smtp.json"
    chmod 600 "$CREDENTIALS_DIR/smtp.json"
  fi
}

setup_local_mariadb() {
  if [[ "$DB_MODE" == "remote" ]]; then
    return 0
  fi
  step "[5/7] MariaDB lokaal installeren..."
  local script="$APP_DIR/scripts/setup-local-mariadb.sh"
  if [[ ! -f "$script" ]]; then
    curl -fsSL "${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}/scripts/setup-local-mariadb.sh" -o /tmp/setup-local-mariadb.sh
    script="/tmp/setup-local-mariadb.sh"
  fi
  chmod +x "$script"
  HOMELAB_QUIET="$([[ "$QUIET" == true ]] && echo 1 || echo 0)" \
    HOMELAB_INSTALL_LOG="$INSTALL_LOG" \
    HOMELAB_CREDENTIALS_DIR="$CREDENTIALS_DIR" \
    HOMELAB_APP_ROOT="$APP_DIR" \
    bash "$script"
}

setup_ssh_dir() {
  step "[6/7] SSH-map voorbereiden..."
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  if [[ ! -f /root/.ssh/config ]]; then
    cat >/root/.ssh/config <<'EOF'
# Voeg Proxmox/PBS hosts toe, bijv.:
# Host proxmox.lan
#   HostName 10.0.30.3
#   User root
#   IdentityFile ~/.ssh/id_ed25519
EOF
    chmod 600 /root/.ssh/config
  fi
}

setup_systemd() {
  step "[7/7] Systemd service configureren..."
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  local public_url="${HOMELAB_PUBLIC_URL:-http://${ip:-127.0.0.1}:${HTTP_PORT}/}"
  local service_path="/etc/systemd/system/${SERVICE_NAME}.service"
  local after_targets="network-online.target"
  local wants_targets="network-online.target"

  if [[ "$DB_MODE" != "remote" ]]; then
    after_targets="network-online.target mariadb.service"
    wants_targets="network-online.target mariadb.service"
  fi

  cat >"$service_path" <<EOF
[Unit]
Description=Homelab Dashboard (live logs + SSH)
After=$after_targets
Wants=$wants_targets

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=HOMELAB_APP_ROOT=$APP_DIR
Environment=HOMELAB_CREDENTIALS_DIR=$CREDENTIALS_DIR
Environment=HOMELAB_PUBLIC_URL=$public_url
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/homelab_dashboard.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
}

if [[ "$UPDATE_MODE" == true ]]; then
  if [[ ! -d "$APP_DIR" ]]; then
    echo "Geen installatie gevonden in $APP_DIR"
    exit 1
  fi
  clone_or_update_repo
  setup_venv
  install_python_packages
  systemctl restart "$SERVICE_NAME"
else
  install_dependencies
  if [[ ! -f "$APP_DIR/homelab_dashboard.py" ]]; then
    clone_or_update_repo
  fi
  setup_venv
  install_python_packages
  prepare_dirs
  setup_local_mariadb
  setup_ssh_dir
  setup_systemd
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

if [[ "$QUIET" == true ]]; then
  step "Klaar — dashboard op http://${IP:-<container-ip>}:${HTTP_PORT}/"
  exit 0
fi

echo ""
echo "✅ Klaar!"
echo ""
echo "Dashboard: http://${IP:-<container-ip>}:${HTTP_PORT}/"
echo "WebSocket SSH: poort ${WS_PORT}"
echo "MariaDB: lokaal op 127.0.0.1 (automatisch geconfigureerd)"
echo "Credentials: $CREDENTIALS_DIR"
echo ""
echo "Standaard login (eerste start): admin / homelab123"
echo ""
if [[ "$DB_MODE" == "remote" ]]; then
  echo "Externe MariaDB koppelen:"
  echo "  bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)\""
  echo ""
fi
echo "Handige commando's:"
echo "  systemctl status $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
echo "  bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-install.sh)\" -- --update"
echo ""