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
var_ram="${var_ram:-2048}"
var_disk="${var_disk:-4}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_arm64="${var_arm64:-yes}"
var_unprivileged="${var_unprivileged:-1}"

function finish_dashboard_install() {
  local ctid="${CTID:-}"
  [[ -n "$ctid" ]] || return 0

  local pve_name pve_ip proxmox_target seed_log="/var/log/homelab-dashboard-seed-${ctid}.log"
  pve_name="$(hostname -s 2>/dev/null || hostname)"
  pve_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  proxmox_target="${pve_ip:-$pve_name}"

  msg_info "installing systemfiles (patience)"
  local ready=false
  for _ in $(seq 1 45); do
    if pct exec "$ctid" -- systemctl is-active --quiet homelab-dashboard 2>/dev/null \
      && pct exec "$ctid" -- systemctl is-active --quiet mariadb 2>/dev/null; then
      ready=true
      break
    fi
    sleep 2
  done
  if [[ "$ready" != true ]]; then
    msg_warn "Dashboard-service nog niet actief — panelen mogelijk leeg bij eerste openen"
    return 1
  fi

  local pubkey
  pubkey="$(pct exec "$ctid" -- cat /root/.ssh/id_ed25519_default.pub 2>/dev/null || true)"
  if [[ -n "$pubkey" ]]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    touch /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    grep -qF "$pubkey" /root/.ssh/authorized_keys 2>/dev/null || echo "$pubkey" >> /root/.ssh/authorized_keys
  fi

  if [[ -n "$pve_name" && -n "$pve_ip" ]]; then
    pct exec "$ctid" -- bash -c \
      "grep -qE '[[:space:]]${pve_name}([[:space:]]|\$)' /etc/hosts 2>/dev/null || echo '${pve_ip} ${pve_name}' >> /etc/hosts" \
      2>/dev/null || true
  fi

  : >"$seed_log"
  local attempt panel_count=0
  for attempt in 1 2 3 4 5; do
    if pct exec "$ctid" -- env \
      HOMELAB_PROXMOX_HOST="$proxmox_target" \
      HOMELAB_NODE_NAME="$pve_name" \
      HOMELAB_APP_ROOT=/opt/homelab-dashboard \
      bash /opt/homelab-dashboard/scripts/seed-proxmox-node.sh >>"$seed_log" 2>&1; then
      panel_count="$(grep -oE 'Auto-panelen: [0-9]+' "$seed_log" | tail -1 | awk '{print $2}')"
      [[ -n "$panel_count" && "$panel_count" -gt 0 ]] && break
    fi
    sleep 3
  done

  if [[ -n "$panel_count" && "$panel_count" -gt 0 ]]; then
    return 0
  fi

  msg_warn "Auto-panelen niet klaar — log: ${seed_log}"
  return 1
}

function configure_dashboard_proxmox_from_host() {
  finish_dashboard_install
}

function install_dashboard_in_ct() {
  local mode="${1:-}"
  local install_script="/tmp/homelab-dashboard-lxc-install.sh"
  local install_log="/var/log/homelab-dashboard-install-${CTID}.log"
  local pve_name pve_ip proxmox_target
  pve_name="$(hostname -s 2>/dev/null || hostname)"
  pve_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  proxmox_target="${pve_ip:-$pve_name}"

  if [[ "$mode" == "--update" ]]; then
    curl -fsSL "${RAW_BASE}/lxc-update.sh" -o "$install_script"
  else
    curl -fsSL "${RAW_BASE}/lxc-install.sh" -o "$install_script"
  fi
  pct push "$CTID" "$install_script" /tmp/homelab-update.sh
  pct exec "$CTID" -- chmod +x /tmp/homelab-update.sh

  if ! HOMELAB_QUIET=1 HOMELAB_PROXMOX_HOST="$proxmox_target" HOMELAB_NODE_NAME="$pve_name" \
    pct exec "$CTID" -- bash /tmp/homelab-update.sh >>"$install_log" 2>&1; then
    msg_error "Installation failed (log: ${install_log})"
    tail -20 "$install_log" || true
    exit 1
  fi

  configure_dashboard_proxmox_from_host
  pct set "$CTID" -tags homelab-dashboard
  rm -f "$install_script"
}

eval "$(declare -f build_container | sed '1s/^build_container/_homelab_build_container_orig/')"

build_container() {
  curl() {
    local url=""
    for arg in "$@"; do
      [[ "$arg" == http* ]] && url="$arg"
    done
    if [[ "$url" == *"community-scripts/ProxmoxVE/main/install/"* ]]; then
      command curl -fsSL "${RAW_BASE}/install/pve-container-install.sh"
      return $?
    fi
    command curl "$@"
  }
  _homelab_build_container_orig
  unset -f curl
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

  install_dashboard_in_ct --update
  msg_ok "Updated successfully!"
  exit
}

start
build_container
pct set "$CTID" -tags homelab-dashboard 2>/dev/null || true
description

finish_dashboard_install || true

clear
msg_ok "Completed successfully!\n"
echo -e "${GATEWAY}${BGN}http://${IP}:8765${CL}"
echo -e "Login: admin / homelab123"