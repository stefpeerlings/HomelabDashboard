# Homelab Dashboard

<div align="right">

[![Release](https://img.shields.io/github/v/release/stefpeerlings/HomelabDashboard?label=release&style=for-the-badge)](https://github.com/stefpeerlings/HomelabDashboard/releases/latest)

</div>

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

- **MariaDB** wordt automatisch in de dashboard-LXC geïnstalleerd (lokaal op `127.0.0.1`)
- **SSH-toegang** vanaf de dashboard-container naar Proxmox/PBS hosts
- Poorten **8765** (HTTP) en **8766** (WebSocket SSH)
- **RAM:** minimaal 2 GB aanbevolen (MariaDB + dashboard)

## Database

Panels, SSH-hosts, categorieën en gebruikers staan in **MariaDB**.  
Bij een standaard LXC-installatie draait MariaDB **in dezelfde container** — geen aparte koppeling nodig.

### Standaard (lokaal, automatisch)

`lxc-install.sh` en de Proxmox-installer regelen dit zelf:

- MariaDB-server installeren
- Database `homelab_dashboard` + gebruiker aanmaken
- `service.json` schrijven naar `/root/.homelab-db/credentials/`
- Dashboard starten

Na installatie direct openen: `http://<container-ip>:8765`

### Optioneel — externe MariaDB

Gebruik je al een gedeelde MariaDB-server? Installeer met:

```bash
HOMELAB_DB_MODE=remote bash lxc-install.sh
```

Koppel daarna:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/stefpeerlings/HomelabDashboard/main/scripts/setup-database.sh)"
```

### Eerste start van de app

- Tabellen worden automatisch aangemaakt als ze nog niet bestaan
- **Lege database:** standaardinstellingen + gebruiker `admin` (wachtwoord `homelab123` — direct wijzigen)
- **Bestaande database:** alle panels en gebruikers blijven behouden
- Draai liever **één** dashboard tegelijk per database

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

MariaDB wordt automatisch lokaal geïnstalleerd.

### Eerste login

| | |
|---|---|
| Gebruiker | `admin` |
| Wachtwoord | `homelab123` |

Wijzig dit meteen na de eerste login via **Account → Wachtwoord**.

## Wachtwoord reset via e-mail

Wachtwoord vergeten werkt alleen als SMTP is ingesteld en `enabled` op `true` staat.

| | |
|---|---|
| Voorbeeld op GitHub | [config/smtp.json.example](https://github.com/stefpeerlings/HomelabDashboard/blob/main/config/smtp.json.example) |
| Configuratie op server | `/root/.homelab-db/credentials/smtp.json` |

### Instellen

1. Open of maak `smtp.json` in de dashboard-container (zie [voorbeeld op GitHub](https://github.com/stefpeerlings/HomelabDashboard/blob/main/config/smtp.json.example))
2. Zet `"enabled": true` en vul `host`, `port`, `user`, `password` en `from` in
3. Stel `dashboard_url` in op de URL waar gebruikers het dashboard openen (bijv. `http://10.0.10.22:8765/`)
4. Herstart: `systemctl restart homelab-dashboard`
5. Koppel per gebruiker een e-mailadres via **Account → Gebruikers**

Zonder SMTP toont de loginknop *Wachtwoord vergeten?* een melding met een link naar het GitHub-voorbeeld.

## Projectstructuur

```
homelab-dashboard/
├── homelab_dashboard.py    # Hoofdapplicatie
├── static/                 # xterm.js, logo
├── lxc-install.sh          # Installatie in LXC
├── ct/homelab-dashboard.sh # Proxmox install + update (zelfde link)
├── config/                 # Voorbeeld-configs (smtp.json.example, geen secrets)
└── scripts/
    ├── setup-local-mariadb.sh # Lokaal in dashboard-LXC (standaard)
    ├── setup-database.sh      # Externe MariaDB koppelen
    ├── setup-mariadb.sh       # Database op aparte MariaDB-host
    ├── setup-mariadb.sql
    ├── setup-credentials.sh   # Alleen service.json
    └── test-db-connection.sh
```

## Omgevingsvariabelen

| Variabele | Default | Beschrijving |
|-----------|---------|--------------|
| `HOMELAB_APP_ROOT` | `/opt/homelab-dashboard` | Applicatiemap |
| `HOMELAB_CREDENTIALS_DIR` | `/root/.homelab-db/credentials` | Secrets |
| `HOMELAB_PUBLIC_URL` | auto-detect | Vaste URL (zet `HOMELAB_AUTO_PUBLIC_URL=0`) |
| `HOMELAB_AUTO_PUBLIC_URL` | `1` | IP automatisch via `hostname -I` |
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