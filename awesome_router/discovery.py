"""Read-only system state discovery. Never modifies anything."""
from __future__ import annotations
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

import psutil


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=timeout
        ).decode(errors="ignore").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Interface info
# ---------------------------------------------------------------------------

@dataclass
class InterfaceAddr:
    ip: str
    cidr: str
    secondary: bool = False


@dataclass
class InterfaceInfo:
    name: str
    state: str                              # "UP", "DOWN", "UNKNOWN"
    mac: str = ""
    addresses: list[InterfaceAddr] = field(default_factory=list)
    mtu: int = 1500

    @property
    def is_up(self) -> bool:
        return self.state in ("UP", "UNKNOWN")

    @property
    def primary_ip(self) -> Optional[str]:
        for a in self.addresses:
            if not a.secondary:
                return a.ip
        return self.addresses[0].ip if self.addresses else None


def get_interfaces() -> dict[str, InterfaceInfo]:
    """Return all network interfaces with their addresses."""
    result = {}

    # ip -brief link show
    for line in _run(["ip", "-brief", "link", "show"]).splitlines():
        parts = line.split()
        if len(parts) >= 3:
            name = parts[0]
            state = parts[1]
            mac = parts[2] if len(parts) > 2 else ""
            result[name] = InterfaceInfo(name=name, state=state, mac=mac)

    # ip -4 addr show
    out = _run(["ip", "-4", "-o", "addr", "show"])
    for line in out.splitlines():
        parts = line.split()
        # "2: enX0    inet 10.188.147.113/28 ..."
        iface = parts[1] if len(parts) > 1 else ""
        for i, p in enumerate(parts):
            if p == "inet" and i + 1 < len(parts):
                cidr = parts[i + 1]
                ip = cidr.split("/")[0]
                secondary = "secondary" in line
                if iface in result:
                    result[iface].addresses.append(
                        InterfaceAddr(ip=ip, cidr=cidr, secondary=secondary)
                    )
    return result


def get_unconfigured_interfaces(configured: set[str]) -> list[InterfaceInfo]:
    """Return interfaces that exist but are not in the config (candidates for new WANs).

    Includes DOWN interfaces so users can configure them before bringing them up.
    """
    all_ifs = get_interfaces()
    skip = {"lo"} | configured
    return [info for name, info in all_ifs.items() if name not in skip]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@dataclass
class Route:
    destination: str       # "default" or CIDR
    gateway: Optional[str] = None
    device: Optional[str] = None
    metric: Optional[int] = None
    table: Optional[str] = None
    raw: str = ""


def get_routes(table: str = "main") -> list[Route]:
    """Get routes for a specific routing table."""
    out = _run(["ip", "-4", "route", "show", "table", table])
    routes = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        r = Route(destination=parts[0], raw=line, table=table)
        for i, p in enumerate(parts):
            if p == "via" and i + 1 < len(parts):
                r.gateway = parts[i + 1]
            elif p == "dev" and i + 1 < len(parts):
                r.device = parts[i + 1]
            elif p == "metric" and i + 1 < len(parts):
                try:
                    r.metric = int(parts[i + 1])
                except ValueError:
                    pass
        routes.append(r)
    return routes


def get_default_gateway(table: str = "main") -> Optional[str]:
    """Get the default gateway for a routing table."""
    for r in get_routes(table):
        if r.destination == "default" and r.gateway:
            return r.gateway
    return None


# ---------------------------------------------------------------------------
# IP rules
# ---------------------------------------------------------------------------

@dataclass
class IpRule:
    priority: int
    source: str         # e.g. "10.188.147.117/32" or "all"
    table: str          # e.g. "bestel", "main", "100"
    raw: str = ""


def get_rules() -> list[IpRule]:
    """Get all ip rules."""
    out = _run(["ip", "rule", "show"])
    rules = []
    for line in out.splitlines():
        m = re.match(r"(\d+):\s+from\s+(\S+).*lookup\s+(\S+)", line)
        if m:
            rules.append(IpRule(
                priority=int(m.group(1)),
                source=m.group(2),
                table=m.group(3),
                raw=line.strip(),
            ))
    return rules


# ---------------------------------------------------------------------------
# nftables
# ---------------------------------------------------------------------------

def get_nftables() -> str:
    """Get the full nftables ruleset."""
    return _run(["sudo", "nft", "list", "ruleset"])


# ---------------------------------------------------------------------------
# Service status
# ---------------------------------------------------------------------------

@dataclass
class ServiceStatus:
    name: str
    active: bool
    status: str          # "active (running)", "active (exited)", "inactive (dead)"
    enabled: bool = False
    uptime: str = ""


def get_service_status(name: str) -> ServiceStatus:
    """Get systemd service status."""
    active = _run(["systemctl", "is-active", name]) == "active"
    enabled = _run(["systemctl", "is-enabled", name]) == "enabled"
    status_out = _run(["systemctl", "show", name,
                        "--property=ActiveState,SubState,ActiveEnterTimestamp"])
    state = "unknown"
    uptime = ""
    for line in status_out.splitlines():
        if line.startswith("ActiveState="):
            astate = line.split("=", 1)[1]
        if line.startswith("SubState="):
            sstate = line.split("=", 1)[1]
            state = f"{astate} ({sstate})" if astate else sstate
        if line.startswith("ActiveEnterTimestamp="):
            uptime = line.split("=", 1)[1].strip()

    return ServiceStatus(name=name, active=active, status=state, enabled=enabled, uptime=uptime)


# ---------------------------------------------------------------------------
# System metrics
# ---------------------------------------------------------------------------

@dataclass
class SystemMetrics:
    cpu_percent: float
    mem_total: int
    mem_used: int
    mem_percent: float
    disk_total: int
    disk_used: int
    disk_percent: float
    uptime_seconds: float
    load_1: float
    load_5: float
    load_15: float


def get_system_metrics() -> SystemMetrics:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = psutil.getloadavg()
    return SystemMetrics(
        cpu_percent=psutil.cpu_percent(interval=None),
        mem_total=vm.total,
        mem_used=vm.used,
        mem_percent=vm.percent,
        disk_total=disk.total,
        disk_used=disk.used,
        disk_percent=disk.percent,
        uptime_seconds=time.time() - psutil.boot_time(),
        load_1=load[0],
        load_5=load[1],
        load_15=load[2],
    )


# ---------------------------------------------------------------------------
# Bandwidth from SQLite
# ---------------------------------------------------------------------------

DB_PATH = "/var/lib/awesome-router-monitor.db"


@dataclass
class BandwidthSnapshot:
    """Aggregate bandwidth for an interface over a time window."""
    interface: str
    window_seconds: int
    rx_bytes: int = 0
    tx_bytes: int = 0
    elapsed_seconds: int = 0

    @property
    def rx_bps(self) -> float:
        return (self.rx_bytes / max(1, self.elapsed_seconds)) * 8 if self.rx_bytes else 0

    @property
    def tx_bps(self) -> float:
        return (self.tx_bytes / max(1, self.elapsed_seconds)) * 8 if self.tx_bytes else 0


def get_bandwidth(interface: str, window_seconds: int = 3600) -> Optional[BandwidthSnapshot]:
    """Get aggregate bandwidth for an interface from the SQLite database.

    Handles counter resets (reboots) by finding the last reset point
    and only using data from after it.
    """
    try:
        con = sqlite3.connect(DB_PATH)
        now = int(time.time())
        start = now - window_seconds
        rows = list(con.execute(
            "SELECT ts, rx, tx FROM samples WHERE iface=? AND ts>=? ORDER BY ts ASC",
            (interface, start)
        ))
        con.close()
        if len(rows) < 2:
            return None

        # Find the last counter reset (where rx decreases)
        reset_idx = 0
        for i in range(1, len(rows)):
            if rows[i][1] < rows[i - 1][1]:  # rx decreased = counter reset
                reset_idx = i

        rows = rows[reset_idx:]
        if len(rows) < 2:
            return None

        first_ts, first_rx, first_tx = rows[0]
        last_ts, last_rx, last_tx = rows[-1]
        elapsed = max(1, last_ts - first_ts)
        return BandwidthSnapshot(
            interface=interface,
            window_seconds=window_seconds,
            rx_bytes=max(0, last_rx - first_rx),
            tx_bytes=max(0, last_tx - first_tx),
            elapsed_seconds=elapsed,
        )
    except Exception:
        return None


def get_instant_bandwidth() -> dict[str, tuple[float, float]]:
    """Get near-instant rx/tx bytes-per-second for all interfaces using psutil."""
    counters1 = psutil.net_io_counters(pernic=True)
    time.sleep(0.5)
    counters2 = psutil.net_io_counters(pernic=True)
    result = {}
    for iface in counters2:
        if iface in counters1:
            c1, c2 = counters1[iface], counters2[iface]
            rx = max(0, c2.bytes_recv - c1.bytes_recv) * 2  # * 2 because 0.5s sample
            tx = max(0, c2.bytes_sent - c1.bytes_sent) * 2
            result[iface] = (rx, tx)
    return result


# ---------------------------------------------------------------------------
# Gateway latency
# ---------------------------------------------------------------------------

def ping_gateway(gateway: str, count: int = 3, timeout: int = 1) -> Optional[float]:
    """Ping a gateway and return average RTT in ms, or None if unreachable."""
    out = _run(["ping", "-n", "-c", str(count), "-W", str(timeout), gateway],
               timeout=timeout * count + 2)
    for line in out.splitlines():
        if "min/avg/max" in line or "round-trip" in line:
            try:
                stats = line.split("=")[1].split()[0]
                return float(stats.split("/")[1])
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Public IP detection
# ---------------------------------------------------------------------------

_PUBLIC_IP_CACHE: dict[str, tuple[str, float]] = {}   # key -> (ip, fetched_at)
_PUBLIC_IP_TTL = 300   # 5 minutes
_PUBLIC_IP_ENDPOINTS = [
    "https://ifconfig.me",
    "https://api.ipify.org",
    "https://icanhazip.com",
]


def _looks_like_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except Exception:
        return False


def get_public_ip(interface: str, source_ip: Optional[str] = None,
                   force: bool = False) -> Optional[str]:
    """Detect the public IP seen from the internet via a specific interface.

    Uses curl --interface to force the outgoing source, tries multiple
    endpoints, and caches the result for 5 minutes per source IP.
    """
    key = source_ip or interface
    now = time.time()

    if not force:
        cached = _PUBLIC_IP_CACHE.get(key)
        if cached and now - cached[1] < _PUBLIC_IP_TTL:
            return cached[0] or None

    iface_arg = source_ip or interface
    for url in _PUBLIC_IP_ENDPOINTS:
        out = _run(
            ["curl", "-4", "--silent", "--max-time", "3",
             "--interface", iface_arg, url],
            timeout=4,
        )
        ip = out.strip()
        if _looks_like_ipv4(ip):
            _PUBLIC_IP_CACHE[key] = (ip, now)
            return ip

    _PUBLIC_IP_CACHE[key] = ("", now)   # negative cache — don't hammer
    return None
