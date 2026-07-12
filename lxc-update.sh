#!/usr/bin/env bash
#
# Homelab Dashboard - update
#
# In een LXC container:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"
#
# Vanaf Proxmox host:
#   pct exec <CTID> -t -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"

set -Eeuo pipefail

REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
APP_DIR="${HOMELAB_DIR:-/opt/homelab-dashboard}"
HTTP_PORT="${HOMELAB_HTTP_PORT:-8765}"

load_ui() {
  local ui_script="${HOMELAB_UI_SCRIPT:-}"
  if [[ -n "$ui_script" && -f "$ui_script" ]]; then
    # shellcheck disable=SC1090
    source "$ui_script"
    return 0
  fi
  # shellcheck disable=SC1091
  source <(curl -fsSL "${REPO_RAW}/scripts/lxc-ui.sh")
}

load_ui
init_updater_log
choose_verbose_mode
export VERBOSE LOG_FILE INSTALL_LOG="$LOG_FILE"
enable_error_trap

if [[ "$(id -u)" -ne 0 ]]; then
  msg_error "Dit script moet als root worden uitgevoerd."
  exit 1
fi

show_header "HOMELAB DASHBOARD UPDATER"

if [[ ! -f "${APP_DIR}/homelab_dashboard.py" ]]; then
  msg_error "Geen Homelab Dashboard installatie gevonden in ${APP_DIR}."
  exit 1
fi

install_script="$(mktemp /tmp/homelab-lxc-update.XXXXXX.sh)"

msg_info "Updater-script ophalen"
silent curl -fsSL "${REPO_RAW}/lxc-install.sh" -o "$install_script"
chmod +x "$install_script"
msg_ok "Updater-script opgehaald"

HOMELAB_UI=community VERBOSE="$VERBOSE" LOG_FILE="$LOG_FILE" INSTALL_LOG="$LOG_FILE" \
  bash "$install_script" --update
rm -f "$install_script"

msg_info "Overbodige pakketten opruimen"
export DEBIAN_FRONTEND=noninteractive
silent apt-get autoremove -y
silent apt-get autoclean -y
msg_ok "Systeem opgeruimd"

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo -e "\n${GN}✔ Updateproces succesvol afgerond!${BGN_OFF}"
echo -e "  ${CY}Dashboard:${BGN_OFF}  ${BGN} http://${ip:-<container-ip>}:${HTTP_PORT} ${BGN_OFF}\n"