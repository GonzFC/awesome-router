"""System routes: apply, rollback, logs, config view."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from awesome_router import config as cfg, discovery, apply_engine, rollback

bp = Blueprint("system", __name__)


@bp.route("/")
def index():
    services = {
        name: discovery.get_service_status(name)
        for name in ["udm-router-apply", "awesome-router-web",
                      "awesome-router-apply", "router-stats-sampler", "nftables"]
    }
    metrics = discovery.get_system_metrics()
    return render_template("system.html", services=services, metrics=metrics)


@bp.route("/apply", methods=["GET", "POST"])
def apply_config():
    router_cfg = cfg.load()

    if request.method == "GET" or request.form.get("action") == "plan":
        # Dry-run: show what would change
        result = apply_engine.apply(router_cfg, dry_run=True)
        return render_template("apply.html", cfg=router_cfg,
                               result=result, confirmed=False)

    if request.form.get("action") == "apply":
        result = apply_engine.apply(router_cfg, dry_run=False)
        if result["ok"]:
            flash(f"Applied {len(result.get('changes', []))} change(s) successfully.", "success")
        else:
            flash(f"Apply failed: {result.get('error', result.get('errors', 'unknown'))}", "error")
        return render_template("apply.html", cfg=router_cfg,
                               result=result, confirmed=True)

    return redirect(url_for("system.apply_config"))


@bp.route("/reset", methods=["POST"])
def reset_stack():
    """Flush custom routing/firewall state and re-apply from config."""
    router_cfg = cfg.load()
    result = apply_engine.reset_and_reapply(router_cfg)
    if result["ok"]:
        flash("Network stack reset complete. " + "; ".join(result.get("actions", [])), "success")
    else:
        flash(f"Reset failed: {result.get('error') or result.get('errors') or 'unknown'}", "error")
    return redirect(url_for("system.index"))


@bp.route("/restart-networkd", methods=["POST"])
def restart_networkd():
    """Restart systemd-networkd (bounces interfaces)."""
    result = apply_engine.restart_networkd()
    if result["ok"]:
        flash(result.get("message", "networkd restart scheduled"), "warning")
    else:
        flash(f"Restart failed: {result.get('error', 'unknown')}", "error")
    return redirect(url_for("system.index"))


@bp.route("/rollback", methods=["GET", "POST"])
def rollback_view():
    snapshots = rollback.list_snapshots()

    if request.method == "POST":
        snap_path = request.form.get("snapshot_path", "")
        if snap_path:
            actions = rollback.restore(snap_path)
            flash(f"Rollback complete: {len(actions)} action(s)", "success")
            return redirect(url_for("system.rollback_view"))

    return render_template("rollback.html", snapshots=snapshots)


@bp.route("/config")
def view_config():
    try:
        with open(cfg.DEFAULT_CONFIG_PATH) as f:
            content = f.read()
    except Exception as e:
        content = f"Error reading config: {e}"
    return render_template("config_view.html", content=content)


@bp.route("/nftables")
def view_nftables():
    nft = discovery.get_nftables()
    return render_template("nft_view.html", content=nft)
