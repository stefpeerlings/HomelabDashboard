#!/usr/bin/env bash
#
# Homelab Dashboard - update
#
# In een LXC container:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"
#
# Vanaf Proxmox host:
#   pct exec <CTID> -t -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"

set -euo pipefail

REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
APP_DIR="${HOMELAB_DIR:-/opt/homelab-dashboard}"
HTTP_PORT="${HOMELAB_HTTP_PORT:-8765}"
VERBOSE="${VERBOSE:-no}"
INSTALL_LOG="${INSTALL_LOG:-/root/.homelab-update-$(date +%Y%m%d_%H%M%S).log}"

load_ui() {
  local ui_script="${HOMELAB_UI_SCRIPT:-}"
  if [[ -z "$ui_script" && -f "${APP_DIR}/scripts/lxc-ui.sh" ]]; then
    ui_script="${APP_DIR}/scripts/lxc-ui.sh"
  fi
  if [[ -n "$ui_script" && -f "$ui_script" ]]; then
    # shellcheck disable=SC1090
    source "$ui_script"
    return 0
  fi
  # shellcheck disable=SC1091
  source <(curl -fsSL "${REPO_RAW}/scripts/lxc-ui.sh")
}

load_ui

run_silent() {
  if [[ "$VERBOSE" == "yes" ]]; then
    "$@"
  else
    "$@" >>"$INSTALL_LOG" 2>&1
  fi
}

choose_update_mode() {
  if [[ -n "${PHS_SILENT:-}" && "${PHS_SILENT}" == "1" ]]; then
    VERBOSE="no"
    return 0
  fi

  if ! command -v whiptail >/dev/null 2>&1 || ! [[ -t 0 ]] || [[ "${TERM:-}" == "dumb" ]]; then
    msg_warn "Geen interactieve terminal — silent mode wordt gebruikt."
    VERBOSE="no"
    return 0
  fi

  local choice
  choice="$(whiptail --backtitle "Homelab Dashboard" \
    --title "Homelab Dashboard LXC Update" \
    --menu "Kies een update-modus:" 12 68 3 \
    "1" "YES (Silent Mode)" \
    "2" "YES (Verbose Mode)" \
    "3" "NO (Cancel Update)" \
    --nocancel --default-item "1" 3>&1 1>&2 2>&3)" || choice="3"

  case "$choice" in
  1) VERBOSE="no" ;;
  2) VERBOSE="yes" ;;
  *)
    clear
    msg_error "Update geannuleerd door gebruiker."
    exit 0
    ;;
  esac
}

run_update() {
  local install_script ip

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

  msg_info "Updater-script ophalen..."
  if run_silent curl -fsSL "${REPO_RAW}/lxc-install.sh" -o "$install_script"; then
    chmod +x "$install_script"
    msg_ok "Updater-script opgehaald."
  else
    rm -f "$install_script"
    msg_error "Ophalen updater-script mislukt (log: ${INSTALL_LOG})."
    exit 1
  fi

  msg_info "Homelab Dashboard bijwerken..."
  if HOMELAB_UI=community VERBOSE="$VERBOSE" INSTALL_LOG="$INSTALL_LOG" \
    bash "$install_script" --update; then
    msg_ok "Homelab Dashboard bijgewerkt."
  else
    rm -f "$install_script"
    msg_error "Update mislukt (log: ${INSTALL_LOG})."
    exit 1
  fi
  rm -f "$install_script"

  msg_info "Overbodige pakketten opruimen..."
  export DEBIAN_FRONTEND=noninteractive
  run_silent apt-get autoremove -y
  run_silent apt-get autoclean -y
  msg_ok "Systeem is opgeruimd."

  msg_ok "Update voltooid!"
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo ""
  echo -e "  ${CY}Dashboard:${BGN_OFF}  ${BGN} http://${ip:-<container-ip>}:${HTTP_PORT} ${BGN_OFF}"
  echo ""
}

choose_update_mode
export VERBOSE INSTALL_LOG
run_update