"""Failover configuration and status routes."""
import json
import os
import sqlite3

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from awesome_router import config as cfg, discovery, probe as probe_mod, switcher
from awesome_router.models import FailoverConfig, HealthConfig

bp = Blueprint("failover", __name__)

HEALTH_STATE_FILE = "/run/awesome-router/health.json"
DB_PATH = "/var/lib/awesome-router-monitor.db"


def _load_health_state():
    try:
        with open(HEALTH_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _recent_events(limit=20):
    """Return [(timestamp_str, message)] formatted in local time."""
    try:
        con = sqlite3.connect(DB_PATH)
        rows = list(con.execute(
            """SELECT datetime(ts, 'unixepoch', 'localtime'), message
               FROM failover_events ORDER BY ts DESC LIMIT ?""",
            (limit,)
        ))
        con.close()
        return rows
    except Exception:
        return []


@bp.route("/")
def index():
    router_cfg = cfg.load()
    state = _load_health_state()
    events = _recent_events()
    # Surface manual override info from intent file too
    intent = probe_mod.read_intent() or {}
    if state is None:
        state = {}
    state["manual_override_wan"] = intent.get("target_wan")
    state["manual_override_phase"] = intent.get("phase", "")
    return render_template("failover.html",
                           cfg=router_cfg,
                           state=state,
                           events=events)


@bp.route("/edit", methods=["GET", "POST"])
def edit():
    router_cfg = cfg.load()

    if request.method == "GET":
        return render_template("failover_form.html", cfg=router_cfg)

    # POST: save failover config
    f = router_cfg.failover
    f.enabled = "enabled" in request.form
    f.failover_ip = request.form.get("failover_ip", "").strip()
    try:
        f.table_id = int(request.form.get("table_id", 1000))
    except ValueError:
        f.table_id = 1000

    # Priority: comma-separated or newline-separated list from textarea
    pri_raw = request.form.get("priority", "").strip()
    if pri_raw:
        f.priority = [p.strip() for p in pri_raw.replace(",", "\n").splitlines() if p.strip()]
    else:
        f.priority = []

    # Health settings
    targets_raw = request.form.get("targets", "").strip()
    if targets_raw:
        f.health.targets = [t.strip() for t in targets_raw.replace(",", "\n").splitlines() if t.strip()]
    try:
        f.health.interval_seconds = int(request.form.get("interval_seconds", 10))
        f.health.timeout_seconds = int(request.form.get("timeout_seconds", 3))
        f.health.fail_threshold = int(request.form.get("fail_threshold", 3))
        f.health.recover_threshold = int(request.form.get("recover_threshold", 2))
    except ValueError:
        pass

    # Per-WAN failover SNAT IPs
    for wan_id, wan in router_cfg.wans.items():
        field_name = f"snat_{wan_id}"
        val = request.form.get(field_name, "").strip()
        wan.failover_snat_ip = val or None

    errors = cfg.validate(router_cfg)
    if errors:
        flash("Validation errors: " + "; ".join(errors), "error")
        return redirect(url_for("failover.edit"))

    cfg.save(router_cfg)
    flash("Failover settings saved. Click Apply to activate.", "success")
    return redirect(url_for("failover.index"))


@bp.route("/switch", methods=["POST"])
def switch():
    """Switch the failover IP route to a specific WAN, with verification."""
    target_wan = request.form.get("target_wan", "").strip()
    skip_verification = "skip_verification" in request.form
    if not target_wan:
        flash("Pick a WAN to switch to.", "error")
        return redirect(url_for("failover.index"))

    result = switcher.switch_failover_to(target_wan, skip_verification=skip_verification)

    if result["ok"]:
        flash(result["message"], "success")
    else:
        category = "warning" if result.get("phase") == "rolled_back" else "error"
        flash(result["message"], category)
    return redirect(url_for("failover.index"))


@bp.route("/release-override", methods=["POST"])
def release_override():
    """Clear the manual override and return to auto-priority mode."""
    result = switcher.release_override()
    flash(result["message"], "success" if result["ok"] else "error")
    return redirect(url_for("failover.index"))


@bp.route("/e2e-probe-now", methods=["POST"])
def e2e_probe_now():
    """Run a single end-to-end probe right now and return the result."""
    router_cfg = cfg.load()
    if not router_cfg.e2e_probe.enabled:
        return jsonify({"ok": False, "error": "e2e probe not enabled"}), 400
    result = probe_mod.run_probe(router_cfg.e2e_probe)
    return jsonify({
        "ok": result.ok,
        "reason": result.reason,
        "samples_attempted": result.samples_attempted,
        "samples_passed": result.samples_passed,
        "targets": {t: {"ok": ok, "rtt_ms": rtt}
                     for t, (ok, rtt) in result.target_results.items()},
    })


@bp.route("/api/switch-status")
def switch_status():
    """Return current manual-override intent (for the GUI)."""
    intent = probe_mod.read_intent() or {}
    return jsonify(intent)


@bp.route("/reorder", methods=["POST"])
def reorder():
    """Update the priority order via JSON POST."""
    data = request.get_json(silent=True) or {}
    new_order = data.get("priority", [])
    if not isinstance(new_order, list):
        return {"ok": False, "error": "priority must be a list"}, 400

    router_cfg = cfg.load()
    # Only accept WAN ids that exist
    router_cfg.failover.priority = [wid for wid in new_order if wid in router_cfg.wans]
    cfg.save(router_cfg)
    return {"ok": True, "priority": router_cfg.failover.priority}
