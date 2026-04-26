"""Apply engine: reads config, computes desired state, applies changes idempotently.

Can be run as CLI:  python3 -m awesome_router.apply_engine [--dry-run] [--config PATH]
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from . import config as cfg
from . import discovery
from . import netplan_gen
from . import nftables_gen
from . import rollback
from .models import RouterConfig, WanConfig

NETPLAN_PATH = "/etc/netplan/01-router.yaml"
RT_TABLES_PATH = "/etc/iproute2/rt_tables"
SYSCTL_PATH = "/etc/sysctl.d/99-router.conf"
NFTABLES_CONF = "/etc/nftables.conf"

SYSCTL_CONTENT = """\
net.ipv4.ip_forward=1
net.ipv4.conf.all.rp_filter=2
net.ipv4.conf.default.rp_filter=2
net.ipv4.conf.all.accept_redirects=0
net.ipv4.conf.default.accept_redirects=0
net.ipv4.conf.all.send_redirects=0
net.ipv4.conf.default.send_redirects=0
net.ipv4.conf.all.log_martians=1
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], check: bool = False, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )


def _run_ok(cmd: list[str], timeout: float = 10) -> bool:
    r = _run(cmd, timeout=timeout)
    return r.returncode == 0


def _discover_gateway(interface: str) -> str | None:
    """Discover current DHCP gateway for an interface."""
    r = _run(["ip", "-4", "route", "show", "dev", interface, "default"])
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if "via" in parts:
            idx = parts.index("via")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return None


def _discover_primary_ip(interface: str) -> str | None:
    """Get the primary (non-secondary) IPv4 address assigned to an interface."""
    r = _run(["ip", "-4", "-o", "addr", "show", "dev", interface])
    for line in r.stdout.splitlines():
        if " secondary " in line or line.rstrip().endswith(" secondary"):
            continue
        parts = line.split()
        for i, p in enumerate(parts):
            if p == "inet" and i + 1 < len(parts):
                return parts[i + 1].split("/")[0]
    return None


# ---------------------------------------------------------------------------
# Plan — what changes are needed
# ---------------------------------------------------------------------------

@dataclass
class Change:
    category: str        # "sysctl", "rt_table", "route", "rule", "nftables"
    action: str          # "add", "replace", "remove", "apply"
    description: str
    commands: list[list[str]] = field(default_factory=list)


def plan(config: RouterConfig) -> list[Change]:
    """Compute list of changes needed to reach desired state."""
    changes: list[Change] = []

    # 1. netplan (must come first — DHCP interfaces need to be configured before routes)
    _plan_netplan(changes, config)

    # 2. sysctl
    _plan_sysctl(changes)

    # 3. rt_tables entries
    for w in config.wan_list():
        if w.enabled:
            _plan_rt_table(changes, w)
    if config.failover.enabled:
        _plan_failover_rt_table(changes, config)

    # 4. Per-WAN routing tables and default routes
    for w in config.wan_list():
        if w.enabled:
            _plan_wan_route(changes, w)
    if config.failover.enabled:
        _plan_failover_route(changes, config)

    # 5. ip rules for source-based routing
    _plan_ip_rules(changes, config)

    # 6. Main table default route (VM's own traffic)
    _plan_main_default(changes, config)

    # 7. nftables
    _plan_nftables(changes, config)

    return changes


def _plan_netplan(changes: list[Change], config: RouterConfig):
    """Compare desired netplan with current and plan update if needed."""
    desired = netplan_gen.generate(config)
    try:
        with open(NETPLAN_PATH) as f:
            current = f.read()
    except FileNotFoundError:
        current = ""

    # Normalize YAML for comparison (ignore comments, whitespace variations)
    import yaml
    try:
        desired_data = yaml.safe_load(desired)
        current_data = yaml.safe_load(current)
    except Exception:
        desired_data = desired
        current_data = current

    if desired_data != current_data:
        changes.append(Change(
            category="netplan",
            action="apply",
            description="Update /etc/netplan/01-router.yaml and apply (DHCP for new interfaces)",
        ))


def _plan_sysctl(changes: list[Change]):
    try:
        with open(SYSCTL_PATH) as f:
            current = f.read()
    except FileNotFoundError:
        current = ""

    if current.strip() != SYSCTL_CONTENT.strip():
        changes.append(Change(
            category="sysctl",
            action="apply",
            description="Write /etc/sysctl.d/99-router.conf and reload",
        ))


def _plan_rt_table(changes: list[Change], wan: WanConfig):
    """Ensure the routing table name exists in /etc/iproute2/rt_tables."""
    try:
        with open(RT_TABLES_PATH) as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    table_name = wan.id
    pattern = rf"^\s*{wan.table_id}\s+{re.escape(table_name)}\s*$"
    if not re.search(pattern, content, re.MULTILINE):
        changes.append(Change(
            category="rt_table",
            action="add",
            description=f"Add rt_table entry: {wan.table_id} {table_name}",
        ))


def _plan_failover_rt_table(changes: list[Change], config):
    """Ensure rt_tables has a 'failover' entry for the dedicated table."""
    try:
        with open(RT_TABLES_PATH) as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    tid = config.failover.table_id
    pattern = rf"^\s*{tid}\s+failover\s*$"
    if not re.search(pattern, content, re.MULTILINE):
        changes.append(Change(
            category="rt_table",
            action="add",
            description=f"Add rt_table entry: {tid} failover",
        ))


def _plan_failover_route(changes: list[Change], config):
    """Ensure failover table has a default route via the current priority WAN.

    The health daemon will update this dynamically, but we seed it at apply
    time so failover works immediately (before the daemon has run).
    """
    f = config.failover
    table = str(f.table_id)
    routes = discovery.get_routes(table)
    has_default = any(r.destination == "default" and r.gateway for r in routes)

    # Pick the first priority WAN as the initial default
    active_wan = None
    for wan_id in f.priority:
        w = config.get_wan(wan_id)
        if w and w.enabled:
            active_wan = w
            break

    if not active_wan:
        return

    gw = active_wan.gateway
    if gw == "auto":
        gw = _discover_gateway(active_wan.interface)

    if not gw:
        return

    if not has_default:
        changes.append(Change(
            category="route",
            action="add",
            description=f"Seed failover table ({table}) default via {gw} dev {active_wan.interface} ({active_wan.id})",
            commands=[["sudo", "ip", "route", "replace", "table", table,
                       "default", "via", gw, "dev", active_wan.interface]],
        ))


def _plan_wan_route(changes: list[Change], wan: WanConfig):
    """Ensure routing table has default route via WAN gateway."""
    table = str(wan.table_id)
    routes = discovery.get_routes(table)
    has_default = any(r.destination == "default" and r.gateway for r in routes)

    gw = wan.gateway
    if gw == "auto":
        gw = _discover_gateway(wan.interface)

    if gw and not has_default:
        changes.append(Change(
            category="route",
            action="add",
            description=f"Add default route in table {wan.table_id} ({wan.id}) via {gw} dev {wan.interface}",
            commands=[["sudo", "ip", "route", "replace", "table", table,
                       "default", "via", gw, "dev", wan.interface]],
        ))
    elif gw and has_default:
        # Check if gateway matches
        current_gw = None
        for r in routes:
            if r.destination == "default":
                current_gw = r.gateway
                break
        if current_gw != gw:
            changes.append(Change(
                category="route",
                action="replace",
                description=f"Update default route in table {wan.table_id} ({wan.id}): {current_gw} -> {gw}",
                commands=[["sudo", "ip", "route", "replace", "table", table,
                           "default", "via", gw, "dev", wan.interface]],
            ))


def _normalize_source(src: str) -> str:
    """Normalize ip rule source: '10.0.0.1' -> '10.0.0.1/32', passthrough if already CIDR."""
    if "/" not in src:
        return f"{src}/32"
    return src


def _plan_ip_rules(changes: list[Change], config: RouterConfig):
    """Ensure source-based ip rules exist for all WAN private IPs."""
    current_rules = discovery.get_rules()
    current_map: dict[str, str] = {}  # normalized source -> table name/id
    for r in current_rules:
        if r.priority not in (0, 32766, 32767):
            current_map[_normalize_source(r.source)] = r.table

    desired: dict[str, tuple[str, int, int]] = {}  # src -> (table_name, table_id, priority)
    for w in config.wan_list():
        if not w.enabled:
            continue
        # Private-side sources (UDM-facing IPs that NAT through this WAN)
        for ip in w.private_ips:
            desired[f"{ip}/32"] = (w.id, w.table_id, 100)

        # Router's own WAN-side IPs — ensure outbound from the router itself
        # on this WAN's own IP actually leaves via this WAN, not via main.
        # For static WANs: use configured router_ip and all other addresses.
        # For DHCP WANs: discover current primary IP at plan time.
        if w.type == "static":
            own_ips = set()
            if w.router_ip:
                own_ips.add(w.router_ip)
            for addr in w.addresses:
                own_ips.add(addr.split("/")[0])
            for ip in own_ips:
                desired.setdefault(f"{ip}/32", (w.id, w.table_id, 110))
        elif w.type == "dhcp":
            own_ip = _discover_primary_ip(w.interface)
            if own_ip:
                desired.setdefault(f"{own_ip}/32", (w.id, w.table_id, 110))

    # Failover IP rule (higher priority than per-WAN rules)
    f = config.failover
    if f.enabled and f.failover_ip:
        desired[f"{f.failover_ip}/32"] = ("failover", f.table_id, 50)

    # Rules to add
    for src, (tname, tid, pref) in desired.items():
        current_table = current_map.get(src)
        if current_table is None:
            changes.append(Change(
                category="rule",
                action="add",
                description=f"Add ip rule: from {src} -> table {tid} ({tname}) pref {pref}",
                commands=[["sudo", "ip", "rule", "add", "from", src,
                           "table", str(tid), "pref", str(pref)]],
            ))
        elif current_table != str(tid) and current_table != tname:
            # Rule exists but points to wrong table — need to replace
            changes.append(Change(
                category="rule",
                action="replace",
                description=f"Replace ip rule: from {src} table {current_table} -> {tid} ({tname})",
                commands=[
                    ["sudo", "ip", "rule", "del", "from", src, "table", current_table],
                    ["sudo", "ip", "rule", "add", "from", src,
                     "table", str(tid), "pref", str(pref)],
                ],
            ))

    # Rules to remove (source no longer in any WAN)
    for src, table in current_map.items():
        if src not in desired and src != "all":
            changes.append(Change(
                category="rule",
                action="remove",
                description=f"Remove stale ip rule: from {src} table {table}",
                commands=[["sudo", "ip", "rule", "del", "from", src, "table", table]],
            ))


def _plan_main_default(changes: list[Change], config: RouterConfig):
    """Ensure the main table has EXACTLY ONE default route, pointing to the preferred WAN.

    Any other default routes (e.g. Bestel leaking in from netplan, other DHCP
    WANs) get removed so the router's own outbound traffic uses vm_default_wan.
    """
    default_wan = config.default_wan
    if not default_wan:
        return

    gw = default_wan.gateway
    if gw == "auto":
        gw = _discover_gateway(default_wan.interface)

    if not gw:
        return

    routes = discovery.get_routes("main")
    default_routes = [r for r in routes if r.destination == "default"]

    # Check if the desired route is present AND is the only one
    desired_present = any(
        r.device == default_wan.interface and r.gateway == gw
        for r in default_routes
    )
    stale_routes = [
        r for r in default_routes
        if not (r.device == default_wan.interface and r.gateway == gw)
    ]

    if not desired_present:
        changes.append(Change(
            category="route",
            action="replace",
            description=f"Pin main default route to {default_wan.id} ({gw} dev {default_wan.interface})",
            commands=[
                ["sudo", "ip", "route", "replace", "default", "via", gw,
                 "dev", default_wan.interface, "metric", str(default_wan.metric)],
            ],
        ))

    # Remove ALL other default routes so only the preferred one remains
    for r in stale_routes:
        # Build a precise delete command that matches metric/gateway when set
        cmd = ["sudo", "ip", "route", "del", "default"]
        if r.gateway:
            cmd += ["via", r.gateway]
        if r.device:
            cmd += ["dev", r.device]
        if r.metric is not None:
            cmd += ["metric", str(r.metric)]
        changes.append(Change(
            category="route",
            action="remove",
            description=f"Remove stale main default via {r.device}" + (f" metric {r.metric}" if r.metric is not None else " (no metric)"),
            commands=[cmd],
        ))


def _normalize_nft(text: str) -> str:
    """Normalize nftables output for comparison.

    Handles: keyword vs numeric priorities, IP set ordering, multiline
    elements blocks, and single-element sets ({ x } vs x).
    """
    # First join continuation lines (nft wraps long lines with leading whitespace)
    joined = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # If line starts with whitespace and previous didn't end with {, it's continuation
        if line and line[0] in (" ", "\t") and joined and not joined.rstrip().endswith("{"):
            joined = joined.rstrip() + " " + stripped
        else:
            if joined:
                joined += "\n"
            joined += stripped
    text = joined

    lines = []
    for s in text.splitlines():
        s = s.strip()
        if not s or s in ("{", "}") or s.startswith("flush") or s.startswith("table "):
            continue
        # Normalize priority keywords to numbers
        s = s.replace("priority dstnat", "priority -100")
        s = s.replace("priority srcnat", "priority 100")
        s = s.replace("priority filter", "priority 0")
        # Sort items inside { } sets
        def _sort_braces(m):
            items = sorted(i.strip() for i in m.group(1).split(",") if i.strip())
            if len(items) == 1:
                return items[0]  # unwrap single-element sets
            return "{ " + ", ".join(items) + " }"
        s = re.sub(r'\{([^}]+)\}', _sort_braces, s)
        # Normalize single-element ip saddr/daddr with no braces (already handled above)
        lines.append(s)
    lines.sort()
    return "\n".join(lines)


def _plan_nftables(changes: list[Change], config: RouterConfig):
    """Compare desired nftables with current and plan update if needed."""
    desired = nftables_gen.generate(config)
    current = discovery.get_nftables()

    if _normalize_nft(desired) != _normalize_nft(current):
        changes.append(Change(
            category="nftables",
            action="apply",
            description="Update nftables ruleset",
        ))


# ---------------------------------------------------------------------------
# Apply — execute planned changes
# ---------------------------------------------------------------------------

def apply(config: RouterConfig, *, dry_run: bool = False) -> dict:
    """Plan and apply changes. Returns result dict."""
    errors = cfg.validate(config)
    if errors:
        return {"ok": False, "errors": errors, "changes": []}

    changes = plan(config)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changes": [{"category": c.category, "action": c.action,
                          "description": c.description} for c in changes],
        }

    if not changes:
        return {"ok": True, "changes": [], "message": "No changes needed"}

    # Take snapshot before applying
    snap_path = rollback.snapshot()

    applied = []
    try:
        for c in changes:
            _apply_change(c, config)
            applied.append({"category": c.category, "action": c.action,
                            "description": c.description})
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "changes_applied": applied,
            "snapshot": snap_path,
            "message": f"Failed during apply. Snapshot at {snap_path}. "
                       f"Rollback with: python3 -m awesome_router.rollback {snap_path}",
        }

    return {
        "ok": True,
        "changes": applied,
        "snapshot": snap_path,
    }


def reset_and_reapply(config: RouterConfig) -> dict:
    """Hard reset: flush all custom routing state, then re-apply from config.

    Useful when the routing/firewall state has drifted and the usual Apply is
    insufficient. Does NOT touch interfaces (netplan/networkd), so the web GUI
    stays reachable on enX0.

    Steps:
      1. Snapshot current state (for rollback if apply fails).
      2. Flush custom ip rules (any pref not in {0, 32766, 32767}).
      3. Flush each configured WAN table + failover table.
      4. Flush nftables ruleset.
      5. Run the normal apply engine which rebuilds everything.
    """
    errors = cfg.validate(config)
    if errors:
        return {"ok": False, "errors": errors}

    # Snapshot BEFORE flush so rollback can restore the pre-reset state
    snap_path = rollback.snapshot()
    actions = []

    # 1. Flush custom ip rules (system rules keep their pref 0/32766/32767)
    r = subprocess.run(["ip", "rule", "show"], capture_output=True, text=True, timeout=5)
    for line in r.stdout.splitlines():
        # parse "100: from 10.188.147.117 lookup bestel"
        parts = line.split(":", 1)
        if len(parts) < 2:
            continue
        try:
            pref = int(parts[0].strip())
        except ValueError:
            continue
        if pref in (0, 32766, 32767):
            continue
        subprocess.run(["sudo", "ip", "rule", "del", "pref", str(pref)],
                       capture_output=True, timeout=5)
    actions.append("Flushed custom ip rules")

    # 2. Flush custom routing tables (each WAN's table + failover)
    tables_to_flush = set()
    for w in config.wan_list():
        tables_to_flush.add(str(w.table_id))
    if config.failover.enabled:
        tables_to_flush.add(str(config.failover.table_id))
    for t in tables_to_flush:
        subprocess.run(["sudo", "ip", "route", "flush", "table", t],
                       capture_output=True, timeout=5)
    if tables_to_flush:
        actions.append(f"Flushed routing tables: {', '.join(sorted(tables_to_flush))}")

    # 3. Flush nftables atomically (empty ruleset, apply engine rebuilds it)
    subprocess.run(["sudo", "nft", "flush", "ruleset"],
                   capture_output=True, timeout=5)
    actions.append("Flushed nftables ruleset")

    # 4. Re-run apply (this creates its own snapshot too, which is fine)
    result = apply(config, dry_run=False)
    if not result["ok"]:
        return {
            "ok": False,
            "error": result.get("error", "apply failed after reset"),
            "actions": actions,
            "snapshot": snap_path,
            "message": f"Reset completed but apply failed. Rollback available at {snap_path}.",
        }

    return {
        "ok": True,
        "actions": actions + [f"Re-applied {len(result.get('changes', []))} change(s) from config"],
        "snapshot": snap_path,
        "apply_result": result,
    }


def restart_networkd() -> dict:
    """Restart systemd-networkd. Bounces all network interfaces.

    More aggressive than reset_and_reapply — will cause brief loss of
    connectivity on all interfaces while networkd reloads. Also triggers
    DHCP renewal on dynamic WANs. The awesome-router-apply.service is
    re-triggered automatically after networkd is back up (via
    network-online.target), so routing/nftables get rebuilt.
    """
    # Take a snapshot first for safety
    snap_path = rollback.snapshot()

    # systemd-run detaches this so the web request doesn't hang if the
    # connection briefly drops while networkd restarts.
    r = subprocess.run(
        ["sudo", "systemd-run", "--no-block",
         "systemctl", "restart", "systemd-networkd.service"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr or "systemd-run failed", "snapshot": snap_path}

    return {
        "ok": True,
        "message": "systemd-networkd restart scheduled. Interfaces will bounce briefly; "
                   "apply service re-runs automatically once network-online.target is reached.",
        "snapshot": snap_path,
    }


def _apply_change(change: Change, config: RouterConfig):
    """Execute a single change."""
    if change.category == "netplan":
        desired = netplan_gen.generate(config)
        # Backup and write
        import shutil
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        if os.path.exists(NETPLAN_PATH):
            shutil.copy2(NETPLAN_PATH, f"{NETPLAN_PATH}.bak-{ts}")
        with open(NETPLAN_PATH, "w") as f:
            f.write(desired)
        os.chmod(NETPLAN_PATH, 0o600)
        # Apply netplan (this triggers DHCP for new interfaces)
        r = subprocess.run(["sudo", "netplan", "apply"],
                            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"netplan apply failed: {r.stderr}")
        # Wait a few seconds for DHCP to get an address
        import time
        time.sleep(5)

    elif change.category == "sysctl":
        with open(SYSCTL_PATH, "w") as f:
            f.write(SYSCTL_CONTENT)
        subprocess.run(["sudo", "sysctl", "--system"],
                        capture_output=True, timeout=10)

    elif change.category == "rt_table":
        # Extract table_id and name from description
        parts = change.description.split(":")
        if len(parts) >= 2:
            entry = parts[1].strip()
            with open(RT_TABLES_PATH, "a") as f:
                f.write(f"\n{entry}\n")

    elif change.category == "nftables":
        desired = nftables_gen.generate(config)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".nft", delete=False) as tf:
            tf.write(desired)
            tf_path = tf.name
        try:
            # Validate first
            r = subprocess.run(["sudo", "nft", "-c", "-f", tf_path],
                                capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise RuntimeError(f"nftables validation failed: {r.stderr}")
            # Apply atomically
            r = subprocess.run(["sudo", "nft", "-f", tf_path],
                                capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                raise RuntimeError(f"nftables apply failed: {r.stderr}")
            # Persist
            with open(NFTABLES_CONF, "w") as f:
                f.write(desired)
            subprocess.run(["sudo", "systemctl", "enable", "--now", "nftables"],
                            capture_output=True, timeout=10)
        finally:
            os.unlink(tf_path)

    elif change.commands:
        for cmd in change.commands:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if r.returncode != 0 and change.action != "remove":
                raise RuntimeError(
                    f"Command failed: {' '.join(cmd)}\n{r.stderr}"
                )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Awesome Router apply engine")
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = cfg.load(args.config)
    result = apply(config, dry_run=args.dry_run)

    if result.get("dry_run"):
        changes = result["changes"]
        if not changes:
            print("No changes needed.")
        else:
            print(f"{len(changes)} change(s) planned:")
            for c in changes:
                print(f"  [{c['category']}] {c['action']}: {c['description']}")
    elif result["ok"]:
        changes = result["changes"]
        if not changes:
            print("No changes needed.")
        else:
            print(f"Applied {len(changes)} change(s):")
            for c in changes:
                print(f"  [{c['category']}] {c['action']}: {c['description']}")
            print(f"Snapshot: {result.get('snapshot', 'n/a')}")
    else:
        print(f"ERROR: {result.get('error', result.get('errors', 'unknown'))}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
