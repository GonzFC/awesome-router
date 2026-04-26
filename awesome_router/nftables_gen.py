"""Generate nftables ruleset from RouterConfig."""
from __future__ import annotations
from .models import RouterConfig, WanConfig

WEB_PORT = 5000


def generate(config: RouterConfig) -> str:
    """Build a complete nftables.conf from the router config."""
    lines = ["flush ruleset", ""]

    # ── NAT table ──
    lines.append("table ip nat {")

    # Map for onetoone WANs (DNAT inbound)
    for w in config.wan_list():
        if w.nat_mode == "onetoone" and w.pairs and w.enabled:
            lines.append(f"    map {w.id}_pub2lan {{")
            lines.append("        type ipv4_addr : ipv4_addr")
            elems = ", ".join(f"{p.public} : {p.private}" for p in w.pairs)
            lines.append(f"        elements = {{ {elems} }}")
            lines.append("    }")
            lines.append("")

    # Prerouting chain (DNAT)
    lines.append("    chain prerouting {")
    lines.append("        type nat hook prerouting priority -100; policy accept;")
    for w in config.wan_list():
        if w.nat_mode == "onetoone" and w.pairs and w.enabled:
            pub_ips = ", ".join(p.public for p in w.pairs)
            lines.append(
                f'        iifname "{w.interface}" ip daddr {{ {pub_ips} }}'
                f" dnat to ip daddr map @{w.id}_pub2lan"
            )
    lines.append("    }")
    lines.append("")

    # Postrouting chain (SNAT / masquerade)
    lines.append("    chain postrouting {")
    lines.append("        type nat hook postrouting priority 100; policy accept;")
    for w in config.wan_list():
        if not w.enabled:
            continue
        if w.nat_mode == "onetoone":
            for p in w.pairs:
                lines.append(
                    f'        oifname "{w.interface}" ip saddr {p.private} snat to {p.public}'
                )
        elif w.nat_mode == "masquerade":
            for src in w.sources:
                lines.append(
                    f'        oifname "{w.interface}" ip saddr {src} masquerade'
                )

    # Failover IP NAT rules — one per WAN in priority list.
    # Routing decides which interface is active; only that rule fires.
    f = config.failover
    if f.enabled and f.failover_ip:
        for wan_id in f.priority:
            w = config.get_wan(wan_id)
            if not w or not w.enabled:
                continue
            if w.failover_snat_ip:
                lines.append(
                    f'        oifname "{w.interface}" ip saddr {f.failover_ip}'
                    f" snat to {w.failover_snat_ip}"
                )
            else:
                lines.append(
                    f'        oifname "{w.interface}" ip saddr {f.failover_ip} masquerade'
                )

    lines.append("    }")
    lines.append("}")
    lines.append("")

    # ── Filter table ──
    lines.append("table ip filter {")

    # Input chain
    lines.append("    chain input {")
    lines.append("        type filter hook input priority 0; policy drop;")
    lines.append("        ct state established,related accept")
    lines.append('        iif "lo" accept')
    lines.append(
        f'        iifname "{config.lan.interface}" tcp dport {{ 22, {WEB_PORT} }} accept'
    )
    lines.append("    }")
    lines.append("")

    # Forward chain
    lines.append("    chain forward {")
    lines.append("        type filter hook forward priority 0; policy drop;")
    lines.append("        ct state established,related accept")

    for w in config.wan_list():
        if not w.enabled:
            continue
        privates = w.private_ips
        if not privates:
            continue

        ip_set = ", ".join(privates)

        if w.nat_mode == "onetoone":
            # LAN -> WAN for 1:1 sources
            lines.append(
                f'        iifname "{config.lan.interface}" oifname "{w.interface}"'
                f" ip saddr {{ {ip_set} }} accept"
            )
            # WAN -> LAN for DNAT'd traffic
            lines.append(
                f'        iifname "{w.interface}"'
                f" ip daddr {{ {ip_set} }} accept"
            )
        elif w.nat_mode == "masquerade":
            # LAN -> WAN for masquerade sources
            lines.append(
                f'        iifname "{config.lan.interface}" oifname "{w.interface}"'
                f" ip saddr {{ {ip_set} }} accept"
            )

    # Failover IP forward rules: allow from LAN to each WAN in priority list
    f = config.failover
    if f.enabled and f.failover_ip:
        for wan_id in f.priority:
            w = config.get_wan(wan_id)
            if not w or not w.enabled:
                continue
            lines.append(
                f'        iifname "{config.lan.interface}" oifname "{w.interface}"'
                f" ip saddr {f.failover_ip} accept"
            )

    lines.append("    }")
    lines.append("}")
    lines.append("")

    return "\n".join(lines)
