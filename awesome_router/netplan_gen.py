"""Generate netplan YAML from RouterConfig."""
from __future__ import annotations
import yaml
from .models import RouterConfig


def generate(config: RouterConfig) -> str:
    """Build netplan YAML for all interfaces."""
    ethernets = {}

    # LAN interface
    ethernets[config.lan.interface] = {
        "addresses": [config.lan.address],
    }

    # WAN interfaces
    for w in config.wan_list():
        if not w.enabled:
            continue

        if w.type == "static":
            iface_cfg: dict = {
                "addresses": list(w.addresses),
            }
            if w.gateway and w.gateway != "auto":
                iface_cfg["routes"] = [{
                    "to": "0.0.0.0/0",
                    "via": w.gateway,
                    "table": w.table_id,
                    "metric": 100,
                }]
            ethernets[w.interface] = iface_cfg

        elif w.type == "dhcp":
            ethernets[w.interface] = {
                "dhcp4": True,
                "dhcp4-overrides": {
                    "route-metric": w.metric,
                },
            }

    data = {
        "network": {
            "version": 2,
            "renderer": "networkd",
            "ethernets": ethernets,
        }
    }

    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
