"""End-to-end probe and gateway switching primitives.

The "end-to-end probe" sends fping packets from AR's interface in a
UDM-managed VLAN. Packet path:

    AR (probe source IP) → UDM (VLAN gateway) → UDM (WAN port)
      → AR (LAN interface, enX0) → failover routing → active WAN → Internet

A single successful probe proves the entire failover chain works.

The "gateway switcher" pins the failover IP to a specific WAN, with a
verification window + watchdog-backed auto-rollback. The watchdog lives
in the health daemon and is triggered by reading the intent file in
/run/awesome-router/.
"""
from __future__ import annotations
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

from .models import E2eProbeConfig, RouterConfig


INTENT_FILE = "/run/awesome-router/switch-intent.json"
INTENT_DIR = "/run/awesome-router"


# ─── probe ────────────────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    ok: bool
    samples_attempted: int
    samples_passed: int
    target_results: dict[str, tuple[bool, Optional[float]]]   # target -> (ok, rtt_ms)
    reason: str = ""


def run_probe(e: E2eProbeConfig, timeout_ms: int = 1000) -> ProbeResult:
    """Single probe shot. Returns ProbeResult with per-target detail.

    OK if at least one target answers. Uses fping -S to bind source IP.
    """
    if not e.enabled or not e.source_ip or not e.targets:
        return ProbeResult(ok=False, samples_attempted=0, samples_passed=0,
                            target_results={}, reason="probe disabled or misconfigured")

    cmd = [
        "fping", "-q", "-c", "2", "-p", "300",
        "-t", str(timeout_ms), "-B", "1", "-r", "0",
        "-S", e.source_ip,
    ] + list(e.targets)

    target_results: dict[str, tuple[bool, Optional[float]]] = {
        t: (False, None) for t in e.targets
    }

    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=len(e.targets) * 2 + 5)
        for line in r.stderr.splitlines():
            line = line.strip()
            if " : " not in line:
                continue
            target_part, stats_part = line.split(" : ", 1)
            target = target_part.strip()
            if target not in target_results:
                continue
            ok = False
            rtt = None
            if "xmt/rcv/%loss" in stats_part:
                try:
                    loss_part = stats_part.split("=")[1].split(",")[0].strip()
                    rcv = int(loss_part.split("/")[1])
                    ok = rcv > 0
                except Exception:
                    pass
            if "min/avg/max" in stats_part:
                try:
                    rtt_part = stats_part.split("min/avg/max")[1].split("=")[1].strip()
                    rtt = float(rtt_part.split("/")[1])
                except Exception:
                    pass
            target_results[target] = (ok, rtt)
    except subprocess.TimeoutExpired:
        return ProbeResult(ok=False, samples_attempted=1, samples_passed=0,
                            target_results=target_results, reason="probe timed out")
    except FileNotFoundError:
        return ProbeResult(ok=False, samples_attempted=0, samples_passed=0,
                            target_results=target_results, reason="fping not installed")

    any_target_ok = any(ok for ok, _ in target_results.values())
    return ProbeResult(
        ok=any_target_ok,
        samples_attempted=1,
        samples_passed=1 if any_target_ok else 0,
        target_results=target_results,
        reason="" if any_target_ok else "no target responded",
    )


def run_probe_window(e: E2eProbeConfig, duration_seconds: int = 10,
                      sample_interval_seconds: int = 2,
                      required_passing_samples: int = 2) -> ProbeResult:
    """Sample the probe repeatedly over a window. Used by gateway switcher.

    Returns aggregate result. Stops early if required_passing_samples reached.
    """
    aggregate_targets: dict[str, tuple[bool, Optional[float]]] = {
        t: (False, None) for t in e.targets
    }
    passed = 0
    attempted = 0
    deadline = time.time() + duration_seconds

    while time.time() < deadline:
        attempted += 1
        single = run_probe(e)
        # Merge per-target results — keep the latest OK + RTT
        for target, (ok, rtt) in single.target_results.items():
            existing_ok, existing_rtt = aggregate_targets.get(target, (False, None))
            if ok:
                aggregate_targets[target] = (True, rtt or existing_rtt)
            elif existing_rtt is None and rtt is not None:
                aggregate_targets[target] = (existing_ok, rtt)
        if single.ok:
            passed += 1
            if passed >= required_passing_samples:
                break
        if time.time() < deadline:
            time.sleep(sample_interval_seconds)

    return ProbeResult(
        ok=passed >= required_passing_samples,
        samples_attempted=attempted,
        samples_passed=passed,
        target_results=aggregate_targets,
        reason="" if passed >= required_passing_samples
                  else f"only {passed}/{required_passing_samples} samples passed",
    )


# ─── switch intent (atomic file, watched by health daemon watchdog) ──────

def _ensure_intent_dir():
    os.makedirs(INTENT_DIR, exist_ok=True)


def read_intent() -> Optional[dict]:
    try:
        with open(INTENT_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_intent(data: dict):
    _ensure_intent_dir()
    tmp = INTENT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, INTENT_FILE)


def clear_intent():
    try:
        os.unlink(INTENT_FILE)
    except FileNotFoundError:
        pass


# ─── routing primitives (used by switcher AND watchdog) ──────────────────

def current_failover_route(table_id: int) -> Optional[dict]:
    """Capture the current default route in the failover table for snapshot."""
    r = subprocess.run(
        ["ip", "-4", "route", "show", "table", str(table_id), "default"],
        capture_output=True, text=True, timeout=5,
    )
    line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    if not line:
        return None
    parts = line.split()
    out = {"raw": line}
    for i, p in enumerate(parts):
        if p == "via" and i + 1 < len(parts):
            out["via"] = parts[i + 1]
        elif p == "dev" and i + 1 < len(parts):
            out["dev"] = parts[i + 1]
    return out


def set_failover_route(table_id: int, via: str, dev: str) -> bool:
    """Atomic ip route replace. Returns True on success."""
    r = subprocess.run(
        ["sudo", "ip", "route", "replace", "table", str(table_id),
         "default", "via", via, "dev", dev],
        capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0


def flush_conntrack(source_ip: str) -> int:
    """Flush conntrack entries for a source IP. Returns count flushed."""
    r = subprocess.run(
        ["sudo", "conntrack", "-D", "-s", source_ip],
        capture_output=True, text=True, timeout=5,
    )
    # conntrack returns 0 if entries removed, 1 if none — both fine
    # the count of flushed entries appears in stderr like "conntrack v1.4.7: 12 flow entries"
    import re
    m = re.search(r"(\d+)\s+flow entries", r.stderr)
    return int(m.group(1)) if m else 0


def refresh_arp(interface: str):
    """Flush ARP cache on an interface to force re-learn."""
    subprocess.run(
        ["sudo", "ip", "neigh", "flush", "dev", interface],
        capture_output=True, timeout=5,
    )


def discover_wan_gateway(config: RouterConfig, wan_id: str) -> Optional[tuple[str, str]]:
    """Return (gateway, interface) for a WAN by id, or None if not resolvable.

    Lookup order:
      1. Static config (wan.gateway != "auto")
      2. The WAN's own dedicated routing table (where apply_engine places it)
      3. systemd-networkd DHCP lease file
      4. Main routing table fallback
    """
    wan = config.get_wan(wan_id)
    if not wan or not wan.enabled:
        return None

    # 1. Static gateway from config
    if wan.gateway and wan.gateway != "auto":
        return (wan.gateway, wan.interface)

    # 2. Look in the WAN's own table — apply_engine populates this even when
    #    the route is absent from main.
    r = subprocess.run(
        ["ip", "-4", "route", "show", "table", str(wan.table_id), "default"],
        capture_output=True, text=True, timeout=5,
    )
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if "via" in parts:
            idx = parts.index("via")
            if idx + 1 < len(parts):
                return (parts[idx + 1], wan.interface)

    # 3. DHCP lease file (systemd-networkd)
    try:
        # Find interface index
        idx_r = subprocess.run(
            ["ip", "-o", "link", "show", "dev", wan.interface],
            capture_output=True, text=True, timeout=5,
        )
        ifindex = idx_r.stdout.split(":")[0].strip()
        if ifindex:
            lease_path = f"/run/systemd/netif/leases/{ifindex}"
            with open(lease_path) as f:
                for line in f:
                    if line.startswith("ROUTER="):
                        return (line.split("=", 1)[1].strip(), wan.interface)
    except Exception:
        pass

    # 4. Main table fallback
    r = subprocess.run(
        ["ip", "-4", "route", "show", "dev", wan.interface, "default"],
        capture_output=True, text=True, timeout=5,
    )
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if "via" in parts:
            idx = parts.index("via")
            if idx + 1 < len(parts):
                return (parts[idx + 1], wan.interface)

    return None
