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

### Database credentials

```bash
bash /opt/homelab-dashboard/scripts/setup-credentials.sh
# of handmatig: /root/.homelab-db/credentials/service.json
```

Voorbeeld: `config/service.json.example`

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
└── scripts/setup-credentials.sh
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