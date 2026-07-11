#!/usr/bin/env bash
# Homelab Dashboard — Proxmox VE container install (debian base + app)
# Draait in de LXC tijdens build_container, vóór "Cleaned".

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os
motd_ssh
customize

homelab_install_msg() {
  local text="$1"
  if [[ -n "${YW:-}" && -n "${CL:-}" ]]; then
    echo -e "${YW}${text}${CL}"
  else
    echo "$text"
  fi
}

echo ""
homelab_install_msg "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
homelab_install_msg "  Installing Homelab Dashboard system files"
homelab_install_msg "  Please be patient — this may take a few minutes"
homelab_install_msg "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

HOMELAB_QUIET=1
HOMELAB_REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
curl -fsSL "${HOMELAB_REPO_RAW}/lxc-install.sh" -o /tmp/homelab-lxc-install.sh
if bash /tmp/homelab-lxc-install.sh >>/root/.homelab-install.log 2>&1; then
  homelab_install_msg "  ✓ Homelab Dashboard system files installed"
else
  echo "Homelab Dashboard install failed — see /root/.homelab-install.log"
  exit 1
fi
rm -f /tmp/homelab-lxc-install.sh

cleanup_lxc