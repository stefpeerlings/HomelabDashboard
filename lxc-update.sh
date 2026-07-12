#!/usr/bin/env bash
#
# Homelab Dashboard - update (community-script layout)
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"
#
# Vanaf Proxmox host:
#   pct exec <CTID> -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"

set -euo pipefail

REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
APP_DIR="${HOMELAB_DIR:-/opt/homelab-dashboard}"
HTTP_PORT="${HOMELAB_HTTP_PORT:-8765}"
COMMUNITY_BASE="https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc"

if command -v curl >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  source <(curl -fsSL "${COMMUNITY_BASE}/core.func") 2>/dev/null || true
  # shellcheck disable=SC1091
  source <(curl -fsSL "${COMMUNITY_BASE}/error_handler.func") 2>/dev/null || true
  load_functions 2>/dev/null || true
  catch_errors 2>/dev/null || true
  color 2>/dev/null || true
fi

if ! declare -F msg_info >/dev/null 2>&1; then
  msg_info() { echo "➜ $*"; }
  msg_ok() { echo "✓ $*"; }
  msg_error() { echo "✖ $*" >&2; exit 1; }
fi

header_info() {
  clear
  cat <<"EOF"
 _   _                      _       _
| | | | ___  _ __ ___   ___| | __ _| |__
| |_| |/ _ \| '_ ` _ \ / _ \ |/ _` | '_ \
|  _  | (_) | | | | | |  __/ | (_| | |_) |
|_| |_|\___/|_| |_| |_|\___|_|\__,_|_.__/

 ____            _     _                         _
|  _ \  __ _ ___| |__ | |__   ___   __ _ _ __ __| |
| | | |/ _` / __| '_ \| '_ \ / _ \ / _` | '__/ _` |
| |_| | (_| \__ \ | | | |_) | (_) | (_| | | | (_| |
|____/ \__,_|___/_| |_|_.__/ \___/ \__,_|_|  \__,_|

EOF
}

header_info

if [[ "$(id -u)" -ne 0 ]]; then
  msg_error "Dit script moet als root worden uitgevoerd"
  exit 1
fi

if [[ ! -f "${APP_DIR}/homelab_dashboard.py" ]]; then
  msg_error "Geen Homelab Dashboard installatie gevonden in ${APP_DIR}"
  exit 1
fi

msg_info "Updating Homelab Dashboard"

INSTALL_SCRIPT="$(mktemp /tmp/homelab-lxc-update.XXXXXX.sh)"
trap 'rm -f "$INSTALL_SCRIPT"' EXIT
curl -fsSL "${REPO_RAW}/lxc-install.sh" -o "$INSTALL_SCRIPT"
chmod +x "$INSTALL_SCRIPT"
HOMELAB_UI=community bash "$INSTALL_SCRIPT" --update

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
msg_ok "Updated successfully!"
if [[ -n "${GATEWAY:-}" && -n "${BGN:-}" && -n "${CL:-}" ]]; then
  echo -e "${GATEWAY}${BGN}http://${IP:-<container-ip>}:${HTTP_PORT}${CL}"
else
  echo "http://${IP:-<container-ip>}:${HTTP_PORT}"
fi