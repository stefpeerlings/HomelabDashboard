# Homelab Dashboard

Live log monitoring en browser-SSH voor Proxmox VE homelabs.  
Logs van Proxmox nodes, LXC containers, Docker, PBS en eigen commando's — met gebruikersbeheer en MariaDB-opslag.

## Functies

- Live log panels (SSE) per categorie: Proxmox, Backup, Containers, Docker
- Auto-panels voor VZDump, LXC, Docker containers
- Browser SSH-terminal (WebSocket)
- Gebruikersrollen: admin, operator, viewer
- Wachtwoord-reset via SMTP
- Configuratie in MariaDB (panels, SSH hosts, categorieën)

## Vereisten

- **MariaDB** (aparte LXC of host) met database `homelab_dashboard`
- **SSH-toegang** vanaf de dashboard-container naar Proxmox/PBS hosts
- Poorten **8765** (HTTP) en **8766** (WebSocket SSH)

## Database (MariaDB) — eerst dit

Het dashboard slaat panels, SSH-hosts, categorieën en gebruikers op in **MariaDB**.  
De database draait **niet** in de dashboard-container — alleen een netwerkverbinding ernaartoe.

### Stap 1 — Database aanmaken (op MariaDB-server/LXC)

Op je MariaDB-host (bijv. CT 130) als root:

```bash
curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-mariadb.sh -o /tmp/setup-mariadb.sh
bash /tmp/setup-mariadb.sh
```

Of handmatig met SQL (`scripts/setup-mariadb.sql` — vervang `CHANGE_ME`).

Dit maakt aan:
- Database `homelab_dashboard` (utf8mb4)
- Gebruiker `homelab_dashboard` met toegang vanaf `10.0.%` en `localhost`

### Stap 2 — Credentials op dashboard-container

```bash
bash /opt/homelab-dashboard/scripts/setup-credentials.sh
```

Bestand: `/root/.homelab-db/credentials/service.json`

### Stap 3 — Verbinding testen

```bash
bash /opt/homelab-dashboard/scripts/test-db-connection.sh
```

### Wat gebeurt bij eerste start?

De app maakt automatisch tabellen aan (`settings`, `panels`, `ssh_hosts`, `dashboard_users`, …).  
Bij een **lege** database: standaardinstellingen + gebruiker `admin` / `homelab123`.  
Bij een **bestaande** database: alle data blijft behouden (meerdere dashboard-instanties kunnen dezelfde DB delen — draai er liever maar één).

### Bestaande MariaDB hergebruiken

Heb je al CT 130 (`10.0.10.17`) met `homelab_dashboard`?  
Dan hoef je alleen `service.json` op de nieuwe dashboard-LXC in te vullen — geen data-migratie.

## Snelle installatie (LXC)

### Optie A — Nieuwe LXC op Proxmox (aanbevolen)

Op je Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/ct/homelab-dashboard.sh)"
```

### Optie B — Bestaande Debian LXC/container

In de container als root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/lxc-install.sh)
```

Zie [Database (MariaDB)](#database-mariadb--eerst-dit) hierboven voor de volledige DB-setup.

### Eerste login

- Gebruiker: `admin`
- Wachtwoord: `homelab123` (wijzig direct na eerste login)

## Update

In de container:

```bash
bash /opt/homelab-dashboard/lxc-install.sh --update
```

## Projectstructuur

```
homelab-dashboard/
├── homelab_dashboard.py   # Hoofdapplicatie
├── static/                # xterm.js, logo
├── lxc-install.sh         # Installatie in LXC
├── ct/homelab-dashboard.sh # Proxmox community-scripts installer
├── config/                # Voorbeeld-configs (geen secrets)
└── scripts/
    ├── setup-mariadb.sh      # Database aanmaken (op MariaDB-host)
    ├── setup-mariadb.sql     # SQL-variant
    ├── setup-credentials.sh  # service.json op dashboard
    └── test-db-connection.sh # Verbinding testen
```

## Omgevingsvariabelen

| Variabele | Default | Beschrijving |
|-----------|---------|--------------|
| `HOMELAB_APP_ROOT` | `/opt/homelab-dashboard` | Applicatiemap |
| `HOMELAB_CREDENTIALS_DIR` | `/root/.homelab-db/credentials` | Secrets |
| `HOMELAB_PUBLIC_URL` | auto | URL voor e-mail links |
| `HOMELAB_STATIC_DIR` | `$APP_ROOT/static` | Statische bestanden |

## SSH-keys

Plaats keys in `/root/.ssh/` en configureer `~/.ssh/config` met je Proxmox/PBS hosts.  
Het dashboard gebruikt SSH voor node-logs, LXC (`pct`), Docker (remote) en terminals.

## Handmatige installatie (zonder LXC-script)

```bash
apt install python3 python3-venv python3-pip git
git clone https://github.com/stefpeerlings/HomelabDashboard.git /opt/homelab-dashboard
cd /opt/homelab-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# credentials invullen, daarna:
python homelab_dashboard.py
```

## Licentie

MIT — zie [LICENSE](LICENSE).