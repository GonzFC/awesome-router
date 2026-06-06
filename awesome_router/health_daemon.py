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
from .models import RouterConfig, WanConfig
from .udm_client import (
    UdmClient, UdmError, UdmUnauthorized, UdmUnreachable, UdmStats,
)

STATE_DIR = "/run/awesome-router"
STATE_FILE = f"{STATE_DIR}/health.json"
EVENT_LOG = "/var/lib/awesome-router/failover-events.log"
DB_PATH = "/var/lib/awesome-router-monitor.db"


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
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


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
    """Pick the highest-priority healthy WAN from the priority list."""
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

        # UDM signals trouble if:
        #   - state != ONLINE  (UDM controller marks it offline), OR
        #   - uplink_rx_bps == 0 AND uplink_tx_bps == 0 AND AR thinks healthy
        #     for one poll cycle. Idle networks can be 0 bps, so a single
        #     zero-tick alone isn't enough — we accumulate across cycles.
        udm_silent = (u.uplink_tx_bps == 0 and u.uplink_rx_bps == 0)
        udm_offline = u.device_state and u.device_state != "ONLINE"

        if ar_thinks_ok and udm_offline:
            u.consecutive_disagreements += 1
            log_event(f"UDM disagrees: AR says active WAN OK, UDM state={u.device_state} (count {u.consecutive_disagreements})")
        elif ar_thinks_ok and udm_silent and u.reachable:
            # Silent + AR healthy could be idle. Count once and let the
            # threshold decide. Heartbeat freshness is a sanity check.
            u.consecutive_disagreements += 1
        else:
            if u.consecutive_disagreements > 0:
                log_event(f"UDM agrees again (reset {u.consecutive_disagreements} -> 0)")
            u.consecutive_disagreements = 0

        # Trigger corrective action if threshold crossed
        threshold = self.config.udm.disagreement_threshold
        if u.consecutive_disagreements >= threshold:
            self._take_corrective_action(now)

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
            if new_active and new_active != self.state.active_wan:
                old = self.state.active_wan
                if update_failover_route(self.config, new_active):
                    log_event(f"Failover: {old or '(none)'} -> {new_active}")
                    self.state.active_wan = new_active
            elif new_active:
                self.state.active_wan = new_active

        # Poll UDM after we know our own state — verification compares them
        self._poll_udm(now)

        save_state(self.state)


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
