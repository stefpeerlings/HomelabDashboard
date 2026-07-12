#!/usr/bin/env bash
#
# Homelab Dashboard - update (volledig zelfstandig — geen CDN-afhankelijkheid)
#
# In een LXC container (interactieve shell):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"
#   homelab-update.sh
#
# Vanaf Proxmox host (geen menu — gebruik container-console):
#   pct exec <CTID> -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"

set -Eeuo pipefail

REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
APP_DIR="${HOMELAB_DIR:-/opt/homelab-dashboard}"
VERBOSE="${VERBOSE:-no}"
LOG_FILE="${LOG_FILE:-/tmp/homelab-updater.log}"

CL=$(echo -e "\033[0K")
RD=$(echo -e "\033[01;31m")
GN=$(echo -e "\033[01;32m")
BL=$(echo -e "\033[01;34m")
PP=$(echo -e "\033[01;35m")
BGN_OFF=$(echo -e "\033[0m")

init_updater_log() {
  echo "" >"$LOG_FILE"
  INSTALL_LOG="$LOG_FILE"
}

e_space() {
  local -i n=$((60 - ${#1}))
  ((n < 0)) && n=0
  for ((i = 0; i < n; i++)); do echo -n " "; done
}

msg_info() {
  local msg="${1-}"
  echo -ne "  ${BL}[Info]${BGN_OFF}  ${msg}$(e_space "$msg")"
}

msg_ok() {
  local msg="${1-}"
  echo -e "${CL}  ${GN}[OK]${BGN_OFF}  ${msg}"
}

msg_error() {
  local msg="${1-}"
  echo -e "${CL}  ${RD}[ERROR]${BGN_OFF}  ${msg}" >&2
}

silent() {
  if [[ "$VERBOSE" == "yes" ]]; then
    if [[ "$#" -eq 1 && -n "$1" ]]; then
      eval "$1"
    else
      "$@"
    fi
  else
    if [[ "$#" -eq 1 && -n "$1" ]]; then
      eval "$1" >>"$LOG_FILE" 2>&1
    else
      "$@" >>"$LOG_FILE" 2>&1
    fi
  fi
}

catch_errors() {
  local exit_code="$1"
  local line_number="$2"
  [[ "$exit_code" -eq 0 ]] && return 0
  echo ""
  msg_error "Fout opgetreden in regel ${line_number} (Exit Code: ${exit_code})"
  if [[ "$VERBOSE" != "yes" && -f "$LOG_FILE" ]]; then
    echo -e "\n${RD}Laatste regels uit het logbestand:${BGN_OFF}" >&2
    tail -n 15 "$LOG_FILE" >&2
  fi
  exit "$exit_code"
}

enable_error_trap() {
  [[ "${_HOMELAB_ERR_TRAP:-}" == "1" ]] && return 0
  _HOMELAB_ERR_TRAP=1
  trap 'catch_errors $? $LINENO' ERR
}

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

choose_verbose_mode() {
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

show_header() {
  local title="${1:-HOMELAB DASHBOARD UPDATER}"
  local -i pad=$(((56 - ${#title}) / 2))
  local -i pad_right=$((56 - ${#title} - pad))

  clear
  echo -e "${PP}┌────────────────────────────────────────────────────────┐${BGN_OFF}"
  printf "${PP}│${BGN_OFF}%*s%s%*s${PP}│${BGN_OFF}\n" "$pad" "" "$title" "$pad_right" ""
  echo -e "${PP}└────────────────────────────────────────────────────────┘${BGN_OFF}"
  echo ""
}

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

echo -e "\n${GN}✔ Updateproces succesvol afgerond!${BGN_OFF}\n"