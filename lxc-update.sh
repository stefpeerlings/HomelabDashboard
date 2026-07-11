#!/usr/bin/env bash
#
# Homelab Dashboard - update (in bestaande LXC/container)
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"
#
# Vanaf Proxmox host:
#   pct exec <CTID> -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-update.sh)"

set -euo pipefail

REPO_RAW="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}"
INSTALL_SCRIPT="$(mktemp /tmp/homelab-lxc-update.XXXXXX.sh)"
trap 'rm -f "$INSTALL_SCRIPT"' EXIT

curl -fsSL "${REPO_RAW}/lxc-install.sh" -o "$INSTALL_SCRIPT"
chmod +x "$INSTALL_SCRIPT"
exec bash "$INSTALL_SCRIPT" --update