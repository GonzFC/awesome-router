"""One-time migration: read old 3-file config and produce /etc/awesome-router.yaml."""
from __future__ import annotations
import re
import subprocess
import sys

from . import config as cfg
from .models import Bandwidth, LanConfig, Pair, RouterConfig, WanConfig

OLD_CONF = "/etc/udm-router.conf"
OLD_PAIRS = "/etc/udm-router.pairs"
OLD_TELMEX = "/etc/udm-router.telmex"
NETPLAN = "/etc/netplan/01-router.yaml"


def _read_shell_vars(path: str) -> dict[str, str]:
    """Parse a bash-style KEY=VALUE config file."""
    result = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^(\w+)\s*=\s*"?([^"#]*)"?\s*(?:#.*)?$', line)
            if m:
                result[m.group(1)] = m.group(2).strip()
    return result


def _read_pairs(path: str) -> list[Pair]:
    """Parse the udm-router.pairs file."""
    pairs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                pairs.append(Pair(public=parts[0], private=parts[1]))
    return pairs


def _read_sources(path: str) -> list[str]:
    """Parse the udm-router.telmex file."""
    sources = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sources.append(line.split()[0])
    return sources


def _read_netplan_addresses(path: str, interface: str) -> list[str]:
    """Extract addresses for an interface from netplan YAML."""
    try:
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        eth = data.get("network", {}).get("ethernets", {}).get(interface, {})
        return eth.get("addresses", [])
    except Exception:
        return []


def _discover_dhcp_gateway(interface: str) -> str:
    """Get current default gateway for a DHCP interface."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "route", "show", "dev", interface, "default"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
        # "default via 192.168.1.254 ..."
        for line in out.splitlines():
            parts = line.split()
            if "via" in parts:
                idx = parts.index("via")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    except Exception:
        pass
    return "auto"


def _read_monitor_conf() -> dict:
    """Read bandwidth settings from the old monitor config."""
    try:
        import configparser
        cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        cp.read("/etc/awesome-router-monitor.conf")
        return {
            "bestel_bw_down": cp.getfloat("monitor", "bestel_bw_down", fallback=0),
            "bestel_bw_up": cp.getfloat("monitor", "bestel_bw_up", fallback=0),
            "telmex_bw_down": cp.getfloat("monitor", "telmex_bw_down", fallback=0),
            "telmex_bw_up": cp.getfloat("monitor", "telmex_bw_up", fallback=0),
        }
    except Exception:
        return {}


def build_config() -> RouterConfig:
    """Build a RouterConfig from the old 3-file format."""
    v = _read_shell_vars(OLD_CONF)
    pairs = _read_pairs(OLD_PAIRS)
    sources = _read_sources(OLD_TELMEX)
    monitor = _read_monitor_conf()

    lan_if = v.get("LAN_IF", "enX0")
    lan_ip = v.get("LAN_IP", "10.188.147.113")
    bestel_if = v.get("BESTEL_IF", "enX1")
    telmex_if = v.get("TELMEX_IF", "enX2")
    bestel_gw = v.get("BESTEL_GW", "")

    # Get Bestel addresses from netplan
    bestel_addrs = _read_netplan_addresses(NETPLAN, bestel_if)

    # Figure out which Bestel address is the router's own (not in pairs)
    pair_publics = {p.public for p in pairs}
    router_ip = None
    for addr in bestel_addrs:
        ip = addr.split("/")[0]
        if ip not in pair_publics:
            router_ip = ip
            break

    # Discover Telmex gateway
    telmex_gw = _discover_dhcp_gateway(telmex_if)

    # Determine LAN CIDR
    lan_cidr = _discover_lan_cidr(lan_if, lan_ip)

    wans = {
        "bestel": WanConfig(
            id="bestel",
            name="Bestel",
            interface=bestel_if,
            type="static",
            gateway=bestel_gw,
            table_id=100,
            nat_mode="onetoone",
            addresses=bestel_addrs,
            router_ip=router_ip,
            pairs=pairs,
            bandwidth=Bandwidth(
                down_mbps=monitor.get("bestel_bw_down", 40),
                up_mbps=monitor.get("bestel_bw_up", 40),
            ),
        ),
        "telmex1": WanConfig(
            id="telmex1",
            name="Telmex 1",
            interface=telmex_if,
            type="dhcp",
            gateway=telmex_gw,
            table_id=200,
            nat_mode="masquerade",
            sources=sources,
            metric=10,
            bandwidth=Bandwidth(
                down_mbps=monitor.get("telmex_bw_down", 1000),
                up_mbps=monitor.get("telmex_bw_up", 1000),
            ),
        ),
    }

    return RouterConfig(
        version=2,
        lan=LanConfig(interface=lan_if, address=lan_cidr),
        vm_default_wan="telmex1",
        wans=wans,
    )


def _discover_lan_cidr(interface: str, expected_ip: str) -> str:
    """Get the CIDR notation for the LAN interface."""
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "dev", interface],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode().strip()
        for line in out.splitlines():
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    cidr = parts[i + 1]
                    if cidr.startswith(expected_ip):
                        return cidr
    except Exception:
        pass
    return f"{expected_ip}/28"


def migrate(output_path: str = cfg.DEFAULT_CONFIG_PATH, *, dry_run: bool = False):
    """Run the full migration."""
    config = build_config()

    errors = cfg.validate(config)
    if errors:
        print("Validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        from . import config as cfg_mod
        data = cfg_mod._serialize(config)
        import yaml
        print(yaml.dump(data, default_flow_style=False, sort_keys=False))
        return config

    cfg.save(config, output_path, backup=True)
    print(f"Config written to {output_path}")
    return config


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    migrate(dry_run=dry)
