"""Dashboard route — main overview page."""
import json
from flask import Blueprint, render_template
from awesome_router import config as cfg, discovery

bp = Blueprint("dashboard", __name__)

HEALTH_STATE_FILE = "/run/awesome-router/health.json"


def _load_health():
    try:
        with open(HEALTH_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


@bp.route("/")
def index():
    router_cfg = cfg.load()
    interfaces = discovery.get_interfaces()
    rules = discovery.get_rules()
    metrics = discovery.get_system_metrics()
    health = _load_health()

    wan_status = []
    for w in router_cfg.wan_list():
        iface_info = interfaces.get(w.interface)
        gw = w.gateway
        if gw == "auto":
            gw = discovery.get_default_gateway(str(w.table_id)) or discovery.get_default_gateway("main")

        bw = discovery.get_bandwidth(w.interface, window_seconds=3600)
        wan_health = None
        is_active = False
        if health:
            wan_health = health.get("wans", {}).get(w.id)
            is_active = health.get("active_wan") == w.id

        # Public IP for this WAN (cached ~5 min in discovery module)
        public_ip = None
        if iface_info and iface_info.is_up:
            source_ip = iface_info.primary_ip
            public_ip = discovery.get_public_ip(w.interface, source_ip=source_ip)

        wan_status.append({
            "config": w,
            "iface": iface_info,
            "gateway": gw,
            "is_up": iface_info.is_up if iface_info else False,
            "primary_ip": iface_info.primary_ip if iface_info else None,
            "public_ip": public_ip,
            "bandwidth": bw,
            "health": wan_health,
            "is_active_failover": is_active,
        })

    return render_template("dashboard.html",
                           cfg=router_cfg, wans=wan_status,
                           rules=rules, metrics=metrics, health=health)
