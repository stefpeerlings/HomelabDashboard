#!/usr/bin/env bash
#
# Homelab Dashboard - LXC installatie/update script
#
# In een LXC container (Debian 12/13):
#   bash <(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-install.sh)
#
# Update:
#   bash lxc-install.sh --update

set -euo pipefail

APP_DIR="${HOMELAB_DIR:-/opt/homelab-dashboard}"
REPO_URL="${HOMELAB_REPO:-https://github.com/stefpeerlings/HomelabDashboard.git}"
REPO_BRANCH="${HOMELAB_BRANCH:-main}"
SERVICE_NAME="homelab-dashboard"
CREDENTIALS_DIR="${HOMELAB_CREDENTIALS_DIR:-/root/.homelab-db/credentials}"
HTTP_PORT="${HOMELAB_HTTP_PORT:-8765}"
WS_PORT="${HOMELAB_WS_PORT:-8766}"
UPDATE_MODE=false

if [[ "${1:-}" == "--update" || "${1:-}" == "-u" ]]; then
  UPDATE_MODE=true
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Dit script moet als root worden uitgevoerd (sudo bash lxc-install.sh)"
  exit 1
fi

echo "=== Homelab Dashboard LXC Installer ==="
echo ""

if [[ -f "homelab_dashboard.py" && -f "requirements.txt" ]]; then
  APP_DIR="$(pwd)"
fi

clone_or_update_repo() {
  if [[ -d "$APP_DIR/.git" ]]; then
    echo "Repository bijwerken..."
    git -C "$APP_DIR" pull origin "$REPO_BRANCH"
  else
    echo "Repository clonen..."
    mkdir -p "$APP_DIR"
    git clone -b "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
  fi
}

install_dependencies() {
  echo "[1/6] Systeempackages installeren..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git curl openssh-client
}

setup_venv() {
  echo "[2/6] Python virtualenv voorbereiden..."
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
  echo "[3/6] Python dependencies installeren..."
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
}

prepare_dirs() {
  echo "[4/6] Mappen en credentials voorbereiden..."
  mkdir -p "$CREDENTIALS_DIR" /etc/homelab-dashboard
  chmod 700 "$CREDENTIALS_DIR"

  if [[ ! -f "$CREDENTIALS_DIR/service.json" ]]; then
    cp "$APP_DIR/config/service.json.example" "$CREDENTIALS_DIR/service.json"
    chmod 600 "$CREDENTIALS_DIR/service.json"
    echo "⚠️  MariaDB nog niet geconfigureerd."
    echo "    Op MariaDB-host:"
    echo "      bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)\" -- --server"
    echo "    Op deze container (credentials + test + herstart):"
    echo "      bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)\""
  fi

  if [[ ! -f "$CREDENTIALS_DIR/smtp.json" ]]; then
    cp "$APP_DIR/config/smtp.json.example" "$CREDENTIALS_DIR/smtp.json"
    chmod 600 "$CREDENTIALS_DIR/smtp.json"
  fi
}

setup_ssh_dir() {
  echo "[5/6] SSH-map voorbereiden..."
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
  echo "[6/6] Systemd service configureren..."
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  local public_url="${HOMELAB_PUBLIC_URL:-http://${ip:-127.0.0.1}:${HTTP_PORT}/}"
  local service_path="/etc/systemd/system/${SERVICE_NAME}.service"

  cat >"$service_path" <<EOF
[Unit]
Description=Homelab Dashboard (live logs + SSH)
After=network-online.target
Wants=network-online.target

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
  setup_ssh_dir
  setup_systemd
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo ""
echo "✅ Klaar!"
echo ""
echo "Dashboard: http://${IP:-<container-ip>}:${HTTP_PORT}/"
echo "WebSocket SSH: poort ${WS_PORT}"
echo "Credentials: $CREDENTIALS_DIR"
echo ""
echo "MariaDB alles-in-één (aanbevolen):"
echo "  bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)\""
echo ""
echo "Alleen database aanmaken (op MariaDB-host):"
echo "  bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)\" -- --server"
echo ""
echo "Standaard login (eerste start, lege DB): admin / homelab123"
echo ""
echo "Handige commando's:"
echo "  systemctl status $SERVICE_NAME"
echo "  journalctl -u $SERVICE_NAME -f"
echo "  bash $0 --update"
echo ""