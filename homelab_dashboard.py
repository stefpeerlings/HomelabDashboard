#!/usr/bin/env python3
"""Homelab dashboard: live logs + browser SSH terminals."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import pty
import re
import secrets
import select
import shlex
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import paramiko
import websockets

def _resolve_app_root() -> Path:
    env_root = os.environ.get("HOMELAB_APP_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    here = Path(__file__).resolve().parent
    if (here / "static").is_dir():
        return here
    return Path("/opt/homelab-dashboard")


def _resolve_static_dir(app_root: Path) -> Path:
    env_static = os.environ.get("HOMELAB_STATIC_DIR", "").strip()
    if env_static:
        return Path(env_static)
    bundled = app_root / "static"
    if bundled.is_dir():
        return bundled
    legacy = Path("/usr/local/share/pbs-monitor")
    return legacy if legacy.is_dir() else bundled


APP_ROOT = _resolve_app_root()
CREDENTIALS_DIR = Path(os.environ.get("HOMELAB_CREDENTIALS_DIR", "/root/.homelab-db/credentials"))
CONFIG_PATH = Path(os.environ.get("HOMELAB_CONFIG_PATH", "/etc/homelab-dashboard/config.json"))
DB_PATH = Path(os.environ.get("HOMELAB_DB_PATH", "/etc/homelab-dashboard/dashboard.db"))
DB_CONFIG_PATH = CREDENTIALS_DIR / "service.json"
DASHBOARD_AUTH_PATH = CREDENTIALS_DIR / "dashboard-auth.json"
DASHBOARD_LOGIN_PATH = CREDENTIALS_DIR / "dashboard-login.json"
SMTP_CONFIG_PATH = CREDENTIALS_DIR / "smtp.json"
STATIC_DIR = _resolve_static_dir(APP_ROOT)
PUBLIC_DASHBOARD_URL = os.environ.get("HOMELAB_PUBLIC_URL", "").strip()
SESSION_COOKIE = "homelab_session"


def resolve_public_dashboard_url() -> str:
    """Bepaal dashboard-URL; standaard automatisch via container-IP (DHCP-vriendelijk)."""
    auto = os.environ.get("HOMELAB_AUTO_PUBLIC_URL", "1").strip().lower() not in ("0", "false", "no")
    env_url = os.environ.get("HOMELAB_PUBLIC_URL", "").strip()
    if env_url and not auto:
        return env_url if env_url.endswith("/") else f"{env_url}/"
    try:
        ip = subprocess.check_output(["hostname", "-I"], text=True, timeout=3).strip().split()[0]
        if ip:
            port = os.environ.get("HOMELAB_HTTP_PORT", "8765")
            return f"http://{ip}:{port}/"
    except Exception:
        pass
    if env_url:
        return env_url if env_url.endswith("/") else f"{env_url}/"
    return "http://127.0.0.1:8765/"


STATUS_HOST_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def status_host_valid(host: str) -> bool:
    host = (host or "").strip()
    return bool(host) and bool(STATUS_HOST_RE.fullmatch(host))


def proxmox_ssh_shell_prefix(host: str) -> str | None:
    """Shell-prefix voor remote Proxmox-commando's; '' = lokaal; None = niet beschikbaar."""
    host = (host or "").strip()
    if not status_host_valid(host):
        return None
    try:
        conn = resolve_ssh_target(host)
    except (ValueError, OSError):
        return None
    if conn.get("local"):
        return ""
    prefix_parts = proxmox_ssh_command(conn, "true")[:-1]
    return " ".join(shlex.quote(part) for part in prefix_parts)


def _status_remote_exec(prefix: str | None, script: str) -> str | None:
    if prefix is None:
        return None
    script = script.strip()
    if not script:
        return None
    if prefix == "":
        return script
    return f"{prefix} {shlex.quote(script)}"


def _status_http_check(label: str, host: str, port: int) -> str:
    host_q = shlex.quote(host)
    return (
        f"echo -n '{label} ({host}): '"
        f"; if curl -sk --connect-timeout 4 https://{host_q}:{port}/ >/dev/null 2>&1; "
        f"then echo online; else echo offline; fi"
    )


def build_status_command(settings: dict | None = None) -> list:
    settings = settings or {}
    proxmox_host = (settings.get("status_proxmox_host") or "").strip()
    pbs_host = (settings.get("status_pbs_host") or "").strip()
    px_prefix = proxmox_ssh_shell_prefix(proxmox_host) if proxmox_host else None

    parts = [
        "IP=$(hostname -I 2>/dev/null | awk '{print $1}')",
        'echo "Dashboard: http://${IP:-onbekend}:8765"',
        "systemctl is-active --quiet homelab-dashboard && echo 'Service: active' || echo 'Service: inactive'",
        "systemctl is-active --quiet mariadb 2>/dev/null && echo 'MariaDB: active' || true",
        "free -m 2>/dev/null | awk '/^Mem:/{printf \"RAM: %dM / %dM\\n\", $3,$2}'",
        "df -h / 2>/dev/null | awk 'NR==2{printf \"Disk /: %s (%s van %s)\\n\", $5,$3,$2}'",
    ]

    if status_host_valid(proxmox_host):
        parts.append(_status_http_check("Proxmox", proxmox_host, 8006))

    if status_host_valid(pbs_host):
        parts.append(_status_http_check("PBS", pbs_host, 8007))

    vm_cmd = (
        "qm list 2>/dev/null | awk 'NR>1 && $1~/^[0-9]+$/"
        "{t++; if(tolower($3)==\"running\") r++} END{printf \"VM: %d/%d running\\n\", r+0, t+0}'"
    )
    lxc_cmd = (
        "pct list 2>/dev/null | awk 'NR>1 && $1~/^[0-9]+$/"
        "{t++; if(tolower($2)==\"running\") r++} END{printf \"LXC: %d/%d running\\n\", r+0, t+0}'"
    )
    backup_cmd = (
        "if pgrep -f '[v]zdump' >/dev/null 2>&1; then echo 'Backup: vzdump bezig'; "
        "else LAST=$(journalctl --no-pager -n 400 -o cat 2>/dev/null | "
        "grep -iE 'vzdump|backup job' | grep -iE 'finished|failed|error|TASK OK' | tail -1); "
        "if echo \"$LAST\" | grep -qiE 'fail|error'; then echo 'Backup: MISLUKT'; "
        "elif [ -n \"$LAST\" ]; then TS=$(echo \"$LAST\" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:]{4,8}' | tail -1); "
        "echo \"Backup: OK${TS:+ ($TS)}\"; else echo 'Backup: geen recente vzdump'; fi; fi"
    )
    shutdown_cmd = (
        "if systemctl list-timers pbs-shutdown.timer --no-pager 2>/dev/null | grep -q pbs-shutdown; then "
        "LEFT=$(systemctl list-timers pbs-shutdown.timer --no-pager --no-legend 2>/dev/null | awk 'NR==1{print $1,$2,$3}'); "
        "echo \"PBS shutdown: gepland (${LEFT:-onbekend})\"; "
        "else echo 'PBS shutdown: shutdown mag'; fi"
    )

    for script in (vm_cmd, lxc_cmd, backup_cmd, shutdown_cmd):
        remote = _status_remote_exec(px_prefix, script)
        if remote:
            parts.append(remote)

    parts.append("uptime -p 2>/dev/null || uptime")
    return ["bash", "-lc", "\n".join(parts)]
SESSION_DAYS = 14
RESET_TOKEN_HOURS = 2
_db_lock = threading.Lock()
_db_ready = False

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError as exc:
    raise SystemExit("python3-pymysql is vereist: apt install python3-pymysql") from exc
PBS_DEFAULT_KEY = "/root/.ssh/pbs_key"
PBS_REMOTE_HOSTS = {"10.0.30.5", "pbs"}
PROXMOX_DEFAULT_KEY = "/root/.ssh/id_ed25519_default"
PROXMOX_FALLBACK_KEYS = (
    "/root/.ssh/id_ed25519_default",
    "/root/.ssh/id_ed25519",
    "/root/.ssh/id_rsa",
)
LOCAL_PROXMOX_HOSTS = {"localhost", "127.0.0.1", "minilab", "10.0.30.3"}
DEFAULT_CONFIG = {
    "title": "Homelab Dashboard",
    "host": "0.0.0.0",
    "port": 8765,
    "ws_port": 8766,
    "status": {
        "label": "Status",
        "command": build_status_command(),
        "interval_seconds": 5,
    },
    "panels": [],
    "ssh_hosts": [],
}

DEFAULT_LOG_CATEGORIES = {
    "proxmox": {"title": "Proxmox", "sub": "Node & VZDump"},
    "backup": {"title": "Backup", "sub": "PBS & timers"},
    "container": {"title": "Containers", "sub": "LXC via pct exec"},
    "docker": {"title": "Docker", "sub": "Container logs"},
}

AUTO_SYNC_CATEGORIES = {"proxmox", "container", "docker"}


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS settings (
        setting_key VARCHAR(128) PRIMARY KEY,
        setting_value LONGTEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS panels (
        id VARCHAR(64) PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        description TEXT NOT NULL,
        category VARCHAR(32) NOT NULL DEFAULT 'proxmox',
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        height INT NOT NULL DEFAULT 220,
        command LONGTEXT NOT NULL,
        auto_flag TINYINT(1) NOT NULL DEFAULT 0,
        ctid VARCHAR(16) NULL,
        sort_order INT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS ssh_hosts (
        id VARCHAR(64) PRIMARY KEY,
        title VARCHAR(255) NOT NULL,
        description TEXT NOT NULL,
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        conn_type VARCHAR(16) NOT NULL DEFAULT 'ssh',
        host VARCHAR(255) NULL,
        port INT NULL,
        ssh_user VARCHAR(64) NULL,
        auth VARCHAR(16) NULL,
        key_file TEXT NULL,
        password TEXT NULL,
        command LONGTEXT NULL,
        sort_order INT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboard_users (
        username VARCHAR(64) PRIMARY KEY,
        password_hash TEXT NOT NULL,
        role VARCHAR(32) NOT NULL DEFAULT 'viewer',
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        email VARCHAR(255) NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboard_reset_tokens (
        token_hash CHAR(64) PRIMARY KEY,
        username VARCHAR(64) NOT NULL,
        expires_at DATETIME NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_reset_username (username),
        INDEX idx_reset_expires (expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]

DASHBOARD_ROLES = ("admin", "operator", "viewer")
ROLE_LABELS = {
    "admin": "Beheerder",
    "operator": "Operator",
    "viewer": "Alleen lezen",
}
ROLE_LEVEL = {"viewer": 1, "operator": 2, "admin": 3}


def load_db_config() -> dict:
    if not DB_CONFIG_PATH.exists():
        raise RuntimeError(
            f"Database config ontbreekt: {DB_CONFIG_PATH}. "
            "Maak service.json aan met host/user/password/database."
        )
    with DB_CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    required = ("host", "user", "password", "database")
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise RuntimeError(f"service.json mist velden: {', '.join(missing)}")
    cfg.setdefault("port", 3306)
    return cfg


def _save_secret_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def load_dashboard_auth() -> dict | None:
    if not DASHBOARD_AUTH_PATH.exists():
        return None
    with DASHBOARD_AUTH_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("session_secret"):
        return {"session_secret": data["session_secret"]}
    return data


def role_permissions(role: str) -> dict:
    level = ROLE_LEVEL.get(role, 0)
    return {
        "view_logs": level >= 1,
        "use_ssh": level >= 2,
        "manage_panels": level >= 2,
        "manage_ssh": level >= 2,
        "manage_users": level >= 3,
    }


def normalize_username(value: str) -> str:
    username = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", username):
        raise ValueError("Gebruikersnaam: 3-32 tekens, begin met letter (a-z, 0-9, _, -)")
    return username


def normalize_role(value: str) -> str:
    role = (value or "").strip().lower()
    if role not in DASHBOARD_ROLES:
        raise ValueError(f"Rol moet een van {', '.join(DASHBOARD_ROLES)} zijn")
    return role


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    email = (value or "").strip().lower()
    if not email:
        return None
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", email):
        raise ValueError("Ongeldig e-mailadres")
    return email


def ensure_smtp_template() -> None:
    if SMTP_CONFIG_PATH.exists():
        return
    _save_secret_json(
        SMTP_CONFIG_PATH,
        {
            "enabled": False,
            "host": "smtp.voorbeeld.nl",
            "port": 587,
            "user": "dashboard@home-labe.com",
            "password": "VUL_AAN",
            "from": "Homelab Dashboard <dashboard@home-labe.com>",
            "use_tls": True,
            "dashboard_url": resolve_public_dashboard_url(),
            "note": "Zet enabled op true en vul SMTP-gegevens in voor wachtwoord-reset mails",
        },
    )


def load_smtp_config() -> dict | None:
    ensure_smtp_template()
    if not SMTP_CONFIG_PATH.exists():
        return None
    cfg = json.loads(SMTP_CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("enabled"):
        return None
    required = ("host", "port", "from", "dashboard_url")
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise RuntimeError(f"SMTP config mist: {', '.join(missing)}")
    return cfg


def send_email(to_addr: str, subject: str, body: str, html: str | None = None) -> None:
    cfg = load_smtp_config()
    if not cfg:
        raise RuntimeError("E-mail is niet geconfigureerd (smtp.json)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = to_addr
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=20) as smtp:
        if cfg.get("use_tls", True):
            smtp.starttls()
        user = cfg.get("user")
        password = cfg.get("password")
        if user and password and password != "VUL_AAN":
            smtp.login(user, password)
        smtp.send_message(msg)


def ensure_dashboard_schema_updates(conn) -> None:
    row = _fetchone(conn, "SHOW COLUMNS FROM dashboard_users LIKE 'email'")
    if not row:
        _execute(conn, "ALTER TABLE dashboard_users ADD COLUMN email VARCHAR(255) NULL")
    row = _fetchone(conn, "SHOW COLUMNS FROM dashboard_users LIKE 'session_version'")
    if not row:
        _execute(
            conn,
            "ALTER TABLE dashboard_users ADD COLUMN session_version INT NOT NULL DEFAULT 1",
        )
    row = _fetchone(conn, "SELECT setting_value FROM settings WHERE setting_key='status_command'")
    if row:
        status_cmd = row.get("setting_value") or ""
        if "pbs-manage.sh" in status_cmd or "pbs-monitor" in status_cmd:
            _execute(
                conn,
                "INSERT INTO settings (setting_key, setting_value) VALUES ('status_proxmox_host', '10.0.30.3') "
                "ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)",
            )
            rebuilt = build_status_command({"status_proxmox_host": "10.0.30.3"})
            _execute(
                conn,
                "UPDATE settings SET setting_value=%s WHERE setting_key='status_command'",
                (json.dumps(rebuilt),),
            )
    if not _fetchone(conn, "SELECT setting_value FROM settings WHERE setting_key='status_proxmox_host'"):
        _execute(
            conn,
            "INSERT INTO settings (setting_key, setting_value) VALUES ('status_proxmox_host', '10.0.30.3') "
            "ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)",
        )
    if not _fetchone(conn, "SELECT setting_value FROM settings WHERE setting_key='status_pbs_host'"):
        _execute(
            conn,
            "INSERT INTO settings (setting_key, setting_value) VALUES ('status_pbs_host', '') "
            "ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)",
        )


def cleanup_reset_tokens(conn) -> None:
    _execute(conn, "DELETE FROM dashboard_reset_tokens WHERE expires_at < NOW()")


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_password_reset(email: str) -> bool:
    email = normalize_email(email)
    if not email:
        raise ValueError("Vul een geldig e-mailadres in")
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            cleanup_reset_tokens(conn)
            row = _fetchone(
                conn,
                "SELECT username, email, enabled FROM dashboard_users WHERE email=%s",
                (email,),
            )
            if not row:
                conn.commit()
                raise ValueError("Dit e-mailadres is niet gekoppeld aan een account")
            if not row.get("enabled"):
                conn.commit()
                raise ValueError("Dit account is uitgeschakeld — neem contact op met een beheerder")
            token = secrets.token_urlsafe(32)
            token_hash = _hash_reset_token(token)
            expires = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(time.time() + RESET_TOKEN_HOURS * 3600),
            )
            _execute(conn, "DELETE FROM dashboard_reset_tokens WHERE username=%s", (row["username"],))
            _execute(
                conn,
                "INSERT INTO dashboard_reset_tokens (token_hash, username, expires_at) VALUES (%s, %s, %s)",
                (token_hash, row["username"], expires),
            )
            conn.commit()
        finally:
            conn.close()
    cfg = load_smtp_config()
    if not cfg:
        raise RuntimeError("E-mail is niet geconfigureerd")
    base = cfg["dashboard_url"].rstrip("/")
    reset_url = f"{base}/reset?token={token}"
    body = (
        f"Hallo {row['username']},\n\n"
        f"Je hebt een nieuw wachtwoord aangevraagd voor het Homelab Dashboard.\n\n"
        f"Open deze link (geldig {RESET_TOKEN_HOURS} uur):\n{reset_url}\n\n"
        f"Heb je dit niet aangevraagd? Negeer deze mail.\n"
    )
    html = (
        f"<p>Hallo <b>{row['username']}</b>,</p>"
        f"<p>Je hebt een nieuw wachtwoord aangevraagd voor het Homelab Dashboard.</p>"
        f'<p><a href="{reset_url}" style="display:inline-block;padding:12px 20px;background:#3b82f6;'
        f'color:#fff;text-decoration:none;border-radius:8px;font-weight:600">'
        f"Nieuw wachtwoord instellen</a></p>"
        f"<p style='color:#666;font-size:13px'>Link geldig {RESET_TOKEN_HOURS} uur.<br>"
        f'<a href="{reset_url}">{reset_url}</a></p>'
        f"<p>Heb je dit niet aangevraagd? Negeer deze mail.</p>"
    )
    send_email(email, "Homelab Dashboard — wachtwoord resetten", body, html=html)
    return True


def lookup_reset_token(token: str) -> dict | None:
    token = (token or "").strip()
    if not token:
        return None
    token_hash = _hash_reset_token(token)
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            cleanup_reset_tokens(conn)
            conn.commit()
            return _fetchone(
                conn,
                "SELECT username, expires_at FROM dashboard_reset_tokens WHERE token_hash=%s",
                (token_hash,),
            )
        finally:
            conn.close()


def reset_password_with_token(token: str, new_password: str) -> str:
    token = (token or "").strip()
    if not token:
        raise ValueError("Reset-link is ongeldig")
    if len(new_password) < 8:
        raise ValueError("Nieuw wachtwoord moet minstens 8 tekens zijn")
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            cleanup_reset_tokens(conn)
            row = _fetchone(
                conn,
                "SELECT username, expires_at FROM dashboard_reset_tokens WHERE token_hash=%s",
                (_hash_reset_token(token),),
            )
            if not row:
                raise ValueError("Reset-link is ongeldig of verlopen")
            _execute(
                conn,
                "DELETE FROM dashboard_reset_tokens WHERE token_hash=%s",
                (_hash_reset_token(token),),
            )
            conn.commit()
            username = row["username"]
        finally:
            conn.close()
    update_dashboard_user(username, password=new_password)
    return username


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000)
    return f"pbkdf2_sha256$260000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, hash_hex = stored.split("$", 3)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def ensure_session_secret() -> dict:
    auth = load_dashboard_auth()
    if auth and auth.get("session_secret"):
        return auth
    auth = {"session_secret": secrets.token_hex(32)}
    _save_secret_json(DASHBOARD_AUTH_PATH, auth)
    return auth


def dashboard_user_row(row) -> dict:
    return {
        "username": row["username"],
        "role": row["role"],
        "role_label": ROLE_LABELS.get(row["role"], row["role"]),
        "enabled": bool(row["enabled"]),
        "email": row.get("email"),
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
    }


def get_dashboard_user(username: str) -> dict | None:
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            row = _fetchone(
                conn,
                "SELECT username, password_hash, role, enabled, email, created_at, session_version "
                "FROM dashboard_users WHERE username=%s",
                (username,),
            )
            return row
        finally:
            conn.close()


def list_dashboard_users() -> list[dict]:
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            rows = _fetchall(
                conn,
                "SELECT username, role, enabled, email, created_at FROM dashboard_users ORDER BY username ASC",
            )
            return [dashboard_user_row(row) for row in rows]
        finally:
            conn.close()


def auth_enabled() -> bool:
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            row = _fetchone(conn, "SELECT COUNT(*) AS c FROM dashboard_users WHERE enabled=1")
            return bool(row and row["c"])
        finally:
            conn.close()


def user_has_role(username: str, min_role: str) -> bool:
    user = get_dashboard_user(username)
    if not user or not user.get("enabled"):
        return False
    return ROLE_LEVEL.get(user["role"], 0) >= ROLE_LEVEL.get(min_role, 0)


def verify_dashboard_login(username: str, password: str) -> dict | None:
    user = get_dashboard_user(username)
    if not user or not user.get("enabled"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def create_dashboard_user(
    username: str,
    password: str,
    role: str,
    email: str | None = None,
) -> dict:
    username = normalize_username(username)
    role = normalize_role(role)
    email = normalize_email(email)
    if len(password) < 8:
        raise ValueError("Wachtwoord moet minstens 8 tekens zijn")
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            if _fetchone(conn, "SELECT username FROM dashboard_users WHERE username=%s", (username,)):
                raise ValueError(f"Gebruiker '{username}' bestaat al")
            if email and _fetchone(conn, "SELECT username FROM dashboard_users WHERE email=%s", (email,)):
                raise ValueError(f"E-mail '{email}' is al in gebruik")
            _execute(
                conn,
                "INSERT INTO dashboard_users (username, password_hash, role, enabled, email) "
                "VALUES (%s, %s, %s, 1, %s)",
                (username, hash_password(password), role, email),
            )
            conn.commit()
            row = _fetchone(
                conn,
                "SELECT username, role, enabled, email, created_at FROM dashboard_users WHERE username=%s",
                (username,),
            )
            return dashboard_user_row(row)
        finally:
            conn.close()


def update_dashboard_user(
    username: str,
    *,
    role: str | None = None,
    password: str | None = None,
    enabled: bool | None = None,
    email: str | None = None,
    clear_email: bool = False,
) -> dict:
    username = normalize_username(username)
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            row = _fetchone(conn, "SELECT username FROM dashboard_users WHERE username=%s", (username,))
            if not row:
                raise ValueError(f"Gebruiker '{username}' niet gevonden")
            updates = []
            params = []
            if role is not None:
                updates.append("role=%s")
                params.append(normalize_role(role))
            if password is not None:
                if len(password) < 8:
                    raise ValueError("Wachtwoord moet minstens 8 tekens zijn")
                updates.append("password_hash=%s")
                params.append(hash_password(password))
            if enabled is not None:
                if not enabled:
                    target = _fetchone(
                        conn,
                        "SELECT role, enabled FROM dashboard_users WHERE username=%s",
                        (username,),
                    )
                    if target and target["role"] == "admin" and target["enabled"]:
                        admins = _fetchone(
                            conn,
                            "SELECT COUNT(*) AS c FROM dashboard_users WHERE role='admin' AND enabled=1",
                        )
                        if admins and admins["c"] <= 1:
                            raise ValueError("Kan de laatste actieve admin niet uitschakelen")
                updates.append("enabled=%s")
                params.append(1 if enabled else 0)
            if clear_email:
                updates.append("email=NULL")
            elif email is not None:
                email = normalize_email(email)
                if email and _fetchone(
                    conn,
                    "SELECT username FROM dashboard_users WHERE email=%s AND username<>%s",
                    (email, username),
                ):
                    raise ValueError(f"E-mail '{email}' is al in gebruik")
                updates.append("email=%s")
                params.append(email)
            if not updates:
                raise ValueError("Geen wijzigingen opgegeven")
            params.append(username)
            _execute(conn, f"UPDATE dashboard_users SET {', '.join(updates)} WHERE username=%s", tuple(params))
            conn.commit()
            row = _fetchone(
                conn,
                "SELECT username, role, enabled, email, created_at FROM dashboard_users WHERE username=%s",
                (username,),
            )
            return dashboard_user_row(row)
        finally:
            conn.close()


def delete_dashboard_user(username: str, *, actor: str) -> None:
    username = normalize_username(username)
    if username == actor:
        raise ValueError("Je kunt je eigen account niet verwijderen")
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            admins = _fetchone(
                conn,
                "SELECT COUNT(*) AS c FROM dashboard_users WHERE role='admin' AND enabled=1",
            )
            target = _fetchone(
                conn,
                "SELECT role, enabled FROM dashboard_users WHERE username=%s",
                (username,),
            )
            if not target:
                raise ValueError(f"Gebruiker '{username}' niet gevonden")
            if target["role"] == "admin" and target["enabled"] and admins and admins["c"] <= 1:
                raise ValueError("Kan de laatste actieve admin niet verwijderen")
            _execute(conn, "DELETE FROM dashboard_users WHERE username=%s", (username,))
            conn.commit()
        finally:
            conn.close()


def change_dashboard_password(username: str, current_password: str, new_password: str) -> None:
    user = verify_dashboard_login(username, current_password)
    if not user:
        raise ValueError("Huidig wachtwoord is onjuist")
    if len(new_password) < 8:
        raise ValueError("Nieuw wachtwoord moet minstens 8 tekens zijn")
    update_dashboard_user(username, password=new_password)


def ensure_dashboard_users() -> None:
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            count_row = _fetchone(conn, "SELECT COUNT(*) AS c FROM dashboard_users")
            if count_row and count_row["c"]:
                return

            legacy_auth = None
            if DASHBOARD_AUTH_PATH.exists():
                legacy_auth = json.loads(DASHBOARD_AUTH_PATH.read_text(encoding="utf-8"))

            if legacy_auth and legacy_auth.get("username") and legacy_auth.get("password_hash"):
                username = normalize_username(legacy_auth["username"])
                password_hash = legacy_auth["password_hash"]
            else:
                username = "admin"
                password_hash = hash_password("homelab123")

            _execute(
                conn,
                "INSERT INTO dashboard_users (username, password_hash, role, enabled) VALUES (%s, %s, 'admin', 1)",
                (username, password_hash),
            )
            conn.commit()

            password_hint = "homelab123" if username == "admin" and not (
                legacy_auth and legacy_auth.get("password_hash")
            ) else "(bestaand wachtwoord)"
            if legacy_auth and legacy_auth.get("password_hash") and DASHBOARD_LOGIN_PATH.exists():
                try:
                    login_info = json.loads(DASHBOARD_LOGIN_PATH.read_text(encoding="utf-8"))
                    password_hint = login_info.get("password", password_hint)
                except json.JSONDecodeError:
                    pass

            _save_secret_json(
                DASHBOARD_LOGIN_PATH,
                {
                    "user": username,
                    "password": password_hint if isinstance(password_hint, str) else "homelab123",
                    "url": resolve_public_dashboard_url(),
                    "note": "Dashboard login — beheer gebruikers via Account menu",
                },
            )
            secret = (legacy_auth or {}).get("session_secret") or ensure_session_secret()["session_secret"]
            _save_secret_json(DASHBOARD_AUTH_PATH, {"session_secret": secret})
            print(f"Dashboard gebruiker '{username}' aangemaakt (rol: admin)")
        finally:
            conn.close()


def ensure_dashboard_auth() -> dict:
    auth = ensure_session_secret()
    ensure_smtp_template()
    ensure_dashboard_users()
    return auth


def _session_secret() -> bytes:
    auth = load_dashboard_auth()
    if not auth or not auth.get("session_secret"):
        raise RuntimeError("Dashboard auth ontbreekt")
    return auth["session_secret"].encode("utf-8")


def create_session(username: str) -> str:
    user = get_dashboard_user(username)
    version = int((user or {}).get("session_version") or 1)
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{username}:{exp}:{version}"
    sig = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    token = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")


def _parse_session_token(token: str) -> tuple[str, int, int] | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) < 3:
            return None
        sig = parts[-1]
        if len(parts) == 3:
            username, exp = parts[0], parts[1]
            version = 1
            payload = f"{username}:{exp}"
        else:
            username, exp, version = parts[0], parts[1], parts[2]
            payload = f"{username}:{exp}:{version}"
        expected = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(exp) < time.time():
            return None
        return username, int(exp), int(version)
    except Exception:
        return None


def verify_session(token: str) -> str | None:
    parsed = _parse_session_token(token)
    if not parsed:
        return None
    username, _, version = parsed
    user = get_dashboard_user(username)
    if not user or not user.get("enabled"):
        return None
    if version != int(user.get("session_version") or 1):
        return None
    return username


def kick_dashboard_user(username: str) -> dict:
    username = normalize_username(username)
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            row = _fetchone(conn, "SELECT username FROM dashboard_users WHERE username=%s", (username,))
            if not row:
                raise ValueError(f"Gebruiker '{username}' niet gevonden")
            _execute(
                conn,
                "UPDATE dashboard_users SET session_version=session_version+1 WHERE username=%s",
                (username,),
            )
            conn.commit()
            row = _fetchone(
                conn,
                "SELECT username, role, enabled, email, created_at FROM dashboard_users WHERE username=%s",
                (username,),
            )
            return dashboard_user_row(row)
        finally:
            conn.close()


def password_reset_enabled() -> bool:
    try:
        return load_smtp_config() is not None
    except Exception:
        return False


def auth_status_payload(handler) -> dict:
    reset_enabled = password_reset_enabled()
    if not auth_enabled():
        return {
            "auth_enabled": False,
            "logged_in": True,
            "username": None,
            "role": None,
            "role_label": None,
            "permissions": role_permissions("admin"),
            "password_reset_enabled": reset_enabled,
        }
    cookie = handler.headers.get("Cookie", "")
    token = ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(f"{SESSION_COOKIE}="):
            token = part.split("=", 1)[1]
            break
    username = verify_session(token)
    role = None
    role_label = None
    permissions = role_permissions("viewer")
    if username:
        user = get_dashboard_user(username)
        if user and user.get("enabled"):
            role = user["role"]
            role_label = ROLE_LABELS.get(role, role)
            permissions = role_permissions(role)
        else:
            username = None
    return {
        "auth_enabled": True,
        "logged_in": bool(username),
        "username": username,
        "role": role,
        "role_label": role_label,
        "permissions": permissions,
        "roles": [{"id": r, "label": ROLE_LABELS[r]} for r in DASHBOARD_ROLES],
        "password_reset_enabled": reset_enabled,
    }


def get_db_connection():
    cfg = load_db_config()
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg["port"]),
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=15,
        read_timeout=30,
        write_timeout=30,
    )


def _fetchone(conn, sql: str, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _fetchall(conn, sql: str, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _execute(conn, sql: str, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _executemany(conn, sql: str, params):
    with conn.cursor() as cur:
        cur.executemany(sql, params)


def _row_panel(row: dict) -> dict:
    panel = {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"] or "",
        "category": row["category"] or "proxmox",
        "enabled": bool(row["enabled"]),
        "height": int(row["height"] or 220),
        "command": json.loads(row["command"]),
    }
    if row.get("auto_flag"):
        panel["auto"] = True
    if row.get("ctid"):
        panel["ctid"] = row["ctid"]
    return panel


def _row_ssh_host(row: dict) -> dict:
    host = {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"] or "",
        "enabled": bool(row["enabled"]),
        "type": row.get("conn_type") or row.get("type") or "ssh",
    }
    if row.get("host"):
        host["host"] = row["host"]
    if row.get("port") is not None:
        host["port"] = int(row["port"])
    ssh_user = row.get("ssh_user") or row.get("user")
    if ssh_user:
        host["user"] = ssh_user
    if row.get("auth"):
        host["auth"] = row["auth"]
    if row.get("key_file"):
        host["key_file"] = row["key_file"]
    if row.get("password"):
        host["password"] = row["password"]
    if row.get("command"):
        host["command"] = json.loads(row["command"])
    return host


BUILTIN_CATEGORY_ORDER = ("proxmox", "backup", "container", "docker")


def normalize_log_categories(raw: dict | None) -> dict:
    merged = {}
    for cat_id, defaults in DEFAULT_LOG_CATEGORIES.items():
        merged[cat_id] = {
            "title": defaults["title"],
            "sub": defaults["sub"],
            "builtin": True,
        }
    if not isinstance(raw, dict):
        return merged
    for cat_id in BUILTIN_CATEGORY_ORDER:
        entry = raw.get(cat_id)
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "")).strip()
        sub = str(entry.get("sub", "")).strip()
        if title:
            merged[cat_id]["title"] = title
        if sub:
            merged[cat_id]["sub"] = sub
    for cat_id, entry in raw.items():
        if cat_id in DEFAULT_LOG_CATEGORIES or not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "")).strip()
        sub = str(entry.get("sub", "")).strip()
        if not title:
            continue
        merged[cat_id] = {"title": title, "sub": sub, "custom": True}
    return merged


def category_order(categories: dict | None) -> list[str]:
    cats = normalize_log_categories(categories)
    ordered = [cat_id for cat_id in BUILTIN_CATEGORY_ORDER if cat_id in cats]
    custom = sorted(
        cat_id for cat_id in cats if cat_id not in DEFAULT_LOG_CATEGORIES
    )
    return ordered + custom


def _seed_default_settings(conn) -> None:
    defaults = {
        "title": DEFAULT_CONFIG["title"],
        "host": DEFAULT_CONFIG["host"],
        "port": str(DEFAULT_CONFIG["port"]),
        "ws_port": str(DEFAULT_CONFIG["ws_port"]),
        "status_label": DEFAULT_CONFIG["status"]["label"],
        "status_proxmox_host": "",
        "status_pbs_host": "",
        "status_command": json.dumps(build_status_command()),
        "status_interval_seconds": str(DEFAULT_CONFIG["status"]["interval_seconds"]),
        "log_categories": json.dumps(DEFAULT_LOG_CATEGORIES),
    }
    _executemany(
        conn,
        """
        INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """,
        list(defaults.items()),
    )


def write_config_to_db(conn, config: dict) -> None:
    settings = {
        "title": config.get("title", DEFAULT_CONFIG["title"]),
        "host": config.get("host", DEFAULT_CONFIG["host"]),
        "port": str(config.get("port", DEFAULT_CONFIG["port"])),
        "ws_port": str(config.get("ws_port", DEFAULT_CONFIG["ws_port"])),
        "status_label": config.get("status", {}).get("label", DEFAULT_CONFIG["status"]["label"]),
        "status_proxmox_host": config.get("status", {}).get("proxmox_host", ""),
        "status_pbs_host": config.get("status", {}).get("pbs_host", ""),
        "status_command": json.dumps(
            build_status_command(
                {
                    "status_proxmox_host": config.get("status", {}).get("proxmox_host", ""),
                    "status_pbs_host": config.get("status", {}).get("pbs_host", ""),
                }
            )
        ),
        "status_interval_seconds": str(
            config.get("status", {}).get("interval_seconds", DEFAULT_CONFIG["status"]["interval_seconds"])
        ),
        "log_categories": json.dumps(normalize_log_categories(config.get("categories"))),
    }
    _executemany(
        conn,
        """
        INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)
        """,
        list(settings.items()),
    )

    _execute(conn, "DELETE FROM panels")
    for idx, panel in enumerate(config.get("panels", [])):
        if not panel.get("id"):
            continue
        _execute(
            conn,
            """
            INSERT INTO panels (
                id, title, description, category, enabled, height, command, auto_flag, ctid, sort_order
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                panel["id"],
                panel.get("title", panel["id"]),
                panel.get("description", ""),
                panel.get("category", "proxmox"),
                1 if panel.get("enabled", True) else 0,
                int(panel.get("height", 220)),
                json.dumps(panel.get("command", [])),
                1 if panel.get("auto") else 0,
                panel.get("ctid"),
                idx,
            ),
        )

    _execute(conn, "DELETE FROM ssh_hosts")
    for idx, host in enumerate(config.get("ssh_hosts", [])):
        if not host.get("id"):
            continue
        _execute(
            conn,
            """
            INSERT INTO ssh_hosts (
                id, title, description, enabled, conn_type, host, port, ssh_user, auth,
                key_file, password, command, sort_order
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                host["id"],
                host.get("title", host["id"]),
                host.get("description", ""),
                1 if host.get("enabled", True) else 0,
                host.get("type", "ssh"),
                host.get("host"),
                host.get("port"),
                host.get("user"),
                host.get("auth"),
                host.get("key_file"),
                host.get("password"),
                json.dumps(host["command"]) if host.get("command") else None,
                idx,
            ),
        )


def read_config_from_db(conn) -> dict:
    rows = _fetchall(conn, "SELECT setting_key, setting_value FROM settings")
    settings = {row["setting_key"]: row["setting_value"] for row in rows}
    if not settings:
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        write_config_to_db(conn, config)
        conn.commit()
        return config

    proxmox_host = settings.get("status_proxmox_host", "")
    pbs_host = settings.get("status_pbs_host", "")
    status_command_list = build_status_command(settings)

    panels = [
        _row_panel(row)
        for row in _fetchall(conn, "SELECT * FROM panels ORDER BY sort_order ASC, title ASC")
    ]
    ssh_hosts = [
        _row_ssh_host(row)
        for row in _fetchall(conn, "SELECT * FROM ssh_hosts ORDER BY sort_order ASC, title ASC")
    ]

    categories_raw = settings.get("log_categories")
    try:
        categories_data = json.loads(categories_raw) if categories_raw else None
    except json.JSONDecodeError:
        categories_data = None

    return {
        "title": settings.get("title", DEFAULT_CONFIG["title"]),
        "host": settings.get("host", DEFAULT_CONFIG["host"]),
        "port": int(settings.get("port", DEFAULT_CONFIG["port"])),
        "ws_port": int(settings.get("ws_port", DEFAULT_CONFIG["ws_port"])),
        "status": {
            "label": settings.get("status_label", DEFAULT_CONFIG["status"]["label"]),
            "proxmox_host": proxmox_host,
            "pbs_host": pbs_host,
            "command": status_command_list,
            "interval_seconds": int(
                settings.get("status_interval_seconds", DEFAULT_CONFIG["status"]["interval_seconds"])
            ),
        },
        "categories": normalize_log_categories(categories_data),
        "panels": panels,
        "ssh_hosts": ssh_hosts,
    }


def update_status_settings(config: dict, payload: dict) -> dict:
    label = str(payload.get("label", "")).strip() or DEFAULT_CONFIG["status"]["label"]
    proxmox_host = str(payload.get("proxmox_host", "")).strip()
    pbs_host = str(payload.get("pbs_host", "")).strip()
    try:
        interval = int(payload.get("interval_seconds", DEFAULT_CONFIG["status"]["interval_seconds"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("Ongeldig interval") from exc
    if interval < 2 or interval > 300:
        raise ValueError("Interval moet tussen 2 en 300 seconden zijn")
    if proxmox_host and not status_host_valid(proxmox_host):
        raise ValueError("Ongeldig Proxmox host/IP")
    if pbs_host and not status_host_valid(pbs_host):
        raise ValueError("Ongeldig PBS host/IP")

    status_settings = {
        "status_label": label,
        "status_proxmox_host": proxmox_host,
        "status_pbs_host": pbs_host,
        "status_interval_seconds": str(interval),
    }
    status_settings["status_command"] = json.dumps(build_status_command(status_settings))

    with _db_lock:
        conn = get_db_connection()
        try:
            for key, value in status_settings.items():
                _execute(
                    conn,
                    """
                    INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)
                    """,
                    (key, value),
                )
            conn.commit()
        finally:
            conn.close()

    config.setdefault("status", {})
    config["status"].update(
        {
            "label": label,
            "proxmox_host": proxmox_host,
            "pbs_host": pbs_host,
            "command": json.loads(status_settings["status_command"]),
            "interval_seconds": interval,
        }
    )
    return config["status"]


def _read_sqlite_config() -> dict | None:
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        if not rows:
            return None
        settings = {row["key"]: row["value"] for row in rows}
        status_command = settings.get("status_command")
        try:
            status_command_list = json.loads(status_command) if status_command else DEFAULT_CONFIG["status"]["command"]
        except json.JSONDecodeError:
            status_command_list = DEFAULT_CONFIG["status"]["command"]

        def sqlite_panel(row):
            panel = {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"] or "",
                "category": row["category"] or "proxmox",
                "enabled": bool(row["enabled"]),
                "height": int(row["height"] or 220),
                "command": json.loads(row["command"]),
            }
            if row["auto"]:
                panel["auto"] = True
            if row["ctid"]:
                panel["ctid"] = row["ctid"]
            return panel

        def sqlite_host(row):
            host = {
                "id": row["id"],
                "title": row["title"],
                "description": row["description"] or "",
                "enabled": bool(row["enabled"]),
                "type": row["type"] or "ssh",
            }
            for field in ("host", "auth", "key_file", "password"):
                if row[field]:
                    host[field] = row[field]
            if row["port"] is not None:
                host["port"] = int(row["port"])
            if row["user"]:
                host["user"] = row["user"]
            if row["command"]:
                host["command"] = json.loads(row["command"])
            return host

        panels = [sqlite_panel(r) for r in conn.execute("SELECT * FROM panels ORDER BY sort_order ASC").fetchall()]
        ssh_hosts = [sqlite_host(r) for r in conn.execute("SELECT * FROM ssh_hosts ORDER BY sort_order ASC").fetchall()]
        return {
            "title": settings.get("title", DEFAULT_CONFIG["title"]),
            "host": settings.get("host", DEFAULT_CONFIG["host"]),
            "port": int(settings.get("port", DEFAULT_CONFIG["port"])),
            "ws_port": int(settings.get("ws_port", DEFAULT_CONFIG["ws_port"])),
            "status": {
                "label": settings.get("status_label", DEFAULT_CONFIG["status"]["label"]),
                "command": status_command_list,
                "interval_seconds": int(
                    settings.get("status_interval_seconds", DEFAULT_CONFIG["status"]["interval_seconds"])
                ),
            },
            "panels": panels,
            "ssh_hosts": ssh_hosts,
        }
    finally:
        conn.close()


def migrate_legacy_sources(conn) -> bool:
    panel_count = _fetchone(conn, "SELECT COUNT(*) AS c FROM panels")["c"]
    if panel_count > 0:
        return False

    config = _read_sqlite_config()
    source = "sqlite"
    if not config and CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as fh:
            config = json.load(fh)
        source = "json"
    if not config:
        _seed_default_settings(conn)
        conn.commit()
        return True

    write_config_to_db(conn, config)
    conn.commit()

    if source == "sqlite" and DB_PATH.exists():
        backup = DB_PATH.with_suffix(".db.bak")
        if backup.exists():
            backup.unlink()
        DB_PATH.rename(backup)
        print(f"SQLite gemigreerd naar MariaDB, backup: {backup}")
    elif source == "json" and CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_suffix(".json.bak")
        if backup.exists():
            backup.unlink()
        CONFIG_PATH.rename(backup)
        print(f"JSON gemigreerd naar MariaDB, backup: {backup}")
    return True


def init_db() -> None:
    global _db_ready
    if _db_ready:
        return
    DB_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db_lock:
        conn = get_db_connection()
        try:
            for statement in SCHEMA_STATEMENTS:
                _execute(conn, statement)
            ensure_dashboard_schema_updates(conn)
            migrate_legacy_sources(conn)
            conn.commit()
        finally:
            conn.close()
        if DB_CONFIG_PATH.exists():
            os.chmod(DB_CONFIG_PATH, 0o600)
    _db_ready = True


def load_config() -> dict:
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            return read_config_from_db(conn)
        finally:
            conn.close()


def save_config(config: dict) -> None:
    init_db()
    with _db_lock:
        conn = get_db_connection()
        try:
            write_config_to_db(conn, config)
            conn.commit()
        finally:
            conn.close()
    if DB_CONFIG_PATH.exists():
        os.chmod(DB_CONFIG_PATH, 0o600)


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def valid_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value))


def build_journalctl_command(units: list[str]) -> list[str]:
    command = ["journalctl"]
    for unit in units:
        name = unit if unit.endswith(".service") else f"{unit}.service"
        command.extend(["-u", name])
    command.extend(["-f", "--no-pager", "-n", "50", "-o", "short-iso"])
    return command


def parse_systemctl_units(output: str) -> list[dict]:
    units = []
    seen = set()
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        name = parts[0].lstrip("●").strip()
        if not name.endswith(".service") or name in seen:
            continue
        seen.add(name)
        units.append(
            {
                "name": name,
                "load": parts[1],
                "active": parts[2],
                "sub": parts[3],
                "description": parts[4].strip() if len(parts) > 4 else "",
            }
        )

    def sort_key(item: dict) -> tuple:
        active_rank = 0 if item["active"] == "active" else 1
        running_rank = 0 if item["sub"] == "running" else 1
        return (active_rank, running_rank, item["name"])

    units.sort(key=sort_key)
    return units


def list_systemd_units(ctid: str | None = None) -> list[dict]:
    if ctid:
        if not ctid.isdigit():
            raise ValueError("Ongeldig container ID")
        command = [
            "pct", "exec", ctid, "--",
            "systemctl", "list-units", "--type=service", "--all",
            "--plain", "--no-legend", "--no-pager",
        ]
    else:
        command = [
            "systemctl", "list-units", "--type=service", "--all",
            "--plain", "--no-legend", "--no-pager",
        ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except Exception as exc:
        raise ValueError(f"Kan systemd units niet ophalen: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout or "systemctl mislukt").strip())
    return parse_systemctl_units(proc.stdout)


def parse_units_field(payload: dict) -> list[str]:
    units: list[str] = []
    raw_units = payload.get("units", "")
    if isinstance(raw_units, str):
        units.extend(u.strip() for u in raw_units.split(",") if u.strip())
    elif isinstance(raw_units, list):
        units.extend(str(u).strip() for u in raw_units if str(u).strip())

    picked = payload.get("unit_pick", [])
    if isinstance(picked, str):
        picked = [picked]
    if isinstance(picked, list):
        units.extend(str(u).strip() for u in picked if str(u).strip())

    deduped: list[str] = []
    seen = set()
    for unit in units:
        key = unit.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(unit)
    return deduped


def parse_pct_list_output(output: str) -> list[dict]:
    containers = []
    for line in output.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        vmid, status = parts[0], parts[1]
        if not vmid.isdigit():
            continue
        name = parts[-1] if len(parts) > 3 else parts[2]
        containers.append({"id": vmid, "name": name, "status": status})
    containers.sort(key=lambda item: int(item["id"]))
    return containers


def is_local_proxmox_host(hostname: str) -> bool:
    return hostname.lower().strip() in LOCAL_PROXMOX_HOSTS


def parse_ssh_config() -> dict[str, dict[str, str]]:
    config_path = Path.home() / ".ssh" / "config"
    hosts: dict[str, dict[str, str]] = {}
    if not config_path.exists():
        return hosts
    current: str | None = None
    for raw_line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower()
        if lower.startswith("host "):
            current = None
            for pattern in line.split()[1:]:
                if pattern != "*":
                    current = pattern
                    hosts.setdefault(current, {})
            continue
        if current is None:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        hosts[current][key] = value
    return hosts


def _expand_ssh_path(path: str) -> str:
    path = (path or "").strip()
    if path.startswith("~/"):
        return str(Path.home() / path[2:])
    return path


def resolve_ssh_target(
    host: str,
    *,
    user: str = "",
    port: int | None = None,
    key_file: str = "",
) -> dict:
    host = (host or "").strip()
    if not host:
        raise ValueError("Vul een host of hostname in")

    alias_cfg = parse_ssh_config().get(host, {})
    resolved_host = alias_cfg.get("hostname", host)
    resolved_user = (user or alias_cfg.get("user", "root")).strip() or "root"
    if port is not None and port > 0:
        resolved_port = port
    elif alias_cfg.get("port"):
        resolved_port = int(alias_cfg["port"])
    else:
        resolved_port = 22
    if resolved_port < 1 or resolved_port > 65535:
        raise ValueError("Poort moet tussen 1 en 65535 liggen")

    resolved_key = _expand_ssh_path(key_file or alias_cfg.get("identityfile", ""))
    if not resolved_key:
        if host in PBS_REMOTE_HOSTS or resolved_host in PBS_REMOTE_HOSTS:
            resolved_key = PBS_DEFAULT_KEY
        else:
            resolved_key = PROXMOX_DEFAULT_KEY

    local = is_local_proxmox_host(resolved_host) or is_local_proxmox_host(host)
    if not local and resolved_key and not os.path.isfile(resolved_key):
        for candidate in PROXMOX_FALLBACK_KEYS:
            if os.path.isfile(candidate):
                resolved_key = candidate
                break
        else:
            raise ValueError(f"SSH key bestand niet gevonden op minilab: {resolved_key}")

    return {
        "alias": host if alias_cfg else None,
        "host": resolved_host,
        "port": resolved_port,
        "user": resolved_user,
        "key_file": resolved_key,
        "local": local,
    }


def proxmox_connection_from_payload(payload: dict) -> dict:
    port_raw = str(payload.get("port", "")).strip()
    port = int(port_raw) if port_raw else None
    return resolve_ssh_target(
        str(payload.get("host", "")),
        user=str(payload.get("user", "")),
        port=port,
        key_file=str(payload.get("key_file", "")),
    )


def proxmox_connection_from_query(query: dict) -> dict | None:
    host = (query.get("host") or [""])[0].strip()
    if not host:
        return None
    port_raw = (query.get("port") or [""])[0].strip()
    port = int(port_raw) if port_raw else None
    return resolve_ssh_target(
        host,
        user=(query.get("user") or [""])[0],
        port=port,
        key_file=(query.get("key_file") or [""])[0],
    )


def proxmox_ssh_command(conn: dict, remote_command: str) -> list[str]:
    base = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if conn.get("alias"):
        return base + [conn["alias"], remote_command]
    return base + [
        "-i", conn["key_file"],
        "-p", str(conn["port"]),
        f"{conn['user']}@{conn['host']}",
        remote_command,
    ]


def run_proxmox_remote(conn: dict, remote_command: str, *, timeout: int = 20) -> subprocess.CompletedProcess:
    if conn["local"]:
        return subprocess.run(
            ["bash", "-c", remote_command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    return subprocess.run(
        proxmox_ssh_command(conn, remote_command),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def list_containers(conn: dict | None = None) -> list[dict]:
    try:
        if conn:
            proc = run_proxmox_remote(conn, "pct list", timeout=15)
        else:
            proc = subprocess.run(
                ["pct", "list"],
                capture_output=True,
                text=True,
                timeout=15,
            )
    except Exception as exc:
        raise ValueError(f"Kan containers niet ophalen: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout or "pct list mislukt").strip())
    return parse_pct_list_output(proc.stdout)


def list_proxmox_systemd_units(conn: dict, ctid: str | None = None) -> list[dict]:
    if ctid:
        if not ctid.isdigit():
            raise ValueError("Ongeldig container ID")
        remote = (
            f"pct exec {ctid} -- systemctl list-units --type=service --all "
            "--plain --no-legend --no-pager"
        )
    else:
        remote = (
            "systemctl list-units --type=service --all "
            "--plain --no-legend --no-pager"
        )
    try:
        proc = run_proxmox_remote(conn, remote, timeout=20)
    except Exception as exc:
        raise ValueError(f"Kan systemd units niet ophalen: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout or "systemctl mislukt").strip())
    return parse_systemctl_units(proc.stdout)


def container_log_command(ctid: str) -> list[str]:
    return [
        "pct", "exec", ctid, "--",
        "journalctl", "-f", "--no-pager", "-n", "40", "-o", "short-iso",
    ]


DOCKER_PS_FORMAT = '{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}'


def docker_connection_from_payload(payload: dict) -> dict:
    host = str(payload.get("host", "")).strip()
    if not host:
        return {"local": True, "host": "", "port": 0, "user": "", "key_file": "", "alias": None}
    port_raw = str(payload.get("port", "")).strip()
    port = int(port_raw) if port_raw else None
    return resolve_ssh_target(
        host,
        user=str(payload.get("user", "")),
        port=port,
        key_file=str(payload.get("key_file", "")),
    )


def docker_connection_from_query(query: dict) -> dict:
    host = (query.get("host") or [""])[0].strip()
    if not host:
        return {"local": True, "host": "", "port": 0, "user": "", "key_file": "", "alias": None}
    port_raw = (query.get("port") or [""])[0].strip()
    port = int(port_raw) if port_raw else None
    return resolve_ssh_target(
        host,
        user=(query.get("user") or [""])[0],
        port=port,
        key_file=(query.get("key_file") or [""])[0],
    )


def parse_docker_ps_output(output: str) -> list[dict]:
    containers = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cid, name, status = parts[0], parts[1], parts[2]
        image = parts[3] if len(parts) > 3 else ""
        containers.append({"id": cid, "name": name, "status": status, "image": image})
    containers.sort(key=lambda item: item["name"].lower())
    return containers


def list_docker_containers(conn: dict | None = None) -> list[dict]:
    remote_cmd = f"docker ps -a --format '{DOCKER_PS_FORMAT}'"
    try:
        if conn and not conn.get("local"):
            proc = run_proxmox_remote(conn, remote_cmd, timeout=15)
        else:
            proc = subprocess.run(
                ["docker", "ps", "-a", "--format", DOCKER_PS_FORMAT],
                capture_output=True,
                text=True,
                timeout=15,
            )
    except FileNotFoundError as exc:
        raise ValueError("Docker is niet geïnstalleerd op deze host") from exc
    except Exception as exc:
        raise ValueError(f"Kan Docker containers niet ophalen: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "docker ps mislukt").strip()
        raise ValueError(err)
    return parse_docker_ps_output(proc.stdout)


def docker_is_running(status: str) -> bool:
    s = (status or "").lower()
    return s.startswith("up") or "running" in s


def docker_log_command(container: str, conn: dict | None = None, *, tail: int = 50) -> list[str]:
    name = (container or "").strip()
    if not name:
        raise ValueError("Kies een Docker container")
    safe = shlex.quote(name)
    remote_cmd = f"docker logs -f --tail {int(tail)} --timestamps {safe}"
    if conn and not conn.get("local"):
        return proxmox_ssh_command(conn, remote_cmd)
    return ["docker", "logs", "-f", "--tail", str(int(tail)), "--timestamps", name]


def pbs_remote_journalctl(units: list[str]) -> list[str]:
    journal_parts = ["journalctl"]
    for unit in units:
        name = unit if unit.endswith(".service") else f"{unit}.service"
        journal_parts.extend(["-u", shlex.quote(name)])
    journal_parts.extend(["-f", "--no-pager", "-n", "50", "-o", "short-iso"])
    remote_cmd = " ".join(journal_parts)
    return [
        "ssh",
        "-i", PBS_DEFAULT_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "root@10.0.30.5",
        remote_cmd,
    ]


def build_auto_panels() -> list[dict]:
    panels = [
        {
            "id": "proxmox-vzdump",
            "category": "proxmox",
            "title": "VZDump",
            "description": "Backup taken op Proxmox node (minilab)",
            "enabled": True,
            "height": 220,
            "auto": True,
            "command": [
                "bash", "-c",
                "journalctl -f --no-pager -o short-iso 2>/dev/null | "
                "grep --line-buffered -iE \"vzdump|pbs-vault|backup:|backup-\"",
            ],
        },
        {
            "id": "proxmox-pve",
            "category": "proxmox",
            "title": "Proxmox VE",
            "description": "pvedaemon en pveproxy",
            "enabled": True,
            "height": 200,
            "auto": True,
            "command": build_journalctl_command(["pvedaemon", "pveproxy"]),
        },
        {
            "id": "backup-pbs",
            "category": "backup",
            "title": "PBS Server",
            "description": "Proxmox Backup Server (10.0.30.5)",
            "enabled": True,
            "height": 220,
            "auto": True,
            "command": pbs_remote_journalctl(["proxmox-backup"]),
        },
        {
            "id": "backup-safety",
            "category": "backup",
            "title": "PBS Wake & Shutdown",
            "description": "Veiligheids timers op minilab",
            "enabled": True,
            "height": 200,
            "auto": True,
            "command": build_journalctl_command(["pbs-wake", "pbs-shutdown"]),
        },
    ]

    try:
        containers = list_containers()
    except ValueError:
        containers = []

    for ct in containers:
        if ct["status"] != "running":
            continue
        panels.append(
            {
                "id": f"ct-{ct['id']}",
                "category": "container",
                "title": f"{ct['name']}",
                "description": f"CT {ct['id']} — alle logs",
                "enabled": True,
                "height": 180,
                "auto": True,
                "ctid": ct["id"],
                "command": container_log_command(ct["id"]),
            }
        )

    try:
        docker_containers = list_docker_containers()
    except ValueError:
        docker_containers = []

    for dc in docker_containers:
        if not docker_is_running(dc["status"]):
            continue
        slug = slugify(dc["name"]) or dc["id"][:12]
        panels.append(
            {
                "id": f"docker-{slug}",
                "category": "docker",
                "title": dc["name"],
                "description": f"Docker — {dc.get('image') or 'container'}",
                "enabled": True,
                "height": 180,
                "auto": True,
                "command": docker_log_command(dc["name"]),
            }
        )
    return panels


def sync_log_panels(config: dict, category: str | None = None) -> dict:
    legacy_remove = {
        "vzdump", "pbs-services", "pbs-server",
        "homepage-logs", "homepage-service",
    }
    auto = build_auto_panels()
    if category:
        categories = normalize_log_categories(config.get("categories"))
        if category not in categories:
            raise ValueError("Onbekende categorie")
        auto_cat = [p for p in auto if p.get("category") == category]
        kept = [
            p for p in config.get("panels", [])
            if not (p.get("auto") and p.get("category") == category)
        ]
        config["panels"] = kept + auto_cat
        save_config(config)
        return {
            "category": category,
            "auto": len(auto_cat),
            "total": len(config["panels"]),
        }

    manual = [
        p for p in config.get("panels", [])
        if not p.get("auto") and p.get("id") not in legacy_remove
    ]
    auto_ids = {p["id"] for p in auto}
    manual = [p for p in manual if p.get("id") not in auto_ids]
    config["panels"] = auto + manual
    save_config(config)
    return {
        "proxmox": sum(1 for p in auto if p.get("category") == "proxmox"),
        "backup": sum(1 for p in auto if p.get("category") == "backup"),
        "container": sum(1 for p in auto if p.get("category") == "container"),
        "docker": sum(1 for p in auto if p.get("category") == "docker"),
        "total": len(config["panels"]),
    }


def update_category_entry(config: dict, category_id: str, payload: dict) -> dict:
    cat_id = (category_id or "").strip()
    categories = normalize_log_categories(config.get("categories"))
    if cat_id not in categories:
        raise ValueError("Onbekende categorie")
    title = str(payload.get("title", "")).strip()
    sub = str(payload.get("sub", "")).strip()
    if not title:
        raise ValueError("Titel is verplicht")
    entry = {"title": title, "sub": sub}
    if categories[cat_id].get("builtin"):
        entry["builtin"] = True
    if categories[cat_id].get("custom"):
        entry["custom"] = True
    categories[cat_id] = entry
    config["categories"] = categories
    save_config(config)
    return categories[cat_id]


def add_category_entry(config: dict, payload: dict) -> dict:
    cat_id = slugify(str(payload.get("id") or payload.get("title", "")).strip())
    if not valid_id(cat_id):
        raise ValueError("Ongeldig categorie-id (a-z, 0-9, streepjes)")
    categories = normalize_log_categories(config.get("categories"))
    if cat_id in categories:
        raise ValueError(f"Categorie '{cat_id}' bestaat al")
    title = str(payload.get("title", "")).strip()
    sub = str(payload.get("sub", "")).strip()
    if not title:
        raise ValueError("Naam is verplicht")
    entry = {"title": title, "sub": sub, "custom": True}
    categories[cat_id] = entry
    config["categories"] = categories
    save_config(config)
    return {"id": cat_id, **entry}


def add_panel_entry(config: dict, payload: dict) -> dict:
    panel_id = slugify(payload.get("id") or payload.get("title", ""))
    if not valid_id(panel_id):
        raise ValueError("Ongeldig panel id")
    if panel_id in panel_map(config):
        raise ValueError(f"Panel '{panel_id}' bestaat al")

    source = payload.get("source", "journalctl")
    if source == "proxmox":
        conn = proxmox_connection_from_payload(payload)
        ctid = str(payload.get("ctid", "")).strip()
        units = parse_units_field(payload)
        if ctid:
            if not ctid.isdigit():
                raise ValueError("Kies een geldig container ID")
            if units:
                inner = shlex.join(build_journalctl_command(units))
                remote_cmd = f"pct exec {ctid} -- {inner}"
            else:
                remote_cmd = (
                    f"pct exec {ctid} -- journalctl -f --no-pager -n 40 -o short-iso"
                )
        else:
            if not units:
                raise ValueError("Kies minstens één systemd unit voor node logs")
            journal_parts = ["journalctl"]
            for unit in units:
                name = unit if unit.endswith(".service") else f"{unit}.service"
                journal_parts.extend(["-u", shlex.quote(name)])
            journal_parts.extend(["-f", "--no-pager", "-n", "50", "-o", "short-iso"])
            remote_cmd = " ".join(journal_parts)
        if conn["local"]:
            command = ["bash", "-c", remote_cmd]
        else:
            command = proxmox_ssh_command(conn, remote_cmd)
    elif source == "journalctl":
        units = parse_units_field(payload)
        if not units:
            raise ValueError("Kies minstens één systemd unit")
        command = build_journalctl_command(units)
    elif source == "container":
        ctid = str(payload.get("ctid", "")).strip()
        if not ctid.isdigit():
            raise ValueError("Kies een geldig container ID")
        units = parse_units_field(payload)
        if units:
            command = ["pct", "exec", ctid, "--"] + build_journalctl_command(units)
        else:
            command = container_log_command(ctid)
    elif source == "remote":
        port_raw = str(payload.get("port", "")).strip()
        port = int(port_raw) if port_raw else None
        conn = resolve_ssh_target(
            str(payload.get("host", "")),
            user=str(payload.get("user", "")),
            port=port,
            key_file=str(payload.get("key_file", "")),
        )
        units = parse_units_field(payload)
        if not units:
            raise ValueError("Kies minstens één systemd unit")
        journal_parts = ["journalctl"]
        for unit in units:
            name = unit if unit.endswith(".service") else f"{unit}.service"
            journal_parts.extend(["-u", shlex.quote(name)])
        journal_parts.extend(["-f", "--no-pager", "-n", "50", "-o", "short-iso"])
        remote_cmd = " ".join(journal_parts)
        command = proxmox_ssh_command(conn, remote_cmd)
    elif source == "docker":
        conn = docker_connection_from_payload(payload)
        docker_name = str(payload.get("docker_container", "")).strip()
        if not docker_name:
            raise ValueError("Kies een Docker container")
        command = docker_log_command(docker_name, conn)
    elif source == "command":
        command = payload.get("command")
        if isinstance(command, str):
            command = ["bash", "-c", command.strip()]
        if not command or not isinstance(command, list):
            raise ValueError("Vul een geldig commando in")
    else:
        raise ValueError("Onbekende log bron")

    category = (payload.get("category") or "").strip()
    if not category:
        if source == "docker":
            category = "docker"
        elif source in ("container", "proxmox") and str(payload.get("ctid", "")).strip():
            category = "container"
        elif source == "remote":
            category = "backup"
        else:
            category = "proxmox"

    panel = {
        "id": panel_id,
        "title": payload.get("title", panel_id).strip() or panel_id,
        "description": payload.get("description", "").strip(),
        "category": category,
        "enabled": True,
        "height": int(payload.get("height", 220)),
        "command": command,
    }
    config.setdefault("panels", []).append(panel)
    save_config(config)
    return panel


def delete_panel_entry(config: dict, panel_id: str) -> None:
    if not valid_id(panel_id):
        raise ValueError("Ongeldig panel id")
    panels = config.get("panels", [])
    new_panels = [p for p in panels if p.get("id") != panel_id]
    if len(new_panels) == len(panels):
        raise ValueError(f"Panel '{panel_id}' niet gevonden")
    config["panels"] = new_panels
    save_config(config)


def update_panel_entry(config: dict, panel_id: str, payload: dict) -> dict:
    if not valid_id(panel_id):
        raise ValueError("Ongeldig panel id")
    panels = config.get("panels", [])
    idx = next((i for i, p in enumerate(panels) if p.get("id") == panel_id), None)
    if idx is None:
        raise ValueError(f"Panel '{panel_id}' niet gevonden")
    panel = panels[idx]
    title = str(payload.get("title", "")).strip()
    if not title:
        raise ValueError("Titel is verplicht")
    category = str(payload.get("category", panel.get("category", "proxmox"))).strip()
    categories = normalize_log_categories(config.get("categories"))
    if category not in categories:
        raise ValueError("Onbekende categorie")
    panel["title"] = title
    panel["description"] = str(payload.get("description", "")).strip()
    panel["category"] = category
    panel["height"] = max(120, min(800, int(payload.get("height", panel.get("height", 220)))))
    panels[idx] = panel
    config["panels"] = panels
    save_config(config)
    return panel


def delete_ssh_entry(config: dict, host_id: str) -> None:
    if not valid_id(host_id):
        raise ValueError("Ongeldig host id")
    hosts = config.get("ssh_hosts", [])
    new_hosts = [h for h in hosts if h.get("id") != host_id]
    if len(new_hosts) == len(hosts):
        raise ValueError(f"SSH host '{host_id}' niet gevonden")
    config["ssh_hosts"] = new_hosts
    save_config(config)


def add_ssh_entry(config: dict, payload: dict) -> dict:
    host_id = slugify(payload.get("id") or payload.get("title", ""))
    if not valid_id(host_id):
        raise ValueError("Ongeldig host id")
    if host_id in ssh_map(config):
        raise ValueError(f"SSH host '{host_id}' bestaat al")

    conn_type = payload.get("type", "ssh")
    host = {
        "id": host_id,
        "title": payload.get("title", host_id).strip() or host_id,
        "description": payload.get("description", "").strip(),
        "enabled": True,
        "type": conn_type,
    }

    if conn_type == "local":
        host["command"] = ["/bin/bash", "-l"]
    else:
        hostname = payload.get("host", "").strip()
        auth = payload.get("auth", "key").strip() or "key"
        if not hostname:
            raise ValueError("Vul een host/IP in")
        if auth not in ("key", "password"):
            raise ValueError("Authenticatie moet 'key' of 'password' zijn")
        port_raw = str(payload.get("port", "")).strip()
        port = int(port_raw) if port_raw else 22
        if port < 1 or port > 65535:
            raise ValueError("Poort moet tussen 1 en 65535 liggen")

        entry = {
            "host": hostname,
            "port": port,
            "user": payload.get("user", "root").strip() or "root",
            "auth": auth,
        }
        if auth == "password":
            password = payload.get("password", "").strip()
            if not password:
                raise ValueError("Vul een wachtwoord in")
            entry["password"] = password
        else:
            entry["key_file"] = validate_key_file_path(payload.get("key_file", ""))
        host.update(entry)

    config.setdefault("ssh_hosts", []).append(host)
    save_config(config)
    safe = dict(host)
    if "password" in safe:
        safe["password"] = "***"
    return safe


def update_ssh_entry(config: dict, host_id: str, payload: dict) -> dict:
    if not valid_id(host_id):
        raise ValueError("Ongeldig host id")
    hosts = config.get("ssh_hosts", [])
    idx = next((i for i, h in enumerate(hosts) if h.get("id") == host_id), None)
    if idx is None:
        raise ValueError(f"SSH host '{host_id}' niet gevonden")

    existing = hosts[idx]
    conn_type = payload.get("type", existing.get("type", "ssh"))
    host = {
        "id": host_id,
        "title": payload.get("title", existing.get("title", host_id)).strip() or host_id,
        "description": payload.get("description", "").strip(),
        "enabled": existing.get("enabled", True),
        "type": conn_type,
    }

    if conn_type == "local":
        host["command"] = existing.get("command", ["/bin/bash", "-l"])
    else:
        hostname = payload.get("host", existing.get("host", "")).strip()
        auth = payload.get("auth", existing.get("auth", "key")).strip() or "key"
        if not hostname:
            raise ValueError("Vul een host/IP in")
        if auth not in ("key", "password"):
            raise ValueError("Authenticatie moet 'key' of 'password' zijn")
        port_raw = str(payload.get("port", existing.get("port", ""))).strip()
        port = int(port_raw) if port_raw else 22
        if port < 1 or port > 65535:
            raise ValueError("Poort moet tussen 1 en 65535 liggen")

        entry = {
            "host": hostname,
            "port": port,
            "user": payload.get("user", existing.get("user", "root")).strip() or "root",
            "auth": auth,
        }
        if auth == "password":
            password = payload.get("password", "").strip()
            if password:
                entry["password"] = password
            elif existing.get("password"):
                entry["password"] = existing["password"]
            else:
                raise ValueError("Vul een wachtwoord in")
        else:
            key_file = payload.get("key_file", existing.get("key_file", "")).strip()
            entry["key_file"] = validate_key_file_path(key_file)
        host.update(entry)

    hosts[idx] = host
    config["ssh_hosts"] = hosts
    save_config(config)
    safe = dict(host)
    if "password" in safe:
        safe["password"] = "***"
    return safe


def panel_map(config: dict) -> dict[str, dict]:
    return {
        panel["id"]: panel
        for panel in config.get("panels", [])
        if panel.get("enabled", True) and panel.get("id")
    }


def ssh_map(config: dict) -> dict[str, dict]:
    return {
        host["id"]: host
        for host in config.get("ssh_hosts", [])
        if host.get("enabled", True) and host.get("id")
    }


def validate_key_file_path(path: str) -> str:
    path = path.strip()
    if not path:
        raise ValueError("Vul een pad naar de SSH key in")
    if path.startswith(("ssh-", "ecdsa-", "ssh-rsa")) or "AAAA" in path:
        raise ValueError(
            "Dit is een public key, geen bestandspad. "
            "Vul het pad in op minilab, bijv. /root/.ssh/id_ed25519_homepage"
        )
    if not path.startswith("/"):
        raise ValueError("SSH key pad moet absoluut zijn, bijv. /root/.ssh/pbs_key")
    if not os.path.isfile(path):
        raise ValueError(f"SSH key bestand niet gevonden op minilab: {path}")
    return path


def load_private_key(path: str):
    loaders = (
        paramiko.Ed25519Key,
        paramiko.RSAKey,
        paramiko.ECDSAKey,
    )
    errors = []
    for loader in loaders:
        try:
            return loader.from_private_key_file(path)
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError(f"Kan SSH key niet laden ({path}): {'; '.join(errors)}")


def run_status(config: dict) -> dict:
    status_cfg = config.get("status", {})
    command = status_cfg.get("command") or DEFAULT_CONFIG["status"]["command"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
        output = (proc.stdout or proc.stderr or "").strip()
        return {"output": output, "exit_code": proc.returncode}
    except Exception as exc:
        return {"output": f"Fout bij status: {exc}", "exit_code": 1}


def public_config(config: dict) -> dict:
    return {
        "title": config.get("title", DEFAULT_CONFIG["title"]),
        "ws_port": int(config.get("ws_port", DEFAULT_CONFIG["ws_port"])),
        "status": {
            "label": config.get("status", {}).get("label", "Status"),
            "proxmox_host": config.get("status", {}).get("proxmox_host", ""),
            "pbs_host": config.get("status", {}).get("pbs_host", ""),
            "interval_seconds": config.get("status", {}).get("interval_seconds", 5),
        },
        "panels": [
            {
                "id": panel["id"],
                "title": panel.get("title", panel["id"]),
                "description": panel.get("description", ""),
                "category": panel.get("category", "proxmox"),
                "height": panel.get("height", 220),
            }
            for panel in config.get("panels", [])
            if panel.get("enabled", True) and panel.get("id")
        ],
        "ssh_hosts": [
            {
                "id": host["id"],
                "title": host.get("title", host["id"]),
                "description": host.get("description", ""),
                "user": host.get("user", "root"),
                "hostname": host.get("host", "local"),
                "port": int(host.get("port", 22)),
                "type": host.get("type", "ssh"),
                "auth": host.get("auth", "key" if host.get("key_file") else "password"),
                "key_file": host.get("key_file", ""),
            }
            for host in config.get("ssh_hosts", [])
            if host.get("enabled", True) and host.get("id")
        ],
        "categories": normalize_log_categories(config.get("categories")),
    }


def stream_command(command: list[str], write):
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            write(f"data: {line.rstrip(chr(10))}\n\n")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


async def bridge_local_shell(websocket, host_cfg: dict):
    command = host_cfg.get("command") or ["/bin/bash", "-l"]
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    loop = asyncio.get_running_loop()

    async def read_pty():
        try:
            while proc.poll() is None:
                ready, _, _ = await loop.run_in_executor(
                    None, select.select, [master_fd], [], [], 0.05
                )
                if master_fd in ready:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if data:
                        await websocket.send(data.decode("utf-8", errors="replace"))
                await asyncio.sleep(0.01)
        except Exception:
            pass

    async def write_pty():
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    os.write(master_fd, message)
                else:
                    payload = json.loads(message)
                    if payload.get("type") == "resize":
                        rows = int(payload.get("rows", 32))
                        cols = int(payload.get("cols", 120))
                        import struct
                        import fcntl
                        import termios

                        winsize = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                    elif payload.get("type") == "input":
                        os.write(master_fd, payload.get("data", "").encode("utf-8"))
        except Exception:
            pass

    read_task = asyncio.create_task(read_pty())
    write_task = asyncio.create_task(write_pty())
    try:
        await asyncio.wait({read_task, write_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        read_task.cancel()
        write_task.cancel()
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
        os.close(master_fd)


async def bridge_ssh(websocket, host_cfg: dict):
    if host_cfg.get("type") == "local":
        await bridge_local_shell(websocket, host_cfg)
        return

    host = host_cfg["host"]
    port = int(host_cfg.get("port", 22))
    user = host_cfg.get("user", "root")
    auth = host_cfg.get("auth", "key" if host_cfg.get("key_file") else "password")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs = {
        "hostname": host,
        "port": port,
        "username": user,
        "look_for_keys": False,
        "allow_agent": False,
        "timeout": 15,
    }
    if auth == "password":
        password = host_cfg.get("password", "")
        if not password:
            await websocket.send("\r\n[SSH fout] Geen wachtwoord geconfigureerd\r\n")
            return
        connect_kwargs["password"] = password
    else:
        connect_kwargs["pkey"] = load_private_key(host_cfg["key_file"])

    try:
        client.connect(**connect_kwargs)
    except Exception as exc:
        await websocket.send(f"\r\n[SSH fout] {exc}\r\n")
        return

    transport = client.get_transport()
    if transport is None:
        await websocket.send("\r\n[SSH fout] Geen transport\r\n")
        client.close()
        return

    channel = transport.open_session()
    channel.get_pty(term="xterm-256color", width=120, height=32)
    channel.invoke_shell()

    async def ssh_to_ws():
        try:
            while not channel.closed:
                if channel.recv_ready():
                    data = channel.recv(4096)
                    if data:
                        await websocket.send(data.decode("utf-8", errors="replace"))
                if channel.exit_status_ready():
                    break
                await asyncio.sleep(0.02)
        except Exception:
            pass

    async def ws_to_ssh():
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    channel.send(message)
                else:
                    payload = json.loads(message)
                    if payload.get("type") == "resize":
                        channel.resize_pty(
                            width=int(payload.get("cols", 120)),
                            height=int(payload.get("rows", 32)),
                        )
                    elif payload.get("type") == "input":
                        channel.send(payload.get("data", ""))
        except Exception:
            pass

    recv_task = asyncio.create_task(ssh_to_ws())
    send_task = asyncio.create_task(ws_to_ssh())
    try:
        await asyncio.wait(
            {recv_task, send_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        recv_task.cancel()
        send_task.cancel()
        channel.close()
        client.close()


async def ws_handler(websocket):
    path = urlparse(websocket.request.path).path if websocket.request else ""
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or parts[0] != "ssh":
        await websocket.close(code=1008, reason="Onbekend pad")
        return

    host_id = parts[1]
    config = load_config()
    host_cfg = ssh_map(config).get(host_id)
    if not host_cfg:
        await websocket.close(code=1008, reason="Onbekende SSH host")
        return

    await bridge_ssh(websocket, host_cfg)


def run_ws_server(config: dict):
    host = config.get("host", "0.0.0.0")
    port = int(config.get("ws_port", 8766))

    async def _serve():
        async with websockets.serve(ws_handler, host, port):
            print(f"SSH WebSocket op ws://{host}:{port}/ssh/<host-id>")
            await asyncio.Future()

    asyncio.run(_serve())


HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Homelab Dashboard</title>
  <link rel="icon" href="/static/logo.svg?v=5" type="image/svg+xml">
  <link rel="stylesheet" href="/static/xterm.min.css">
  <style>
    :root {
      --bg: #080b10;
      --bg-elevated: #0d1117;
      --panel: #11161d;
      --panel-2: #0c1016;
      --border: #1e2a3a;
      --border-subtle: #162030;
      --text: #edf2f7;
      --text-secondary: #a8b8cc;
      --muted: #6b7f96;
      --accent: #4d9fff;
      --accent-soft: rgba(77,159,255,.12);
      --accent-glow: rgba(77,159,255,.35);
      --ok: #34d399;
      --ok-soft: rgba(52,211,153,.12);
      --warn: #fbbf24;
      --warn-soft: rgba(251,191,36,.12);
      --bad: #f87171;
      --bad-soft: rgba(248,113,113,.12);
      --mono: "JetBrains Mono", "SF Mono", "Fira Code", ui-monospace, monospace;
      --sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --radius: 12px;
      --radius-sm: 8px;
      --shadow: 0 1px 2px rgba(0,0,0,.25), 0 8px 24px rgba(0,0,0,.2);
      --header-h: 64px;
    }
    * { box-sizing: border-box; }
    html { -webkit-font-smoothing: antialiased; }
    body {
      margin: 0; background: var(--bg); color: var(--text);
      font-family: var(--sans); min-height: 100vh; line-height: 1.5;
      background-image:
        radial-gradient(ellipse 80% 50% at 50% -20%, rgba(77,159,255,.08), transparent),
        radial-gradient(ellipse 60% 40% at 100% 0%, rgba(52,211,153,.04), transparent);
    }
    button, input, select, textarea { font: inherit; }
    button {
      transition: background .15s, border-color .15s, color .15s, box-shadow .15s;
    }
    button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible {
      outline: 2px solid var(--accent); outline-offset: 2px;
    }
    header {
      position: sticky; top: 0; z-index: 30;
      height: var(--header-h);
      backdrop-filter: blur(16px) saturate(1.2);
      background: rgba(8,11,16,.88);
      border-bottom: 1px solid var(--border-subtle);
      padding: 0 1.5rem;
      display: flex; justify-content: space-between; align-items: center; gap: 1rem;
    }
    .brand { display: flex; align-items: center; gap: .85rem; min-width: 0; }
    .brand-mark {
      width: 40px; height: 40px; border-radius: 10px; flex-shrink: 0;
      background: linear-gradient(165deg, #161f2e 0%, #0e131b 100%);
      border: 1px solid rgba(148,163,184,.16);
      display: grid; place-items: center;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 2px 10px rgba(0,0,0,.35);
    }
    .brand-logo { width: 26px; height: 26px; display: block; }
    .brand-text { min-width: 0; }
    h1 {
      margin: 0; font-size: 1rem; font-weight: 650; letter-spacing: -.02em;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .brand-sub { font-size: .72rem; color: var(--muted); margin-top: .1rem; }
    .header-right { display: flex; gap: .5rem; align-items: center; flex-shrink: 0; }
    .account-wrap { position: relative; }
    .account-btn {
      display: inline-flex; align-items: center; gap: .45rem;
      background: var(--panel); color: var(--text-secondary);
      border: 1px solid var(--border); border-radius: 999px;
      padding: .35rem .75rem .35rem .55rem; cursor: pointer; font-size: .74rem; font-weight: 500;
    }
    .account-btn:hover { color: var(--text); border-color: #2a3d52; background: var(--panel-2); }
    .account-btn svg { width: 15px; height: 15px; opacity: .85; }
    .account-menu {
      position: absolute; right: 0; top: calc(100% + .45rem); min-width: 210px;
      background: var(--panel); border: 1px solid var(--border);
      border-radius: var(--radius-sm); box-shadow: var(--shadow);
      padding: .45rem; z-index: 40;
    }
    .account-menu[hidden] { display: none; }
    .account-menu-head {
      padding: .55rem .65rem .7rem; border-bottom: 1px solid var(--border-subtle); margin-bottom: .35rem;
    }
    .account-menu-name { display: block; font-size: .82rem; font-weight: 600; color: var(--text); }
    .account-menu-role { display: block; font-size: .68rem; color: var(--muted); margin-top: .15rem; }
    .account-menu-item {
      width: 100%; text-align: left; background: transparent; color: var(--text-secondary);
      border: none; border-radius: 6px; padding: .55rem .65rem; cursor: pointer; font-size: .78rem;
    }
    .account-menu-item:hover { background: var(--panel-2); color: var(--text); }
    .account-menu-danger { color: var(--bad); }
    .account-menu-danger:hover { background: var(--bad-soft); color: #fca5a5; }
    .auth-gate {
      position: fixed; inset: 0; z-index: 60;
      display: none; align-items: center; justify-content: center; padding: 1rem;
      background: rgba(0,0,0,.72); backdrop-filter: blur(6px);
    }
    .auth-gate.open { display: flex; }
    .auth-card[hidden] { display: none !important; }
    .auth-card {
      width: min(380px, 100%); background: var(--panel);
      border: 1px solid var(--border); border-radius: 16px; overflow: hidden;
      box-shadow: 0 24px 80px rgba(0,0,0,.55);
    }
    .auth-card-head {
      padding: 1.1rem 1.15rem; border-bottom: 1px solid var(--border-subtle);
      background: var(--panel-2);
    }
    .auth-card-title { font-size: .95rem; font-weight: 650; margin: 0; }
    .auth-card-sub { margin: .3rem 0 0; font-size: .74rem; color: var(--muted); }
    .auth-card-body { padding: 1.15rem; display: grid; gap: .85rem; }
    .badge {
      font-size: .7rem; font-weight: 500; padding: .3rem .65rem;
      border-radius: 999px; border: 1px solid var(--border);
      color: var(--text-secondary); background: var(--panel);
      font-variant-numeric: tabular-nums;
    }
    .badge.live {
      color: var(--ok); border-color: rgba(52,211,153,.35); background: var(--ok-soft);
    }
    .app-shell {
      display: grid; grid-template-columns: 288px 1fr;
      min-height: calc(100vh - var(--header-h));
    }
    .content { min-width: 0; overflow: auto; order: 2; }
    .sidebar {
      order: 1; border-right: 1px solid var(--border-subtle);
      background: var(--bg-elevated);
      padding: 1.25rem 1rem; display: flex; flex-direction: column; gap: 1.25rem;
      position: sticky; top: var(--header-h);
      height: calc(100vh - var(--header-h)); overflow-y: auto;
    }
    .sidebar-section { display: flex; flex-direction: column; gap: .5rem; }
    .sidebar-label {
      font-size: .65rem; text-transform: uppercase; letter-spacing: .1em;
      color: var(--muted); font-weight: 600; padding: 0 .35rem;
    }
    .tabs { display: flex; flex-direction: column; gap: .35rem; }
    .tab {
      width: 100%; text-align: left; background: transparent; color: var(--text-secondary);
      border: 1px solid transparent; border-radius: var(--radius-sm);
      padding: .6rem .75rem; cursor: pointer; font-size: .84rem; font-weight: 500;
      display: flex; align-items: center; gap: .6rem;
    }
    .tab svg { width: 16px; height: 16px; opacity: .7; flex-shrink: 0; }
    .tab:hover { background: var(--panel); color: var(--text); }
    .tab.active {
      color: var(--text); background: var(--accent-soft);
      border-color: rgba(77,159,255,.25);
    }
    .tab.active svg { opacity: 1; color: var(--accent); }
    .meta {
      color: var(--muted); font-size: .72rem;
      display: flex; flex-direction: column; gap: .5rem;
      padding: .85rem; border-radius: var(--radius-sm);
      background: var(--panel); border: 1px solid var(--border-subtle);
      margin-top: auto;
    }
    .meta-row {
      display: flex; justify-content: space-between; gap: .5rem; line-height: 1.35;
    }
    .meta-row span:last-child { color: var(--text-secondary); text-align: right; }
    .view { display: none; height: 100%; }
    .view.active { display: block; }
    .layout {
      padding: 1.5rem; display: grid; gap: 1.25rem;
      max-width: 1440px;
    }
    .page-header { display: flex; justify-content: space-between; align-items: flex-end; gap: 1rem; flex-wrap: wrap; }
    .page-title { margin: 0; font-size: 1.35rem; font-weight: 650; letter-spacing: -.03em; }
    .page-sub { margin: .25rem 0 0; font-size: .84rem; color: var(--muted); }
    .status-widget, .panel, .terminal-wrap {
      background: var(--panel); border: 1px solid var(--border-subtle);
      border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow);
    }
    .status-widget-head {
      display: flex; justify-content: space-between; align-items: center; gap: 1rem;
      padding: .85rem 1.1rem; border-bottom: 1px solid var(--border-subtle);
      background: var(--panel-2);
    }
    .status-widget-title {
      display: flex; align-items: center; gap: .55rem;
      font-size: .8rem; font-weight: 600; color: var(--text-secondary);
    }
    .status-dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex-shrink: 0;
    }
    .status-dot.idle { background: var(--ok); box-shadow: 0 0 10px var(--ok); }
    .status-dot.busy { background: var(--warn); box-shadow: 0 0 10px var(--warn); }
    .status-dot.offline { background: var(--bad); box-shadow: 0 0 10px var(--bad); }
    .status-updated { font-size: .72rem; color: var(--muted); font-variant-numeric: tabular-nums; }
    .status-card {
      margin: 0; padding: 1rem 1.1rem; font-family: var(--mono); font-size: .82rem;
      white-space: pre-wrap; line-height: 1.55; background: #06080c;
      border: none; border-radius: 0; box-shadow: none;
    }
    .status-card.idle { border-left: 3px solid var(--ok); }
    .status-card.busy { border-left: 3px solid var(--warn); }
    .status-card.offline { border-left: 3px solid var(--bad); }
    .panel-head, .terminal-head {
      display: flex; justify-content: space-between; align-items: center; gap: .8rem;
      padding: .8rem 1rem; background: var(--panel-2); border-bottom: 1px solid var(--border-subtle);
    }
    .panel-title, .terminal-title { font-size: .88rem; font-weight: 600; letter-spacing: -.01em; }
    .panel-desc, .terminal-desc { font-size: .72rem; color: var(--muted); margin-top: .2rem; }
    .panel-actions { display: flex; gap: .35rem; align-items: center; flex-shrink: 0; }
    .btn, .host-btn {
      background: var(--panel-2); color: var(--text-secondary);
      border: 1px solid var(--border); border-radius: var(--radius-sm);
      padding: .45rem .7rem; font-size: .74rem; font-weight: 500; cursor: pointer;
      width: 100%; text-align: left;
    }
    .btn:hover, .host-btn:hover {
      color: var(--text); border-color: #2a3d52; background: var(--panel);
    }
    .btn:disabled { opacity: .45; cursor: not-allowed; }
    .btn-toggle-off { color: var(--warn, #fbbf24); border-color: rgba(251,191,36,.35); }
    .btn-toggle-off:hover { background: rgba(251,191,36,.12); color: #fde68a; }
    .btn-sm { width: auto; padding: .35rem .55rem; font-size: .68rem; }
    .host-btn {
      display: flex; flex-direction: column; align-items: flex-start;
      padding: .65rem .75rem;
    }
    .host-btn.active {
      color: var(--accent); border-color: rgba(77,159,255,.4);
      background: var(--accent-soft);
    }
    .host-list { display: flex; flex-direction: column; gap: .4rem; }
    .host-item { display: flex; gap: .35rem; align-items: stretch; }
    .host-item .host-btn { flex: 1; min-width: 0; }
    .host-item-actions {
      display: flex; flex-direction: column; gap: .35rem; flex-shrink: 0;
      position: relative; z-index: 2; min-width: 4.75rem;
    }
    .host-item-actions .btn { width: auto; text-align: center; min-width: 4.75rem; }
    #sidebar-logs, #sidebar-ssh { display: none; }
    #sidebar-logs.visible, #sidebar-ssh.visible { display: flex; }
    .sidebar-divider {
      height: 1px; background: var(--border-subtle); margin: .15rem 0;
    }
    .sidebar-actions { display: flex; flex-direction: column; gap: .4rem; }
    .panel-log {
      margin: 0; padding: .9rem 1rem; overflow-y: auto; font-family: var(--mono); font-size: .74rem;
      line-height: 1.5; background: #06080c; white-space: pre-wrap; word-break: break-word;
    }
    .panel-badge {
      display: inline-flex; align-items: center; gap: .35rem;
      font-size: .68rem; font-weight: 500; color: var(--muted);
      padding: .25rem .55rem; border-radius: 999px;
      background: var(--panel-2); border: 1px solid var(--border-subtle);
    }
    .panel-badge::before {
      content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--muted);
    }
    .panel-badge.live { color: var(--ok); border-color: rgba(52,211,153,.3); background: var(--ok-soft); }
    .panel-badge.live::before { background: var(--ok); animation: pulse 2s ease infinite; }
    .panel-badge.err { color: var(--bad); border-color: rgba(248,113,113,.3); background: var(--bad-soft); }
    .panel-badge.err::before { background: var(--bad); }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: .4; }
    }
    .log-categories {
      display: flex; flex-direction: column; gap: .35rem;
    }
    .cat-tab {
      width: 100%; text-align: left; background: transparent; color: var(--text-secondary);
      border: 1px solid transparent; border-radius: var(--radius-sm);
      padding: .65rem .7rem; cursor: pointer;
      display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: .65rem;
    }
    .cat-icon {
      width: 32px; height: 32px; border-radius: 8px;
      background: var(--panel); border: 1px solid var(--border-subtle);
      display: grid; place-items: center; flex-shrink: 0;
    }
    .cat-icon svg { width: 16px; height: 16px; opacity: .75; }
    .cat-info { min-width: 0; display: flex; flex-direction: column; gap: .1rem; }
    .cat-name { font-size: .82rem; font-weight: 600; color: var(--text); }
    .cat-desc { font-size: .68rem; color: var(--muted); line-height: 1.3; }
    .cat-count {
      font-size: .68rem; font-weight: 600; font-variant-numeric: tabular-nums;
      color: var(--muted); background: var(--panel);
      border: 1px solid var(--border-subtle); border-radius: 999px;
      padding: .15rem .5rem; min-width: 1.5rem; text-align: center;
    }
    .cat-tab:hover { background: var(--panel); border-color: var(--border-subtle); }
    .cat-tab:hover .cat-icon { border-color: #2a3d52; }
    .cat-tab.active {
      background: var(--accent-soft); border-color: rgba(77,159,255,.3);
    }
    .cat-tab.active .cat-icon {
      background: rgba(77,159,255,.15); border-color: rgba(77,159,255,.35);
    }
    .cat-tab.active .cat-icon svg { opacity: 1; color: var(--accent); }
    .cat-tab.active .cat-count {
      color: var(--accent); border-color: rgba(77,159,255,.35);
      background: rgba(77,159,255,.1);
    }
    .cat-item { display: flex; gap: .35rem; align-items: stretch; }
    .cat-item .cat-tab { flex: 1; min-width: 0; }
    .cat-item-actions {
      display: flex; flex-direction: column; gap: .35rem; flex-shrink: 0;
      position: relative; z-index: 2; min-width: 3.5rem;
    }
    .cat-item-actions .btn {
      width: auto; text-align: center; min-width: 3.5rem;
      padding: .35rem .45rem; font-size: .66rem;
    }

    .grid { display: grid; gap: 1rem; }
    @media (min-width: 1100px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (min-width: 1500px) { .grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
    .terminal-body { padding: .75rem; background: #06080c; min-height: calc(100vh - 200px); }
    #terminal { height: calc(100vh - 220px); min-height: 420px; border-radius: var(--radius-sm); overflow: hidden; }
    @media (max-width: 900px) {
      .app-shell { grid-template-columns: 1fr; }
      .sidebar {
        position: static; height: auto; border-right: none;
        border-bottom: 1px solid var(--border-subtle); order: 1;
      }
      .content { order: 2; }
      .layout { padding: 1rem; }
      .meta { margin-top: 0; }
    }
    .help {
      color: var(--muted); font-size: .75rem; line-height: 1.6;
      padding: .85rem 1rem; border-radius: var(--radius-sm);
      background: var(--panel-2); border: 1px solid var(--border-subtle);
    }
    .help code { font-family: var(--mono); color: var(--accent); font-size: .72rem; }
    .line-warn { color: var(--warn); }
    .line-err { color: var(--bad); }
    .line-ok { color: var(--ok); }
    .line-info { color: var(--accent); }
    .btn-add {
      background: var(--accent-soft); color: var(--accent);
      border-color: rgba(77,159,255,.35); font-weight: 600;
    }
    .btn-add:hover {
      background: rgba(77,159,255,.2); color: #7ec0ff;
      border-color: rgba(77,159,255,.5);
    }
    .modal-backdrop {
      position: fixed; inset: 0; background: rgba(0,0,0,.65);
      backdrop-filter: blur(4px);
      display: none; align-items: center; justify-content: center; z-index: 50; padding: 1rem;
    }
    .modal-backdrop.open { display: flex; }
    .modal {
      width: min(540px, 100%); background: var(--panel);
      border: 1px solid var(--border); border-radius: 16px; overflow: hidden;
      box-shadow: 0 24px 80px rgba(0,0,0,.55);
      display: flex; flex-direction: column; max-height: min(92vh, 900px);
    }
    .modal.modal-wide { width: min(680px, 100%); }
    .modal-head {
      padding: 1rem 1.15rem; border-bottom: 1px solid var(--border-subtle);
      display: flex; justify-content: space-between; align-items: center;
      background: var(--panel-2); flex-shrink: 0;
    }
    .modal-title { font-size: .95rem; font-weight: 650; }
    .modal-body {
      padding: 1.15rem; display: grid; gap: .85rem;
      overflow-y: auto; min-height: 0; flex: 1;
    }
    .modal-footer {
      padding: .9rem 1.15rem; border-top: 1px solid var(--border-subtle);
      background: var(--panel-2); flex-shrink: 0;
    }
    .modal-footer .modal-actions { padding-top: 0; margin: 0; }
    .modal-tabs {
      display: flex; gap: .25rem; padding: .25rem;
      background: var(--panel-2); border-radius: var(--radius-sm);
      border: 1px solid var(--border-subtle);
    }
    .modal-tab {
      flex: 1; background: transparent; color: var(--muted); border: none;
      border-radius: 6px; padding: .5rem .6rem; cursor: pointer;
      font-size: .78rem; font-weight: 500;
    }
    .modal-tab.active { color: var(--text); background: var(--panel); box-shadow: var(--shadow); }
    .field { display: grid; gap: .35rem; }
    .field label { font-size: .74rem; font-weight: 500; color: var(--text-secondary); }
    .field input, .field select, .field textarea {
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      border-radius: var(--radius-sm); padding: .6rem .7rem; font-size: .82rem;
    }
    .field input:hover, .field select:hover, .field textarea:hover { border-color: #2a3d52; }
    .field select[multiple] {
      min-height: 6.5rem; max-height: 10rem; font-family: var(--mono);
      font-size: .76rem; line-height: 1.35;
    }
    .form-section {
      display: grid; gap: .75rem; padding: .85rem;
      background: var(--bg); border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
    }
    .form-section-title {
      font-size: .72rem; font-weight: 650; letter-spacing: .04em;
      text-transform: uppercase; color: var(--muted); margin: 0;
    }
    .source-hint {
      padding: .55rem .65rem; border-radius: var(--radius-sm);
      background: var(--panel-2); border: 1px solid var(--border-subtle);
      color: var(--muted); font-size: .72rem; line-height: 1.45;
    }
    .field select[multiple] option:checked { background: var(--accent-soft); color: var(--text); }
    .field-row { display: grid; grid-template-columns: 2fr 1fr; gap: .6rem; }
    @media (max-width: 520px) { .field-row { grid-template-columns: 1fr; } }
    .field textarea { min-height: 72px; resize: vertical; font-family: var(--mono); }
    .field small { color: var(--muted); font-size: .7rem; line-height: 1.4; }
    .form-pane { display: none; gap: .85rem; }
    .form-pane.active { display: grid; }
    .modal-actions { display: flex; gap: .5rem; justify-content: flex-end; padding-top: .35rem; }
    .btn-primary {
      background: linear-gradient(180deg, #5aabff 0%, #3b82f6 100%);
      color: #fff; border: none; font-weight: 600;
      box-shadow: 0 2px 8px rgba(59,130,246,.35);
    }
    .btn-primary:hover { filter: brightness(1.06); color: #fff; }
    .btn-danger { color: var(--bad); border-color: rgba(248,113,113,.35); }
    .btn-danger:hover { background: var(--bad-soft); color: #fca5a5; }
    .form-error { color: var(--bad); font-size: .76rem; min-height: 1rem; }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg class="brand-logo" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="6" y="7" width="17" height="18" rx="2.5" stroke="#b8c5d6" stroke-width="1.2"/>
          <line x1="6" y1="10.5" x2="23" y2="10.5" stroke="#b8c5d6" stroke-width=".7" opacity=".35"/>
          <line x1="8.5" y1="13.5" x2="20.5" y2="13.5" stroke="#edf2f7" stroke-width="1.05" stroke-linecap="round" opacity=".42"/>
          <line x1="8.5" y1="16.5" x2="18" y2="16.5" stroke="#edf2f7" stroke-width="1.05" stroke-linecap="round" opacity=".62"/>
          <line x1="8.5" y1="19.5" x2="15.5" y2="19.5" stroke="#edf2f7" stroke-width="1.05" stroke-linecap="round" opacity=".82"/>
          <path d="M8.5 22.2 10 23.6 8.5 25" stroke="#4d9fff" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/>
          <line x1="11.2" y1="23.6" x2="12.6" y2="23.6" stroke="#4d9fff" stroke-width="1.1" stroke-linecap="round"/>
          <line x1="23" y1="16" x2="25.2" y2="16" stroke="#4d9fff" stroke-width="1.05" stroke-linecap="round"/>
          <circle cx="27" cy="16" r="1.8" stroke="#4d9fff" stroke-width="1.05"/>
        </svg>
      </div>
      <div class="brand-text">
        <h1 id="title">Dashboard laden...</h1>
        <div class="brand-sub">Homelab monitoring</div>
      </div>
    </div>
    <div class="header-right">
      <div class="account-wrap">
        <button class="account-btn" id="account-btn" type="button" aria-haspopup="true" aria-expanded="false">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21a8 8 0 0 0-16 0"/><circle cx="12" cy="7" r="4"/></svg>
          <span id="account-label">Account</span>
        </button>
        <div class="account-menu" id="account-menu" hidden>
          <div class="account-menu-head">
            <span class="account-menu-name" id="account-menu-name">—</span>
            <span class="account-menu-role" id="account-menu-role">Dashboard account</span>
          </div>
          <button type="button" class="account-menu-item" id="account-users-btn" hidden>Gebruikers beheren</button>
          <button type="button" class="account-menu-item" id="account-password-btn">Wachtwoord wijzigen</button>
          <button type="button" class="account-menu-item account-menu-danger" id="account-logout-btn">Uitloggen</button>
        </div>
      </div>
      <span id="clock" class="badge">--:--:--</span>
      <span id="conn" class="badge">verbinden...</span>
    </div>
  </header>

  <div class="app-shell">
    <main class="content">
      <div id="view-logs" class="view active">
        <div class="layout">
          <div class="page-header">
            <div>
              <h2 class="page-title" id="page-title">Proxmox</h2>
              <p class="page-sub" id="page-sub">Node logs en VZDump taken</p>
            </div>
          </div>

          <section class="status-widget">
            <div class="status-widget-head">
              <div class="status-widget-title">
                <span class="status-dot busy" id="status-dot"></span>
                <span id="status-label">Status</span>
              </div>
              <div class="panel-actions">
                <button class="btn btn-sm" id="status-edit-btn" type="button" style="display:none">Instellen</button>
                <span class="status-updated" id="updated">--</span>
              </div>
            </div>
            <pre id="status" class="status-card busy">Status laden...</pre>
          </section>

          <div id="panels" class="grid"></div>
        </div>
      </div>

      <div id="view-ssh" class="view">
        <div class="layout">
          <div class="page-header">
            <div>
              <h2 class="page-title">SSH terminal</h2>
              <p class="page-sub">Verbind via je desktop — keys blijven lokaal</p>
            </div>
          </div>
          <section class="terminal-wrap">
            <div class="terminal-head">
              <div>
                <div class="terminal-title" id="ssh-title">Selecteer een host</div>
                <div class="terminal-desc" id="ssh-desc">SSH terminals via browser</div>
              </div>
              <div class="panel-actions">
                <span class="panel-badge" id="ssh-badge">idle</span>
              </div>
            </div>
            <div class="terminal-body"><div id="terminal"></div></div>
          </section>
        </div>
      </div>
    </main>

    <aside class="sidebar">
      <div class="sidebar-section">
        <div class="sidebar-label">Navigatie</div>
        <div class="tabs">
          <button class="tab active" data-view="logs">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg>
            Logs
          </button>
          <button class="tab" data-view="ssh">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M6 21h12"/><path d="M12 17v4"/></svg>
            SSH
          </button>
        </div>
      </div>

      <div class="sidebar-section visible" id="sidebar-logs">
        <div class="sidebar-label">Categorieën</div>
        <div class="log-categories" id="log-categories"></div>
        <button class="btn btn-add" id="btn-cat-add" type="button" style="display:none">+ Categorie toevoegen</button>
        <button class="btn btn-add" id="btn-add" style="margin-top:.5rem">+ Panel toevoegen</button>
      </div>

      <div class="sidebar-divider"></div>

      <div class="sidebar-section" id="sidebar-ssh">
        <div class="sidebar-label">SSH hosts</div>
        <div class="host-list" id="host-list"></div>
        <button class="btn btn-add" id="btn-add-ssh">+ Host toevoegen</button>
        <div class="sidebar-actions">
          <button class="btn" id="ssh-reconnect">Herverbinden</button>
        </div>
      </div>

      <div class="sidebar-section meta">
        <div class="meta-row"><span>Verversing</span><span id="interval">--</span></div>
        <div class="meta-row"><span>Panels</span><span id="panel-count">--</span></div>
        <div class="meta-row"><span>SSH hosts</span><span id="ssh-count">--</span></div>
        <div class="meta-row"><span>Bridge</span><span id="local-bridge">--</span></div>
      </div>
    </aside>
  </div>

  <div class="auth-gate" id="login-gate">
    <div class="auth-card" id="login-pane">
      <div class="auth-card-head">
        <h3 class="auth-card-title">Inloggen</h3>
        <p class="auth-card-sub">Log in om het dashboard te gebruiken</p>
      </div>
      <div class="auth-card-body">
        <div class="field">
          <label for="login-user">Gebruiker</label>
          <input id="login-user" type="text" autocomplete="username" value="admin">
        </div>
        <div class="field">
          <label for="login-pass">Wachtwoord</label>
          <input id="login-pass" type="password" autocomplete="current-password">
        </div>
        <div class="form-error" id="login-error"></div>
        <div class="modal-actions" style="padding-top:0;justify-content:space-between">
          <button class="btn" id="login-forgot" type="button" hidden>Wachtwoord vergeten?</button>
          <button class="btn btn-primary" id="login-submit" type="button">Inloggen</button>
        </div>
      </div>
    </div>
    <div class="auth-card" id="forgot-pane" hidden>
      <div class="auth-card-head">
        <h3 class="auth-card-title">Wachtwoord vergeten</h3>
        <p class="auth-card-sub">Vul je e-mailadres in — je krijgt een reset-link</p>
      </div>
      <div class="auth-card-body">
        <div class="field">
          <label for="forgot-email">E-mail</label>
          <input id="forgot-email" type="email" autocomplete="email">
        </div>
        <div class="form-error" id="forgot-error"></div>
        <div class="modal-actions" style="padding-top:0;justify-content:space-between">
          <button class="btn" id="forgot-back" type="button">Terug</button>
          <button class="btn btn-primary" id="forgot-submit" type="button">Verstuur link</button>
        </div>
      </div>
    </div>
    <div class="auth-card" id="reset-pane" hidden>
      <div class="auth-card-head">
        <h3 class="auth-card-title">Nieuw wachtwoord</h3>
        <p class="auth-card-sub">Kies een nieuw wachtwoord voor je account</p>
      </div>
      <div class="auth-card-body">
        <div class="field">
          <label for="reset-pass">Nieuw wachtwoord</label>
          <input id="reset-pass" type="password" autocomplete="new-password">
        </div>
        <div class="field">
          <label for="reset-pass2">Bevestig wachtwoord</label>
          <input id="reset-pass2" type="password" autocomplete="new-password">
        </div>
        <div class="form-error" id="reset-error"></div>
        <div class="modal-actions" style="padding-top:0">
          <button class="btn btn-primary" id="reset-submit" type="button">Wachtwoord opslaan</button>
        </div>
      </div>
    </div>
  </div>
  <script>
    (function () {
      var params = new URLSearchParams(location.search);
      var token = (params.get("reset") || params.get("token") || "").trim();
      if (!token) return;
      function showResetPane() {
        var gate = document.getElementById("login-gate");
        var loginPane = document.getElementById("login-pane");
        var forgotPane = document.getElementById("forgot-pane");
        var resetPane = document.getElementById("reset-pane");
        if (gate) gate.classList.add("open");
        if (loginPane) loginPane.hidden = true;
        if (forgotPane) forgotPane.hidden = true;
        if (resetPane) resetPane.hidden = false;
      }
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", showResetPane);
      } else {
        showResetPane();
      }
    })();
  </script>

  <div class="modal-backdrop" id="users-modal">
    <div class="modal" style="width:min(720px,100%)">
      <div class="modal-head">
        <div class="modal-title">Gebruikers</div>
        <button class="btn btn-sm" id="users-close" type="button">Sluiten</button>
      </div>
      <div class="modal-body">
        <div class="help" style="margin:0">Rollen: <b>Beheerder</b> = alles incl. gebruikers · <b>Operator</b> = logs/SSH/beheer · <b>Alleen lezen</b> = alleen bekijken · <b>Kick</b> = gebruiker uitloggen</div>
        <div class="form-error" id="users-error"></div>
        <div id="users-list" class="host-list"></div>
        <div class="sidebar-divider"></div>
        <div class="field">
          <label for="user-new-name">Nieuwe gebruiker</label>
          <input id="user-new-name" type="text" placeholder="bijv. stef" autocomplete="off">
        </div>
        <div class="field">
          <label for="user-new-email">E-mail (voor wachtwoord-reset)</label>
          <input id="user-new-email" type="email" placeholder="naam@voorbeeld.nl" autocomplete="off">
        </div>
        <div class="field-row">
          <div class="field">
            <label for="user-new-pass">Wachtwoord</label>
            <input id="user-new-pass" type="password" autocomplete="new-password">
          </div>
          <div class="field">
            <label for="user-new-role">Rol</label>
            <select id="user-new-role"></select>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn btn-primary" id="users-create" type="button">Gebruiker aanmaken</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="password-modal">
    <div class="modal">
      <div class="modal-head">
        <div class="modal-title">Wachtwoord wijzigen</div>
        <button class="btn btn-sm" id="password-close" type="button">Sluiten</button>
      </div>
      <div class="modal-body">
        <div class="field">
          <label for="password-current">Huidig wachtwoord</label>
          <input id="password-current" type="password" autocomplete="current-password">
        </div>
        <div class="field">
          <label for="password-new">Nieuw wachtwoord</label>
          <input id="password-new" type="password" autocomplete="new-password">
        </div>
        <div class="field">
          <label for="password-confirm">Bevestig nieuw wachtwoord</label>
          <input id="password-confirm" type="password" autocomplete="new-password">
        </div>
        <div class="form-error" id="password-error"></div>
        <div class="modal-actions">
          <button class="btn btn-primary" id="password-submit" type="button">Opslaan</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="cat-add-modal">
    <div class="modal">
      <div class="modal-head">
        <div class="modal-title">Categorie toevoegen</div>
        <button class="btn btn-sm" id="cat-add-close" type="button">Sluiten</button>
      </div>
      <div class="modal-body">
        <form id="form-cat-add" autocomplete="off">
          <div class="field">
            <label>Naam</label>
            <input name="title" id="cat-add-name" placeholder="Bijv. Netwerk" required>
          </div>
          <div class="field">
            <label>ID (optioneel)</label>
            <input name="id" id="cat-add-id" placeholder="netwerk" pattern="[a-z0-9][a-z0-9-]*">
            <small>Leeg = automatisch uit naam. Alleen kleine letters, cijfers en streepjes.</small>
          </div>
          <div class="field">
            <label>Ondertitel</label>
            <input name="sub" id="cat-add-sub" placeholder="Korte beschrijving">
          </div>
        </form>
        <div class="form-error" id="cat-add-error"></div>
      </div>
      <div class="modal-footer">
        <div class="modal-actions">
          <button class="btn" id="cat-add-cancel" type="button">Annuleren</button>
          <button class="btn btn-primary" id="cat-add-save" type="button">Toevoegen</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="cat-edit-modal">
    <div class="modal">
      <div class="modal-head">
        <div class="modal-title" id="cat-edit-title">Categorie bewerken</div>
        <button class="btn btn-sm" id="cat-edit-close" type="button">Sluiten</button>
      </div>
      <div class="modal-body">
        <form id="form-cat-edit" autocomplete="off">
          <input type="hidden" name="id" id="cat-edit-id">
          <div class="field">
            <label>Naam</label>
            <input name="title" id="cat-edit-name" required>
          </div>
          <div class="field">
            <label>Ondertitel</label>
            <input name="sub" id="cat-edit-sub" placeholder="Korte beschrijving">
          </div>
        </form>
        <div class="form-error" id="cat-edit-error"></div>
      </div>
      <div class="modal-footer">
        <div class="modal-actions">
          <button class="btn" id="cat-edit-cancel" type="button">Annuleren</button>
          <button class="btn btn-primary" id="cat-edit-save" type="button">Opslaan</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="status-edit-modal">
    <div class="modal">
      <div class="modal-head">
        <div class="modal-title">Statusbalk instellen</div>
        <button class="btn btn-sm" id="status-edit-close" type="button">Sluiten</button>
      </div>
      <div class="modal-body">
        <form id="form-status-edit" autocomplete="off">
          <div class="field">
            <label>Titel</label>
            <input name="label" id="status-edit-label" placeholder="Status" required>
          </div>
          <div class="field">
            <label>Proxmox host / IP</label>
            <input name="proxmox_host" id="status-edit-proxmox" placeholder="minilab of 10.0.30.3">
            <small>Online-check (8006), VM/LXC-telling en backup-status via SSH. Leeg = overslaan.</small>
          </div>
          <div class="field">
            <label>PBS host / IP</label>
            <input name="pbs_host" id="status-edit-pbs" placeholder="pbs of 10.0.30.5">
            <small>Online-check op poort 8007. Leeg = overslaan.</small>
          </div>
          <div class="field">
            <label>Verversing (seconden)</label>
            <input name="interval_seconds" id="status-edit-interval" type="number" min="2" max="300" value="5">
          </div>
        </form>
        <div class="form-error" id="status-edit-error"></div>
      </div>
      <div class="modal-footer">
        <div class="modal-actions">
          <button class="btn" id="status-edit-cancel" type="button">Annuleren</button>
          <button class="btn btn-primary" id="status-edit-save" type="button">Opslaan</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="panel-edit-modal">
    <div class="modal">
      <div class="modal-head">
        <div class="modal-title" id="panel-edit-title">Panel bewerken</div>
        <button class="btn btn-sm" id="panel-edit-close" type="button">Sluiten</button>
      </div>
      <div class="modal-body">
        <form id="form-panel-edit" autocomplete="off">
          <input type="hidden" name="id" id="panel-edit-id">
          <div class="field">
            <label>Titel</label>
            <input name="title" id="panel-edit-name" required>
          </div>
          <div class="field">
            <label>Beschrijving</label>
            <input name="description" id="panel-edit-desc" placeholder="Optioneel">
          </div>
          <div class="field-row">
            <div class="field">
              <label>Categorie</label>
              <select name="category" id="panel-edit-category"></select>
            </div>
            <div class="field">
              <label>Hoogte (px)</label>
              <input name="height" id="panel-edit-height" type="number" min="120" max="800" value="220">
            </div>
          </div>
        </form>
        <div class="form-error" id="panel-edit-error"></div>
      </div>
      <div class="modal-footer">
        <div class="modal-actions">
          <button class="btn" id="panel-edit-cancel" type="button">Annuleren</button>
          <button class="btn btn-primary" id="panel-edit-save" type="button">Opslaan</button>
        </div>
      </div>
    </div>
  </div>

  <div class="modal-backdrop" id="add-modal">
    <div class="modal modal-wide">
      <div class="modal-head">
        <div class="modal-title" id="add-modal-title">Log panel toevoegen</div>
        <button class="btn btn-sm" id="add-close" type="button">Sluiten</button>
      </div>
      <div class="modal-body">
        <div class="modal-tabs">
          <button class="modal-tab active" type="button" data-pane="log">Log panel</button>
          <button class="modal-tab" type="button" data-pane="ssh">SSH host</button>
        </div>

        <form id="form-log" class="form-pane active" autocomplete="off">
          <div class="form-section">
            <p class="form-section-title">Basis</p>
            <div class="field">
              <label>Titel</label>
              <input name="title" placeholder="Bijv. AdGuard logs" required>
            </div>
            <div class="field-row">
              <div class="field">
                <label>ID (optioneel)</label>
                <input name="id" placeholder="adguard-logs">
              </div>
              <div class="field">
                <label>Hoogte (px)</label>
                <input name="height" type="number" value="220" min="120" max="800">
              </div>
            </div>
            <div class="field">
              <label>Beschrijving</label>
              <input name="description" placeholder="Optioneel">
            </div>
            <input type="hidden" name="category" id="log-category-input">
          </div>

          <div class="form-section">
            <p class="form-section-title">Logbron</p>
            <div class="field">
              <label>Bron</label>
              <select name="source" id="log-source-select">
                <option value="proxmox">Proxmox node (SSH)</option>
                <option value="docker">Docker (lokaal of via SSH)</option>
                <option value="remote">PBS / andere host (SSH)</option>
                <option value="command">Eigen commando</option>
              </select>
            </div>
            <div class="source-hint" id="log-source-hint">
              Verbind met een Proxmox node via SSH — node logs of een LXC container.
            </div>
            <div id="log-ssh-fields">
              <div class="field-row">
                <div class="field">
                  <label>Host / hostname</label>
                  <input name="log_host" id="log-ssh-host" placeholder="proxmox.lan" autocomplete="off">
                  <small>Of SSH-alias uit ~/.ssh/config</small>
                </div>
                <div class="field">
                  <label>Poort (optioneel)</label>
                  <input name="log_port" id="log-ssh-port" type="number" min="1" max="65535" placeholder="22" autocomplete="off">
                </div>
              </div>
              <div class="field-row">
                <div class="field">
                  <label>Gebruiker</label>
                  <input name="log_user" id="log-ssh-user" placeholder="root" autocomplete="off">
                </div>
                <div class="field">
                  <label>SSH key pad</label>
                  <input name="log_key_file" id="log-ssh-key" placeholder="~/.ssh/id_ed25519" autocomplete="off">
                  <small>Leeg = automatisch via ~/.ssh/config of id_ed25519</small>
                </div>
              </div>
            </div>
            <div class="field" id="log-container-field">
              <label>LXC container (optioneel)</label>
              <select name="ctid" id="log-ctid-select">
                <option value="">Node logs (geen container)</option>
              </select>
              <small>Lijst via <code>pct list</code> op de gekozen node — leeg = journalctl op de node zelf</small>
            </div>
            <div class="field" id="log-docker-field" style="display:none">
              <label>Docker container</label>
              <select name="docker_container" id="log-docker-select" required>
                <option value="">Laden...</option>
              </select>
              <small>Lijst via <code>docker ps</code> — host leeg = minilab, anders via SSH</small>
            </div>
            <div class="field" id="field-units">
              <label>systemd unit(s)</label>
              <select name="unit_pick" id="log-unit-select" multiple size="5">
                <option value="">Kies eerst een bron/container...</option>
              </select>
              <input name="units" id="log-units-input" placeholder="Extra units, komma-gescheiden (optioneel)">
              <small id="log-units-hint">Ctrl+klik voor meerdere units. Lijst wordt live opgehaald.</small>
            </div>
            <div class="field" id="field-command" style="display:none">
              <label>Commando</label>
              <textarea name="command" placeholder="tail -n 50 -F /var/log/syslog"></textarea>
            </div>
          </div>
        </form>

        <form id="form-ssh" class="form-pane" autocomplete="off">
          <div class="form-section">
            <p class="form-section-title">Basis</p>
            <div class="field">
              <label>Titel</label>
              <input name="title" placeholder="Bijv. Caddy server" required>
            </div>
            <div class="field-row">
              <div class="field">
                <label>ID (optioneel)</label>
                <input name="id" placeholder="caddy">
              </div>
              <div class="field">
                <label>Type</label>
                <select name="type">
                  <option value="ssh">SSH naar IP</option>
                  <option value="local">Lokale shell (deze pc)</option>
                </select>
              </div>
            </div>
            <div class="field">
              <label>Beschrijving</label>
              <input name="description" placeholder="Optioneel">
            </div>
          </div>
          <div class="form-section" id="ssh-remote-fields">
            <p class="form-section-title">Verbinding</p>
            <div class="field-row">
              <div class="field">
                <label>Host / hostname</label>
                <input name="ssh_host" placeholder="proxmox.lan" autocomplete="off">
                <small>Of SSH-alias uit ~/.ssh/config</small>
              </div>
              <div class="field">
                <label>Poort (optioneel)</label>
                <input name="ssh_port" type="number" min="1" max="65535" placeholder="22" autocomplete="off">
              </div>
            </div>
            <div class="field-row">
              <div class="field">
                <label>Gebruiker</label>
                <input name="ssh_user" placeholder="root" autocomplete="off">
              </div>
              <div class="field">
                <label>Authenticatie</label>
                <select name="ssh_auth">
                  <option value="key">SSH key</option>
                  <option value="password">Wachtwoord</option>
                </select>
              </div>
            </div>
            <div class="field" id="ssh-key-field">
              <label>SSH key pad</label>
              <input name="ssh_key_file" placeholder="~/.ssh/id_ed25519" autocomplete="off">
              <small>Leeg = automatisch via ~/.ssh/config of id_ed25519</small>
            </div>
            <div class="field" id="ssh-password-field" style="display:none">
              <label>Wachtwoord</label>
              <input name="ssh_password" type="password" placeholder="SSH wachtwoord" autocomplete="new-password">
              <small>Wordt lokaal in je browser opgeslagen</small>
            </div>
          </div>
        </form>

        <div class="form-error" id="add-error"></div>
      </div>
      <div class="modal-footer">
        <div class="modal-actions">
          <button class="btn" type="button" id="add-cancel">Annuleer</button>
          <button class="btn btn-primary" type="button" id="add-save">Opslaan</button>
        </div>
      </div>
    </div>
  </div>

  <script src="/static/xterm.min.js"></script>
  <script src="/static/addon-fit.min.js"></script>
  <script>
    const statusEl = document.getElementById("status");
    const statusDotEl = document.getElementById("status-dot");
    const titleEl = document.getElementById("title");
    const clockEl = document.getElementById("clock");
    const connEl = document.getElementById("conn");
    const updatedEl = document.getElementById("updated");
    const intervalEl = document.getElementById("interval");
    const statusLabelEl = document.getElementById("status-label");
    const panelCountEl = document.getElementById("panel-count");
    const sshCountEl = document.getElementById("ssh-count");
    const panelsEl = document.getElementById("panels");
    const logCategoriesEl = document.getElementById("log-categories");
    const sidebarLogsEl = document.getElementById("sidebar-logs");
    const pageTitleEl = document.getElementById("page-title");
    const pageSubEl = document.getElementById("page-sub");
    const hostListEl = document.getElementById("host-list");
    const sshTitleEl = document.getElementById("ssh-title");
    const sshDescEl = document.getElementById("ssh-desc");
    const sshBadgeEl = document.getElementById("ssh-badge");
    const sshReconnectEl = document.getElementById("ssh-reconnect");
    const sidebarSshEl = document.getElementById("sidebar-ssh");
    const addModalEl = document.getElementById("add-modal");
    const addErrorEl = document.getElementById("add-error");
    const formLogEl = document.getElementById("form-log");
    const formSshEl = document.getElementById("form-ssh");
    const fieldUnitsEl = document.getElementById("field-units");
    const fieldCommandEl = document.getElementById("field-command");
    const logSshFieldsEl = document.getElementById("log-ssh-fields");
    const logContainerFieldEl = document.getElementById("log-container-field");
    const logDockerFieldEl = document.getElementById("log-docker-field");
    const logDockerSelectEl = document.getElementById("log-docker-select");
    const logSshHostEl = document.getElementById("log-ssh-host");
    const logSshPortEl = document.getElementById("log-ssh-port");
    const logSshUserEl = document.getElementById("log-ssh-user");
    const logSshKeyEl = document.getElementById("log-ssh-key");
    const logCtidSelectEl = document.getElementById("log-ctid-select");
    const logUnitSelectEl = document.getElementById("log-unit-select");
    const logUnitsInputEl = document.getElementById("log-units-input");
    const logUnitsHintEl = document.getElementById("log-units-hint");
    const sshRemoteFieldsEl = document.getElementById("ssh-remote-fields");
    const localBridgeEl = document.getElementById("local-bridge");
    const accountBtnEl = document.getElementById("account-btn");
    const accountMenuEl = document.getElementById("account-menu");
    const accountLabelEl = document.getElementById("account-label");
    const accountMenuNameEl = document.getElementById("account-menu-name");
    const accountMenuRoleEl = document.getElementById("account-menu-role");
    const accountUsersBtnEl = document.getElementById("account-users-btn");
    const accountPasswordBtnEl = document.getElementById("account-password-btn");
    const accountLogoutBtnEl = document.getElementById("account-logout-btn");
    const usersModalEl = document.getElementById("users-modal");
    const usersListEl = document.getElementById("users-list");
    const usersErrorEl = document.getElementById("users-error");
    const userNewNameEl = document.getElementById("user-new-name");
    const userNewPassEl = document.getElementById("user-new-pass");
    const userNewRoleEl = document.getElementById("user-new-role");
    const usersCreateEl = document.getElementById("users-create");
    const usersCloseEl = document.getElementById("users-close");
    const btnAddPanelEl = document.getElementById("btn-add");
    const btnAddSshEl = document.getElementById("btn-add-ssh");
    const btnCatAddEl = document.getElementById("btn-cat-add");
    const catEditModalEl = document.getElementById("cat-edit-modal");
    const catEditErrorEl = document.getElementById("cat-edit-error");
    const formCatEditEl = document.getElementById("form-cat-edit");
    const panelEditModalEl = document.getElementById("panel-edit-modal");
    const panelEditErrorEl = document.getElementById("panel-edit-error");
    const formPanelEditEl = document.getElementById("form-panel-edit");
    const panelEditCategoryEl = document.getElementById("panel-edit-category");
    const statusEditBtnEl = document.getElementById("status-edit-btn");
    const statusEditModalEl = document.getElementById("status-edit-modal");
    const statusEditErrorEl = document.getElementById("status-edit-error");
    const formStatusEditEl = document.getElementById("form-status-edit");
    const catAddModalEl = document.getElementById("cat-add-modal");
    const catAddErrorEl = document.getElementById("cat-add-error");
    const formCatAddEl = document.getElementById("form-cat-add");
    const sshTabEl = document.querySelector('.tab[data-view="ssh"]');
    const loginGateEl = document.getElementById("login-gate");
    const loginPaneEl = document.getElementById("login-pane");
    const forgotPaneEl = document.getElementById("forgot-pane");
    const resetPaneEl = document.getElementById("reset-pane");
    const loginUserEl = document.getElementById("login-user");
    const loginPassEl = document.getElementById("login-pass");
    const loginErrorEl = document.getElementById("login-error");
    const loginSubmitEl = document.getElementById("login-submit");
    const loginForgotEl = document.getElementById("login-forgot");
    const forgotEmailEl = document.getElementById("forgot-email");
    const forgotErrorEl = document.getElementById("forgot-error");
    const forgotSubmitEl = document.getElementById("forgot-submit");
    const forgotBackEl = document.getElementById("forgot-back");
    const resetPassEl = document.getElementById("reset-pass");
    const resetPass2El = document.getElementById("reset-pass2");
    const resetErrorEl = document.getElementById("reset-error");
    const resetSubmitEl = document.getElementById("reset-submit");
    const userNewEmailEl = document.getElementById("user-new-email");
    let pendingResetToken = null;
    const passwordModalEl = document.getElementById("password-modal");
    const passwordCurrentEl = document.getElementById("password-current");
    const passwordNewEl = document.getElementById("password-new");
    const passwordConfirmEl = document.getElementById("password-confirm");
    const passwordErrorEl = document.getElementById("password-error");
    const passwordSubmitEl = document.getElementById("password-submit");
    const passwordCloseEl = document.getElementById("password-close");

    const LOCAL_WS_PORT = 8767;
    const SSH_STORAGE_KEY = "homelab-ssh-hosts-v1";

    let config = null;
    let sshHosts = [];
    let activeAddPane = "log";
    let editingSshId = null;
    let statusTimer = null;
    const panelViews = new Map();
    let activeLogCategory = "proxmox";
    let term = null;
    let fitAddon = null;
    let sshSocket = null;
    let activeHost = null;
    let authState = {
      auth_enabled: false,
      logged_in: true,
      username: null,
      role: null,
      role_label: null,
      permissions: {
        view_logs: true,
        use_ssh: true,
        manage_panels: true,
        manage_ssh: true,
        manage_users: true,
      },
      roles: [],
    };

    async function apiFetch(url, options = {}) {
      const res = await fetch(url, { credentials: "same-origin", ...options });
      if (res.status === 401) {
        authState.logged_in = false;
        updateAccountUi();
        applyPermissions();
        showLoginGate(true);
        throw new Error("Niet ingelogd");
      }
      if (res.status === 403) {
        throw new Error("Onvoldoende rechten");
      }
      return res;
    }

    function applyPermissions() {
      const p = authState.permissions || {};
      const enabled = !!authState.auth_enabled;
      const loggedIn = !enabled || !!authState.logged_in;
      if (btnAddPanelEl) btnAddPanelEl.style.display = loggedIn && p.manage_panels ? "" : "none";
      if (btnCatAddEl) btnCatAddEl.style.display = loggedIn && p.manage_panels ? "" : "none";
      if (statusEditBtnEl) statusEditBtnEl.style.display = loggedIn && p.manage_panels ? "" : "none";
      if (btnAddSshEl) btnAddSshEl.style.display = loggedIn && p.manage_ssh ? "" : "none";
      if (sshTabEl) sshTabEl.style.display = loggedIn && p.use_ssh ? "" : "none";
      if (accountUsersBtnEl) accountUsersBtnEl.hidden = !(loggedIn && p.manage_users);
      if (!p.use_ssh && document.getElementById("view-ssh")?.classList.contains("active")) {
        switchView("logs");
      }
    }

    function updateAccountUi() {
      const enabled = !!authState.auth_enabled;
      const loggedIn = !enabled || !!authState.logged_in;
      accountBtnEl.style.display = enabled ? "inline-flex" : "none";
      if (!enabled) return;
      const name = authState.username || "Gast";
      accountLabelEl.textContent = loggedIn ? name : "Inloggen";
      accountMenuNameEl.textContent = loggedIn ? name : "Niet ingelogd";
      accountMenuRoleEl.textContent = loggedIn
        ? (authState.role_label || "Dashboard account")
        : "Niet ingelogd";
      accountPasswordBtnEl.disabled = !loggedIn;
      accountLogoutBtnEl.disabled = !loggedIn;
      applyPermissions();
    }

    function showAuthPane(name) {
      loginPaneEl.hidden = name !== "login";
      forgotPaneEl.hidden = name !== "forgot";
      resetPaneEl.hidden = name !== "reset";
    }

    function showLoginGate(open, pane = "login") {
      if (open && pendingResetToken) pane = "reset";
      loginGateEl.classList.toggle("open", open);
      loginErrorEl.textContent = "";
      forgotErrorEl.textContent = "";
      if (!open || pane !== "reset") resetErrorEl.textContent = "";
      if (open) {
        showAuthPane(pane);
        if (pane === "login") loginPassEl.focus();
        if (pane === "forgot") forgotEmailEl.focus();
        if (pane === "reset") {
          resetSubmitEl.disabled = false;
          resetPassEl.focus();
          validateResetToken();
        }
      }
    }

    function readResetTokenFromUrl() {
      const params = new URLSearchParams(location.search);
      const token = (params.get("reset") || params.get("token") || "").trim();
      return token || null;
    }

    async function validateResetToken() {
      if (!pendingResetToken) return;
      resetErrorEl.style.color = "";
      try {
        const res = await fetch(
          "/api/auth/reset-info?token=" + encodeURIComponent(pendingResetToken)
        );
        const data = await res.json();
        if (!res.ok || !data.ok) {
          resetErrorEl.textContent = data.error || "Reset-link ongeldig of verlopen";
          resetSubmitEl.disabled = true;
          return;
        }
        resetErrorEl.style.color = "var(--muted)";
        resetErrorEl.textContent = "Account: " + (data.username || "");
      } catch (err) {
        resetErrorEl.textContent = "Kon reset-link niet controleren: " + err.message;
        resetSubmitEl.disabled = true;
      }
    }

    function updateForgotButton() {
      loginForgotEl.hidden = !authState.password_reset_enabled;
    }

    function toggleAccountMenu(open) {
      const shouldOpen = open ?? accountMenuEl.hidden;
      accountMenuEl.hidden = !shouldOpen;
      accountBtnEl.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
    }

    function startSessionWatch() {
      if (sessionWatchTimer) clearInterval(sessionWatchTimer);
      sessionWatchTimer = null;
      if (!authState.auth_enabled) return;
      sessionWatchTimer = setInterval(async () => {
        if (!authState.logged_in || pendingResetToken) return;
        try {
          const res = await fetch("/api/auth/status", { credentials: "same-origin" });
          const data = await res.json();
          if (!data.logged_in && authState.logged_in) {
            authState.logged_in = false;
            authState.username = null;
            updateAccountUi();
            applyPermissions();
            showLoginGate(true);
            loginErrorEl.style.color = "";
            loginErrorEl.textContent = "Je sessie is beëindigd door een beheerder.";
            if (statusTimer) {
              clearInterval(statusTimer);
              statusTimer = null;
            }
          }
        } catch (_) {}
      }, 15000);
    }

    async function loadAuthStatus() {
      const res = await fetch("/api/auth/status", { credentials: "same-origin" });
      authState = await res.json();
      updateForgotButton();
      updateAccountUi();
      if (pendingResetToken) {
        showLoginGate(true, "reset");
        return false;
      }
      if (authState.auth_enabled && !authState.logged_in) {
        showLoginGate(true, "login");
        return false;
      }
      showLoginGate(false);
      startSessionWatch();
      return true;
    }

    async function submitForgotPassword() {
      forgotErrorEl.textContent = "";
      forgotErrorEl.style.color = "";
      try {
        const res = await fetch("/api/auth/forgot-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: forgotEmailEl.value.trim() }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          forgotErrorEl.textContent = data.error || "Versturen mislukt";
          return;
        }
        forgotErrorEl.style.color = "var(--ok)";
        forgotErrorEl.textContent = data.message || "Reset-link verstuurd — check je inbox.";
      } catch (err) {
        forgotErrorEl.textContent = err.message;
      }
    }

    async function submitResetPassword() {
      resetErrorEl.textContent = "";
      if (resetPassEl.value !== resetPass2El.value) {
        resetErrorEl.textContent = "Wachtwoorden komen niet overeen";
        return;
      }
      try {
        const res = await fetch("/api/auth/reset-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            token: pendingResetToken,
            password: resetPassEl.value,
          }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          resetErrorEl.textContent = data.error || "Reset mislukt";
          return;
        }
        pendingResetToken = null;
        history.replaceState(null, "", location.pathname);
        loginUserEl.value = data.username || "";
        resetPassEl.value = "";
        resetPass2El.value = "";
        showLoginGate(true, "login");
        loginErrorEl.style.color = "var(--ok)";
        loginErrorEl.textContent = "Wachtwoord gewijzigd — log nu in.";
      } catch (err) {
        resetErrorEl.textContent = err.message;
      }
    }

    async function submitLogin() {
      loginErrorEl.textContent = "";
      try {
        const res = await fetch("/api/auth/login", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: loginUserEl.value.trim(),
            password: loginPassEl.value,
          }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          loginErrorEl.textContent = data.error || "Inloggen mislukt";
          return;
        }
        authState = {
          auth_enabled: true,
          logged_in: true,
          username: data.username,
          role: data.role,
          role_label: data.role_label,
          permissions: data.permissions || authState.permissions,
          roles: authState.roles,
        };
        loginPassEl.value = "";
        updateAccountUi();
        showLoginGate(false);
        startSessionWatch();
        await bootDashboard();
      } catch (err) {
        loginErrorEl.textContent = "Inloggen mislukt: " + err.message;
      }
    }

    async function logoutAccount() {
      if (sessionWatchTimer) {
        clearInterval(sessionWatchTimer);
        sessionWatchTimer = null;
      }
      await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      authState.logged_in = false;
      authState.username = null;
      updateAccountUi();
      toggleAccountMenu(false);
      showLoginGate(true);
      if (statusTimer) clearInterval(statusTimer);
      statusTimer = null;
    }

    function openPasswordModal() {
      passwordCurrentEl.value = "";
      passwordNewEl.value = "";
      passwordConfirmEl.value = "";
      passwordErrorEl.textContent = "";
      passwordModalEl.classList.add("open");
      toggleAccountMenu(false);
    }

    function closePasswordModal() {
      passwordModalEl.classList.remove("open");
    }

    async function submitPasswordChange() {
      passwordErrorEl.textContent = "";
      if (passwordNewEl.value !== passwordConfirmEl.value) {
        passwordErrorEl.textContent = "Nieuwe wachtwoorden komen niet overeen";
        return;
      }
      try {
        const res = await apiFetch("/api/auth/password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            current_password: passwordCurrentEl.value,
            new_password: passwordNewEl.value,
          }),
        });
        const data = await res.json();
        if (!data.ok) {
          passwordErrorEl.textContent = data.error || "Opslaan mislukt";
          return;
        }
        closePasswordModal();
      } catch (err) {
        passwordErrorEl.textContent = err.message;
      }
    }

    function fillRoleSelect(selectEl, selected) {
      const roles = authState.roles?.length
        ? authState.roles
        : [
            { id: "admin", label: "Beheerder" },
            { id: "operator", label: "Operator" },
            { id: "viewer", label: "Alleen lezen" },
          ];
      selectEl.textContent = "";
      roles.forEach((role) => {
        const opt = document.createElement("option");
        opt.value = role.id;
        opt.textContent = role.label;
        if (role.id === selected) opt.selected = true;
        selectEl.appendChild(opt);
      });
    }

    let togglePendingUser = null;
    let deletePendingUser = null;
    let kickPendingUser = null;
    let sessionWatchTimer = null;

    function showUsersError(message, color) {
      usersErrorEl.style.color = color || "";
      usersErrorEl.textContent = message || "";
      if (message) usersErrorEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    function renderUsersList(users) {
      usersListEl.textContent = "";
      if (!users.length) {
        usersListEl.innerHTML = '<div class="help">Nog geen gebruikers.</div>';
        return;
      }
      users.forEach((user) => {
        const item = document.createElement("div");
        item.className = "host-item";

        const info = document.createElement("div");
        info.className = "host-btn";
        info.style.cursor = "default";
        info.innerHTML =
          `<span>${user.username}</span>` +
          `<small style="display:block;color:var(--muted);font-size:.68rem;margin-top:.15rem">` +
          `${user.role_label || user.role}${user.enabled ? "" : " · uitgeschakeld"}` +
          `${user.email ? " · " + user.email : ""}` +
          `</small>`;

        const emailInput = document.createElement("input");
        emailInput.type = "email";
        emailInput.className = "btn";
        emailInput.style.width = "100%";
        emailInput.style.marginTop = ".35rem";
        emailInput.placeholder = "e-mail voor reset";
        emailInput.value = user.email || "";
        emailInput.addEventListener("change", async () => {
          showUsersError("");
          try {
            const body = { username: user.username, email: emailInput.value.trim() };
            const res = await apiFetch("/api/users/update", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!data.ok) throw new Error(data.error || "Opslaan mislukt");
          } catch (err) {
            showUsersError(err.message);
          }
        });
        info.appendChild(emailInput);

        const actions = document.createElement("div");
        actions.className = "host-item-actions";

        const roleSel = document.createElement("select");
        roleSel.className = "btn";
        roleSel.style.width = "auto";
        roleSel.style.padding = ".35rem .45rem";
        fillRoleSelect(roleSel, user.role);
        roleSel.addEventListener("change", async () => {
          showUsersError("");
          try {
            const res = await apiFetch("/api/users/update", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ username: user.username, role: roleSel.value }),
            });
            const data = await res.json();
            if (!data.ok) throw new Error(data.error || "Opslaan mislukt");
            await openUsersModal();
          } catch (err) {
            showUsersError(err.message);
            roleSel.value = user.role;
          }
        });

        const toggleBtn = document.createElement("button");
        toggleBtn.type = "button";
        const isSelfDisable = user.enabled && user.username === authState.username;
        toggleBtn.className = user.enabled
          ? (isSelfDisable ? "btn" : "btn btn-toggle-off")
          : "btn btn-primary";
        toggleBtn.textContent = user.enabled ? "Uit" : "Aan";
        toggleBtn.disabled = isSelfDisable;
        toggleBtn.title = isSelfDisable
          ? "Je kunt je eigen account niet uitschakelen"
          : (user.enabled ? "Account uitschakelen" : "Account inschakelen");
        toggleBtn.addEventListener("click", async (e) => {
          e.preventDefault();
          e.stopPropagation();
          const newEnabled = !user.enabled;
          if (!newEnabled && togglePendingUser !== user.username) {
            togglePendingUser = user.username;
            deletePendingUser = null;
            kickPendingUser = null;
            toggleBtn.textContent = "Zeker?";
            toggleBtn.className = "btn btn-danger";
            showUsersError(`Klik nogmaals op 'Zeker?' om '${user.username}' uit te schakelen.`);
            return;
          }
          togglePendingUser = null;
          showUsersError("");
          toggleBtn.disabled = true;
          const oldLabel = toggleBtn.textContent;
          const oldClass = toggleBtn.className;
          toggleBtn.textContent = "...";
          try {
            const res = await apiFetch("/api/users/update", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ username: user.username, enabled: newEnabled }),
            });
            const data = await res.json();
            if (!res.ok || !data.ok) throw new Error(data.error || "Opslaan mislukt");
            await openUsersModal();
            showUsersError(
              `Account '${user.username}' ${newEnabled ? "ingeschakeld" : "uitgeschakeld"}.`,
              "var(--ok)"
            );
          } catch (err) {
            showUsersError(err.message);
            toggleBtn.disabled = false;
            toggleBtn.textContent = oldLabel;
            toggleBtn.className = oldClass;
          }
        });

        const kickBtn = document.createElement("button");
        kickBtn.type = "button";
        kickBtn.className = "btn";
        kickBtn.textContent = "Kick";
        kickBtn.disabled = user.username === authState.username;
        kickBtn.title = kickBtn.disabled
          ? "Je kunt je eigen sessie niet beëindigen"
          : "Sessie beëindigen (gebruiker wordt uitgelogd)";
        kickBtn.addEventListener("click", async (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (kickPendingUser !== user.username) {
            kickPendingUser = user.username;
            togglePendingUser = null;
            deletePendingUser = null;
            kickBtn.textContent = "Zeker?";
            kickBtn.className = "btn btn-danger";
            showUsersError(`Klik nogmaals op 'Zeker?' om '${user.username}' uit te loggen.`);
            return;
          }
          kickPendingUser = null;
          showUsersError("");
          kickBtn.disabled = true;
          const oldLabel = kickBtn.textContent;
          const oldClass = kickBtn.className;
          kickBtn.textContent = "...";
          try {
            const res = await apiFetch("/api/users/kick", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ username: user.username }),
            });
            const data = await res.json();
            if (!res.ok || !data.ok) throw new Error(data.error || "Kick mislukt");
            await openUsersModal();
            showUsersError(`Gebruiker '${user.username}' is uitgelogd.`, "var(--ok)");
          } catch (err) {
            showUsersError(err.message);
            kickBtn.disabled = user.username === authState.username;
            kickBtn.textContent = oldLabel;
            kickBtn.className = oldClass;
          }
        });

        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "btn btn-danger";
        delBtn.textContent = "wis";
        delBtn.disabled = user.username === authState.username;
        delBtn.addEventListener("click", async (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (deletePendingUser !== user.username) {
            deletePendingUser = user.username;
            togglePendingUser = null;
            kickPendingUser = null;
            delBtn.textContent = "Zeker?";
            showUsersError(`Klik nogmaals op 'Zeker?' om '${user.username}' permanent te verwijderen.`);
            return;
          }
          deletePendingUser = null;
          showUsersError("");
          delBtn.disabled = true;
          const oldLabel = delBtn.textContent;
          delBtn.textContent = "...";
          try {
            const res = await apiFetch("/api/users/delete", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ username: user.username }),
            });
            const data = await res.json();
            if (!res.ok || !data.ok) throw new Error(data.error || "Verwijderen mislukt");
            await openUsersModal();
            showUsersError(`Gebruiker '${user.username}' verwijderd.`, "var(--ok)");
          } catch (err) {
            showUsersError(err.message);
            delBtn.disabled = user.username === authState.username;
            delBtn.textContent = oldLabel;
          }
        });

        actions.append(roleSel, toggleBtn, kickBtn, delBtn);
        item.append(info, actions);
        usersListEl.appendChild(item);
      });
    }

    async function openUsersModal() {
      togglePendingUser = null;
      deletePendingUser = null;
      kickPendingUser = null;
      showUsersError("");
      userNewNameEl.value = "";
      userNewPassEl.value = "";
      fillRoleSelect(userNewRoleEl, "viewer");
      usersModalEl.classList.add("open");
      toggleAccountMenu(false);
      try {
        const res = await apiFetch("/api/users");
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || "Laden mislukt");
        if (data.roles) authState.roles = data.roles;
        fillRoleSelect(userNewRoleEl, "viewer");
        renderUsersList(data.users || []);
      } catch (err) {
        showUsersError(err.message);
      }
    }

    function closeUsersModal() {
      usersModalEl.classList.remove("open");
    }

    async function createUser() {
      showUsersError("");
      try {
        const res = await apiFetch("/api/users/create", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: userNewNameEl.value.trim(),
            password: userNewPassEl.value,
            role: userNewRoleEl.value,
            email: userNewEmailEl.value.trim(),
          }),
        });
        const data = await res.json();
        if (!data.ok) throw new Error(data.error || "Aanmaken mislukt");
        userNewNameEl.value = "";
        userNewPassEl.value = "";
        await openUsersModal();
      } catch (err) {
        showUsersError(err.message);
      }
    }

    accountBtnEl.addEventListener("click", () => {
      if (authState.auth_enabled && !authState.logged_in) {
        showLoginGate(true);
        return;
      }
      toggleAccountMenu();
    });
    accountUsersBtnEl.addEventListener("click", openUsersModal);
    accountPasswordBtnEl.addEventListener("click", openPasswordModal);
    accountLogoutBtnEl.addEventListener("click", logoutAccount);
    usersCreateEl.addEventListener("click", createUser);
    usersCloseEl.addEventListener("click", closeUsersModal);
    usersModalEl.addEventListener("click", (e) => { if (e.target === usersModalEl) closeUsersModal(); });
    loginSubmitEl.addEventListener("click", submitLogin);
    loginForgotEl.addEventListener("click", () => showLoginGate(true, "forgot"));
    forgotBackEl.addEventListener("click", () => showLoginGate(true, "login"));
    forgotSubmitEl.addEventListener("click", submitForgotPassword);
    resetSubmitEl.addEventListener("click", submitResetPassword);
    loginPassEl.addEventListener("keydown", (e) => { if (e.key === "Enter") submitLogin(); });
    forgotEmailEl.addEventListener("keydown", (e) => { if (e.key === "Enter") submitForgotPassword(); });
    passwordSubmitEl.addEventListener("click", submitPasswordChange);
    passwordCloseEl.addEventListener("click", closePasswordModal);
    passwordModalEl.addEventListener("click", (e) => { if (e.target === passwordModalEl) closePasswordModal(); });
    document.addEventListener("click", (e) => {
      if (!accountBtnEl.contains(e.target) && !accountMenuEl.contains(e.target)) {
        toggleAccountMenu(false);
      }
    });

    function tickClock() {
      clockEl.textContent = new Date().toLocaleTimeString("nl-NL");
    }
    tickClock();
    setInterval(tickClock, 1000);

    function slugify(value) {
      return (value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "item";
    }

    function loadSshHosts() {
      try {
        const raw = localStorage.getItem(SSH_STORAGE_KEY);
        return raw ? JSON.parse(raw) : [];
      } catch {
        return [];
      }
    }

    function saveSshHosts(hosts) {
      localStorage.setItem(SSH_STORAGE_KEY, JSON.stringify(hosts));
      sshHosts = hosts;
    }

    function migrateServerSshHosts() {
      const existing = loadSshHosts();
      if (existing.length || !config?.ssh_hosts?.length) return existing;
      const migrated = config.ssh_hosts.map((h) => ({
        id: h.id,
        title: h.title || h.id,
        description: h.description || "",
        type: h.type === "local" ? "ssh" : (h.type || "ssh"),
        hostname: h.hostname || h.host || "",
        port: h.port || 22,
        user: h.user || "root",
        auth: h.auth || "key",
        key_file: h.key_file || "",
      })).filter((h) => h.type !== "local" && h.hostname);
      saveSshHosts(migrated);
      return migrated;
    }

    function buildSshHostFromForm(existingId) {
      const raw = payloadFromSshForm();
      const id = existingId || slugify(raw.id || raw.title);
      const host = {
        id,
        title: (raw.title || id).trim() || id,
        description: (raw.description || "").trim(),
        type: raw.type || "ssh",
      };
      if (host.type !== "local") {
        host.hostname = (raw.host || "").trim();
        host.port = raw.port ? parseInt(raw.port, 10) : 22;
        host.user = (raw.user || "root").trim() || "root";
        host.auth = raw.auth || "key";
        if (!host.hostname) throw new Error("Vul een host of hostname in");
        if (host.auth === "password") {
          if (raw.password) host.password = raw.password;
        } else {
          host.key_file = (raw.key_file || "").trim();
        }
      }
      return host;
    }

    function renderSshHosts() {
      hostListEl.textContent = "";
      sshCountEl.textContent = String(sshHosts.length);
      sshHosts.forEach(createHostButton);
      if (!activeHost && sshHosts.length) activeHost = sshHosts[0];
      if (!sshHosts.length) sshDescEl.textContent = "Voeg een host toe via + Add (hostname uit ~/.ssh/config)";
    }

    async function checkLocalBridge() {
      if (!localBridgeEl) return;
      try {
        await new Promise((resolve, reject) => {
          const ws = new WebSocket(`ws://127.0.0.1:${LOCAL_WS_PORT}/`);
          ws.onopen = () => { ws.close(); resolve(); };
          ws.onerror = reject;
          setTimeout(() => reject(new Error("timeout")), 2000);
        });
        localBridgeEl.textContent = "Actief";
        localBridgeEl.style.color = "var(--ok)";
      } catch {
        localBridgeEl.textContent = "Offline";
        localBridgeEl.style.color = "var(--bad)";
      }
    }

    const BUILTIN_CATEGORY_ORDER = ["proxmox", "backup", "container", "docker"];
    const CAT_ICONS = {
      proxmox: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01"/><path d="M6 18h.01"/></svg>',
      backup: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>',
      container: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>',
      docker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="10.01" y2="16"/></svg>',
      default: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h10"/></svg>',
    };

    function getCategoryOrder() {
      const builtin = BUILTIN_CATEGORY_ORDER.filter((id) => categoryMeta[id]);
      const custom = Object.keys(categoryMeta)
        .filter((id) => !BUILTIN_CATEGORY_ORDER.includes(id))
        .sort((a, b) => (categoryMeta[a]?.title || a).localeCompare(categoryMeta[b]?.title || b, "nl"));
      return [...builtin, ...custom];
    }

    function categoryIcon(catId) {
      return CAT_ICONS[catId] || CAT_ICONS.default;
    }
    const CATEGORY_SOURCE_PRESETS = {
      proxmox: "proxmox",
      backup: "remote",
      container: "proxmox",
      docker: "docker",
    };
    let categoryMeta = {};

    function switchView(viewName) {
      document.querySelectorAll(".tab").forEach((t) => {
        t.classList.toggle("active", t.dataset.view === viewName);
      });
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
      document.getElementById("view-" + viewName).classList.add("active");
      if (sidebarLogsEl) sidebarLogsEl.classList.toggle("visible", viewName === "logs");
      sidebarSshEl.classList.toggle("visible", viewName === "ssh");
      if (viewName === "ssh") {
        try {
          ensureTerminal();
          if (fitAddon) setTimeout(() => fitAddon.fit(), 50);
          if (!sshSocket || sshSocket.readyState !== WebSocket.OPEN) {
            const host = activeHost || (sshHosts && sshHosts[0]);
            if (host) connectSsh(host);
          }
        } catch (e) {
          sshDescEl.textContent = "Terminal fout: " + e;
        }
      }
    }

    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
    });

    let containerList = [];
    let dockerList = [];
    let unitList = [];

    function unitOptionLabel(unit) {
      const state = unit.active === "active" ? unit.sub || "active" : unit.active;
      const desc = unit.description ? ` — ${unit.description}` : "";
      return `${unit.name} [${state}]${desc}`;
    }

    async function loadUnits() {
      if (!logUnitSelectEl) return;
      const source = formLogEl.source.value;
      const selected = Array.from(logUnitSelectEl.selectedOptions).map((o) => o.value);
      logUnitSelectEl.innerHTML = '<option value="">Laden...</option>';

      if (source === "remote") {
        unitList = [];
        logUnitSelectEl.innerHTML = "";
        const hint = document.createElement("option");
        hint.value = "";
        hint.textContent = "Vul units handmatig in onderaan";
        logUnitSelectEl.appendChild(hint);
        if (logUnitsHintEl) {
          logUnitsHintEl.textContent = "PBS/remote: typ unit namen handmatig (bijv. proxmox-backup.service)";
        }
        return;
      }

      if (source === "proxmox" && !(logSshHostEl?.value || "").trim()) {
        logUnitSelectEl.innerHTML = "";
        const hint = document.createElement("option");
        hint.value = "";
        hint.textContent = "Vul eerst het IP/hostname van de Proxmox node in";
        logUnitSelectEl.appendChild(hint);
        return;
      }

      const ctid = source === "proxmox" ? (logCtidSelectEl?.value || "") : "";

      try {
        let url = "/api/units";
        if (source === "proxmox") {
          const q = logSshQueryParams();
          url = `/api/units?${q.toString()}`;
          if (ctid) url += `&ctid=${encodeURIComponent(ctid)}`;
        } else if (ctid) {
          url = `/api/units?ctid=${encodeURIComponent(ctid)}`;
        }
        const res = await apiFetch(url);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Laden mislukt");
        unitList = data.units || [];
        logUnitSelectEl.textContent = "";
        if (!unitList.length) {
          const empty = document.createElement("option");
          empty.value = "";
          empty.textContent = "Geen systemd services gevonden";
          logUnitSelectEl.appendChild(empty);
        } else {
          unitList.forEach((unit) => {
            const opt = document.createElement("option");
            opt.value = unit.name;
            opt.textContent = unitOptionLabel(unit);
            logUnitSelectEl.appendChild(opt);
          });
        }
        selected.forEach((name) => {
          const opt = Array.from(logUnitSelectEl.options).find((o) => o.value === name);
          if (opt) opt.selected = true;
        });
        if (logUnitsHintEl) {
          if (source === "proxmox") {
            logUnitsHintEl.textContent = ctid
              ? "Units uit gekozen container op de Proxmox node. Ctrl+klik voor meerdere."
              : "Units van de Proxmox node. Ctrl+klik voor meerdere.";
          } else {
            logUnitsHintEl.textContent = "Ctrl+klik voor meerdere units.";
          }
        }
      } catch (e) {
        logUnitSelectEl.textContent = "";
        const err = document.createElement("option");
        err.value = "";
        err.textContent = "Units laden mislukt";
        logUnitSelectEl.appendChild(err);
      }
    }

    async function loadContainers() {
      if (!logCtidSelectEl) return;
      const source = formLogEl.source.value;
      if (source !== "proxmox") return;
      const host = (logSshHostEl?.value || "").trim();
      const selected = logCtidSelectEl.value;
      logCtidSelectEl.innerHTML = '<option value="">Laden...</option>';
      if (!host) {
        logCtidSelectEl.innerHTML = "";
        const hint = document.createElement("option");
        hint.value = "";
        hint.textContent = "Vul eerst het IP/hostname in";
        logCtidSelectEl.appendChild(hint);
        return;
      }
      try {
        const q = logSshQueryParams();
        const res = await apiFetch(`/api/containers?${q.toString()}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Laden mislukt");
        containerList = data.containers || [];
        logCtidSelectEl.textContent = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Node logs (geen container)";
        logCtidSelectEl.appendChild(placeholder);
        containerList.forEach((ct) => {
          const opt = document.createElement("option");
          opt.value = ct.id;
          opt.textContent = `${ct.id} — ${ct.name} (${ct.status})`;
          logCtidSelectEl.appendChild(opt);
        });
        if (selected) logCtidSelectEl.value = selected;
        loadUnits();
      } catch (e) {
        logCtidSelectEl.textContent = "";
        const err = document.createElement("option");
        err.value = "";
        err.textContent = "Containers laden mislukt";
        logCtidSelectEl.appendChild(err);
      }
    }

    async function loadDockerContainers() {
      if (!logDockerSelectEl) return;
      const source = formLogEl.source.value;
      if (source !== "docker") return;
      const selected = logDockerSelectEl.value;
      logDockerSelectEl.innerHTML = '<option value="">Laden...</option>';
      try {
        const q = logSshQueryParams();
        const res = await apiFetch(`/api/docker/containers?${q.toString()}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Laden mislukt");
        dockerList = data.containers || [];
        logDockerSelectEl.textContent = "";
        if (!dockerList.length) {
          const empty = document.createElement("option");
          empty.value = "";
          empty.textContent = "Geen containers gevonden";
          logDockerSelectEl.appendChild(empty);
        } else {
          dockerList.forEach((dc) => {
            const opt = document.createElement("option");
            opt.value = dc.name;
            const state = (dc.status || "").toLowerCase();
            const running = state.startsWith("up") || state.includes("running");
            opt.textContent = `${dc.name} — ${dc.image || "?"} (${dc.status})`;
            if (!running) opt.style.color = "var(--muted)";
            logDockerSelectEl.appendChild(opt);
          });
        }
        if (selected) logDockerSelectEl.value = selected;
      } catch (e) {
        logDockerSelectEl.textContent = "";
        const err = document.createElement("option");
        err.value = "";
        err.textContent = "Containers laden mislukt";
        logDockerSelectEl.appendChild(err);
      }
    }

    function updateLogSourceFields() {
      const source = formLogEl.source.value;
      const isCommand = source === "command";
      const isRemote = source === "remote";
      const isProxmox = source === "proxmox";
      const isDocker = source === "docker";
      fieldUnitsEl.style.display = isCommand || isRemote || isDocker ? "none" : "grid";
      fieldCommandEl.style.display = isCommand ? "grid" : "none";
      logSshFieldsEl.style.display = isCommand ? "none" : "grid";
      logContainerFieldEl.style.display = isProxmox ? "grid" : "none";
      if (logDockerFieldEl) logDockerFieldEl.style.display = isDocker ? "grid" : "none";
      logUnitSelectEl.style.display = isRemote ? "none" : "block";
      if (logSourceHintEl) {
        logSourceHintEl.innerHTML = LOG_SOURCE_HINTS[source] || "";
      }
      if (isProxmox) loadContainers();
      else if (isDocker) loadDockerContainers();
      else if (!isCommand) loadUnits();
    }

    const sshKeyFieldEl = document.getElementById("ssh-key-field");
    const sshPasswordFieldEl = document.getElementById("ssh-password-field");

    function updateSshTypeFields() {
      const isLocal = formSshEl.type.value === "local";
      sshRemoteFieldsEl.style.display = isLocal ? "none" : "grid";
      if (!isLocal) updateSshAuthFields();
    }

    function updateSshAuthFields() {
      const usePassword = formSshEl.ssh_auth.value === "password";
      sshKeyFieldEl.style.display = usePassword ? "none" : "grid";
      sshPasswordFieldEl.style.display = usePassword ? "grid" : "none";
    }

    const addModalTitleEl = document.getElementById("add-modal-title");
    const logSourceHintEl = document.getElementById("log-source-hint");
    const sshIdFieldEl = formSshEl.querySelector('[name="id"]');

    const LOG_SOURCE_HINTS = {
      proxmox: "Verbind met een Proxmox node via SSH. Kies node logs of een LXC container op die node.",
      docker: "Docker logs via <code>docker logs -f</code>. Host leeg = minilab, anders via SSH op die host.",
      remote: "Logs via SSH van een remote host (typisch PBS). Vul IP en units handmatig in.",
      command: "Voer een eigen shell-commando in — bijv. <code>tail -F</code> op een logbestand.",
    };

    function logSshQueryParams() {
      const raw = formData(formLogEl);
      return new URLSearchParams({
        host: (raw.log_host || "").trim(),
        port: raw.log_port || "22",
        user: raw.log_user || "root",
        key_file: raw.log_key_file || "",
      });
    }

    function updateAddModalTitle() {
      if (!addModalTitleEl) return;
      if (activeAddPane === "ssh") {
        addModalTitleEl.textContent = editingSshId ? "SSH host bewerken" : "SSH host toevoegen";
      } else {
        addModalTitleEl.textContent = "Log panel toevoegen";
      }
    }

    function resetAddForms() {
      editingSshId = null;
      formLogEl.reset();
      formSshEl.reset();
      formLogEl.source.value = "proxmox";
      if (logSshHostEl) logSshHostEl.value = "";
      if (logSshPortEl) logSshPortEl.value = "";
      if (logSshUserEl) logSshUserEl.value = "root";
      if (logSshKeyEl) logSshKeyEl.value = "";
      formSshEl.type.value = "ssh";
      formSshEl.ssh_auth.value = "key";
      formLogEl.height.value = "220";
      activeAddPane = "log";
      updateAddModalTitle();
      if (sshIdFieldEl) sshIdFieldEl.readOnly = false;
      document.querySelectorAll(".modal-tab").forEach((t) => {
        t.classList.toggle("active", t.dataset.pane === "log");
        t.disabled = false;
      });
      formLogEl.classList.add("active");
      formSshEl.classList.remove("active");
      updateLogSourceFields();
      updateSshTypeFields();
      updateSshAuthFields();
    }

    function openAddModal() {
      resetAddForms();
      const logCategoryInputEl = document.getElementById("log-category-input");
      if (logCategoryInputEl && activeLogCategory) {
        logCategoryInputEl.value = activeLogCategory;
        const preset = CATEGORY_SOURCE_PRESETS[activeLogCategory];
        if (preset) {
          formLogEl.source.value = preset;
          updateLogSourceFields();
        }
      }
      addErrorEl.textContent = "";
      addModalEl.classList.add("open");
    }

    function openAddSshModal() {
      resetAddForms();
      activeAddPane = "ssh";
      document.querySelectorAll(".modal-tab").forEach((t) => {
        t.classList.toggle("active", t.dataset.pane === "ssh");
        t.disabled = false;
      });
      formLogEl.classList.remove("active");
      formSshEl.classList.add("active");
      updateAddModalTitle();
      updateSshTypeFields();
      addErrorEl.textContent = "";
      addModalEl.classList.add("open");
    }

    function openEditSshModal(host) {
      resetAddForms();
      editingSshId = host.id;
      activeAddPane = "ssh";
      updateAddModalTitle();
      document.querySelectorAll(".modal-tab").forEach((t) => {
        const isSsh = t.dataset.pane === "ssh";
        t.classList.toggle("active", isSsh);
        t.disabled = !isSsh;
      });
      formLogEl.classList.remove("active");
      formSshEl.classList.add("active");
      formSshEl.title.value = host.title || "";
      if (sshIdFieldEl) {
        sshIdFieldEl.value = host.id || "";
        sshIdFieldEl.readOnly = true;
      }
      formSshEl.description.value = host.description || "";
      formSshEl.type.value = host.type || "ssh";
      if (host.type !== "local") {
        formSshEl.ssh_host.value = host.hostname || host.host || "";
        formSshEl.ssh_port.value = host.port && host.port !== 22 ? String(host.port) : "";
        formSshEl.ssh_user.value = host.user || "root";
        formSshEl.ssh_auth.value = host.auth || "key";
        if (host.auth === "password") {
          formSshEl.ssh_password.value = "";
          formSshEl.ssh_password.placeholder = "Laat leeg om huidig wachtwoord te behouden";
        } else {
          formSshEl.ssh_key_file.value = host.key_file || "";
        }
      }
      updateSshTypeFields();
      updateSshAuthFields();
      addErrorEl.textContent = "";
      addModalEl.classList.add("open");
      switchView("ssh");
    }

    function closeAddModal() {
      addModalEl.classList.remove("open");
      addErrorEl.textContent = "";
      resetAddForms();
    }

    function switchAddPane(pane) {
      activeAddPane = pane;
      document.querySelectorAll(".modal-tab").forEach((t) => {
        t.classList.toggle("active", t.dataset.pane === pane);
      });
      formLogEl.classList.toggle("active", pane === "log");
      formSshEl.classList.toggle("active", pane === "ssh");
      updateAddModalTitle();
      if (pane === "log") {
        updateLogSourceFields();
      }
      if (pane === "ssh") updateSshTypeFields();
    }

    async function applyLogSshResolve() {
      const host = (logSshHostEl?.value || "").trim();
      if (!host) {
        scheduleProxmoxRefresh();
        return;
      }
      try {
        const q = logSshQueryParams();
        const res = await apiFetch(`/api/ssh/resolve?${q.toString()}`);
        const data = await res.json();
        if (!res.ok || !data.ok) return;
        if (logSshPortEl && !logSshPortEl.value && data.port) {
          logSshPortEl.value = String(data.port);
        }
        if (logSshUserEl && !logSshUserEl.value && data.user) {
          logSshUserEl.value = data.user;
        }
        if (logSshKeyEl && !logSshKeyEl.value && data.key_file) {
          logSshKeyEl.value = data.key_file;
        }
      } catch (_) {}
      scheduleProxmoxRefresh();
    }

    function scheduleProxmoxRefresh() {
      const source = formLogEl.source.value;
      if (source === "proxmox") loadContainers();
      else if (source === "docker") loadDockerContainers();
      else if (source !== "command" && source !== "remote") loadUnits();
    }

    function formData(form) {
      return Object.fromEntries(new FormData(form).entries());
    }

    function payloadFromLogForm() {
      const raw = formData(formLogEl);
      const picked = logUnitSelectEl
        ? Array.from(logUnitSelectEl.selectedOptions).map((o) => o.value).filter(Boolean)
        : [];
      const manual = (raw.units || "").split(",").map((s) => s.trim()).filter(Boolean);
      raw.unit_pick = picked;
      raw.units = [...new Set([...picked, ...manual])].join(", ");
      if (raw.source === "remote" || raw.source === "proxmox" || raw.source === "docker") {
        raw.host = raw.log_host || "";
        raw.port = raw.log_port || "";
        raw.user = raw.log_user || "root";
        raw.key_file = raw.log_key_file || "";
      }
      return raw;
    }

    function payloadFromSshForm() {
      const raw = formData(formSshEl);
      if (raw.type !== "local") {
        raw.host = raw.ssh_host || "";
        raw.port = raw.ssh_port || "";
        raw.user = raw.ssh_user || "root";
        raw.auth = raw.ssh_auth || "key";
        if (raw.auth === "password") {
          raw.password = raw.ssh_password || "";
        } else {
          raw.key_file = raw.ssh_key_file || "";
        }
      }
      return raw;
    }

    async function saveAdd() {
      addErrorEl.textContent = "";
      if (activeAddPane === "log") {
        try {
          const res = await apiFetch("/api/add/panel", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payloadFromLogForm()),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "Opslaan mislukt");
          window.location.reload();
        } catch (e) {
          addErrorEl.textContent = String(e.message || e);
        }
        return;
      }

      try {
        const host = buildSshHostFromForm(editingSshId);
        const hosts = [...sshHosts];
        if (editingSshId) {
          const idx = hosts.findIndex((h) => h.id === editingSshId);
          if (idx < 0) throw new Error("SSH host niet gevonden");
          if (host.auth === "password" && !host.password) {
            host.password = hosts[idx].password || "";
          }
          hosts[idx] = host;
        } else {
          if (hosts.some((h) => h.id === host.id)) {
            throw new Error(`SSH host '${host.id}' bestaat al`);
          }
          hosts.push(host);
        }
        saveSshHosts(hosts);
        closeAddModal();
        renderSshHosts();
        if (document.getElementById("view-ssh").classList.contains("active")) {
          connectSsh(host);
        }
      } catch (e) {
        addErrorEl.textContent = String(e.message || e);
      }
    }

    function deleteSshHost(host) {
      if (!confirm(`SSH host "${host.title}" verwijderen?`)) return;
      const hosts = sshHosts.filter((h) => h.id !== host.id);
      saveSshHosts(hosts);
      if (activeHost && activeHost.id === host.id) {
        disconnectSsh();
        activeHost = hosts[0] || null;
      }
      renderSshHosts();
    }

    document.getElementById("btn-add").addEventListener("click", openAddModal);
    document.getElementById("btn-add-ssh").addEventListener("click", openAddSshModal);
    document.getElementById("add-close").addEventListener("click", closeAddModal);
    document.getElementById("add-cancel").addEventListener("click", closeAddModal);
    document.getElementById("add-save").addEventListener("click", saveAdd);
    addModalEl.addEventListener("click", (e) => { if (e.target === addModalEl) closeAddModal(); });
    document.querySelectorAll(".modal-tab").forEach((tab) => {
      tab.addEventListener("click", () => switchAddPane(tab.dataset.pane));
    });
    formLogEl.source.addEventListener("change", updateLogSourceFields);
    if (logCtidSelectEl) {
      logCtidSelectEl.addEventListener("change", () => {
        if (formLogEl.source.value === "proxmox") loadUnits();
      });
    }
    if (logSshHostEl) {
      logSshHostEl.addEventListener("blur", applyLogSshResolve);
      logSshHostEl.addEventListener("change", applyLogSshResolve);
    }
    [logSshPortEl, logSshUserEl, logSshKeyEl].forEach((el) => {
      if (!el) return;
      el.addEventListener("change", scheduleProxmoxRefresh);
      el.addEventListener("blur", scheduleProxmoxRefresh);
    });
    formSshEl.type.addEventListener("change", updateSshTypeFields);
    formSshEl.ssh_auth.addEventListener("change", updateSshAuthFields);
    resetAddForms();

    function setStatusState(state) {
      statusEl.className = "status-card " + state;
      if (statusDotEl) statusDotEl.className = "status-dot " + state;
    }

    function paintStatus(text) {
      statusEl.textContent = text.trim() || "(geen output)";
      const low = text.toLowerCase();
      if (low.includes("offline") || low.includes("mislukt") || low.includes("service: inactive")) {
        setStatusState("offline");
      } else if (low.includes("shutdown mag") || low.includes("backup: ok")) {
        setStatusState("idle");
      } else if (low.includes("vzdump bezig")) {
        setStatusState("busy");
      } else {
        setStatusState("busy");
      }
    }

    async function refreshStatus() {
      try {
        const res = await apiFetch("/api/status");
        const data = await res.json();
        paintStatus(data.output || "");
        updatedEl.textContent = new Date().toLocaleTimeString("nl-NL");
      } catch (e) {
        statusEl.textContent = "Status ophalen mislukt: " + e;
        setStatusState("offline");
      }
    }

    function colorLine(line) {
      const l = line.toLowerCase();
      if (l.includes("failed") || l.includes("fout") || l.includes("timeout") || l.includes("geblokkeerd")) return "line-err";
      if (l.includes("wachten") || l.includes("waarschuwing") || l.includes("nog niet")) return "line-warn";
      if (l.includes("ok") || l.includes("afgerond") || l.includes("online") || l.includes("finished")) return "line-ok";
      if (l.includes("pbs") || l.includes("vzdump") || l.includes("prune") || l.includes("garbage") || l.includes("backup")) return "line-info";
      return "";
    }

    function appendLog(logEl, text, autoScroll) {
      const span = document.createElement("span");
      span.className = colorLine(text);
      span.textContent = text + "\n";
      logEl.appendChild(span);
      while (logEl.childNodes.length > 400) logEl.removeChild(logEl.firstChild);
      if (autoScroll) logEl.scrollTop = logEl.scrollHeight;
    }

    async function deletePanel(panel) {
      if (!confirm(`Panel "${panel.title}" permanent verwijderen?`)) return;
      try {
        const res = await apiFetch("/api/delete/panel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: panel.id }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Verwijderen mislukt");
        const pv = panelViews.get(panel.id);
        if (pv) stopPanelStream(pv);
        panelViews.delete(panel.id);
        window.location.reload();
      } catch (e) {
        alert(String(e.message || e));
      }
    }

    function startPanelStream(pv) {
      if (!pv || pv.es) return;
      const es = new EventSource("/api/logs/" + encodeURIComponent(pv.panel.id));
      pv.es = es;
      es.onopen = () => {
        pv.badge.textContent = "live";
        pv.badge.classList.add("live");
        connEl.textContent = "Live";
        connEl.classList.add("live");
      };
      es.onmessage = (ev) => {
        if (!pv.paused) appendLog(pv.log, ev.data, pv.autoScroll);
      };
      es.onerror = () => {
        pv.badge.textContent = "offline";
        pv.badge.classList.remove("live");
        pv.badge.classList.add("err");
      };
    }

    function stopPanelStream(pv) {
      if (!pv?.es) return;
      pv.es.close();
      pv.es = null;
      pv.badge.textContent = "idle";
      pv.badge.classList.remove("live", "err");
    }

    function applyLogCategory(cat) {
      activeLogCategory = cat;
      document.querySelectorAll(".cat-tab").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.cat === cat);
      });
      const meta = categoryMeta[cat];
      if (meta) {
        if (pageTitleEl) pageTitleEl.textContent = meta.title;
        if (pageSubEl) pageSubEl.textContent = meta.sub;
      }
      panelViews.forEach((pv) => {
        const show = pv.panel.category === cat;
        pv.wrap.style.display = show ? "" : "none";
        if (show) startPanelStream(pv);
        else stopPanelStream(pv);
      });
    }

    function updateCategoryCounts(counts) {
      document.querySelectorAll(".cat-tab").forEach((btn) => {
        const countEl = btn.querySelector(".cat-count");
        if (countEl) countEl.textContent = String(counts[btn.dataset.cat] || 0);
      });
    }

    function renderLogCategories(counts = {}) {
      if (!logCategoriesEl) return;
      logCategoriesEl.textContent = "";
      const canManage = !!authState.permissions?.manage_panels;
      getCategoryOrder().forEach((catId) => {
        const meta = categoryMeta[catId] || { title: catId, sub: "" };
        const item = document.createElement("div");
        item.className = "cat-item";
        item.dataset.cat = catId;

        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cat-tab" + (activeLogCategory === catId ? " active" : "");
        btn.dataset.cat = catId;
        btn.innerHTML =
          `<span class="cat-icon">${categoryIcon(catId)}</span>` +
          `<span class="cat-info"><span class="cat-name">${meta.title}</span>` +
          `<span class="cat-desc">${meta.sub || ""}</span></span>` +
          `<span class="cat-count">${counts[catId] || 0}</span>`;
        btn.addEventListener("click", () => applyLogCategory(catId));

        if (canManage) {
          const actions = document.createElement("div");
          actions.className = "cat-item-actions";
          const editBtn = document.createElement("button");
          editBtn.type = "button";
          editBtn.className = "btn";
          editBtn.textContent = "edit";
          editBtn.title = `${meta.title || catId} bewerken`;
          editBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            openEditCategoryModal(catId);
          });
          actions.appendChild(editBtn);
          item.append(btn, actions);
        } else {
          item.appendChild(btn);
        }
        logCategoriesEl.appendChild(item);
      });
    }

    function openEditCategoryModal(catId) {
      const meta = categoryMeta[catId];
      if (!meta) return;
      document.getElementById("cat-edit-id").value = catId;
      document.getElementById("cat-edit-name").value = meta.title || "";
      document.getElementById("cat-edit-sub").value = meta.sub || "";
      if (catEditErrorEl) catEditErrorEl.textContent = "";
      const titleEl = document.getElementById("cat-edit-title");
      if (titleEl) titleEl.textContent = `Categorie bewerken — ${meta.title || catId}`;
      catEditModalEl?.classList.add("open");
    }

    function closeEditCategoryModal() {
      catEditModalEl?.classList.remove("open");
      if (catEditErrorEl) catEditErrorEl.textContent = "";
    }

    async function saveCategoryEdit() {
      if (!formCatEditEl) return;
      if (catEditErrorEl) catEditErrorEl.textContent = "";
      const payload = Object.fromEntries(new FormData(formCatEditEl).entries());
      try {
        const res = await apiFetch("/api/edit/category", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Opslaan mislukt");
        categoryMeta[payload.id] = data.category;
        const counts = {};
        (config.panels || []).forEach((p) => {
          counts[p.category] = (counts[p.category] || 0) + 1;
        });
        renderLogCategories(counts);
        applyLogCategory(payload.id || activeLogCategory);
        closeEditCategoryModal();
      } catch (e) {
        if (catEditErrorEl) catEditErrorEl.textContent = String(e.message || e);
      }
    }

    function fillPanelEditCategories(selected) {
      if (!panelEditCategoryEl) return;
      panelEditCategoryEl.textContent = "";
      getCategoryOrder().forEach((catId) => {
        const opt = document.createElement("option");
        opt.value = catId;
        opt.textContent = categoryMeta[catId]?.title || catId;
        if (catId === selected) opt.selected = true;
        panelEditCategoryEl.appendChild(opt);
      });
    }

    function openEditPanelModal(panel) {
      if (!formPanelEditEl) return;
      document.getElementById("panel-edit-id").value = panel.id;
      document.getElementById("panel-edit-name").value = panel.title || "";
      document.getElementById("panel-edit-desc").value = panel.description || "";
      document.getElementById("panel-edit-height").value = String(panel.height || 220);
      fillPanelEditCategories(panel.category || "proxmox");
      const titleEl = document.getElementById("panel-edit-title");
      if (titleEl) titleEl.textContent = `Panel bewerken — ${panel.title || panel.id}`;
      if (panelEditErrorEl) panelEditErrorEl.textContent = "";
      panelEditModalEl?.classList.add("open");
    }

    function closeEditPanelModal() {
      panelEditModalEl?.classList.remove("open");
      if (panelEditErrorEl) panelEditErrorEl.textContent = "";
    }

    function openEditStatusModal() {
      if (!formStatusEditEl) return;
      document.getElementById("status-edit-label").value = config.status?.label || "Status";
      document.getElementById("status-edit-proxmox").value = config.status?.proxmox_host || "";
      document.getElementById("status-edit-pbs").value = config.status?.pbs_host || "";
      document.getElementById("status-edit-interval").value = String(config.status?.interval_seconds || 5);
      if (statusEditErrorEl) statusEditErrorEl.textContent = "";
      statusEditModalEl?.classList.add("open");
    }

    function closeEditStatusModal() {
      statusEditModalEl?.classList.remove("open");
      if (statusEditErrorEl) statusEditErrorEl.textContent = "";
    }

    async function saveStatusEdit() {
      if (!formStatusEditEl) return;
      if (statusEditErrorEl) statusEditErrorEl.textContent = "";
      const payload = Object.fromEntries(new FormData(formStatusEditEl).entries());
      try {
        const res = await apiFetch("/api/edit/status", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Opslaan mislukt");
        config.status = data.status;
        statusLabelEl.textContent = config.status?.label || "Status";
        const interval = (config.status?.interval_seconds || 5) * 1000;
        if (statusTimer) clearInterval(statusTimer);
        statusTimer = setInterval(refreshStatus, interval);
        intervalEl.textContent = `${config.status?.interval_seconds || 5}s`;
        closeEditStatusModal();
        refreshStatus();
      } catch (e) {
        if (statusEditErrorEl) statusEditErrorEl.textContent = String(e.message || e);
      }
    }

    statusEditBtnEl?.addEventListener("click", openEditStatusModal);
    document.getElementById("status-edit-close")?.addEventListener("click", closeEditStatusModal);
    document.getElementById("status-edit-cancel")?.addEventListener("click", closeEditStatusModal);
    document.getElementById("status-edit-save")?.addEventListener("click", saveStatusEdit);
    statusEditModalEl?.addEventListener("click", (e) => {
      if (e.target === statusEditModalEl) closeEditStatusModal();
    });

    async function savePanelEdit() {
      if (!formPanelEditEl) return;
      if (panelEditErrorEl) panelEditErrorEl.textContent = "";
      const payload = Object.fromEntries(new FormData(formPanelEditEl).entries());
      try {
        const res = await apiFetch("/api/edit/panel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Opslaan mislukt");
        closeEditPanelModal();
        window.location.reload();
      } catch (e) {
        if (panelEditErrorEl) panelEditErrorEl.textContent = String(e.message || e);
      }
    }

    btnCatAddEl?.addEventListener("click", openAddCategoryModal);
    document.getElementById("cat-edit-close")?.addEventListener("click", closeEditCategoryModal);
    document.getElementById("cat-edit-cancel")?.addEventListener("click", closeEditCategoryModal);
    document.getElementById("cat-edit-save")?.addEventListener("click", saveCategoryEdit);
    catEditModalEl?.addEventListener("click", (e) => {
      if (e.target === catEditModalEl) closeEditCategoryModal();
    });
    document.getElementById("cat-add-close")?.addEventListener("click", closeAddCategoryModal);
    document.getElementById("cat-add-cancel")?.addEventListener("click", closeAddCategoryModal);
    document.getElementById("cat-add-save")?.addEventListener("click", saveAddCategory);
    catAddModalEl?.addEventListener("click", (e) => {
      if (e.target === catAddModalEl) closeAddCategoryModal();
    });

    function openAddCategoryModal() {
      if (!formCatAddEl) return;
      formCatAddEl.reset();
      if (catAddErrorEl) catAddErrorEl.textContent = "";
      catAddModalEl?.classList.add("open");
      document.getElementById("cat-add-name")?.focus();
    }

    function closeAddCategoryModal() {
      catAddModalEl?.classList.remove("open");
      if (catAddErrorEl) catAddErrorEl.textContent = "";
    }

    async function saveAddCategory() {
      if (!formCatAddEl) return;
      if (catAddErrorEl) catAddErrorEl.textContent = "";
      const payload = Object.fromEntries(new FormData(formCatAddEl).entries());
      try {
        const res = await apiFetch("/api/add/category", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Toevoegen mislukt");
        closeAddCategoryModal();
        window.location.reload();
      } catch (e) {
        if (catAddErrorEl) catAddErrorEl.textContent = String(e.message || e);
      }
    }
    document.getElementById("panel-edit-close")?.addEventListener("click", closeEditPanelModal);
    document.getElementById("panel-edit-cancel")?.addEventListener("click", closeEditPanelModal);
    document.getElementById("panel-edit-save")?.addEventListener("click", savePanelEdit);
    panelEditModalEl?.addEventListener("click", (e) => {
      if (e.target === panelEditModalEl) closeEditPanelModal();
    });

    function createPanel(panel) {
      const wrap = document.createElement("section");
      wrap.className = "panel";
      wrap.dataset.panelId = panel.id;
      wrap.dataset.category = panel.category || "proxmox";
      wrap.style.display = "none";
      const head = document.createElement("div");
      head.className = "panel-head";
      const info = document.createElement("div");
      info.innerHTML = `<div class="panel-title">${panel.title}</div>` +
        (panel.description ? `<div class="panel-desc">${panel.description}</div>` : "");
      const actions = document.createElement("div");
      actions.className = "panel-actions";
      const badge = document.createElement("span");
      badge.className = "panel-badge";
      badge.textContent = "verbinden";
      const clearBtn = document.createElement("button");
      clearBtn.className = "btn btn-sm";
      clearBtn.textContent = "Wissen";
      clearBtn.title = "Logtekst wissen";
      const pauseBtn = document.createElement("button");
      pauseBtn.className = "btn btn-sm";
      pauseBtn.textContent = "Pause";
      const editBtn = document.createElement("button");
      editBtn.className = "btn btn-sm";
      editBtn.textContent = "edit";
      editBtn.title = "Panel bewerken";
      const deleteBtn = document.createElement("button");
      deleteBtn.className = "btn btn-sm btn-danger";
      deleteBtn.textContent = "Verwijder";
      deleteBtn.title = "Panel permanent verwijderen";
      if (!authState.permissions?.manage_panels) {
        editBtn.style.display = "none";
        deleteBtn.style.display = "none";
      }
      actions.append(badge, pauseBtn, clearBtn, editBtn, deleteBtn);
      head.append(info, actions);
      const log = document.createElement("pre");
      log.className = "panel-log";
      log.style.height = (panel.height || 220) + "px";
      wrap.append(head, log);
      panelsEl.appendChild(wrap);

      const pv = {
        wrap, panel, log, badge, pauseBtn,
        paused: false,
        autoScroll: true,
        es: null,
      };
      log.addEventListener("scroll", () => {
        pv.autoScroll = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      });
      clearBtn.addEventListener("click", () => { log.textContent = ""; });
      pauseBtn.addEventListener("click", () => {
        pv.paused = !pv.paused;
        pauseBtn.textContent = pv.paused ? "Hervat" : "Pause";
      });
      editBtn.addEventListener("click", () => openEditPanelModal(panel));
      deleteBtn.addEventListener("click", () => deletePanel(panel));
      panelViews.set(panel.id, pv);
    }

    function ensureTerminal() {
      if (term) return;
      if (typeof Terminal === "undefined") {
        throw new Error("xterm.js niet geladen — controleer internet/CDN");
      }
      const FitAddonClass = (typeof FitAddon === "function") ? FitAddon : FitAddon?.FitAddon;
      if (!FitAddonClass) {
        throw new Error("xterm FitAddon niet geladen — controleer internet/CDN");
      }
      term = new Terminal({
        cursorBlink: true,
        fontFamily: "JetBrains Mono, Fira Code, monospace",
        fontSize: 13,
        theme: { background: "#070a0f", foreground: "#e8eef7", cursor: "#5eb3ff" },
      });
      fitAddon = new FitAddonClass();
      term.loadAddon(fitAddon);
      term.open(document.getElementById("terminal"));
      fitAddon.fit();
      window.addEventListener("resize", () => { if (fitAddon) fitAddon.fit(); sendResize(); });
      term.onData((data) => {
        if (sshSocket && sshSocket.readyState === WebSocket.OPEN) {
          sshSocket.send(JSON.stringify({ type: "input", data }));
        }
      });
    }

    function sendResize() {
      if (!term || !sshSocket || sshSocket.readyState !== WebSocket.OPEN) return;
      sshSocket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }

    function disconnectSsh() {
      if (sshSocket) {
        sshSocket.close();
        sshSocket = null;
      }
      sshBadgeEl.textContent = "idle";
      sshBadgeEl.classList.remove("live", "err");
    }

    function connectSsh(host) {
      disconnectSsh();
      try {
        ensureTerminal();
      } catch (e) {
        sshBadgeEl.textContent = "fout";
        sshBadgeEl.classList.add("err");
        sshDescEl.textContent = String(e.message || e);
        return;
      }
      term.clear();
      activeHost = host;
      sshTitleEl.textContent = host.title;
      const port = host.port && host.port !== 22 ? `:${host.port}` : "";
      const target = host.hostname || host.host || "local";
      sshDescEl.textContent = host.type === "local" ? "lokale shell op deze pc" : `${host.user || "root"}@${target}${port}`;
      sshBadgeEl.textContent = "verbinden";

      document.querySelectorAll(".host-btn").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.hostId === host.id);
      });

      const url = `ws://127.0.0.1:${LOCAL_WS_PORT}/`;
      sshSocket = new WebSocket(url);

      sshSocket.onopen = () => {
        const payload = host.type === "local"
          ? { type: "local" }
          : {
              host: host.hostname || host.host,
              port: host.port || 22,
              user: host.user || "root",
              auth: host.auth || "key",
              key_file: host.key_file || "",
              password: host.password || "",
            };
        sshSocket.send(JSON.stringify(payload));
        sshBadgeEl.textContent = "live";
        sshBadgeEl.classList.add("live");
        fitAddon.fit();
        sendResize();
      };
      sshSocket.onmessage = (ev) => term.write(ev.data);
      sshSocket.onerror = () => {
        sshBadgeEl.textContent = "fout";
        sshBadgeEl.classList.add("err");
        term.write("\r\n[verbindingsfout — start ~/.local/bin/pbs-ssh-local.py op je desktop]\r\n");
      };
      sshSocket.onclose = () => {
        if (sshBadgeEl.textContent === "live") {
          sshBadgeEl.textContent = "gesloten";
          sshBadgeEl.classList.remove("live");
        }
      };
    }

    function createHostButton(host) {
      const item = document.createElement("div");
      item.className = "host-item";

      const btn = document.createElement("button");
      btn.className = "host-btn";
      btn.dataset.hostId = host.id;
      const port = host.port && host.port !== 22 ? `:${host.port}` : "";
      btn.title = host.type === "local" ? "lokale shell" : `${host.user}@${host.hostname || host.host}${port}`;
      const titleSpan = document.createElement("span");
      titleSpan.textContent = host.title;
      btn.appendChild(titleSpan);
      const sub = document.createElement("small");
      sub.style.display = "block";
      sub.style.color = "var(--muted)";
      sub.style.fontSize = ".68rem";
      sub.style.marginTop = ".15rem";
      sub.textContent = host.type === "local" ? "lokale shell" : `${host.hostname || host.host}${port}`;
      btn.appendChild(sub);
      btn.addEventListener("click", () => connectSsh(host));

      const actions = document.createElement("div");
      actions.className = "host-item-actions";

      const editBtn = document.createElement("button");
      editBtn.className = "btn";
      editBtn.textContent = "edit";
      editBtn.title = "SSH host bewerken";
      editBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openEditSshModal(host);
      });

      const deleteBtn = document.createElement("button");
      deleteBtn.className = "btn btn-danger";
      deleteBtn.textContent = "wis";
      deleteBtn.title = "SSH host permanent verwijderen";
      deleteBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSshHost(host);
      });

      if (authState.permissions?.manage_ssh) {
        actions.append(editBtn, deleteBtn);
        item.append(btn, actions);
      } else {
        item.append(btn);
      }
      hostListEl.appendChild(item);
    }

    sshReconnectEl.addEventListener("click", () => {
      if (activeHost) connectSsh(activeHost);
    });

    async function bootDashboard() {
      const res = await apiFetch("/api/config");
      config = await res.json();

      titleEl.textContent = config.title || "Homelab Dashboard";
      document.title = config.title || "Homelab Dashboard";
      statusLabelEl.textContent = config.status?.label || "Status";
      const interval = (config.status?.interval_seconds || 5) * 1000;
      intervalEl.textContent = (interval / 1000) + "s";
      panelCountEl.textContent = String(config.panels?.length || 0);

      categoryMeta = config.categories || {};
      BUILTIN_CATEGORY_ORDER.forEach((catId) => {
        if (!categoryMeta[catId]) {
          categoryMeta[catId] = { title: catId, sub: "" };
        }
      });

      (config.panels || []).forEach(createPanel);
      const categories = {};
      getCategoryOrder().forEach((catId) => { categories[catId] = 0; });
      (config.panels || []).forEach((p) => {
        const cat = p.category || "proxmox";
        categories[cat] = (categories[cat] || 0) + 1;
      });
      renderLogCategories(categories);
      const defaultCat = getCategoryOrder().find((c) => categories[c]) || "proxmox";
      applyLogCategory(defaultCat);
      sshHosts = migrateServerSshHosts();
      renderSshHosts();
      await checkLocalBridge();
      setInterval(checkLocalBridge, 15000);

      await refreshStatus();
      if (statusTimer) clearInterval(statusTimer);
      statusTimer = setInterval(refreshStatus, interval);

      switchView("logs");
    }

    async function boot() {
      pendingResetToken = readResetTokenFromUrl();
      if (pendingResetToken) {
        showLoginGate(true, "reset");
        await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
      }
      const ready = await loadAuthStatus();
      if (!ready) return;
      await bootDashboard();
    }

    boot().catch((err) => {
      statusEl.textContent = "Dashboard laden mislukt: " + err;
      statusEl.className = "status-card offline";
    });
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "HomelabDashboard/4.3"

    def log_message(self, fmt, *args):
        pass

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _cookie_value(self, name: str) -> str:
        cookie = self.headers.get("Cookie", "")
        prefix = f"{name}="
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith(prefix):
                return part.split("=", 1)[1]
        return ""

    def _current_user(self) -> str | None:
        if not auth_enabled():
            return None
        username = verify_session(self._cookie_value(SESSION_COOKIE))
        if not username:
            return None
        user = get_dashboard_user(username)
        if not user or not user.get("enabled"):
            return None
        return username

    def _set_session_cookie(self, token: str) -> None:
        max_age = SESSION_DAYS * 86400
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; Max-Age={max_age}; SameSite=Lax",
        )

    def _clear_session_cookie(self) -> None:
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax",
        )

    def _require_auth(self) -> str | None:
        if not auth_enabled():
            return None
        user = self._current_user()
        if not user:
            self._send_json({"ok": False, "error": "Niet ingelogd"}, code=401)
            return False
        return user

    def _require_role(self, min_role: str = "viewer") -> str | None:
        user = self._require_auth()
        if user is False:
            return False
        if user is None:
            return None
        if not user_has_role(user, min_role):
            self._send_json({"ok": False, "error": "Onvoldoende rechten"}, code=403)
            return False
        return user

    def _send_json(self, payload: dict, code: int = 200, extra_headers: list[tuple[str, str]] | None = None):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str) -> bool:
        if not path.startswith("/static/"):
            return False
        rel = path.removeprefix("/static/")
        if ".." in rel or rel.startswith("/"):
            self.send_error(403)
            return True
        file_path = STATIC_DIR / rel
        if not file_path.is_file():
            self.send_error(404)
            return True
        content = file_path.read_bytes()
        ctype = "application/octet-stream"
        if rel.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif rel.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif rel.endswith(".svg"):
            ctype = "image/svg+xml; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        config = load_config()

        if self._serve_static(path):
            return

        if path == "/api/auth/status":
            self._send_json(auth_status_payload(self))
            return

        if path == "/api/auth/reset-info":
            qs = parse_qs(urlparse(self.path).query)
            token = (qs.get("token") or [""])[0]
            row = lookup_reset_token(token)
            if not row:
                self._send_json({"ok": False, "error": "Reset-link is ongeldig of verlopen"}, code=400)
                return
            self._send_json({"ok": True, "username": row["username"]})
            return

        if path == "/reset":
            qs = parse_qs(urlparse(self.path).query)
            token = (qs.get("token") or [""])[0].strip()
            if token:
                self.send_response(302)
                self.send_header("Location", f"/?reset={token}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return

        if path in ("/", "/reset"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self._require_auth() is False:
            accept = self.headers.get("Accept", "")
            if "text/html" in accept and path.startswith("/api/"):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            return

        if path == "/api/users":
            user = self._require_role("admin")
            if user is False:
                return
            self._send_json({"ok": True, "users": list_dashboard_users(), "roles": [
                {"id": r, "label": ROLE_LABELS[r]} for r in DASHBOARD_ROLES
            ]})
            return

        if path == "/api/config":
            if self._require_role("viewer") is False:
                return
            self._send_json(public_config(config))
            return

        if path == "/api/status":
            if self._require_role("viewer") is False:
                return
            self._send_json(run_status(config))
            return

        if path == "/api/ssh/resolve":
            if self._require_role("operator") is False:
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                port_raw = (query.get("port") or [""])[0].strip()
                port = int(port_raw) if port_raw else None
                conn = resolve_ssh_target(
                    (query.get("host") or [""])[0],
                    user=(query.get("user") or [""])[0],
                    port=port,
                    key_file=(query.get("key_file") or [""])[0],
                )
                self._send_json({
                    "ok": True,
                    "alias": conn.get("alias"),
                    "host": conn["host"],
                    "port": conn["port"],
                    "user": conn["user"],
                    "key_file": conn["key_file"],
                    "local": conn["local"],
                })
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, code=400)
            return

        if path == "/api/containers":
            if self._require_role("operator") is False:
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                conn = proxmox_connection_from_query(query)
                self._send_json({"containers": list_containers(conn)})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, code=500)
            return

        if path == "/api/docker/containers":
            if self._require_role("operator") is False:
                return
            query = parse_qs(urlparse(self.path).query)
            try:
                conn = docker_connection_from_query(query)
                self._send_json({"containers": list_docker_containers(conn)})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, code=500)
            return

        if path == "/api/units":
            if self._require_role("operator") is False:
                return
            query = parse_qs(urlparse(self.path).query)
            ctid = (query.get("ctid") or [None])[0]
            try:
                conn = proxmox_connection_from_query(query)
                if conn:
                    self._send_json({"units": list_proxmox_systemd_units(conn, ctid)})
                else:
                    self._send_json({"units": list_systemd_units(ctid)})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, code=500)
            return

        if path.startswith("/api/logs/"):
            if self._require_role("viewer") is False:
                return
            panel_id = path.removeprefix("/api/logs/")
            panel = panel_map(config).get(panel_id)
            if not panel:
                self.send_error(404, "Onbekend panel")
                return

            command = panel.get("command")
            if not command:
                self.send_error(500, "Panel heeft geen command")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def write(msg: str):
                try:
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except BrokenPipeError:
                    raise StopIteration

            try:
                stream_command(command, write)
            except (BrokenPipeError, StopIteration):
                pass
            return

        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        config = load_config()

        try:
            payload = self._read_json_body()
            if path == "/api/auth/forgot-password":
                try:
                    create_password_reset(str(payload.get("email", "")))
                    self._send_json({
                        "ok": True,
                        "message": "Reset-link verstuurd — check je inbox.",
                    })
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, code=400)
                except RuntimeError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, code=503)
                return
            if path == "/api/auth/reset-password":
                try:
                    username = reset_password_with_token(
                        str(payload.get("token", "")),
                        str(payload.get("password", "")),
                    )
                    self._send_json({"ok": True, "username": username})
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, code=400)
                return
            if path == "/api/auth/login":
                if not auth_enabled():
                    self._send_json({"ok": True, "auth_enabled": False})
                    return
                username = str(payload.get("username", "")).strip().lower()
                password = str(payload.get("password", ""))
                user = verify_dashboard_login(username, password)
                if not user:
                    self._send_json({"ok": False, "error": "Ongeldige gebruikersnaam of wachtwoord"}, code=401)
                    return
                token = create_session(username)
                role = user["role"]
                self._send_json(
                    {
                        "ok": True,
                        "username": username,
                        "role": role,
                        "role_label": ROLE_LABELS.get(role, role),
                        "permissions": role_permissions(role),
                    },
                    extra_headers=[(
                        "Set-Cookie",
                        f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; Max-Age={SESSION_DAYS * 86400}; SameSite=Lax",
                    )],
                )
                return
            if path == "/api/auth/logout":
                self._send_json(
                    {"ok": True},
                    extra_headers=[(
                        "Set-Cookie",
                        f"{SESSION_COOKIE}=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax",
                    )],
                )
                return
            if path == "/api/auth/password":
                user = self._require_auth()
                if user is False:
                    return
                try:
                    change_dashboard_password(
                        user,
                        str(payload.get("current_password", "")),
                        str(payload.get("new_password", "")),
                    )
                    if DASHBOARD_LOGIN_PATH.exists() and user == "admin":
                        login_info = json.loads(DASHBOARD_LOGIN_PATH.read_text(encoding="utf-8"))
                        login_info["password"] = str(payload.get("new_password", ""))
                        _save_secret_json(DASHBOARD_LOGIN_PATH, login_info)
                    self._send_json({"ok": True})
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, code=400)
                return
            if path == "/api/users/create":
                actor = self._require_role("admin")
                if actor is False:
                    return
                user = create_dashboard_user(
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                    str(payload.get("role", "viewer")),
                    email=payload.get("email"),
                )
                self._send_json({"ok": True, "user": user})
                return
            if path == "/api/users/update":
                actor = self._require_role("admin")
                if actor is False:
                    return
                username = str(payload.get("username", ""))
                try:
                    if "enabled" in payload and not payload.get("enabled"):
                        if normalize_username(username) == actor:
                            raise ValueError("Je kunt je eigen account niet uitschakelen")
                    user = update_dashboard_user(
                        username,
                        role=payload.get("role"),
                        password=payload.get("password") or None,
                        enabled=payload.get("enabled") if "enabled" in payload else None,
                        email=payload.get("email") if "email" in payload else None,
                        clear_email=bool(payload.get("clear_email")),
                    )
                    self._send_json({"ok": True, "user": user})
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, code=400)
                return
            if path == "/api/users/kick":
                actor = self._require_role("admin")
                if actor is False:
                    return
                username = str(payload.get("username", ""))
                try:
                    if normalize_username(username) == actor:
                        raise ValueError("Je kunt je eigen sessie niet beëindigen")
                    user = kick_dashboard_user(username)
                    self._send_json({"ok": True, "user": user})
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, code=400)
                return
            if path == "/api/users/delete":
                actor = self._require_role("admin")
                if actor is False:
                    return
                delete_dashboard_user(str(payload.get("username", "")), actor=actor)
                self._send_json({"ok": True})
                return
            if path == "/api/add/panel":
                if self._require_role("operator") is False:
                    return
                panel = add_panel_entry(config, payload)
                self._send_json({"ok": True, "panel": panel})
                return
            if path == "/api/add/ssh":
                if self._require_role("operator") is False:
                    return
                host = add_ssh_entry(config, payload)
                self._send_json({"ok": True, "host": host})
                return
            if path == "/api/delete/panel":
                if self._require_role("operator") is False:
                    return
                panel_id = str(payload.get("id", "")).strip()
                delete_panel_entry(config, panel_id)
                self._send_json({"ok": True, "id": panel_id})
                return
            if path == "/api/edit/panel":
                if self._require_role("operator") is False:
                    return
                panel_id = str(payload.get("id", "")).strip()
                panel = update_panel_entry(config, panel_id, payload)
                self._send_json({"ok": True, "panel": {
                    "id": panel["id"],
                    "title": panel.get("title", panel["id"]),
                    "description": panel.get("description", ""),
                    "category": panel.get("category", "proxmox"),
                    "height": panel.get("height", 220),
                }})
                return
            if path == "/api/delete/ssh":
                if self._require_role("operator") is False:
                    return
                host_id = str(payload.get("id", "")).strip()
                delete_ssh_entry(config, host_id)
                self._send_json({"ok": True, "id": host_id})
                return
            if path == "/api/edit/ssh":
                if self._require_role("operator") is False:
                    return
                host_id = str(payload.get("id", "")).strip()
                host = update_ssh_entry(config, host_id, payload)
                self._send_json({"ok": True, "host": host})
                return
            if path == "/api/add/category":
                if self._require_role("operator") is False:
                    return
                category = add_category_entry(config, payload)
                self._send_json({"ok": True, **category})
                return
            if path == "/api/edit/category":
                if self._require_role("operator") is False:
                    return
                cat_id = str(payload.get("id", "")).strip()
                category = update_category_entry(config, cat_id, payload)
                self._send_json({"ok": True, "id": cat_id, "category": category})
                return
            if path == "/api/edit/status":
                if self._require_role("operator") is False:
                    return
                status = update_status_settings(config, payload)
                self._send_json({"ok": True, "status": {
                    "label": status.get("label", "Status"),
                    "proxmox_host": status.get("proxmox_host", ""),
                    "pbs_host": status.get("pbs_host", ""),
                    "interval_seconds": status.get("interval_seconds", 5),
                }})
                return
            if path == "/api/sync/panels":
                if self._require_role("operator") is False:
                    return
                category = str(payload.get("category", "")).strip() or None
                result = sync_log_panels(config, category)
                self._send_json({"ok": True, **result})
                return
            self.send_error(404)
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Ongeldige JSON"}, code=400)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, code=400)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"Serverfout: {exc}"}, code=500)


def main():
    ensure_dashboard_auth()
    config = load_config()
    try:
        sync_log_panels(config)
        config = load_config()
    except Exception as exc:
        print(f"Panel sync overgeslagen: {exc}")
    host = config.get("host", "0.0.0.0")
    port = int(config.get("port", 8765))

    ws_thread = threading.Thread(target=run_ws_server, args=(config,), daemon=True)
    ws_thread.start()

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard op {resolve_public_dashboard_url().rstrip('/')}")
    db_cfg = load_db_config()
    print(f"Database: MariaDB {db_cfg['host']}:{db_cfg['port']}/{db_cfg['database']}")
    httpd.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)