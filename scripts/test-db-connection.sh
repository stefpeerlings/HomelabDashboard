#!/usr/bin/env bash
# Test MariaDB-verbinding vanaf de dashboard-container.

set -euo pipefail

CRED_DIR="${HOMELAB_CREDENTIALS_DIR:-/root/.homelab-db/credentials}"
SERVICE_JSON="${CRED_DIR}/service.json"
APP_DIR="${HOMELAB_APP_ROOT:-/opt/homelab-dashboard}"

if [[ ! -f "$SERVICE_JSON" ]]; then
  echo "Ontbreekt: $SERVICE_JSON"
  echo "Voer eerst uit: bash $APP_DIR/scripts/setup-credentials.sh"
  exit 1
fi

PYTHON=""
if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
  PYTHON="$APP_DIR/.venv/bin/python"
elif command -v python3 &>/dev/null; then
  PYTHON="python3"
else
  echo "python3 niet gevonden"
  exit 1
fi

"$PYTHON" <<'PY'
import json
import sys
from pathlib import Path

import pymysql

path = Path("/root/.homelab-db/credentials/service.json")
if not path.exists():
    import os
    path = Path(os.environ.get("HOMELAB_CREDENTIALS_DIR", "/root/.homelab-db/credentials")) / "service.json"

cfg = json.loads(path.read_text(encoding="utf-8"))
if cfg.get("password") in (None, "", "VUL_AAN"):
    print("service.json: wachtwoord nog niet ingevuld (VUL_AAN)")
    sys.exit(1)

try:
    conn = pymysql.connect(
        host=cfg["host"],
        port=int(cfg.get("port", 3306)),
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        connect_timeout=8,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
    conn.close()
    print(f"OK — verbonden met {cfg['host']}:{cfg.get('port', 3306)}/{cfg['database']}")
    print(f"MariaDB: {version}")
except Exception as exc:
    print(f"Fout: {exc}")
    sys.exit(1)
PY