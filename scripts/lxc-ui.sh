#!/usr/bin/env bash
# Homelab Dashboard — Proxmox Helper Script UI (Verbose / Silent)

if [[ -z "${_HOMELAB_LXC_UI_LOADED:-}" ]]; then
_HOMELAB_LXC_UI_LOADED=1

CL=$(echo -e "\033[0K")
RD=$(echo -e "\033[01;31m")
GN=$(echo -e "\033[01;32m")
YL=$(echo -e "\033[01;33m")
BL=$(echo -e "\033[01;34m")
PP=$(echo -e "\033[01;35m")
CY=$(echo -e "\033[01;36m")
BGN=$(echo -e "\033[42;1;37m")
BGN_OFF=$(echo -e "\033[0m")

VERBOSE="${VERBOSE:-no}"
LOG_FILE="${LOG_FILE:-${INSTALL_LOG:-/tmp/homelab-updater.log}}"

init_updater_log() {
  LOG_FILE="${LOG_FILE:-${INSTALL_LOG:-/tmp/homelab-updater.log}}"
  INSTALL_LOG="$LOG_FILE"
  echo "" >"$LOG_FILE"
}

e_space() {
  local -i n=$((60 - ${#1}))
  ((n < 0)) && n=0
  for ((i = 0; i < n; i++)); do echo -n " "; done
}

msg_info() {
  local msg="$1"
  echo -ne "  ${BL}[Info]${BGN_OFF}  ${msg}$(e_space "$msg")"
}

msg_ok() {
  local msg="$1"
  echo -e "${CL}  ${GN}[OK]${BGN_OFF}  ${msg}"
}

msg_error() {
  local msg="$1"
  echo -e "${CL}  ${RD}[ERROR]${BGN_OFF}  ${msg}" >&2
}

msg_warn() {
  local msg="$1"
  echo -e "${CL}  ${YL}[WARN]${BGN_OFF}  ${msg}" >&2
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

choose_verbose_mode() {
  if [[ -n "${PHS_SILENT:-}" && "${PHS_SILENT}" == "1" ]]; then
    VERBOSE="no"
    return 0
  fi

  if ! command -v whiptail >/dev/null 2>&1 || ! [[ -t 0 ]] || [[ "${TERM:-}" == "dumb" ]]; then
    VERBOSE="no"
    return 0
  fi

  if whiptail --backtitle "Homelab Dashboard" \
    --title "UPDATE MODUS" \
    --yesno "Wilt u de technische output live bekijken (Verbose)?\n\nKies 'Nee' voor de strakke Proxmox-stijl (Silent)." 10 58; then
    VERBOSE="yes"
  else
    VERBOSE="no"
  fi
}

show_header() {
  local title="${1:-HOMELAB DASHBOARD UPDATER}"
  local -i width=56
  local -i pad=$(((width - ${#title}) / 2))
  local -i pad_right=$((width - ${#title} - pad))

  clear
  echo -e "${PP}┌────────────────────────────────────────────────────────┐${BGN_OFF}"
  printf "${PP}│${BGN_OFF}%*s%s%*s${PP}│${BGN_OFF}\n" "$pad" "" "$title" "$pad_right" ""
  echo -e "${PP}└────────────────────────────────────────────────────────┘${BGN_OFF}"
  echo ""
}

fi