"""Gateway switcher with verification + auto-rollback.

Public entry point: switch_failover_to(wan_id)

Flow:
    1. PRE-FLIGHT — validate WAN exists, fping new WAN's source IP (3 probes)
       Abort if the new WAN doesn't pass a quick liveness check.
    2. SNAPSHOT — capture current failover route + state, write intent file
       with deadline. The watchdog in health_daemon will read this if we crash.
    3. ATOMIC SWITCH — ip route replace + conntrack flush
    4. VERIFICATION WINDOW — run end-to-end probes for N seconds
    5. COMMIT or ROLLBACK based on verification outcome
    6. CLEAR intent file (or set phase=committed for the daemon to honor)
"""
from __future__ import annotations
import time
from dataclasses import asdict
from typing import Optional

from . import config as cfg
from . import probe as probe_mod
from .models import RouterConfig


# ─── result types ─────────────────────────────────────────────────────────

def _result(ok: bool, message: str, **extras) -> dict:
    out = {"ok": ok, "message": message}
    out.update(extras)
    return out


# ─── public API ───────────────────────────────────────────────────────────

def switch_failover_to(wan_id: str, *,
                        skip_verification: bool = False,
                        config: Optional[RouterConfig] = None) -> dict:
    """Switch the failover IP's route to the given WAN.

    Returns {ok: bool, message, phase, target_wan, ...} with full diagnostic info.
    """
    if config is None:
        config = cfg.load()

    # ── PRE-FLIGHT ────────────────────────────────────────────────────
    f = config.failover
    if not f.enabled or not f.failover_ip:
        return _result(False, "Failover is not enabled — configure it first",
                        phase="aborted")

    target_wan = config.get_wan(wan_id)
    if not target_wan:
        return _result(False, f"WAN '{wan_id}' not found in config",
                        phase="aborted")
    if not target_wan.enabled:
        return _result(False, f"WAN '{wan_id}' is disabled",
                        phase="aborted")
    if wan_id not in f.priority:
        return _result(False, f"WAN '{wan_id}' is not in the failover priority list",
                        phase="aborted")

    # Resolve the WAN's gateway + interface
    gw_dev = probe_mod.discover_wan_gateway(config, wan_id)
    if not gw_dev:
        return _result(False, f"Cannot determine gateway for WAN '{wan_id}'",
                        phase="aborted")
    new_gw, new_dev = gw_dev

    # Quick liveness check on the target WAN (skip if user forced it)
    if not skip_verification:
        from .health_daemon import _discover_primary_ip, probe_wan_fping
        new_source = _discover_primary_ip(new_dev)
        if not new_source:
            return _result(False, f"WAN '{wan_id}' has no IPv4 address",
                            phase="aborted")
        pre_results = probe_wan_fping(new_source, ["1.1.1.1", "8.8.8.8"], timeout_ms=800)
        any_ok = any(ok for ok, _ in pre_results.values())
        if not any_ok:
            return _result(False,
                            f"Pre-flight: WAN '{wan_id}' did not respond to fping",
                            phase="aborted",
                            target_wan=wan_id, target_gw=new_gw, target_dev=new_dev)

    # Snapshot current route
    snapshot = probe_mod.current_failover_route(f.table_id)
    if snapshot:
        # Already on the requested WAN?
        if snapshot.get("via") == new_gw and snapshot.get("dev") == new_dev:
            return _result(True,
                            f"Already routed to {wan_id} ({new_gw} via {new_dev}); no change",
                            phase="noop",
                            target_wan=wan_id)

    previous_wan = None
    # Try to infer previous wan from snapshot
    if snapshot:
        for w in config.wan_list():
            if w.interface == snapshot.get("dev"):
                previous_wan = w.id
                break

    # ── INTENT (the watchdog will read this if we crash) ───────────────
    now = int(time.time())
    gs = config.gateway_switcher
    intent = {
        "target_wan": wan_id,
        "target_gw": new_gw,
        "target_dev": new_dev,
        "previous_wan": previous_wan,
        "snapshot_route": snapshot or {},
        "started_at": now,
        "deadline": now + gs.watchdog_timeout_seconds,
        "phase": "switching",
    }
    probe_mod.write_intent(intent)

    # ── ATOMIC SWITCH ─────────────────────────────────────────────────
    switched = probe_mod.set_failover_route(f.table_id, new_gw, new_dev)
    if not switched:
        probe_mod.clear_intent()
        return _result(False, "Failed to update failover route",
                        phase="aborted", target_wan=wan_id)

    flushed = probe_mod.flush_conntrack(f.failover_ip)

    # Mark verifying so the watchdog gives us time
    intent["phase"] = "verifying"
    probe_mod.write_intent(intent)

    # ── VERIFICATION ──────────────────────────────────────────────────
    if skip_verification:
        # User forced it — commit immediately
        intent["phase"] = "committed"
        probe_mod.write_intent(intent)
        return _result(True,
                        f"Switched to {wan_id} (verification skipped). conntrack flushed {flushed} entries.",
                        phase="committed",
                        target_wan=wan_id,
                        verified=False,
                        flushed_conntrack=flushed)

    e = config.e2e_probe
    if not e.enabled:
        # No e2e probe configured — fall back to local fping verification
        from .health_daemon import _discover_primary_ip, probe_wan_fping
        new_source = _discover_primary_ip(new_dev) or ""
        verify_ok = False
        verify_reason = "e2e probe disabled; using local fping fallback"
        if new_source:
            for _ in range(gs.required_passing_samples * 2):
                r = probe_wan_fping(new_source, ["1.1.1.1", "8.8.8.8"], timeout_ms=800)
                if any(ok for ok, _ in r.values()):
                    verify_ok = True
                    break
                time.sleep(gs.sample_interval_seconds)
        result = _decide_commit_or_rollback(
            config, intent, verify_ok, verify_reason, flushed,
        )
        return result

    # Full e2e verification
    probe_result = probe_mod.run_probe_window(
        e,
        duration_seconds=gs.verification_window_seconds,
        sample_interval_seconds=gs.sample_interval_seconds,
        required_passing_samples=gs.required_passing_samples,
    )

    return _decide_commit_or_rollback(
        config, intent, probe_result.ok,
        f"e2e: {probe_result.samples_passed}/{probe_result.required if hasattr(probe_result, 'required') else gs.required_passing_samples} samples, {probe_result.reason}".strip(),
        flushed, probe_result=probe_result,
    )


def _decide_commit_or_rollback(config, intent, verify_ok, verify_reason,
                                  flushed, probe_result=None):
    f = config.failover
    gs = config.gateway_switcher
    target_wan = intent["target_wan"]

    if verify_ok:
        intent["phase"] = "committed"
        probe_mod.write_intent(intent)
        msg = f"Switched to {target_wan}. Verified end-to-end. {verify_reason}"
        return _result(True, msg, phase="committed", target_wan=target_wan,
                        verified=True, flushed_conntrack=flushed,
                        probe_result=_serialize_probe(probe_result))

    # Verification failed → rollback
    if not gs.auto_rollback:
        intent["phase"] = "committed"
        probe_mod.write_intent(intent)
        return _result(False,
                        f"Verification failed but auto-rollback disabled. Stayed on {target_wan}. {verify_reason}",
                        phase="committed_unverified", target_wan=target_wan,
                        verified=False, flushed_conntrack=flushed,
                        probe_result=_serialize_probe(probe_result))

    snap = intent.get("snapshot_route") or {}
    previous_wan = intent.get("previous_wan") or "previous"
    if snap.get("via") and snap.get("dev"):
        reverted = probe_mod.set_failover_route(f.table_id, snap["via"], snap["dev"])
        probe_mod.flush_conntrack(f.failover_ip)
        if reverted:
            probe_mod.clear_intent()
            msg = (f"Verification failed — rolled back to {previous_wan} "
                   f"({snap['via']} via {snap['dev']}). {verify_reason}")
            return _result(False, msg, phase="rolled_back",
                            target_wan=target_wan,
                            reverted_to_wan=previous_wan,
                            verified=False,
                            probe_result=_serialize_probe(probe_result))
        else:
            return _result(False,
                            f"CRITICAL: verification failed AND rollback failed. Manual intervention needed.",
                            phase="rollback_failed", target_wan=target_wan,
                            verified=False)
    else:
        # No snapshot to rollback to (e.g. failover table was empty before)
        probe_mod.clear_intent()
        return _result(False,
                        f"Verification failed. No snapshot to roll back to; check failover table manually. {verify_reason}",
                        phase="rolled_back_no_snapshot",
                        target_wan=target_wan, verified=False)


def _serialize_probe(p) -> Optional[dict]:
    if p is None:
        return None
    return {
        "ok": p.ok,
        "samples_attempted": p.samples_attempted,
        "samples_passed": p.samples_passed,
        "reason": p.reason,
        "targets": {t: {"ok": ok, "rtt_ms": rtt}
                     for t, (ok, rtt) in p.target_results.items()},
    }


def release_override() -> dict:
    """Clear any manual override and return to auto-priority selection."""
    intent = probe_mod.read_intent()
    if not intent:
        return _result(True, "No manual override active", phase="none")
    target = intent.get("target_wan", "(unknown)")
    probe_mod.clear_intent()
    return _result(True, f"Released manual override (was pinned to {target})",
                    phase="released", target_wan=target)
