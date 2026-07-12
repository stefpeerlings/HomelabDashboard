#!/usr/bin/env bash
# Homelab Dashboard — LXC updater UI (kleuren & statusbalken)

[[ -n "${_HOMELAB_LXC_UI_LOADED:-}" ]] && return 0
_HOMELAB_LXC_UI_LOADED=1

CL=$(echo -e "\033[m")
RD=$(echo -e "\033[01;31m")
GN=$(echo -e "\033[01;32m")
YL=$(echo -e "\033[01;33m")
BL=$(echo -e "\033[01;34m")
PP=$(echo -e "\033[01;35m")
CY=$(echo -e "\033[01;36m")
BGN=$(echo -e "\033[42;1;37m")
BGN_OFF=$(echo -e "\033[0m")

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

show_header() {
  local title="${1:-HOMELAB DASHBOARD UPDATER}"
  local -i width=56
  local -i pad=$(( (width - ${#title}) / 2 ))
  local -i pad_right=$((width - ${#title} - pad))
  local line

  line="$(printf '─%.0s' $(eval "echo {1..$width}"))"
  clear
  echo -e "${PP}┌${line}┐${BGN_OFF}"
  printf "${PP}│${BGN_OFF}%*s%s%*s${PP}│${BGN_OFF}\n" "$pad" "" "$title" "$pad_right" ""
  echo -e "${PP}└${line}┘${BGN_OFF}"
  echo ""
}