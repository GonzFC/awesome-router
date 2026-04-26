"""JSON API endpoints for AJAX auto-refresh."""
import json
import os
import sqlite3
import time
from flask import Blueprint, jsonify, request
from awesome_router import config as cfg, discovery

DB_PATH = "/var/lib/awesome-router-monitor.db"
HEALTH_STATE_FILE = "/run/awesome-router/health.json"

bp = Blueprint("api", __name__)


def _human(n, suffix="B"):
    for u in ("", "K", "M", "G", "T"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {u}{suffix}"
        n /= 1024.0
    return f"{n:.1f} P{suffix}"


def _human_bps(bps):
    return _human(bps, "b/s")


@bp.route("/status")
def status():
    router_cfg = cfg.load()
    interfaces = discovery.get_interfaces()
    metrics = discovery.get_system_metrics()

    wans = []
    for w in router_cfg.wan_list():
        iface = interfaces.get(w.interface)
        gw = w.gateway
        if gw == "auto":
            gw = discovery.get_default_gateway(str(w.table_id)) or \
                 discovery.get_default_gateway("main")

        bw_1h = discovery.get_bandwidth(w.interface, 3600)

        wans.append({
            "id": w.id,
            "name": w.name,
            "interface": w.interface,
            "is_up": iface.is_up if iface else False,
            "primary_ip": iface.primary_ip if iface else None,
            "gateway": gw,
            "nat_mode": w.nat_mode,
            "enabled": w.enabled,
            "num_pairs": len(w.pairs),
            "num_sources": len(w.sources),
            "bw_rx_1h": _human_bps(bw_1h.rx_bps) if bw_1h else "--",
            "bw_tx_1h": _human_bps(bw_1h.tx_bps) if bw_1h else "--",
            "bw_rx_total_1h": _human(bw_1h.rx_bytes) if bw_1h else "--",
            "bw_tx_total_1h": _human(bw_1h.tx_bytes) if bw_1h else "--",
        })

    return jsonify({
        "wans": wans,
        "system": {
            "cpu": metrics.cpu_percent,
            "mem_percent": metrics.mem_percent,
            "mem_used": _human(metrics.mem_used),
            "mem_total": _human(metrics.mem_total),
            "disk_percent": metrics.disk_percent,
            "uptime_hours": round(metrics.uptime_seconds / 3600, 1),
            "load": f"{metrics.load_1:.2f} {metrics.load_5:.2f} {metrics.load_15:.2f}",
        },
    })


@bp.route("/health")
def health():
    """Return current WAN health state from the daemon."""
    try:
        with open(HEALTH_STATE_FILE) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"active_wan": None, "wans": {}, "last_update": 0})


@bp.route("/routes")
def routes():
    router_cfg = cfg.load()
    tables = {"main": [r.raw for r in discovery.get_routes("main")]}
    for w in router_cfg.wan_list():
        tbl = str(w.table_id)
        tables[f"{w.id} ({tbl})"] = [r.raw for r in discovery.get_routes(tbl)]
    rules = [r.raw for r in discovery.get_rules()]
    return jsonify({"tables": tables, "rules": rules})


@bp.route("/bandwidth/<wan_id>")
def bandwidth_timeseries(wan_id):
    """Return time-series bandwidth data for charts.

    Query params:
      window: "24h" (default), "7d", "30d"

    Returns bucketed average rates in bits/sec for download and upload.
    """
    router_cfg = cfg.load()
    wan = router_cfg.get_wan(wan_id)
    if not wan:
        return jsonify({"error": "WAN not found"}), 404

    window = request.args.get("window", "24h")
    if window == "7d":
        window_secs = 7 * 86400
        bucket_secs = 1800       # 30 min buckets
    elif window == "30d":
        window_secs = 30 * 86400
        bucket_secs = 7200       # 2 hour buckets
    else:  # 24h
        window_secs = 86400
        bucket_secs = 300        # 5 min buckets

    series = _build_timeseries(wan.interface, window_secs, bucket_secs)

    return jsonify({
        "wan_id": wan_id,
        "wan_name": wan.name,
        "interface": wan.interface,
        "window": window,
        "bucket_seconds": bucket_secs,
        "capacity_down_mbps": wan.bandwidth.down_mbps,
        "capacity_up_mbps": wan.bandwidth.up_mbps,
        "series": series,
    })


@bp.route("/bandwidth-all")
def bandwidth_all():
    """Return time-series bandwidth for ALL WANs in one call."""
    router_cfg = cfg.load()
    window = request.args.get("window", "24h")
    if window == "7d":
        window_secs = 7 * 86400
        bucket_secs = 1800
    elif window == "30d":
        window_secs = 30 * 86400
        bucket_secs = 7200
    else:
        window_secs = 86400
        bucket_secs = 300

    result = {}
    for w in router_cfg.wan_list():
        result[w.id] = {
            "name": w.name,
            "interface": w.interface,
            "capacity_down_mbps": w.bandwidth.down_mbps,
            "capacity_up_mbps": w.bandwidth.up_mbps,
            "series": _build_timeseries(w.interface, window_secs, bucket_secs),
        }

    return jsonify({
        "window": window,
        "bucket_seconds": bucket_secs,
        "wans": result,
    })


def _build_timeseries(interface: str, window_secs: int, bucket_secs: int) -> list[dict]:
    """Query SQLite and return bucketed bandwidth rates."""
    try:
        con = sqlite3.connect(DB_PATH)
        now = int(time.time())
        start = now - window_secs
        rows = list(con.execute(
            "SELECT ts, rx, tx FROM samples WHERE iface=? AND ts>=? ORDER BY ts ASC",
            (interface, start)
        ))
        con.close()
    except Exception:
        return []

    if len(rows) < 2:
        return []

    # Handle counter resets (reboots): find segments of continuous counters
    segments = []
    seg_start = 0
    for i in range(1, len(rows)):
        if rows[i][1] < rows[i - 1][1]:  # rx decreased = counter reset
            if i - seg_start >= 2:
                segments.append(rows[seg_start:i])
            seg_start = i
    if len(rows) - seg_start >= 2:
        segments.append(rows[seg_start:])

    if not segments:
        return []

    # Build rate samples from all segments
    rate_samples = []  # (ts, rx_bps, tx_bps)
    for seg in segments:
        for i in range(1, len(seg)):
            ts_prev, rx_prev, tx_prev = seg[i - 1]
            ts_cur, rx_cur, tx_cur = seg[i]
            dt = max(1, ts_cur - ts_prev)
            rx_rate = max(0, (rx_cur - rx_prev) / dt) * 8  # bits/sec
            tx_rate = max(0, (tx_cur - tx_prev) / dt) * 8
            rate_samples.append((ts_cur, rx_rate, tx_rate))

    if not rate_samples:
        return []

    # Bucket the rate samples
    buckets: dict[int, list[tuple[float, float]]] = {}
    for ts, rx, tx in rate_samples:
        bucket_ts = ts - (ts % bucket_secs)
        buckets.setdefault(bucket_ts, []).append((rx, tx))

    # Average each bucket
    series = []
    for bucket_ts in sorted(buckets.keys()):
        samples = buckets[bucket_ts]
        avg_rx = sum(s[0] for s in samples) / len(samples)
        avg_tx = sum(s[1] for s in samples) / len(samples)
        series.append({
            "t": bucket_ts,
            "rx": round(avg_rx, 0),    # bits/sec download
            "tx": round(avg_tx, 0),    # bits/sec upload
        })

    return series
