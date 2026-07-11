#!/usr/bin/env bash
#
# Homelab Dashboard - Proxmox VE LXC Installer
#
# Op je Proxmox host:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/ct/homelab-dashboard.sh)"
#
# Of na clone:
#   git clone https://github.com/stefpeerlings/HomelabDashboard.git
#   cd HomelabDashboard && bash ct/homelab-dashboard.sh

source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)

REPO="stefpeerlings/HomelabDashboard"
BRANCH="main"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/${BRANCH}"

fetch_repo_file() {
  local path="$1"
  local dest="$2"

  if curl -fsSL "${RAW_BASE}/${path}" -o "$dest" 2>/dev/null; then
    return 0
  fi

  local local_path
  local_path="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/${path}"
  if [[ -f "$local_path" ]]; then
    cp "$local_path" "$dest"
    return 0
  fi

  echo "Fout: kan ${path} niet ophalen van GitHub." >&2
  return 1
}

APP="HomelabDashboard"
var_hostname="${var_hostname:-homelab-dashboard}"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-1024}"
var_disk="${var_disk:-4}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_arm64="${var_arm64:-yes}"
var_unprivileged="${var_unprivileged:-1}"

function install_dashboard_in_ct() {
  local install_script="/tmp/homelab-dashboard-lxc-install.sh"
  local install_log="/var/log/homelab-dashboard-install-${CTID}.log"

  msg_info "Installing ${APP} in CT ${CTID}"
  curl -fsSL "${RAW_BASE}/lxc-install.sh" -o "$install_script"
  pct push "$CTID" "$install_script" /tmp/lxc-install.sh

  if ! HOMELAB_QUIET=1 pct exec "$CTID" -- bash /tmp/lxc-install.sh >>"$install_log" 2>&1; then
    msg_error "Installation failed (log: ${install_log})"
    tail -20 "$install_log" || true
    exit 1
  fi

  pct set "$CTID" -tags homelab-dashboard
  rm -f "$install_script"
  msg_ok "Installed ${APP}"
}

function header_info {
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

header_info "$APP"
variables
var_install="debian-install"
color
catch_errors

function default_settings() {
  CT_TYPE="1"
  PW=""
  CT_ID=$(pvesh get /cluster/nextid 2>/dev/null || echo 100)
  HN="homelab-dashboard"
  DISK_SIZE="$var_disk"
  CORE_COUNT="$var_cpu"
  RAM_SIZE="$var_ram"
  BRG="vmbr0"
  NET="dhcp"
  GATE=""
  APT_CACHER=""
  APT_CACHER_IP=""
  DISABLEIP6="no"
  MTU=""
  SD=""
  NS=""
  MAC=""
  VLAN=""
  SSH="no"
  VERB="no"
  echo_default
  TAGS="homelab-dashboard"
}

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if ! pct exec "$CTID" -- test -d /opt/homelab-dashboard; then
    msg_error "Geen Homelab Dashboard installatie gevonden in deze container!"
    exit
  fi

  install_dashboard_in_ct
  msg_ok "Updated successfully!"
  exit
}

start
build_container
install_dashboard_in_ct
description

clear
msg_ok "Completed successfully!\n"
echo -e "${GATEWAY}${BGN}http://${IP}:8765${CL}"