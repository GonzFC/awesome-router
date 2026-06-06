"""UDM Local API client (read-only for v1.2).

The UDM Pro exposes a read-mostly API at:
    https://<udm-host>/proxy/network/integration/v1/

Authentication is via the X-API-KEY header. The key is stored in a
separate file (default /etc/awesome-router/udm.key) with 0600 perms so
it never appears in /etc/awesome-router.yaml or in the GUI form values.

This module is intentionally dependency-free (stdlib + urllib) so it can
be imported by the health daemon without pulling extra packages.
"""
from __future__ import annotations
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional


# ─── exceptions ───────────────────────────────────────────────────────────

class UdmError(Exception):
    """Base for all UDM client failures."""


class UdmUnauthorized(UdmError):
    """API key rejected by UDM (HTTP 401)."""


class UdmUnreachable(UdmError):
    """Network error reaching UDM (timeout, connection refused, no route)."""


# ─── value objects ────────────────────────────────────────────────────────

@dataclass
class UdmDevice:
    id: str
    name: str
    model: str
    mac: str
    ip: str
    state: str
    firmware: str
    raw: dict = field(default_factory=dict)


@dataclass
class UdmStats:
    uptime_sec: int
    last_heartbeat: str            # ISO8601
    cpu_pct: float
    mem_pct: float
    load_1: float
    uplink_tx_bps: int
    uplink_rx_bps: int
    raw: dict = field(default_factory=dict)


# ─── client ───────────────────────────────────────────────────────────────

DEFAULT_KEY_FILE = "/etc/awesome-router/udm.key"
DEFAULT_CACHE_SECONDS = 5
DEFAULT_TIMEOUT = 5.0


class UdmClient:
    """Lightweight UDM Local API client.

    Caches each (method, path) for cache_seconds to avoid hammering the UDM
    when multiple GUI/daemon callers ask the same thing in quick succession.
    Thread-safe.
    """

    def __init__(
        self,
        host: str,
        api_key: Optional[str] = None,
        key_file: str = DEFAULT_KEY_FILE,
        verify_ssl: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
    ):
        self.host = host.rstrip("/")
        if not self.host.startswith("http"):
            self.host = f"https://{self.host}"
        self.base = f"{self.host}/proxy/network/integration/v1"
        self._api_key = api_key or self._load_key(key_file)
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

        if not self._api_key:
            raise UdmError(f"No API key (looked in {key_file})")

        # SSL context
        if verify_ssl:
            self._ssl_ctx = ssl.create_default_context()
        else:
            self._ssl_ctx = ssl._create_unverified_context()

    # ── private ──────────────────────────────────────────────────────────

    @staticmethod
    def _load_key(path: str) -> Optional[str]:
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            return None

    def _get(self, path: str) -> dict:
        """GET request returning parsed JSON. Caches responses briefly."""
        key = f"GET {path}"
        now = time.time()

        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.cache_seconds:
                return cached[1]  # type: ignore[return-value]

        url = f"{self.base}{path}"
        req = urllib.request.Request(url, headers={
            "X-API-KEY": self._api_key,
            "Accept": "application/json",
            "User-Agent": "awesome-router/1.2",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                         context=self._ssl_ctx) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise UdmUnauthorized(f"UDM rejected API key (HTTP {e.code})") from e
            raise UdmError(f"UDM API error: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UdmUnreachable(f"Cannot reach UDM: {e}") from e

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise UdmError(f"UDM returned non-JSON: {body[:100]}") from e

        with self._lock:
            self._cache[key] = (now, data)
        return data

    def invalidate_cache(self):
        with self._lock:
            self._cache.clear()

    # ── public read-only endpoints ───────────────────────────────────────

    def info(self) -> dict:
        """GET /info — returns {applicationVersion: "..."}."""
        return self._get("/info")

    def sites(self) -> list[dict]:
        """List all sites. Returns the .data list."""
        return self._get("/sites").get("data", [])

    def default_site_id(self) -> Optional[str]:
        """Return the first site's id (most installs have exactly one)."""
        sites = self.sites()
        return sites[0]["id"] if sites else None

    def devices(self, site_id: str) -> list[UdmDevice]:
        """List devices in a site."""
        raw = self._get(f"/sites/{site_id}/devices").get("data", [])
        out = []
        for d in raw:
            out.append(UdmDevice(
                id=d.get("id", ""),
                name=d.get("name", ""),
                model=d.get("model", ""),
                mac=d.get("macAddress", ""),
                ip=d.get("ipAddress", ""),
                state=d.get("state", ""),
                firmware=d.get("firmwareVersion", ""),
                raw=d,
            ))
        return out

    def device(self, site_id: str, device_id: str) -> dict:
        """Full detail for one device (ports, radios, features...)."""
        return self._get(f"/sites/{site_id}/devices/{device_id}")

    def device_stats(self, site_id: str, device_id: str) -> UdmStats:
        """Latest statistics for one device.

        For the UDM gateway device, .uplink_rx_bps / .uplink_tx_bps are
        the UDM's own view of its WAN throughput — used by the health
        daemon to cross-check that traffic is actually flowing through
        the active failover WAN.
        """
        raw = self._get(f"/sites/{site_id}/devices/{device_id}/statistics/latest")
        uplink = raw.get("uplink", {}) or {}
        return UdmStats(
            uptime_sec=raw.get("uptimeSec", 0),
            last_heartbeat=raw.get("lastHeartbeatAt", ""),
            cpu_pct=raw.get("cpuUtilizationPct", 0.0),
            mem_pct=raw.get("memoryUtilizationPct", 0.0),
            load_1=raw.get("loadAverage1Min", 0.0),
            uplink_tx_bps=int(uplink.get("txRateBps", 0) or 0),
            uplink_rx_bps=int(uplink.get("rxRateBps", 0) or 0),
            raw=raw,
        )

    def find_gateway(self, site_id: str) -> Optional[UdmDevice]:
        """Return the UDM/UDR/UDM-Pro gateway device in a site.

        We identify it by model prefix 'UDM' or 'UDR' (Unifi Dream Machine /
        Router family). If multiple match, returns the first.
        """
        for d in self.devices(site_id):
            if d.model.startswith("UDM") or d.model.startswith("UDR"):
                return d
        return None
