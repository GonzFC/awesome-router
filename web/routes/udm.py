"""UDM integration routes: configure, test connection, query live status."""
from __future__ import annotations
import os

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from awesome_router import config as cfg
from awesome_router.udm_client import UdmClient, UdmError, UdmUnauthorized, UdmUnreachable

bp = Blueprint("udm", __name__)


def _client_for(udm_cfg) -> UdmClient | None:
    """Build a UdmClient from a UdmConfig, or None if not configured."""
    if not udm_cfg.host:
        return None
    try:
        return UdmClient(
            host=udm_cfg.host,
            key_file=udm_cfg.key_file,
            verify_ssl=udm_cfg.verify_ssl,
            cache_seconds=udm_cfg.cache_seconds,
        )
    except UdmError:
        return None


@bp.route("/edit", methods=["GET", "POST"])
def edit():
    router_cfg = cfg.load()

    if request.method == "GET":
        # Has the key file got contents? (don't show it, just say yes/no)
        key_present = os.path.exists(router_cfg.udm.key_file) and \
                       os.path.getsize(router_cfg.udm.key_file) > 0
        return render_template("udm_form.html",
                                cfg=router_cfg, key_present=key_present)

    # POST: save settings (NOT the key itself — that has its own endpoint)
    u = router_cfg.udm
    u.enabled = "enabled" in request.form
    u.host = request.form.get("host", "").strip()
    u.verify_ssl = "verify_ssl" in request.form
    u.site_id = request.form.get("site_id", "auto").strip() or "auto"
    u.gateway_device_id = request.form.get("gateway_device_id", "auto").strip() or "auto"
    try:
        u.poll_interval_seconds = int(request.form.get("poll_interval_seconds", 30))
        u.disagreement_threshold = int(request.form.get("disagreement_threshold", 3))
    except ValueError:
        pass

    cfg.save(router_cfg)
    flash("UDM integration settings saved. Click Apply to activate verification logic.",
          "success")
    return redirect(url_for("udm.edit"))


@bp.route("/key", methods=["POST"])
def save_key():
    """Save (overwrite) the API key file with strict permissions."""
    router_cfg = cfg.load()
    key = request.form.get("api_key", "").strip()
    if not key:
        flash("API key cannot be empty.", "error")
        return redirect(url_for("udm.edit"))

    path = router_cfg.udm.key_file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # write atomically + chmod 0600
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(key + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    flash(f"API key saved to {path} (0600 root:root).", "success")
    return redirect(url_for("udm.edit"))


@bp.route("/test", methods=["POST"])
def test_connection():
    """Hit /info and /sites and report what we found."""
    router_cfg = cfg.load()
    client = _client_for(router_cfg.udm)
    if client is None:
        flash("UDM not configured — set host and API key first.", "error")
        return redirect(url_for("udm.edit"))

    try:
        info = client.info()
        sites = client.sites()
        gw = None
        if sites:
            site_id = router_cfg.udm.site_id
            if site_id == "auto":
                site_id = sites[0]["id"]
            gw = client.find_gateway(site_id)
        msg_parts = [
            f"Controller v{info.get('applicationVersion', '?')}",
            f"{len(sites)} site(s)",
        ]
        if gw:
            msg_parts.append(f"gateway = {gw.name} ({gw.model})")
        flash("UDM reachable — " + " · ".join(msg_parts), "success")
    except UdmUnauthorized:
        flash("UDM rejected the API key (401). Re-save the key and try again.", "error")
    except UdmUnreachable as e:
        flash(f"Cannot reach UDM: {e}", "error")
    except UdmError as e:
        flash(f"UDM error: {e}", "error")

    return redirect(url_for("udm.edit"))


# ─── JSON API consumed by the Failover page ───────────────────────────────

@bp.route("/api/status")
def api_status():
    """Return current UDM status (model, ports, uplink rates, stats)."""
    router_cfg = cfg.load()
    if not router_cfg.udm.enabled:
        return jsonify({"enabled": False})

    client = _client_for(router_cfg.udm)
    if client is None:
        return jsonify({"enabled": True, "reachable": False,
                         "error": "no API key or host"})

    try:
        site_id = router_cfg.udm.site_id
        if site_id == "auto":
            site_id = client.default_site_id()
        if not site_id:
            return jsonify({"enabled": True, "reachable": True,
                             "error": "no sites found"})

        gw_id = router_cfg.udm.gateway_device_id
        if gw_id == "auto":
            gw = client.find_gateway(site_id)
            if not gw:
                return jsonify({"enabled": True, "reachable": True,
                                 "error": "no UDM/UDR gateway in site"})
            gw_id = gw.id
            gw_name = gw.name
            gw_model = gw.model
            gw_state = gw.state
            gw_ip = gw.ip
            gw_mac = gw.mac
            gw_firmware = gw.firmware
        else:
            d = client.device(site_id, gw_id)
            gw_name = d.get("name", "")
            gw_model = d.get("model", "")
            gw_state = d.get("state", "")
            gw_ip = d.get("ipAddress", "")
            gw_mac = d.get("macAddress", "")
            gw_firmware = d.get("firmwareVersion", "")

        stats = client.device_stats(site_id, gw_id)
        info = client.info()

        return jsonify({
            "enabled": True,
            "reachable": True,
            "controller_version": info.get("applicationVersion", ""),
            "site_id": site_id,
            "gateway": {
                "id": gw_id,
                "name": gw_name,
                "model": gw_model,
                "state": gw_state,
                "ip": gw_ip,
                "mac": gw_mac,
                "firmware": gw_firmware,
                "uptime_sec": stats.uptime_sec,
                "last_heartbeat": stats.last_heartbeat,
                "cpu_pct": stats.cpu_pct,
                "mem_pct": stats.mem_pct,
                "load_1": stats.load_1,
                "uplink_tx_bps": stats.uplink_tx_bps,
                "uplink_rx_bps": stats.uplink_rx_bps,
            },
        })
    except UdmUnauthorized as e:
        return jsonify({"enabled": True, "reachable": False,
                         "error": "unauthorized", "detail": str(e)})
    except UdmUnreachable as e:
        return jsonify({"enabled": True, "reachable": False,
                         "error": "unreachable", "detail": str(e)})
    except UdmError as e:
        return jsonify({"enabled": True, "reachable": True,
                         "error": "api_error", "detail": str(e)})
