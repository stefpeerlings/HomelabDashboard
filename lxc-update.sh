#!/usr/bin/env bash
#
# Homelab Dashboard - update (community-script layout)
#
# In een LXC container:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"
#
# Vanaf Proxmox host:
#   pct exec <CTID> -t -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"

source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)

REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
APP="Homelab Dashboard"
NSAPP="homelab-dashboard"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-2048}"
var_disk="${var_disk:-4}"

function header_info {
  [[ "${_HEADER_SHOWN:-0}" == "1" ]] && return 0
  _HEADER_SHOWN=1
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
color
catch_errors

function update_script() {
  local app_dir="${HOMELAB_DIR:-/opt/homelab-dashboard}"
  local http_port="${HOMELAB_HTTP_PORT:-8765}"
  local install_script install_log

  header_info
  check_container_storage
  check_container_resources

  if [[ ! -f "${app_dir}/homelab_dashboard.py" ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  install_log="/root/.homelab-update-$(date +%Y%m%d_%H%M%S).log"
  export INSTALL_LOG="$install_log"

  install_script="$(mktemp /tmp/homelab-lxc-update.XXXXXX.sh)"

  msg_info "Updating ${APP}"
  $STD curl -fsSL "${REPO_RAW}/lxc-install.sh" -o "$install_script"
  chmod +x "$install_script"

  if ! HOMELAB_UI=community VERBOSE="${VERBOSE:-no}" INSTALL_LOG="$install_log" \
    bash "$install_script" --update; then
    rm -f "$install_script"
    msg_error "Update failed (log: ${install_log})"
    exit 1
  fi
  rm -f "$install_script"

  msg_ok "Updated successfully!"
  local ip="${LOCAL_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  echo -e "${GATEWAY}${BGN}http://${ip:-<container-ip>}:${http_port}${CL}"
}

start