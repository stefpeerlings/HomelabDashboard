#!/usr/bin/env bash
# Koppel Proxmox-node aan statusbalk en maak auto-panelen aan.

set -euo pipefail

APP_DIR="${HOMELAB_APP_ROOT:-/opt/homelab-dashboard}"
PROXMOX_HOST="${HOMELAB_PROXMOX_HOST:-}"
NODE_NAME="${HOMELAB_NODE_NAME:-$PROXMOX_HOST}"
PBS_HOST="${HOMELAB_PBS_HOST:-}"

[[ -n "$PROXMOX_HOST" ]] || exit 0
[[ -d "$APP_DIR" ]] || { echo "App niet gevonden: $APP_DIR" >&2; exit 1; }

cd "$APP_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

export HOMELAB_APP_ROOT="$APP_DIR"
python3 - "$PROXMOX_HOST" "$NODE_NAME" "$PBS_HOST" <<'PY'
import os
import sys

sys.path.insert(0, os.environ.get("HOMELAB_APP_ROOT", "/opt/homelab-dashboard"))
from homelab_dashboard import load_config, update_status_settings

proxmox_host, node_name, pbs_host = sys.argv[1:4]
config = load_config()
update_status_settings(
    config,
    {
        "label": "Status",
        "node_name": node_name,
        "proxmox_host": proxmox_host,
        "pbs_host": pbs_host,
        "interval_seconds": 5,
    },
)
config = load_config()
auto_panels = [p for p in config.get("panels", []) if p.get("auto")]
print(f"Proxmox-node gekoppeld: {node_name} ({proxmox_host})")
print(f"Auto-panelen: {len(auto_panels)}")
if not auto_panels:
    raise SystemExit("Geen auto-panelen aangemaakt — controleer SSH naar Proxmox")
PY