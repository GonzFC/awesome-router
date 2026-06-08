"""WAN health monitor & automatic failover daemon.

Pings each WAN's configured targets at a fixed interval. Applies hysteresis
thresholds to decide whether a WAN is up or down. When state changes,
recomputes the highest-priority healthy WAN and updates the failover
routing table so traffic from the failover IP uses the newly-active WAN.

State is persisted to `/run/awesome-router/health.json` for the web GUI.
Failover events are logged to `/var/lib/awesome-router/failover-events.log`
and to the SQLite monitor DB for history.
"""
from __future__ import annotations
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import config as cfg
from . import probe as probe_mod
from .models import RouterConfig, WanConfig
from .udm_client import (
    UdmClient, UdmError, UdmUnauthorized, UdmUnreachable, UdmStats,
)

STATE_DIR = "/run/awesome-router"
STATE_FILE = f"{STATE_DIR}/health.json"
EVENT_LOG = "/var/lib/awesome-router/failover-events.log"
DB_PATH = "/var/lib/awesome-router-monitor.db"

# Retention for UDM stats samples (days)
UDM_STATS_RETENTION_DAYS = 60


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class TargetState:
    target: str
    up: bool = True                 # last probe result
    last_rtt_ms: Optional[float] = None
    last_checked: int = 0


@dataclass
class WanHealth:
    wan_id: str
    is_up: bool = True              # after hysteresis
    consecutive_ok: int = 0
    consecutive_fail: int = 0
    targets: dict[str, TargetState] = field(default_factory=dict)
    last_state_change: int = 0


@dataclass
class E2eState:
    """Latest end-to-end probe result + history."""
    enabled: bool = False
    last_ok: bool = False
    last_attempted_at: int = 0
    last_ok_at: int = 0
    consecutive_failures: int = 0
    last_rtt_ms: Optional[float] = None    # RTT of fastest target in last probe
    last_reason: str = ""


@dataclass
class UdmState:
    """Latest UDM observation + verification state."""
    reachable: bool = False
    error: str = ""
    controller_version: str = ""
    device_state: str = ""           # ONLINE / OFFLINE / ...
    device_name: str = ""
    device_model: str = ""
    last_heartbeat: str = ""
    uplink_tx_bps: int = 0
    uplink_rx_bps: int = 0
    last_polled: int = 0
    # disagreement counter — how many consecutive cycles AR says "all good"
    # but UDM is not actually transmitting through us
    consecutive_disagreements: int = 0
    last_action: str = ""
    last_action_at: int = 0


@dataclass
class FailoverState:
    active_wan: Optional[str] = None
    wans: dict[str, WanHealth] = field(default_factory=dict)
    last_update: int = 0
    udm: UdmState = field(default_factory=UdmState)
    e2e: E2eState = field(default_factory=E2eState)
    # Manual override info (read from /run/awesome-router/switch-intent.json)
    manual_override_wan: Optional[str] = None
    manual_override_phase: str = ""


# ---------------------------------------------------------------------------
# Probing — uses fping + nping for reliable WAN health detection
# ---------------------------------------------------------------------------

def _discover_primary_ip(interface: str) -> Optional[str]:
    """Get the primary IPv4 address on an interface."""
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", interface],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.splitlines():
            if " secondary " in line:
                continue
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    return parts[i + 1].split("/")[0]
    except Exception:
        pass
    return None


def probe_wan_fping(source_ip: str, targets: list[str],
                     timeout_ms: int = 800) -> dict[str, tuple[bool, Optional[float]]]:
    """Probe multiple targets via fping using source-IP binding.

    Returns {target: (ok, avg_rtt_ms)}.
    Uses -S (source IP bind) which works cleanly with policy routing rules.
    Sends 3 probes per target, 300ms apart — smooths burst loss.
    """
    result = {t: (False, None) for t in targets}
    if not source_ip or not targets:
        return result

    try:
        cmd = [
            "fping", "-q", "-c", "3", "-p", "300",
            "-t", str(timeout_ms), "-B", "1", "-r", "0",
            "-S", source_ip,
        ] + targets
        r = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=len(targets) * 3 + 5)
        # fping writes per-target stats to stderr:
        #   1.1.1.1 : xmt/rcv/%loss = 3/3/0%, min/avg/max = 7.1/7.2/7.3
        #   9.9.9.9 : xmt/rcv/%loss = 3/0/100%
        for line in r.stderr.splitlines():
            line = line.strip()
            if " : " not in line:
                continue
            target_part, stats_part = line.split(" : ", 1)
            target = target_part.strip()
            if target not in result:
                continue
            # Parse loss
            ok = False
            rtt = None
            if "xmt/rcv/%loss" in stats_part:
                try:
                    loss_part = stats_part.split("=")[1].split(",")[0].strip()
                    rcv = int(loss_part.split("/")[1])
                    ok = rcv > 0  # at least one response
                except Exception:
                    pass
            # Parse RTT
            if "min/avg/max" in stats_part:
                try:
                    rtt_part = stats_part.split("min/avg/max")[1].split("=")[1].strip()
                    rtt = float(rtt_part.split("/")[1])
                except Exception:
                    pass
            result[target] = (ok, rtt)
    except Exception:
        pass
    return result


def probe_wan_tcp(source_ip: str, target: str, port: int = 443,
                   timeout_ms: int = 2000) -> bool:
    """TCP connect probe — tiebreaker when ICMP fails.

    TCP/443 is never rate-limited by ISPs. If this succeeds but ICMP
    doesn't, the WAN is up and ICMP is just being filtered.
    """
    if not source_ip:
        return False
    try:
        # Use nping for TCP connect with source-IP binding
        r = subprocess.run(
            ["nping", "--tcp-connect", "-p", str(port),
             "-c", "1", "--delay", "0",
             "--source-ip", source_ip, target],
            capture_output=True, text=True, timeout=timeout_ms / 1000 + 3,
        )
        # nping outputs "Successful connections: 1" when it works
        return "Successful connections: 1" in r.stdout
    except Exception:
        return False


def probe_target(interface: str, target: str, timeout: int) -> tuple[bool, Optional[float]]:
    """Legacy single-target probe (kept for compatibility).

    Prefers fping -S; falls back to ping -I if source IP unknown.
    """
    source_ip = _discover_primary_ip(interface)
    if source_ip:
        results = probe_wan_fping(source_ip, [target], timeout_ms=timeout * 1000)
        return results.get(target, (False, None))
    # Fallback to ping -I
    try:
        r = subprocess.run(
            ["ping", "-I", interface, "-c", "1", "-W", str(timeout),
             "-n", "-q", target],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        if r.returncode != 0:
            return False, None
        for line in r.stdout.splitlines():
            if "min/avg/max" in line:
                try:
                    stats = line.split("=")[1].split()[0]
                    avg = float(stats.split("/")[1])
                    return True, avg
                except Exception:
                    pass
        return True, None
    except Exception:
        return False, None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(EVENT_LOG), exist_ok=True)


def save_state(state: FailoverState):
    _ensure_state_dir()
    u = state.udm
    data = {
        "active_wan": state.active_wan,
        "last_update": state.last_update,
        "wans": {
            wid: {
                "is_up": h.is_up,
                "consecutive_ok": h.consecutive_ok,
                "consecutive_fail": h.consecutive_fail,
                "last_state_change": h.last_state_change,
                "targets": {
                    t: {"target": ts.target, "up": ts.up,
                        "last_rtt_ms": ts.last_rtt_ms,
                        "last_checked": ts.last_checked}
                    for t, ts in h.targets.items()
                },
            }
            for wid, h in state.wans.items()
        },
        "udm": {
            "reachable": u.reachable,
            "error": u.error,
            "controller_version": u.controller_version,
            "device_state": u.device_state,
            "device_name": u.device_name,
            "device_model": u.device_model,
            "last_heartbeat": u.last_heartbeat,
            "uplink_tx_bps": u.uplink_tx_bps,
            "uplink_rx_bps": u.uplink_rx_bps,
            "last_polled": u.last_polled,
            "consecutive_disagreements": u.consecutive_disagreements,
            "last_action": u.last_action,
            "last_action_at": u.last_action_at,
        },
        "e2e": {
            "enabled": state.e2e.enabled,
            "last_ok": state.e2e.last_ok,
            "last_attempted_at": state.e2e.last_attempted_at,
            "last_ok_at": state.e2e.last_ok_at,
            "consecutive_failures": state.e2e.consecutive_failures,
            "last_rtt_ms": state.e2e.last_rtt_ms,
            "last_reason": state.e2e.last_reason,
        },
        "manual_override_wan": state.manual_override_wan,
        "manual_override_phase": state.manual_override_phase,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


def _ensure_udm_stats_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS udm_stats(
        ts INTEGER NOT NULL,
        mem_pct REAL,
        cpu_pct REAL,
        load_1 REAL,
        uplink_tx_bps INTEGER,
        uplink_rx_bps INTEGER,
        uptime_sec INTEGER,
        state TEXT
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_udm_ts ON udm_stats(ts)")


def _persist_udm_sample(now: int, stats, device_state: str):
    """Write one UDM stats sample to SQLite, prune old data."""
    try:
        con = sqlite3.connect(DB_PATH, timeout=2.0)
        _ensure_udm_stats_table(con)
        con.execute(
            "INSERT INTO udm_stats(ts, mem_pct, cpu_pct, load_1, "
            "uplink_tx_bps, uplink_rx_bps, uptime_sec, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, stats.mem_pct, stats.cpu_pct, stats.load_1,
             stats.uplink_tx_bps, stats.uplink_rx_bps,
             stats.uptime_sec, device_state),
        )
        # Retention: prune older than UDM_STATS_RETENTION_DAYS
        cutoff = now - UDM_STATS_RETENTION_DAYS * 24 * 3600
        con.execute("DELETE FROM udm_stats WHERE ts < ?", (cutoff,))
        con.commit()
        con.close()
    except Exception as e:
        print(f"[health] udm sample persist error: {e}", flush=True)


def log_event(message: str):
    _ensure_state_dir()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(EVENT_LOG, "a") as f:
        f.write(f"[{ts}] {message}\n")
    # Also log to DB for GUI history
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""CREATE TABLE IF NOT EXISTS failover_events(
            ts INTEGER NOT NULL, message TEXT NOT NULL
        )""")
        con.execute("INSERT INTO failover_events(ts, message) VALUES (?, ?)",
                    (int(time.time()), message))
        con.commit()
        con.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Failover logic
# ---------------------------------------------------------------------------

def choose_active(config: RouterConfig, state: FailoverState) -> Optional[str]:
    """Pick the highest-priority healthy WAN from the priority list.

    Honors a committed manual override: while the override is in effect,
    don't auto-switch even if a higher-priority WAN comes back. The override
    is cleared by the user via the GUI ("Release override" button).
    """
    intent = probe_mod.read_intent()
    if intent and intent.get("phase") == "committed":
        return intent.get("target_wan")
    for wan_id in config.failover.priority:
        h = state.wans.get(wan_id)
        if h and h.is_up:
            return wan_id
    # All down — keep last active (better than no route)
    return state.active_wan


def update_failover_route(config: RouterConfig, active_wan_id: str) -> bool:
    """Update the failover routing table to route through the active WAN.

    Returns True if the route was changed.
    """
    wan = config.get_wan(active_wan_id)
    if not wan:
        return False

    gw = wan.gateway
    if gw == "auto":
        gw = _discover_dhcp_gw(wan.interface)
    if not gw:
        return False

    table = str(config.failover.table_id)

    # Check current default in failover table
    r = subprocess.run(
        ["ip", "-4", "route", "show", "table", table, "default"],
        capture_output=True, text=True, timeout=5,
    )
    current = r.stdout.strip()
    # Does it already point to this WAN?
    if f"via {gw}" in current and f"dev {wan.interface}" in current:
        return False

    # Replace it
    subprocess.run(
        ["sudo", "ip", "route", "replace", "table", table,
         "default", "via", gw, "dev", wan.interface],
        capture_output=True, timeout=5,
    )
    return True


def _discover_dhcp_gw(interface: str) -> Optional[str]:
    r = subprocess.run(
        ["ip", "-4", "route", "show", "dev", interface, "default"],
        capture_output=True, text=True, timeout=5,
    )
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if "via" in parts:
            idx = parts.index("via")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

class HealthDaemon:
    def __init__(self):
        self.state = FailoverState()
        self.running = True
        self.config: Optional[RouterConfig] = None
        self._last_config_load = 0
        self._tick_count = 0
        self._udm_client: Optional[UdmClient] = None
        self._udm_site_id: Optional[str] = None
        self._udm_gw_id: Optional[str] = None
        self._last_udm_poll = 0
        self._load_config()

    def _load_config(self):
        try:
            new_config = cfg.load()
            # If UDM section changed, rebuild client
            if (self.config is None
                or self.config.udm.host != new_config.udm.host
                or self.config.udm.enabled != new_config.udm.enabled
                or self.config.udm.key_file != new_config.udm.key_file):
                self._udm_client = None
                self._udm_site_id = None
                self._udm_gw_id = None
            self.config = new_config
            self._last_config_load = time.time()
        except Exception as e:
            print(f"[health] config load error: {e}", flush=True)

    def _maybe_reload_config(self):
        if time.time() - self._last_config_load > 30:
            self._load_config()

    def _ensure_udm_client(self) -> Optional[UdmClient]:
        """Build and cache the UDM client lazily."""
        if not self.config or not self.config.udm.enabled or not self.config.udm.host:
            return None
        if self._udm_client is not None:
            return self._udm_client
        try:
            self._udm_client = UdmClient(
                host=self.config.udm.host,
                key_file=self.config.udm.key_file,
                verify_ssl=self.config.udm.verify_ssl,
                cache_seconds=self.config.udm.cache_seconds,
            )
            return self._udm_client
        except UdmError as e:
            self.state.udm.error = f"client init: {e}"
            return None

    def _resolve_udm_targets(self, client: UdmClient) -> bool:
        """Resolve site_id and gateway device_id (caches them)."""
        if self._udm_site_id and self._udm_gw_id:
            return True
        site_id = self.config.udm.site_id
        try:
            if site_id == "auto":
                site_id = client.default_site_id()
            if not site_id:
                self.state.udm.error = "no sites found"
                return False
            self._udm_site_id = site_id
            gw_id = self.config.udm.gateway_device_id
            if gw_id == "auto":
                gw = client.find_gateway(site_id)
                if not gw:
                    self.state.udm.error = "no UDM/UDR gateway in site"
                    return False
                self._udm_gw_id = gw.id
                self.state.udm.device_name = gw.name
                self.state.udm.device_model = gw.model
            else:
                self._udm_gw_id = gw_id
            return True
        except UdmError as e:
            self.state.udm.error = f"resolve: {e}"
            return False

    def _poll_udm(self, now: int):
        """Query UDM, update state.udm, and decide whether to act."""
        if not self.config or not self.config.udm.enabled:
            return

        # Honor poll_interval_seconds (don't poll every tick)
        interval = max(5, self.config.udm.poll_interval_seconds)
        if now - self._last_udm_poll < interval:
            return
        self._last_udm_poll = now

        u = self.state.udm
        client = self._ensure_udm_client()
        if client is None:
            u.reachable = False
            return

        if not self._resolve_udm_targets(client):
            u.reachable = False
            return

        try:
            info = client.info()
            d = client.device(self._udm_site_id, self._udm_gw_id)
            stats = client.device_stats(self._udm_site_id, self._udm_gw_id)
            u.reachable = True
            u.error = ""
            u.controller_version = info.get("applicationVersion", "")
            u.device_state = d.get("state", "")
            u.device_name = d.get("name", u.device_name)
            u.device_model = d.get("model", u.device_model)
            u.last_heartbeat = stats.last_heartbeat
            u.uplink_tx_bps = stats.uplink_tx_bps
            u.uplink_rx_bps = stats.uplink_rx_bps
            u.last_polled = now

            # Persist this sample (memory %, CPU, uptime, etc.) to SQLite
            # for trend graphs and restart detection.
            _persist_udm_sample(now, stats, u.device_state)
            # Detect UDM restarts (uptime jumped backwards)
            self._detect_udm_restart(now, stats.uptime_sec)
        except UdmUnauthorized as e:
            u.reachable = False
            u.error = f"unauthorized: {e}"
            return
        except UdmUnreachable as e:
            u.reachable = False
            u.error = f"unreachable: {e}"
            # If UDM disappears, that's itself a disagreement signal
            u.consecutive_disagreements += 1
        except UdmError as e:
            u.reachable = False
            u.error = f"api: {e}"
            return

        # ─── verification logic ─────────────────────────────────────────
        # AR thinks the active WAN is OK if state.active_wan exists and is up.
        ar_thinks_ok = False
        if self.state.active_wan:
            wh = self.state.wans.get(self.state.active_wan)
            ar_thinks_ok = bool(wh and wh.is_up)

        # ─── UDM state interpretation ──────────────────────────────────
        # Several UDM states are transient (initialization, provisioning,
        # firmware upgrade). Acting on them only INTERFERES with the UDM's
        # own recovery. Only treat truly-offline / unreachable as actionable
        # disagreement.
        UDM_TRANSIENT_STATES = {
            "GETTING_READY", "PROVISIONING", "UPGRADING",
            "ADOPTING", "PENDING", "INSTALLING", "REBOOTING",
        }
        UDM_HEALTHY_STATES = {"ONLINE"}

        udm_state = u.device_state or ""
        is_transient = udm_state in UDM_TRANSIENT_STATES
        is_healthy = udm_state in UDM_HEALTHY_STATES
        is_offline = bool(udm_state) and not is_transient and not is_healthy

        udm_silent = (u.uplink_tx_bps == 0 and u.uplink_rx_bps == 0)

        if is_transient:
            # UDM is busy initializing — leave it alone. Reset counter so we
            # don't accumulate disagreements during a long boot.
            if u.consecutive_disagreements > 0:
                log_event(f"UDM in transient state {udm_state}; pausing verification "
                            f"(reset {u.consecutive_disagreements} -> 0)")
            u.consecutive_disagreements = 0
            return  # no action while transient

        if ar_thinks_ok and is_offline:
            u.consecutive_disagreements += 1
            log_event(f"UDM disagrees: AR says active WAN OK, UDM state={udm_state} (count {u.consecutive_disagreements})")
        elif ar_thinks_ok and udm_silent and u.reachable and is_healthy:
            # Silent + AR healthy + UDM ONLINE — could be idle. Count.
            u.consecutive_disagreements += 1
        else:
            if u.consecutive_disagreements > 0:
                log_event(f"UDM agrees again (reset {u.consecutive_disagreements} -> 0)")
            u.consecutive_disagreements = 0

        # Trigger corrective action if threshold crossed AND cooldown elapsed.
        # Cooldown prevents the AR from hammering an already-stressed UDM:
        # at most one corrective action every 5 minutes.
        CORRECTIVE_ACTION_COOLDOWN_SECS = 300
        threshold = self.config.udm.disagreement_threshold
        if u.consecutive_disagreements >= threshold:
            elapsed_since_last_action = now - u.last_action_at
            if u.last_action_at == 0 or elapsed_since_last_action >= CORRECTIVE_ACTION_COOLDOWN_SECS:
                self._take_corrective_action(now)
            else:
                # In cooldown — keep observing, don't act
                wait_secs = CORRECTIVE_ACTION_COOLDOWN_SECS - elapsed_since_last_action
                if u.consecutive_disagreements == threshold:
                    # log once at the threshold crossing, not every tick
                    log_event(f"Corrective action gated by cooldown; "
                                f"{wait_secs}s until next eligible (last={u.last_action})")

    def _detect_udm_restart(self, now: int, uptime_sec: int):
        """If the UDM's reported uptime jumped backwards, it restarted.

        Logs a restart event with a timestamp + cause-hint based on previous
        memory pressure. Useful for tracking whether our recent fixes have
        actually reduced the restart cadence.
        """
        last_uptime = getattr(self, "_last_udm_uptime", None)
        if last_uptime is not None and uptime_sec is not None:
            # Threshold: uptime must drop AND new uptime is small (fresh boot)
            if uptime_sec < last_uptime and uptime_sec < 3600:
                # Look up the last memory % before the restart for context
                hint = ""
                try:
                    con = sqlite3.connect(DB_PATH, timeout=2.0)
                    _ensure_udm_stats_table(con)
                    cur = con.execute(
                        "SELECT mem_pct, ts FROM udm_stats "
                        "WHERE ts < ? AND mem_pct IS NOT NULL "
                        "ORDER BY ts DESC LIMIT 1",
                        (now - uptime_sec,),
                    )
                    row = cur.fetchone()
                    con.close()
                    if row and row[0] is not None:
                        hint = f" (last memory before restart: {row[0]:.1f}%)"
                except Exception:
                    pass
                log_event(f"UDM restart detected (new uptime {uptime_sec}s){hint}")
        self._last_udm_uptime = uptime_sec

    def _take_corrective_action(self, now: int):
        """Escalating ladder of corrective actions when AR/UDM disagree."""
        if not self.config:
            return
        u = self.state.udm
        fip = self.config.failover.failover_ip
        lan_iface = self.config.lan.interface

        # Pick next action based on what we've tried recently
        # Simple ladder: conntrack flush → ARP refresh → re-apply
        last = u.last_action
        if last in ("", "reapply"):
            action = "conntrack_flush"
        elif last == "conntrack_flush":
            action = "arp_refresh"
        elif last == "arp_refresh":
            action = "reapply"
        else:
            action = "conntrack_flush"

        if action == "conntrack_flush" and fip:
            subprocess.run(
                ["sudo", "conntrack", "-D", "-s", fip],
                capture_output=True, timeout=5,
            )
            log_event(f"Corrective action: flushed conntrack for {fip}")
        elif action == "arp_refresh" and lan_iface:
            subprocess.run(
                ["sudo", "ip", "neigh", "flush", "dev", lan_iface],
                capture_output=True, timeout=5,
            )
            log_event(f"Corrective action: flushed ARP on {lan_iface}")
        elif action == "reapply":
            # Re-run apply engine to rebuild routes/nft from config
            try:
                from . import apply_engine
                result = apply_engine.apply(self.config, dry_run=False)
                changes = len(result.get("changes", [])) if result.get("ok") else 0
                log_event(f"Corrective action: re-applied config ({changes} changes)")
            except Exception as e:
                log_event(f"Corrective action: re-apply FAILED: {e}")

        u.last_action = action
        u.last_action_at = now
        # Reset counter after action to give it a chance to take effect
        u.consecutive_disagreements = 0

    def run(self):
        print("[health] daemon starting (fping+nping mode)", flush=True)
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        while self.running:
            try:
                self._maybe_reload_config()
                if self.config:
                    self._tick(
                        apply_failover=bool(
                            self.config.failover.enabled
                            and self.config.failover.failover_ip
                        )
                    )
                time.sleep(max(1, self.config.failover.health.interval_seconds
                               if self.config else 10))
            except Exception as e:
                print(f"[health] tick error: {e}", flush=True)
                time.sleep(5)

    def _stop(self, *_):
        self.running = False
        print("[health] stopping", flush=True)

    def _tick(self, apply_failover: bool = True):
        if not self.config:
            return
        f = self.config.failover
        now = int(time.time())
        self._tick_count += 1

        # Build the target list: config targets + each WAN's ISP gateway
        base_targets = list(f.health.targets)

        # Every 3rd tick, also run a TCP/443 probe as tiebreaker
        use_tcp_tiebreaker = (self._tick_count % 3 == 0)

        for wan in self.config.wan_list():
            if not wan.enabled:
                continue

            h = self.state.wans.setdefault(wan.id, WanHealth(wan_id=wan.id))
            source_ip = _discover_primary_ip(wan.interface)
            if not source_ip:
                continue

            # Add ISP gateway as a target for this WAN (catches last-mile issues)
            isp_gw = wan.gateway
            if isp_gw == "auto":
                isp_gw = _discover_dhcp_gw(wan.interface)
            wan_targets = list(base_targets)
            if isp_gw and isp_gw not in wan_targets:
                wan_targets.append(isp_gw)

            # Batch probe all targets via fping -S
            fping_results = probe_wan_fping(
                source_ip, wan_targets,
                timeout_ms=max(500, f.health.timeout_seconds * 1000),
            )

            # Update per-target state
            any_target_up = False
            for target in wan_targets:
                ok, rtt = fping_results.get(target, (False, None))
                ts = h.targets.setdefault(target, TargetState(target=target))
                ts.up = ok
                ts.last_rtt_ms = rtt
                ts.last_checked = now
                if ok:
                    any_target_up = True

            # Decision: WAN is UP if ANY target responded (OR logic).
            # This eliminates false positives from single-target ICMP drops.
            probe_ok = any_target_up

            # TCP tiebreaker: if ALL ICMP failed, try TCP/443 to Cloudflare.
            # TCP is never rate-limited — if it works, the WAN is actually up.
            if not probe_ok and use_tcp_tiebreaker:
                tcp_target = "1.1.1.1"  # Cloudflare — best TCP/443 responder
                tcp_ok = probe_wan_tcp(source_ip, tcp_target)
                if tcp_ok:
                    probe_ok = True
                    # Record TCP success in target state for GUI visibility
                    ts = h.targets.setdefault(
                        f"tcp:{tcp_target}:443",
                        TargetState(target=f"tcp:{tcp_target}:443")
                    )
                    ts.up = True
                    ts.last_checked = now
                    ts.last_rtt_ms = None

            # Hysteresis: require consecutive threshold crossings
            if probe_ok:
                h.consecutive_ok += 1
                h.consecutive_fail = 0
                if not h.is_up and h.consecutive_ok >= f.health.recover_threshold:
                    h.is_up = True
                    h.last_state_change = now
                    log_event(f"WAN {wan.id} recovered ({h.consecutive_ok} consecutive OK)")
            else:
                h.consecutive_fail += 1
                h.consecutive_ok = 0
                if h.is_up and h.consecutive_fail >= f.health.fail_threshold:
                    h.is_up = False
                    h.last_state_change = now
                    log_event(f"WAN {wan.id} DOWN ({h.consecutive_fail} consecutive failures, all targets)")

        # Clean up WANs no longer in config
        current_ids = {w.id for w in self.config.wan_list()}
        for stale in [wid for wid in self.state.wans if wid not in current_ids]:
            del self.state.wans[stale]

        self.state.last_update = now

        # Choose active WAN and apply failover if enabled
        if apply_failover and f.enabled and f.failover_ip:
            new_active = choose_active(self.config, self.state)
            if new_active:
                if new_active != self.state.active_wan:
                    old = self.state.active_wan
                    changed = update_failover_route(self.config, new_active)
                    if changed:
                        log_event(f"Failover: {old or '(none)'} -> {new_active}")
                # Always reflect the chosen WAN in state, even if the route
                # was already correct (update_failover_route returns False then).
                self.state.active_wan = new_active

        # Poll UDM after we know our own state — verification compares them
        self._poll_udm(now)

        # Watchdog: catch crashed switcher processes
        self._switch_watchdog(now)

        # End-to-end probe (independent of UDM check)
        self._tick_e2e_probe(now)

        # Pick up manual override state for save_state
        intent = probe_mod.read_intent()
        if intent:
            self.state.manual_override_wan = intent.get("target_wan")
            self.state.manual_override_phase = intent.get("phase", "")
        else:
            self.state.manual_override_wan = None
            self.state.manual_override_phase = ""

        save_state(self.state)

    # ──────── watchdog ────────
    def _switch_watchdog(self, now: int):
        """If a switch intent has a deadline that passed without completing,
        revert to the snapshot. This catches crashed Flask workers.
        """
        intent = probe_mod.read_intent()
        if not intent:
            return
        phase = intent.get("phase", "")
        if phase not in ("switching", "verifying"):
            return
        deadline = int(intent.get("deadline", 0))
        if now < deadline:
            return

        # Deadline passed — force revert
        snap = intent.get("snapshot_route") or {}
        target = intent.get("target_wan", "(unknown)")
        prev = intent.get("previous_wan", "(unknown)")
        if snap.get("via") and snap.get("dev"):
            ok = probe_mod.set_failover_route(
                self.config.failover.table_id if self.config else 1000,
                snap["via"], snap["dev"],
            )
            log_event(
                f"WATCHDOG: switch to {target} took >= {deadline - int(intent.get('started_at', deadline))}s "
                f"without completing — reverted to {prev} via {snap['via']}/{snap['dev']} (success={ok})"
            )
        else:
            log_event(f"WATCHDOG: switch to {target} stalled but no snapshot to revert to")
        # Clear intent — back to auto mode
        probe_mod.clear_intent()
        if self.config and self.config.failover.failover_ip:
            probe_mod.flush_conntrack(self.config.failover.failover_ip)

    # ──────── e2e probe ────────
    def _tick_e2e_probe(self, now: int):
        if not self.config:
            return
        e = self.config.e2e_probe
        self.state.e2e.enabled = e.enabled
        if not e.enabled:
            return
        # Honor configured interval
        if (self.state.e2e.last_attempted_at
            and now - self.state.e2e.last_attempted_at < e.interval_seconds):
            return
        self.state.e2e.last_attempted_at = now
        result = probe_mod.run_probe(e)
        self.state.e2e.last_ok = result.ok
        # Record fastest RTT among successful targets
        rtts = [rtt for ok, rtt in result.target_results.values() if ok and rtt is not None]
        self.state.e2e.last_rtt_ms = min(rtts) if rtts else None
        self.state.e2e.last_reason = result.reason
        if result.ok:
            self.state.e2e.last_ok_at = now
            if self.state.e2e.consecutive_failures > 0:
                log_event(f"E2E probe recovered after {self.state.e2e.consecutive_failures} failures")
            self.state.e2e.consecutive_failures = 0
        else:
            self.state.e2e.consecutive_failures += 1
            if self.state.e2e.consecutive_failures in (1, 3, 10):
                log_event(f"E2E probe failed ({self.state.e2e.consecutive_failures}x): {result.reason}")


# ---------------------------------------------------------------------------
# Monkey-patch WanConfig.wan_key if not present (used for state dict keys)
# ---------------------------------------------------------------------------

def _wan_key(self) -> str:
    return self.id


if not hasattr(WanConfig, "wan_key"):
    WanConfig.wan_key = _wan_key


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    HealthDaemon().run()


if __name__ == "__main__":
    main()
