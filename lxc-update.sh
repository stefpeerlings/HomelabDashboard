#!/usr/bin/env bash
#
# Doorverwijzing naar het community-stijl CT script (zelfde link voor update).
#
# Gebruik bij voorkeur direct:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/ct/homelab-dashboard.sh)"

SCRIPT_URL="${HOMELAB_REPO_RAW:-https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main}/ct/homelab-dashboard.sh"
exec bash <(curl -fsSL "$SCRIPT_URL")