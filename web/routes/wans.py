"""WAN interface CRUD routes."""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from awesome_router import config as cfg, discovery
from awesome_router.models import WanConfig, Pair, Bandwidth

bp = Blueprint("wans", __name__)


@bp.route("/")
def list_wans():
    router_cfg = cfg.load()
    interfaces = discovery.get_interfaces()
    configured_ifs = {router_cfg.lan.interface} | {w.interface for w in router_cfg.wan_list()}
    unconfigured = discovery.get_unconfigured_interfaces(configured_ifs)
    return render_template("wan_list.html", cfg=router_cfg,
                           interfaces=interfaces, unconfigured=unconfigured)


@bp.route("/<wan_id>")
def detail(wan_id):
    router_cfg = cfg.load()
    wan = router_cfg.get_wan(wan_id)
    if not wan:
        flash(f"WAN '{wan_id}' not found", "error")
        return redirect(url_for("wans.list_wans"))

    iface_info = discovery.get_interfaces().get(wan.interface)
    routes = discovery.get_routes(str(wan.table_id))
    bw = discovery.get_bandwidth(wan.interface, 3600)
    public_ip = None
    if iface_info and iface_info.is_up:
        public_ip = discovery.get_public_ip(wan.interface,
                                             source_ip=iface_info.primary_ip)
    return render_template("wan_detail.html", cfg=router_cfg, wan=wan,
                           iface=iface_info, routes=routes, bandwidth=bw,
                           public_ip=public_ip)


@bp.route("/add", methods=["GET", "POST"])
def add():
    router_cfg = cfg.load()
    interfaces = discovery.get_interfaces()
    configured_ifs = {router_cfg.lan.interface} | {w.interface for w in router_cfg.wan_list()}
    unconfigured = discovery.get_unconfigured_interfaces(configured_ifs)

    if request.method == "GET":
        # Suggest next table_id
        used_ids = {w.table_id for w in router_cfg.wan_list()}
        next_id = 100
        while next_id in used_ids:
            next_id += 100
        return render_template("wan_form.html", cfg=router_cfg, wan=None,
                               unconfigured=unconfigured, next_table_id=next_id)

    # POST: create new WAN
    wan_id = request.form["wan_id"].strip().lower().replace(" ", "-")
    if wan_id in router_cfg.wans:
        flash(f"WAN ID '{wan_id}' already exists", "error")
        return redirect(url_for("wans.add"))

    nat_mode = request.form.get("nat_mode", "masquerade")
    wan = WanConfig(
        id=wan_id,
        name=request.form.get("name", wan_id),
        interface=request.form["interface"],
        type=request.form.get("addr_type", "dhcp"),
        gateway=request.form.get("gateway", "auto").strip() or "auto",
        table_id=int(request.form.get("table_id", 200)),
        nat_mode=nat_mode,
        enabled=True,
        metric=int(request.form.get("metric", 100)),
        bandwidth=Bandwidth(
            down_mbps=float(request.form.get("bw_down", 0) or 0),
            up_mbps=float(request.form.get("bw_up", 0) or 0),
        ),
    )

    # Parse addresses for static WANs
    if wan.type == "static":
        addrs = request.form.get("addresses", "").strip()
        if addrs:
            wan.addresses = [a.strip() for a in addrs.splitlines() if a.strip()]
        wan.router_ip = request.form.get("router_ip", "").strip() or None

    # Parse sources for masquerade
    if nat_mode == "masquerade":
        sources = request.form.get("sources", "").strip()
        if sources:
            wan.sources = [s.strip() for s in sources.splitlines() if s.strip()]

    router_cfg.wans[wan_id] = wan
    errors = cfg.validate(router_cfg)
    if errors:
        flash("Validation errors: " + "; ".join(errors), "error")
        return redirect(url_for("wans.add"))

    cfg.save(router_cfg)
    flash(f"WAN '{wan.name}' added. Click Apply to activate.", "success")
    return redirect(url_for("wans.detail", wan_id=wan_id))


@bp.route("/<wan_id>/edit", methods=["GET", "POST"])
def edit(wan_id):
    router_cfg = cfg.load()
    wan = router_cfg.get_wan(wan_id)
    if not wan:
        flash(f"WAN '{wan_id}' not found", "error")
        return redirect(url_for("wans.list_wans"))

    if request.method == "GET":
        return render_template("wan_form.html", cfg=router_cfg, wan=wan,
                               unconfigured=[], next_table_id=wan.table_id)

    # POST: update
    wan.name = request.form.get("name", wan.name)
    wan.gateway = request.form.get("gateway", wan.gateway).strip() or "auto"
    wan.metric = int(request.form.get("metric", wan.metric))
    wan.enabled = "enabled" in request.form
    wan.bandwidth = Bandwidth(
        down_mbps=float(request.form.get("bw_down", 0) or 0),
        up_mbps=float(request.form.get("bw_up", 0) or 0),
    )

    if wan.type == "static":
        addrs = request.form.get("addresses", "").strip()
        if addrs:
            wan.addresses = [a.strip() for a in addrs.splitlines() if a.strip()]
        wan.router_ip = request.form.get("router_ip", "").strip() or None

    if wan.nat_mode == "masquerade":
        sources = request.form.get("sources", "").strip()
        wan.sources = [s.strip() for s in sources.splitlines() if s.strip()] if sources else []

    errors = cfg.validate(router_cfg)
    if errors:
        flash("Validation errors: " + "; ".join(errors), "error")
        return redirect(url_for("wans.edit", wan_id=wan_id))

    cfg.save(router_cfg)
    flash(f"WAN '{wan.name}' updated. Click Apply to activate.", "success")
    return redirect(url_for("wans.detail", wan_id=wan_id))


@bp.route("/<wan_id>/delete", methods=["POST"])
def delete(wan_id):
    router_cfg = cfg.load()
    if wan_id not in router_cfg.wans:
        flash(f"WAN '{wan_id}' not found", "error")
        return redirect(url_for("wans.list_wans"))

    name = router_cfg.wans[wan_id].name
    del router_cfg.wans[wan_id]

    if router_cfg.vm_default_wan == wan_id:
        router_cfg.vm_default_wan = ""

    cfg.save(router_cfg)
    flash(f"WAN '{name}' removed. Click Apply to activate.", "warning")
    return redirect(url_for("wans.list_wans"))


@bp.route("/<wan_id>/pairs/add", methods=["POST"])
def add_pair(wan_id):
    router_cfg = cfg.load()
    wan = router_cfg.get_wan(wan_id)
    if not wan:
        flash(f"WAN '{wan_id}' not found", "error")
        return redirect(url_for("wans.list_wans"))

    if wan.nat_mode == "onetoone":
        public = request.form.get("public", "").strip()
        private = request.form.get("private", "").strip()
        if public and private:
            wan.pairs.append(Pair(public=public, private=private))
    elif wan.nat_mode == "masquerade":
        source = request.form.get("source", "").strip()
        if source and source not in wan.sources:
            wan.sources.append(source)

    cfg.save(router_cfg)
    flash("Mapping added. Click Apply to activate.", "success")
    return redirect(url_for("wans.detail", wan_id=wan_id))


@bp.route("/<wan_id>/pairs/<int:idx>/delete", methods=["POST"])
def delete_pair(wan_id, idx):
    router_cfg = cfg.load()
    wan = router_cfg.get_wan(wan_id)
    if not wan:
        flash(f"WAN '{wan_id}' not found", "error")
        return redirect(url_for("wans.list_wans"))

    if wan.nat_mode == "onetoone" and 0 <= idx < len(wan.pairs):
        wan.pairs.pop(idx)
    elif wan.nat_mode == "masquerade" and 0 <= idx < len(wan.sources):
        wan.sources.pop(idx)

    cfg.save(router_cfg)
    flash("Mapping removed. Click Apply to activate.", "warning")
    return redirect(url_for("wans.detail", wan_id=wan_id))
