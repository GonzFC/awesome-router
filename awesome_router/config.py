"""Load, validate, and save the unified YAML configuration."""
from __future__ import annotations
import copy
import os
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from .models import (
    Bandwidth, FailoverConfig, HealthConfig, LanConfig, Pair,
    RouterConfig, UdmConfig, WanConfig,
)

DEFAULT_CONFIG_PATH = "/etc/awesome-router.yaml"


def load(path: str = DEFAULT_CONFIG_PATH) -> RouterConfig:
    """Load and validate configuration from a YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f)
    return _parse(raw)


def save(config: RouterConfig, path: str = DEFAULT_CONFIG_PATH, *, backup: bool = True):
    """Write configuration to YAML, optionally backing up the previous file."""
    if backup and os.path.exists(path):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, f"{path}.bak-{ts}")

    data = _serialize(config)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _parse(raw: dict) -> RouterConfig:
    """Parse raw YAML dict into RouterConfig."""
    version = raw.get("version", 1)

    lan_raw = raw["lan"]
    lan = LanConfig(interface=lan_raw["interface"], address=lan_raw["address"])

    wans = {}
    for wan_id, w in raw.get("wans", {}).items():
        pairs = [Pair(public=p["public"], private=p["private"]) for p in w.get("pairs", [])]
        bw_raw = w.get("bandwidth", {})
        bw = Bandwidth(down_mbps=bw_raw.get("down_mbps", 0), up_mbps=bw_raw.get("up_mbps", 0))

        wans[wan_id] = WanConfig(
            id=wan_id,
            name=w.get("name", wan_id),
            interface=w["interface"],
            type=w.get("type", "dhcp"),
            gateway=w.get("gateway", "auto"),
            table_id=w["table_id"],
            nat_mode=w.get("nat_mode", "masquerade"),
            enabled=w.get("enabled", True),
            addresses=w.get("addresses", []),
            router_ip=w.get("router_ip"),
            pairs=pairs,
            sources=w.get("sources", []),
            metric=w.get("metric", 100),
            failover_snat_ip=w.get("failover_snat_ip"),
            bandwidth=bw,
        )

    # Failover section (optional)
    failover_raw = raw.get("failover", {}) or {}
    health_raw = failover_raw.get("health", {}) or {}
    health = HealthConfig(
        targets=health_raw.get("targets", ["8.8.8.8", "1.1.1.1", "9.9.9.9"]),
        interval_seconds=health_raw.get("interval_seconds", 10),
        timeout_seconds=health_raw.get("timeout_seconds", 3),
        fail_threshold=health_raw.get("fail_threshold", 3),
        recover_threshold=health_raw.get("recover_threshold", 2),
    )
    failover = FailoverConfig(
        enabled=failover_raw.get("enabled", False),
        failover_ip=failover_raw.get("failover_ip", ""),
        table_id=failover_raw.get("table_id", 1000),
        priority=failover_raw.get("priority", []),
        health=health,
    )

    # UDM section (optional)
    udm_raw = raw.get("udm", {}) or {}
    udm = UdmConfig(
        enabled=udm_raw.get("enabled", False),
        host=udm_raw.get("host", ""),
        key_file=udm_raw.get("key_file", "/etc/awesome-router/udm.key"),
        verify_ssl=udm_raw.get("verify_ssl", False),
        site_id=udm_raw.get("site_id", "auto"),
        gateway_device_id=udm_raw.get("gateway_device_id", "auto"),
        poll_interval_seconds=udm_raw.get("poll_interval_seconds", 30),
        cache_seconds=udm_raw.get("cache_seconds", 5),
        disagreement_threshold=udm_raw.get("disagreement_threshold", 3),
    )

    return RouterConfig(
        version=version,
        lan=lan,
        vm_default_wan=raw.get("vm_default_wan", ""),
        wans=wans,
        failover=failover,
        udm=udm,
    )


def _serialize(config: RouterConfig) -> dict:
    """Serialize RouterConfig to a dict suitable for YAML dump."""
    wans = {}
    for wid, w in config.wans.items():
        wan_dict: dict = {
            "name": w.name,
            "interface": w.interface,
            "type": w.type,
            "gateway": w.gateway,
            "table_id": w.table_id,
            "nat_mode": w.nat_mode,
            "enabled": w.enabled,
        }
        if w.addresses:
            wan_dict["addresses"] = w.addresses
        if w.router_ip:
            wan_dict["router_ip"] = w.router_ip
        if w.pairs:
            wan_dict["pairs"] = [{"public": p.public, "private": p.private} for p in w.pairs]
        if w.sources:
            wan_dict["sources"] = w.sources
        if w.type == "dhcp":
            wan_dict["metric"] = w.metric
        if w.failover_snat_ip:
            wan_dict["failover_snat_ip"] = w.failover_snat_ip
        if w.bandwidth.down_mbps or w.bandwidth.up_mbps:
            wan_dict["bandwidth"] = {"down_mbps": w.bandwidth.down_mbps, "up_mbps": w.bandwidth.up_mbps}
        wans[wid] = wan_dict

    result = {
        "version": config.version,
        "lan": {"interface": config.lan.interface, "address": config.lan.address},
        "vm_default_wan": config.vm_default_wan,
        "wans": wans,
    }

    # Only include failover section if meaningfully configured
    f = config.failover
    if f.enabled or f.failover_ip or f.priority:
        result["failover"] = {
            "enabled": f.enabled,
            "failover_ip": f.failover_ip,
            "table_id": f.table_id,
            "priority": list(f.priority),
            "health": {
                "targets": list(f.health.targets),
                "interval_seconds": f.health.interval_seconds,
                "timeout_seconds": f.health.timeout_seconds,
                "fail_threshold": f.health.fail_threshold,
                "recover_threshold": f.health.recover_threshold,
            },
        }

    # UDM section (only if enabled or partially configured)
    u = config.udm
    if u.enabled or u.host:
        result["udm"] = {
            "enabled": u.enabled,
            "host": u.host,
            "key_file": u.key_file,
            "verify_ssl": u.verify_ssl,
            "site_id": u.site_id,
            "gateway_device_id": u.gateway_device_id,
            "poll_interval_seconds": u.poll_interval_seconds,
            "cache_seconds": u.cache_seconds,
            "disagreement_threshold": u.disagreement_threshold,
        }

    return result


def validate(config: RouterConfig) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors = []

    if not config.lan.interface:
        errors.append("LAN interface not set")
    if not config.lan.address:
        errors.append("LAN address not set")

    if config.vm_default_wan and config.vm_default_wan not in config.wans:
        errors.append(f"vm_default_wan '{config.vm_default_wan}' not found in wans")

    table_ids = {}
    interfaces = {}
    all_privates = set()

    for wid, w in config.wans.items():
        if not w.interface:
            errors.append(f"WAN '{wid}': interface not set")
        if w.interface in interfaces:
            errors.append(f"WAN '{wid}': interface '{w.interface}' already used by '{interfaces[w.interface]}'")
        interfaces[w.interface] = wid

        if w.table_id in table_ids:
            errors.append(f"WAN '{wid}': table_id {w.table_id} already used by '{table_ids[w.table_id]}'")
        table_ids[w.table_id] = wid

        if w.type not in ("static", "dhcp"):
            errors.append(f"WAN '{wid}': type must be 'static' or 'dhcp', got '{w.type}'")
        if w.nat_mode not in ("onetoone", "masquerade"):
            errors.append(f"WAN '{wid}': nat_mode must be 'onetoone' or 'masquerade', got '{w.nat_mode}'")

        if w.nat_mode == "onetoone" and not w.pairs:
            errors.append(f"WAN '{wid}': nat_mode is onetoone but no pairs defined")
        if w.nat_mode == "masquerade" and not w.sources:
            errors.append(f"WAN '{wid}': nat_mode is masquerade but no sources defined")

        for ip in w.private_ips:
            if ip in all_privates:
                errors.append(f"WAN '{wid}': private IP {ip} is assigned to multiple WANs")
            all_privates.add(ip)

    # Failover validation
    f = config.failover
    if f.enabled:
        if not f.failover_ip:
            errors.append("Failover enabled but failover_ip not set")
        if not f.priority:
            errors.append("Failover enabled but priority list is empty")
        for wan_id in f.priority:
            if wan_id not in config.wans:
                errors.append(f"Failover priority references unknown WAN '{wan_id}'")
        if f.table_id in table_ids:
            errors.append(f"Failover table_id {f.table_id} conflicts with WAN '{table_ids[f.table_id]}'")
        if f.failover_ip in all_privates:
            errors.append(f"Failover IP {f.failover_ip} is also a WAN source/private — remove from WANs first")

    return errors
