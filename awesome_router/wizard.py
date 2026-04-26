"""Awesome Router 2 — interactive setup wizard.

Run on a fresh Ubuntu 24.04 install (called by /opt/awesome-router/install.sh)
or any time later to reconfigure. Idempotent: backs up existing config before
any change, validates before applying.

Modes:
    python3 -m awesome_router.wizard            # full setup if no config, else menu
    python3 -m awesome_router.wizard --menu     # menu only
    python3 -m awesome_router.wizard --reset    # wipe config (with confirmation)
    python3 -m awesome_router.wizard --add-wan  # just add a new WAN
"""
from __future__ import annotations
import argparse
import ipaddress
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime

from . import config as cfg
from . import netplan_gen
from .models import (
    Bandwidth, FailoverConfig, HealthConfig, LanConfig, Pair,
    RouterConfig, WanConfig,
)

# ─── colors ───────────────────────────────────────────────────────────────
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[1;32m"
BLUE = "\033[1;34m"
YELLOW = "\033[1;33m"
RED = "\033[1;31m"
CYAN = "\033[1;36m"
NC = "\033[0m"

NETPLAN_PATH = "/etc/netplan/01-router.yaml"
DEFAULT_LAN_CIDR = "10.10.10.1/28"


# ─── prompts ──────────────────────────────────────────────────────────────

def banner(text: str):
    bar = "─" * (len(text) + 4)
    print(f"\n{CYAN}┌{bar}┐\n│  {BOLD}{text}{NC}{CYAN}  │\n└{bar}┘{NC}\n")


def info(text: str):
    print(f"  {BLUE}ℹ{NC}  {text}")


def warn(text: str):
    print(f"  {YELLOW}⚠{NC}  {text}")


def err(text: str):
    print(f"  {RED}✗{NC}  {text}", file=sys.stderr)


def ok(text: str):
    print(f"  {GREEN}✓{NC}  {text}")


def section(num: int, total: int, title: str):
    print(f"\n{BOLD}[{num}/{total}] {title}{NC}\n")


def ask(prompt: str, default: str = "") -> str:
    """Ask for input. If a default is provided, show it in brackets."""
    if default:
        s = input(f"  {prompt} [{DIM}{default}{NC}]: ").strip()
        return s or default
    return input(f"  {prompt}: ").strip()


def ask_required(prompt: str, default: str = "", validator=None) -> str:
    while True:
        v = ask(prompt, default)
        if not v:
            err("This field is required.")
            continue
        if validator:
            try:
                validator(v)
            except Exception as e:
                err(str(e))
                continue
        return v


def ask_yn(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        s = input(f"  {prompt} [{DIM}{d}{NC}]: ").strip().lower()
        if not s:
            return default
        if s in ("y", "yes"):
            return True
        if s in ("n", "no"):
            return False


def ask_choice(prompt: str, options: list[str], default: int = 0) -> int:
    """Pick one of N options. Returns 0-based index."""
    while True:
        for i, o in enumerate(options, 1):
            marker = "•" if i - 1 == default else " "
            print(f"    {marker} {i}. {o}")
        s = input(f"  {prompt} [{DIM}{default + 1}{NC}]: ").strip()
        if not s:
            return default
        try:
            n = int(s) - 1
            if 0 <= n < len(options):
                return n
        except ValueError:
            pass
        err("Pick a number from the list.")


# ─── validators ───────────────────────────────────────────────────────────

def validate_cidr(s: str) -> ipaddress.IPv4Interface:
    return ipaddress.IPv4Interface(s)


def validate_ip(s: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(s)


# ─── interface detection ──────────────────────────────────────────────────

def detect_interfaces() -> list[dict]:
    """List ethernet-like interfaces with state and current IPs."""
    out = subprocess.check_output(["ip", "-o", "link", "show"]).decode()
    interfaces = []
    for line in out.splitlines():
        # "2: enX0: <BROADCAST,..> mtu 1500 ... link/ether 04:f1:1a:...  brd ..."
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        name = parts[1].strip()
        if name == "lo" or name.startswith("docker") or name.startswith("veth"):
            continue
        rest = parts[2]
        state = "UP" if "state UP" in rest else ("DOWN" if "state DOWN" in rest else "?")
        mac = ""
        if "link/ether" in rest:
            try:
                mac = rest.split("link/ether")[1].split()[0]
            except Exception:
                pass
        interfaces.append({"name": name, "state": state, "mac": mac, "ip": ""})

    # Add IPs
    out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"]).decode()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[1]
        cidr = parts[3]
        for entry in interfaces:
            if entry["name"] == iface and not entry["ip"]:
                entry["ip"] = cidr

    return interfaces


def print_interfaces(interfaces: list[dict]):
    print(f"  {DIM}Detected interfaces:{NC}")
    for i, e in enumerate(interfaces, 1):
        ip = e["ip"] or "no IPv4"
        state_color = GREEN if e["state"] == "UP" else RED
        print(f"    {i}. {BOLD}{e['name']:<8}{NC}  {state_color}{e['state']:<4}{NC}  "
              f"{DIM}{e['mac']:<17}{NC}  {ip}")


# ─── wizard steps ─────────────────────────────────────────────────────────

def step_lan(interfaces: list[dict]) -> LanConfig:
    section(1, 5, "LAN INTERFACE")
    print(textwrap.dedent("""\
        The LAN interface connects to your downstream router (e.g. UDM Pro).
        It will hold a static private IP that the downstream router uses as
        its WAN gateway. The other interfaces below will be your ISP uplinks.
    """))

    print_interfaces(interfaces)

    candidates = [e["name"] for e in interfaces]
    default_idx = 0
    name = ask_required("Which interface is the LAN side", candidates[default_idx])

    print()
    info(f"A {BOLD}/28{NC}{BLUE} subnet has 16 IPs (14 usable; router takes 1, leaving 13 for downstream devices)")
    info(f"A {BOLD}/29{NC}{BLUE} subnet has 8 IPs (6 usable; leaving 5)")
    info(f"A {BOLD}/24{NC}{BLUE} subnet has 256 IPs (254 usable; leaving 253). Probably overkill.")
    print()

    cidr = ask_required(
        "LAN gateway CIDR (this router's IP + mask)",
        DEFAULT_LAN_CIDR,
        validator=validate_cidr,
    )
    return LanConfig(interface=name, address=cidr)


def step_wans(interfaces: list[dict], lan_iface: str, lan_network: ipaddress.IPv4Network) -> dict[str, WanConfig]:
    section(2, 5, "WAN INTERFACES")
    print(textwrap.dedent("""\
        Now configure your internet uplinks. You can add as many as you want.
        Each WAN can be:
          • DHCP + masquerade — typical for residential ISPs (Telmex, etc.)
          • Static + 1:1 NAT — for fixed-IP business connections (e.g. Bestel)
    """))

    available = [e for e in interfaces if e["name"] != lan_iface]
    if not available:
        err("No interfaces left after LAN. Aborting.")
        sys.exit(1)

    wans: dict[str, WanConfig] = {}
    used_ifaces = set()
    used_table_ids = set()
    next_table_id = 100

    while True:
        idx = len(wans) + 1
        print(f"\n{BOLD}WAN #{idx}{NC}")

        free = [e for e in available if e["name"] not in used_ifaces]
        if not free:
            warn("No more interfaces available.")
            break

        print_interfaces(free)
        iface = ask_required("Interface for this WAN", free[0]["name"])
        used_ifaces.add(iface)

        wan_id_default = "bestel" if idx == 1 and ask_yn("Is this a static-IP business connection (e.g. Bestel)?", False) else f"wan{idx}"
        if wan_id_default == "bestel" or wan_id_default.startswith("wan"):
            pass  # we'll set details below

        is_static = wan_id_default == "bestel" or ask_yn("Static public IPs (vs DHCP)?", False)

        wan_id = ask_required("Short ID for this WAN (lowercase, no spaces)", wan_id_default).lower()
        name = ask_required("Display name", wan_id.title())

        # Pick table ID (auto-assign next free 100/200/300/...)
        while next_table_id in used_table_ids:
            next_table_id += 100
        table_id = int(ask("Routing table ID (auto)", str(next_table_id)))
        used_table_ids.add(table_id)

        bw_down = float(ask("Download bandwidth (Mbps, for graph scaling)", "1000"))
        bw_up = float(ask("Upload bandwidth (Mbps)", "1000"))

        if is_static:
            print()
            info("Static WAN — enter your ISP-assigned details:")
            print()
            gateway = ask_required("ISP gateway IP", validator=validate_ip)
            print(f"  {DIM}Enter all public IPs assigned to you (CIDR), one per line. Blank to finish:{NC}")
            addresses = []
            while True:
                a = ask("  Public CIDR (e.g. 200.188.147.118/29)")
                if not a:
                    if not addresses:
                        warn("Need at least one address.")
                        continue
                    break
                try:
                    validate_cidr(a)
                except Exception as e:
                    err(str(e))
                    continue
                addresses.append(a)

            print()
            print(f"  {DIM}Which one of those is the router's own IP (NOT 1:1 NATed to anything)?{NC}")
            router_ip = ask_required("Router's public IP", addresses[0].split("/")[0], validator=validate_ip)

            print()
            info("Now define 1:1 NAT pairs: each public IP maps to a private IP on your downstream router.")
            print(f"  {DIM}LAN subnet is {lan_network}. Pairs use private IPs from that range.{NC}")
            pairs = []
            while True:
                pub = ask("  Public IP (blank to finish)")
                if not pub:
                    break
                try:
                    validate_ip(pub)
                except Exception as e:
                    err(str(e))
                    continue
                priv = ask_required("  Private IP (in LAN subnet)", validator=validate_ip)
                if ipaddress.IPv4Address(priv) not in lan_network:
                    warn(f"  {priv} is outside {lan_network}; routing rules will still be added but the downstream router needs to use this IP.")
                pairs.append(Pair(public=pub, private=priv))

            wan = WanConfig(
                id=wan_id, name=name, interface=iface,
                type="static", gateway=gateway, table_id=table_id,
                nat_mode="onetoone",
                addresses=addresses, router_ip=router_ip,
                pairs=pairs, sources=[],
                metric=100,
                bandwidth=Bandwidth(down_mbps=bw_down, up_mbps=bw_up),
            )

        else:
            print()
            info("DHCP WAN — gateway will be auto-discovered.")
            metric = int(ask("Route metric (lower = preferred for default)", "10" if idx == 1 else str(10 * idx)))

            print()
            info("Source IPs from the LAN that should masquerade out via this WAN:")
            print(f"  {DIM}LAN subnet is {lan_network}. Each downstream router VLAN/network using this WAN needs a unique IP here.{NC}")
            sources = []
            while True:
                s = ask("  Private source IP (blank to finish)")
                if not s:
                    if not sources:
                        warn("  Need at least one source IP.")
                        continue
                    break
                try:
                    validate_ip(s)
                except Exception as e:
                    err(str(e))
                    continue
                if ipaddress.IPv4Address(s) not in lan_network:
                    warn(f"  {s} is outside {lan_network}; double-check this is reachable.")
                sources.append(s)

            wan = WanConfig(
                id=wan_id, name=name, interface=iface,
                type="dhcp", gateway="auto", table_id=table_id,
                nat_mode="masquerade",
                sources=sources, pairs=[],
                metric=metric,
                bandwidth=Bandwidth(down_mbps=bw_down, up_mbps=bw_up),
            )

        wans[wan_id] = wan
        ok(f"Added {name} ({iface}, {wan.type}, {wan.nat_mode})")
        next_table_id = max(used_table_ids) + 100

        if not ask_yn("Add another WAN?", False):
            break

    return wans


def step_default_wan(wans: dict[str, WanConfig]) -> str:
    section(3, 5, "VM DEFAULT WAN")
    print(textwrap.dedent("""\
        The router itself needs internet for things like apt updates and the
        public-IP detection of the dashboard. Pick one WAN to be its default.
    """))
    options = [f"{w.name}  ({w.interface}, {w.type})" for w in wans.values()]
    ids = list(wans.keys())
    idx = ask_choice("Which WAN should the router itself use", options, 0)
    return ids[idx]


def step_failover(wans: dict[str, WanConfig], lan_network: ipaddress.IPv4Network) -> FailoverConfig | None:
    section(4, 5, "FAILOVER (optional)")
    print(textwrap.dedent("""\
        Failover lets you give your downstream router a single private IP that
        is ALWAYS routed through the healthiest WAN. If the primary WAN fails,
        traffic transparently shifts to the next WAN — no reconfiguration needed
        on the downstream side.

        Strongly recommended if you have 2+ WANs.
    """))
    if not ask_yn("Configure failover?", len(wans) >= 2):
        return FailoverConfig(enabled=False)

    while True:
        fip = ask_required(
            "Failover IP (private, in LAN subnet)",
            str(list(lan_network.hosts())[7]) if len(list(lan_network.hosts())) > 7 else "",
            validator=validate_ip,
        )
        if ipaddress.IPv4Address(fip) not in lan_network:
            warn(f"{fip} is outside {lan_network}; double-check.")
        # Make sure it's not already a source/pair
        used = set()
        for w in wans.values():
            used.update(w.private_ips)
        if fip in used:
            err(f"{fip} is already used by a WAN as a source/pair. Pick a different IP.")
            continue
        break

    print()
    info("Priority order: WAN at the top is tried first. Drag-reorder later in the GUI.")
    ordered = []
    remaining = list(wans.keys())
    while remaining:
        opts = [f"{wans[w].name}  ({wans[w].interface})" for w in remaining]
        idx = ask_choice(f"Priority slot #{len(ordered) + 1}", opts, 0)
        ordered.append(remaining.pop(idx))

    return FailoverConfig(
        enabled=True,
        failover_ip=fip,
        table_id=1000,
        priority=ordered,
        health=HealthConfig(),  # defaults: 8.8.8.8, 1.1.1.1, 9.9.9.9, 10s interval
    )


def step_review(lan: LanConfig, wans: dict[str, WanConfig], default_wan: str,
                failover: FailoverConfig | None) -> bool:
    section(5, 5, "REVIEW & APPLY")
    print(f"  {BOLD}LAN:{NC} {lan.interface} = {lan.address}")
    print(f"  {BOLD}VM default WAN:{NC} {wans[default_wan].name}\n")
    print(f"  {BOLD}WANs:{NC}")
    for wid, w in wans.items():
        print(f"    • {GREEN}{w.name}{NC} ({wid})  {w.interface}  {w.type}  {w.nat_mode}  "
              f"table {w.table_id}  {w.bandwidth.down_mbps:g}/{w.bandwidth.up_mbps:g} Mbps")
        if w.nat_mode == "onetoone":
            for p in w.pairs:
                print(f"        {p.public} ↔ {p.private}")
        else:
            for s in w.sources:
                print(f"        source {s}")
    if failover and failover.enabled:
        print(f"\n  {BOLD}Failover:{NC}")
        print(f"    failover IP: {GREEN}{failover.failover_ip}{NC}")
        print(f"    priority:    {' → '.join(wans[w].name for w in failover.priority)}")
    print()
    return ask_yn("Apply this configuration?", True)


# ─── apply ────────────────────────────────────────────────────────────────

def apply_config(lan: LanConfig, wans: dict[str, WanConfig], default_wan: str,
                  failover: FailoverConfig | None):
    print()
    banner("APPLYING CONFIGURATION")

    config = RouterConfig(
        version=2, lan=lan, vm_default_wan=default_wan, wans=wans,
        failover=failover or FailoverConfig(enabled=False),
    )
    errors = cfg.validate(config)
    if errors:
        err("Validation errors:")
        for e in errors:
            print(f"    - {e}")
        sys.exit(1)

    # 1. Backup existing files
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if os.path.exists(NETPLAN_PATH):
        shutil.copy2(NETPLAN_PATH, f"{NETPLAN_PATH}.bak-{ts}")
        ok(f"Backed up existing netplan to {NETPLAN_PATH}.bak-{ts}")

    # 2. Write netplan
    netplan_yaml = netplan_gen.generate(config)
    with open(NETPLAN_PATH, "w") as f:
        f.write(netplan_yaml)
    os.chmod(NETPLAN_PATH, 0o600)
    ok(f"Wrote {NETPLAN_PATH}")

    # 3. Write /etc/awesome-router.yaml
    cfg.save(config, backup=True)
    ok(f"Wrote {cfg.DEFAULT_CONFIG_PATH}")

    # 4. Apply netplan
    info("Applying netplan (DHCP may take a few seconds)...")
    r = subprocess.run(["netplan", "apply"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        err(f"netplan apply failed: {r.stderr}")
        sys.exit(1)
    ok("Network configuration applied")

    # 5. Wait for DHCP
    import time
    time.sleep(5)

    # 6. Run apply engine
    from . import apply_engine
    info("Building routing rules, NAT, and firewall...")
    result = apply_engine.apply(config, dry_run=False)
    if not result["ok"]:
        err(f"Apply engine failed: {result.get('error') or result.get('errors')}")
        sys.exit(1)
    ok(f"Applied {len(result.get('changes', []))} routing/firewall changes")

    # 7. Enable + start systemd services
    services = [
        "awesome-router-apply.service",
        "router-stats-sampler.service",
        "awesome-router-health.service",
        "awesome-router-web.service",
    ]
    for s in services:
        subprocess.run(["systemctl", "enable", s], capture_output=True, timeout=15)
        subprocess.run(["systemctl", "restart", s], capture_output=True, timeout=15)
    ok(f"Enabled and started {len(services)} services")

    # 8. Summary
    lan_ip = lan.address.split("/")[0]
    print()
    banner("DONE — AWESOME ROUTER IS LIVE")
    print(f"  {GREEN}🌐  Web GUI:{NC}      http://{lan_ip}:5000")
    print(f"  {GREEN}🔧  Config file:{NC}  /etc/awesome-router.yaml")
    print(f"  {GREEN}📊  Bandwidth DB:{NC} /var/lib/awesome-router-monitor.db")
    print()
    print(f"  {BOLD}Next steps on your downstream router (e.g. Unifi UDM Pro):{NC}")
    print(f"    1. Set the WAN port on the {lan.interface}-side network")
    if failover and failover.enabled:
        print(f"    2. Use static IP {GREEN}{failover.failover_ip}{NC} with gateway {GREEN}{lan_ip}{NC}")
        print(f"    3. The Awesome Router will auto-failover between WANs transparently")
    else:
        first_source = ""
        for w in wans.values():
            if w.private_ips:
                first_source = w.private_ips[0]
                break
        if first_source:
            print(f"    2. Use static IP {GREEN}{first_source}{NC} with gateway {GREEN}{lan_ip}{NC}")
    print()


# ─── menu mode ────────────────────────────────────────────────────────────

def show_menu():
    """Top-level menu when config already exists."""
    banner("AWESOME ROUTER 2 — Setup Menu")
    try:
        existing = cfg.load()
        info(f"Existing config: {len(existing.wans)} WAN(s), failover {'on' if existing.failover.enabled else 'off'}")
    except Exception:
        existing = None
        warn("Existing config could not be loaded (may be corrupt)")

    options = [
        "Re-run full wizard (replaces config)",
        "Add another WAN",
        "Update code from GitHub (no config change)",
        "Reset everything (DESTRUCTIVE)",
        "Exit",
    ]
    idx = ask_choice("What would you like to do", options, 4)

    if idx == 0:
        return run_full_wizard()
    if idx == 1:
        return add_wan_only(existing)
    if idx == 2:
        return update_only()
    if idx == 3:
        return reset_install()
    print("Goodbye.")


def add_wan_only(existing: RouterConfig | None):
    if existing is None:
        err("Cannot add WAN: existing config could not be loaded. Use --reset instead.")
        sys.exit(1)
    interfaces = detect_interfaces()
    lan_network = ipaddress.IPv4Interface(existing.lan.address).network
    new = step_wans_loop_one(interfaces, existing, lan_network)
    if not new:
        info("No WAN added.")
        return
    existing.wans[new.id] = new
    ok(f"Added {new.name}. Click Apply Changes in the web GUI, or run apply now.")
    if ask_yn("Apply now?", True):
        from . import apply_engine
        apply_engine.apply(existing)
        ok("Applied.")
    cfg.save(existing)


def step_wans_loop_one(interfaces, existing, lan_network) -> WanConfig | None:
    used_ifaces = {existing.lan.interface} | {w.interface for w in existing.wans.values()}
    free = [e for e in interfaces if e["name"] not in used_ifaces]
    if not free:
        warn("No free interfaces left.")
        return None
    print_interfaces(free)
    iface = ask_required("Interface for new WAN", free[0]["name"])
    name = ask_required("Display name", iface.upper())
    wan_id = ask_required("WAN ID (lowercase, no spaces)", name.lower().replace(" ", ""))
    is_static = ask_yn("Static public IPs?", False)

    used_tables = {w.table_id for w in existing.wans.values()}
    next_t = 100
    while next_t in used_tables:
        next_t += 100
    table_id = int(ask("Routing table ID", str(next_t)))

    if is_static:
        gateway = ask_required("ISP gateway IP", validator=validate_ip)
        addresses = []
        while True:
            a = ask("Public CIDR (blank to finish)")
            if not a:
                break
            addresses.append(a)
        router_ip = ask_required("Router's own public IP", addresses[0].split("/")[0])
        pairs = []
        while True:
            pub = ask("Public IP for 1:1 pair (blank to finish)")
            if not pub:
                break
            priv = ask_required("Private IP")
            pairs.append(Pair(public=pub, private=priv))
        return WanConfig(
            id=wan_id, name=name, interface=iface, type="static",
            gateway=gateway, table_id=table_id, nat_mode="onetoone",
            addresses=addresses, router_ip=router_ip, pairs=pairs, sources=[],
            metric=100,
        )

    metric = int(ask("Route metric", "20"))
    sources = []
    while True:
        s = ask("Source IP (blank to finish)")
        if not s:
            if sources:
                break
            warn("Need at least one source.")
            continue
        sources.append(s)
    return WanConfig(
        id=wan_id, name=name, interface=iface, type="dhcp",
        gateway="auto", table_id=table_id, nat_mode="masquerade",
        sources=sources, pairs=[], metric=metric,
    )


def update_only():
    info("Pulling latest code from GitHub...")
    r = subprocess.run(["git", "-C", "/opt/awesome-router", "pull"],
                        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        err(f"git pull failed: {r.stderr}")
        return
    print(r.stdout)
    info("Restarting services...")
    for s in ["awesome-router-web", "awesome-router-health", "router-stats-sampler"]:
        subprocess.run(["systemctl", "restart", s], capture_output=True, timeout=15)
    ok("Update complete.")


def reset_install():
    warn("This will:")
    print("    - Stop all Awesome Router services")
    print("    - Remove /etc/awesome-router.yaml")
    print("    - Remove /etc/netplan/01-router.yaml (you'll lose network config!)")
    print("    - Keep code at /opt/awesome-router and bandwidth DB")
    if not ask_yn("Are you ABSOLUTELY sure?", False):
        info("Cancelled.")
        return
    confirm = input(f"  Type {RED}RESET{NC} to confirm: ")
    if confirm != "RESET":
        info("Cancelled.")
        return
    for s in ["awesome-router-web", "awesome-router-health",
              "awesome-router-apply", "router-stats-sampler"]:
        subprocess.run(["systemctl", "stop", s], capture_output=True, timeout=15)
    for f in ["/etc/awesome-router.yaml", NETPLAN_PATH]:
        if os.path.exists(f):
            os.unlink(f)
    ok("Reset complete. Run the wizard again to reconfigure.")


# ─── main ─────────────────────────────────────────────────────────────────

def run_full_wizard():
    banner("AWESOME ROUTER 2 — Setup Wizard")
    print(textwrap.dedent("""\
        Welcome! This wizard configures the Awesome Router on this machine.
        After it finishes, the router will be live, routing and NAT-ing
        traffic from your downstream router (e.g. Unifi UDM Pro) through
        your ISP uplinks.

        Press Ctrl+C any time to cancel. Existing config is backed up before
        any change.
    """))

    interfaces = detect_interfaces()
    if not interfaces:
        err("No network interfaces detected. Aborting.")
        sys.exit(1)

    lan = step_lan(interfaces)
    lan_network = ipaddress.IPv4Interface(lan.address).network
    wans = step_wans(interfaces, lan.interface, lan_network)
    if not wans:
        err("Need at least one WAN. Aborting.")
        sys.exit(1)
    default_wan = step_default_wan(wans)
    failover = step_failover(wans, lan_network)
    if not step_review(lan, wans, default_wan, failover):
        info("Cancelled.")
        return
    apply_config(lan, wans, default_wan, failover)


def main():
    p = argparse.ArgumentParser(description="Awesome Router 2 setup wizard")
    p.add_argument("--menu", action="store_true", help="Show menu (skip wizard)")
    p.add_argument("--reset", action="store_true", help="Wipe config (with confirmation)")
    p.add_argument("--add-wan", action="store_true", help="Just add a new WAN")
    args = p.parse_args()

    if os.geteuid() != 0:
        err("This wizard must be run as root (or via sudo).")
        sys.exit(1)

    try:
        if args.reset:
            return reset_install()
        if args.add_wan:
            try:
                existing = cfg.load()
            except Exception:
                err("No existing config — run full wizard first.")
                sys.exit(1)
            return add_wan_only(existing)
        if args.menu or os.path.exists(cfg.DEFAULT_CONFIG_PATH):
            return show_menu()
        return run_full_wizard()
    except KeyboardInterrupt:
        print("\n")
        warn("Cancelled by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
