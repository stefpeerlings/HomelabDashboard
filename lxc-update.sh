#!/usr/bin/env bash
#
# Homelab Dashboard - update
#
# In een LXC container (interactieve shell):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"
#
# Vanaf Proxmox host (geen menu — gebruik container-console):
#   pct exec <CTID> -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"

set -Eeuo pipefail

REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
HOMELAB_UI_REV="${HOMELAB_UI_REV:-3}"
APP_DIR="${HOMELAB_DIR:-/opt/homelab-dashboard}"
VERBOSE="${VERBOSE:-no}"

_whiptail_tty() {
  if [[ -t 0 ]]; then
    echo "0"
    return 0
  fi
  if [[ -r /dev/tty && -w /dev/tty ]]; then
    echo "/dev/tty"
    return 0
  fi
  return 1
}

choose_update_mode() {
  local choice tty_target whiptail_args=()

  if [[ -n "${PHS_SILENT:-}" && "${PHS_SILENT}" == "1" ]]; then
    VERBOSE="no"
    return 0
  fi

  if ! command -v whiptail >/dev/null 2>&1 || [[ "${TERM:-}" == "dumb" ]]; then
    VERBOSE="no"
    return 0
  fi

  if ! tty_target="$(_whiptail_tty)"; then
    VERBOSE="no"
    return 0
  fi

  whiptail_args=(
    --backtitle "Proxmox VE Helper Scripts"
    --title "Homelab Dashboard LXC Update/Setting"
    --menu "Support/Update functions for Homelab Dashboard LXC. Choose an option:"
    12 60 3
    "1" "YES (Silent Mode)"
    "2" "YES (Verbose Mode)"
    "3" "NO (Cancel Update)"
    --nocancel --default-item "1"
  )

  if [[ "$tty_target" == "0" ]]; then
    choice="$(whiptail "${whiptail_args[@]}" 3>&1 1>&2 2>&3)" || choice="3"
  else
    choice="$(whiptail "${whiptail_args[@]}" </dev/tty >/dev/tty 2>&1)" || choice="3"
  fi

  case "$choice" in
  1) VERBOSE="no" ;;
  2) VERBOSE="yes" ;;
  *)
    clear
    echo "Update geannuleerd."
    exit 0
    ;;
  esac
}

load_ui() {
  local ui_script="${HOMELAB_UI_SCRIPT:-}"
  local ui_url="${REPO_RAW}/scripts/lxc-ui.sh"

  if [[ -z "$ui_script" && -f "${APP_DIR}/scripts/lxc-ui.sh" ]] \
    && grep -q "HOMELAB_LXC_UI_REV=${HOMELAB_UI_REV}" "${APP_DIR}/scripts/lxc-ui.sh" 2>/dev/null; then
    ui_script="${APP_DIR}/scripts/lxc-ui.sh"
  fi

  if [[ -n "$ui_script" && -f "$ui_script" ]]; then
    # shellcheck disable=SC1090
    source "$ui_script"
    return 0
  fi

  # shellcheck disable=SC1091
  if ! source <(curl -fsSL "${ui_url}?rev=${HOMELAB_UI_REV}"); then
    # shellcheck disable=SC1091
    source <(curl -fsSL "${ui_url}")
  fi
}

choose_update_mode
load_ui
init_updater_log
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

echo -e "\n${GN}✔ Updateproces succesvol afgerond!${BGN_OFF}\n"