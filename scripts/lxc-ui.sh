#!/usr/bin/env bash
# Homelab Dashboard — Verbose / Silent updater UI

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
  echo "--- Homelab Dashboard Update Log Start ---" >"$LOG_FILE"
}

msg_info() {
  local msg="$1"
  local -i n=$((50 - ${#msg}))
  ((n < 0)) && n=0
  echo -ne "  ${BL}[Info]${BGN_OFF}  ${msg}"
  for ((i = 0; i < n; i++)); do echo -n " "; done
}

msg_ok() {
  echo -e "${CL}  ${GN}[OK]${BGN_OFF}"
}

msg_error() {
  if [[ "$#" -eq 2 && "$1" =~ ^[0-9]+$ ]]; then
    echo -e "${CL}  ${RD}[ERROR] in regel $1: exit code $2${BGN_OFF}" >&2
  else
    echo -e "${CL}  ${RD}[ERROR]${BGN_OFF}  $*" >&2
  fi
}

msg_warn() {
  echo -e "${CL}  ${YL}[WARN]${BGN_OFF}  $*" >&2
}

silent() {
  if [[ "$VERBOSE" == "yes" ]]; then
    "$@"
  else
    "$@" >>"$LOG_FILE" 2>&1
  fi
}

catch_errors() {
  local exit_code="$1"
  local line_number="$2"
  if [[ "$exit_code" -ne 0 ]]; then
    msg_error "$line_number" "$exit_code"
    echo -e "\n${RD}Het proces is mislukt. Bekijk de details hieronder:${BGN_OFF}\n" >&2
    if [[ "$VERBOSE" != "yes" && -f "$LOG_FILE" ]]; then
      tail -n 20 "$LOG_FILE" >&2
    fi
    exit "$exit_code"
  fi
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
    msg_warn "Geen interactieve terminal — silent mode wordt gebruikt."
    VERBOSE="no"
    return 0
  fi

  if whiptail --backtitle "Homelab Dashboard" \
    --title "Update Modus" \
    --yesno "Wilt u de update uitvoeren in Verbose Modus?\n(Toon alle technische output)" 10 60; then
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
  local line

  line="$(printf '─%.0s' $(eval "echo {1..$width}"))"
  clear
  echo -e "${PP}┌${line}┐${BGN_OFF}"
  printf "${PP}│${BGN_OFF}%*s%s%*s${PP}│${BGN_OFF}\n" "$pad" "" "$title" "$pad_right" ""
  echo -e "${PP}└${line}┘${BGN_OFF}"
  echo ""
}

fi