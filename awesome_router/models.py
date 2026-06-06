"""Data models for Awesome Router 2 configuration."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Pair:
    """A 1:1 NAT mapping (public IP <-> private IP)."""
    public: str
    private: str


@dataclass
class Bandwidth:
    """Line capacity in Mbps."""
    down_mbps: float = 0.0
    up_mbps: float = 0.0


@dataclass
class WanConfig:
    """Configuration for a single WAN interface."""
    id: str                              # unique key, e.g. "bestel", "telmex1"
    name: str                            # display name
    interface: str                       # e.g. "enX1"
    type: str                            # "static" or "dhcp"
    gateway: str                         # IP or "auto" (discover from DHCP)
    table_id: int                        # routing table number (100, 200, …)
    nat_mode: str                        # "onetoone" or "masquerade"
    enabled: bool = True

    # For static WANs
    addresses: list[str] = field(default_factory=list)   # CIDRs on the interface
    router_ip: Optional[str] = None                      # our own IP (not NATed)

    # For onetoone NAT
    pairs: list[Pair] = field(default_factory=list)

    # For masquerade NAT
    sources: list[str] = field(default_factory=list)     # private IPs that exit here

    # DHCP options
    metric: int = 100                    # route metric for DHCP default

    # Failover: for static WANs, which public IP to SNAT failover traffic through.
    # Empty/None = use masquerade (picks interface primary IP).
    failover_snat_ip: Optional[str] = None

    bandwidth: Bandwidth = field(default_factory=Bandwidth)

    @property
    def private_ips(self) -> list[str]:
        """All private IPs routed through this WAN."""
        if self.nat_mode == "onetoone":
            return [p.private for p in self.pairs]
        return list(self.sources)


@dataclass
class LanConfig:
    """LAN interface facing the UDM Pro."""
    interface: str        # e.g. "enX0"
    address: str          # CIDR, e.g. "10.188.147.113/28"

    @property
    def ip(self) -> str:
        return self.address.split("/")[0]


@dataclass
class HealthConfig:
    """Health check settings for WAN failover monitoring."""
    targets: list[str] = field(default_factory=lambda: ["8.8.8.8", "1.1.1.1", "9.9.9.9"])
    interval_seconds: int = 10
    timeout_seconds: int = 3
    fail_threshold: int = 3        # consecutive failures to mark down
    recover_threshold: int = 2     # consecutive successes to mark up


@dataclass
class FailoverConfig:
    """Automatic WAN failover for a shared private IP.

    When enabled, traffic from `failover_ip` is routed through the highest-
    priority healthy WAN via a dedicated routing table (`table_id`).
    """
    enabled: bool = False
    failover_ip: str = ""               # private IP, e.g. "10.188.147.120"
    table_id: int = 1000
    priority: list[str] = field(default_factory=list)   # ordered WAN ids
    health: HealthConfig = field(default_factory=HealthConfig)


@dataclass
class UdmConfig:
    """Optional integration with a Unifi UDM/UDR via Local API.

    When enabled, the AR queries the UDM Local API for an independent view
    of WAN/uplink health and traffic. The health daemon cross-checks AR's
    decisions against UDM reality and can take corrective action when they
    disagree (flush conntrack, refresh ARP, re-apply).

    The api_key is stored in a separate file (key_file) with 0600 perms —
    never inlined into /etc/awesome-router.yaml.
    """
    enabled: bool = False
    host: str = ""                              # e.g. "192.168.14.251"
    key_file: str = "/etc/awesome-router/udm.key"
    verify_ssl: bool = False
    site_id: str = "auto"                       # "auto" = use first/default site
    gateway_device_id: str = "auto"             # "auto" = find UDM/UDR by model
    poll_interval_seconds: int = 30             # how often health daemon queries
    cache_seconds: int = 5                      # in-process response cache TTL

    # Verification thresholds — when AR thinks WAN OK but UDM disagrees:
    disagreement_threshold: int = 3             # consecutive disagreements before acting


@dataclass
class RouterConfig:
    """Complete router configuration."""
    version: int
    lan: LanConfig
    vm_default_wan: str                    # WAN id for VM's own traffic
    wans: dict[str, WanConfig]             # keyed by WAN id
    failover: FailoverConfig = field(default_factory=FailoverConfig)
    udm: UdmConfig = field(default_factory=UdmConfig)

    def get_wan(self, wan_id: str) -> Optional[WanConfig]:
        return self.wans.get(wan_id)

    def wan_list(self) -> list[WanConfig]:
        return list(self.wans.values())

    @property
    def default_wan(self) -> Optional[WanConfig]:
        return self.wans.get(self.vm_default_wan)
