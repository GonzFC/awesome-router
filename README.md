# Awesome Router 2

A multi-WAN router that turns a small Ubuntu 24.04 VM into a transparent
front-end for any downstream router (Unifi UDM Pro, MikroTik, EdgeRouter,
etc.). Aggregate multiple ISP connections, route specific networks through
specific WANs, automatically fail over when one WAN dies, and manage it
all from a clean web GUI.

## Features

- **N WAN connections** — DHCP or static, mixed freely
- **1:1 NAT** for static public IPs (e.g. business connections with fixed addresses)
- **Masquerade** for dynamic IPs (residential ISPs)
- **Source-based routing** — different downstream networks can exit through different WANs
- **Automatic failover** — give your downstream router one private IP that's always reachable
- **Health monitoring** — `fping` + TCP/443 tiebreakers across multiple targets per WAN, with hysteresis (no flapping)
- **Web GUI** — dark-themed, mobile-friendly, with bandwidth graphs (24h / 7d / 30d)
- **Bandwidth sampling** — 15s resolution, 30-day retention in SQLite
- **Snapshots & rollback** — every apply takes a snapshot; one-click revert from the GUI
- **Recovery actions** — flush + re-apply, restart networkd, all from the GUI (no SSH needed when stuck)
- **Idempotent** — safe to re-run the installer or apply at any time

## Quickstart

On a fresh Ubuntu Server 24.04 install (any size — 1 vCPU and 1 GB RAM is plenty):

```bash
curl -fsSL https://raw.githubusercontent.com/GonzFC/awesome-router/main/install.sh | sudo bash
```

That will:
1. Install the required packages (git, python3, flask, nftables, fping, nmap)
2. Clone the repo to `/opt/awesome-router`
3. Install the four systemd services
4. Launch an interactive setup wizard

The wizard walks you through:
1. **LAN interface** — which NIC connects to your downstream router, and what private CIDR (default `10.10.10.1/28`)
2. **WAN interfaces** — add as many as you have, mark each as DHCP+masquerade or static+1:1 NAT
3. **VM default WAN** — which WAN the router itself uses for updates and dashboard public-IP detection
4. **Failover** — optional but recommended for 2+ WANs: a private IP the downstream router always uses
5. **Review & apply** — confirms, generates netplan + config, applies routing/NAT/firewall, starts services

After it finishes, browse to `http://<lan-ip>:5000` and configure your downstream router's WAN.

## Architecture

```
   ┌────────────┐    ┌────────────┐    ┌────────────┐
   │   ISP A    │    │   ISP B    │    │   ISP C    │
   │  (DHCP)    │    │  (static)  │    │  (DHCP)    │
   └─────┬──────┘    └──────┬─────┘    └──────┬─────┘
         │                  │                  │
       enX1               enX2               enX3
         └──────────┬───────┴──────────┬───────┘
                    │                  │
            ┌───────┴──────────────────┴───────┐
            │      AWESOME ROUTER (Ubuntu)     │
            │  • netplan (interfaces)          │
            │  • policy routing (per-WAN tbls) │
            │  • nftables (NAT + firewall)     │
            │  • health daemon (failover)      │
            │  • Flask web GUI                 │
            └───────────────┬──────────────────┘
                            │
                          enX0  (LAN, e.g. 10.10.10.1/28)
                            │
                  ┌─────────┴─────────┐
                  │   UDM Pro / etc.  │
                  └─────────┬─────────┘
                            │
                       LAN / VLANs
```

## Components

| Component | Purpose |
|-----------|---------|
| `awesome_router/config.py` | Loads & validates `/etc/awesome-router.yaml` |
| `awesome_router/apply_engine.py` | Idempotent apply: sysctl, ip rules, routing tables, nftables, netplan |
| `awesome_router/health_daemon.py` | Pings 8.8.8.8 / 1.1.1.1 / 9.9.9.9 + ISP gateway via each WAN; updates failover routing |
| `awesome_router/wizard.py` | Interactive setup wizard |
| `awesome_router/rollback.py` | Snapshots routing state; restores on demand |
| `web/` | Flask web GUI (dashboard, WAN CRUD, failover, system) |
| `installer/scripts/router-stats-sampler.py` | Background bandwidth sampler → SQLite |
| `installer/systemd/*.service` | Four systemd units |

## Systemd services

| Service | Type | Purpose |
|---------|------|---------|
| `awesome-router-apply.service` | oneshot at boot | Apply config to routes / nftables |
| `awesome-router-health.service` | always-on | Ping WANs, update failover route |
| `awesome-router-web.service` | always-on | Flask GUI on port 5000 (LAN-only) |
| `router-stats-sampler.service` | always-on | 15s bandwidth samples → SQLite |

## Reconfiguring

Re-run the installer at any time:

```bash
curl -fsSL https://raw.githubusercontent.com/GonzFC/awesome-router/main/install.sh | sudo bash
```

When config already exists, it shows a menu:
- Re-run full wizard (replaces config)
- Add another WAN
- Update code from GitHub (no config change)
- Reset everything (DESTRUCTIVE)

Or run the wizard directly:

```bash
sudo python3 -m awesome_router.wizard --add-wan
sudo python3 -m awesome_router.wizard --reset
```

## Web GUI

Default URL: `http://<lan-ip>:5000` — accessible only from the LAN side (nftables enforces this).

Pages:
- **Dashboard** — WAN cards with health dots, public IPs, live bandwidth + 24h/7d/30d graphs
- **WAN Interfaces** — add/edit/remove WANs, configure pairs / source IPs
- **Failover** — priority list (drag-reorder), health status, recent events
- **System** — services, resources, recovery actions (Reset Network Stack, Restart networkd), config viewer, firewall viewer

## Uninstall

```bash
sudo /opt/awesome-router/installer/uninstall.sh           # keeps bandwidth DB
sudo /opt/awesome-router/installer/uninstall.sh --purge   # removes everything
```

## License

MIT — see [LICENSE](LICENSE).

## Author

Gonzalo Fernández / VLABS AIT
