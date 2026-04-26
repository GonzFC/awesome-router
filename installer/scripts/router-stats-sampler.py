#!/usr/bin/env python3
"""Awesome Router 2 — WAN bandwidth sampler.

Reads WAN interfaces from /etc/awesome-router.yaml (falls back to the old
monitor config if the new one doesn't exist yet). Samples psutil byte
counters into SQLite every 15 seconds. Purges data older than 30 days.
"""
import os
import sys
import time
import sqlite3
import psutil

DB = "/var/lib/awesome-router-monitor.db"
NEW_CONF = "/etc/awesome-router.yaml"
OLD_CONF = "/etc/awesome-router-monitor.conf"
RETENTION_DAYS = 30
DEFAULT_INTERVAL = 15


def load_interfaces() -> list[str]:
    """Get WAN interface names from config."""
    # Try new unified config first
    if os.path.exists(NEW_CONF):
        try:
            import yaml
            with open(NEW_CONF) as f:
                data = yaml.safe_load(f)
            return [w["interface"] for w in data.get("wans", {}).values()
                    if w.get("enabled", True)]
        except Exception:
            pass

    # Fall back to old config
    try:
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(OLD_CONF)
        bestel = cfg.get("monitor", "bestel_if", fallback="enX1")
        telmex = cfg.get("monitor", "telmex_if", fallback="enX2")
        return [bestel, telmex]
    except Exception:
        return ["enX1", "enX2"]


def load_interval() -> int:
    if os.path.exists(OLD_CONF):
        try:
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(OLD_CONF)
            return cfg.getint("monitor", "sample_interval", fallback=DEFAULT_INTERVAL)
        except Exception:
            pass
    return DEFAULT_INTERVAL


os.makedirs(os.path.dirname(DB), exist_ok=True)
con = sqlite3.connect(DB)
con.execute("""CREATE TABLE IF NOT EXISTS samples(
  ts INTEGER NOT NULL,
  iface TEXT NOT NULL,
  rx  INTEGER NOT NULL,
  tx  INTEGER NOT NULL
)""")
con.execute("CREATE INDEX IF NOT EXISTS idx_ts_iface ON samples(ts, iface)")
con.commit()

INTERVAL = load_interval()


def snap(interfaces: list[str]):
    per = psutil.net_io_counters(pernic=True)
    now = int(time.time())
    rows = []
    for iface in interfaces:
        s = per.get(iface)
        if s:
            rows.append((now, iface, int(s.bytes_recv), int(s.bytes_sent)))
    if rows:
        con.executemany("INSERT INTO samples(ts,iface,rx,tx) VALUES (?,?,?,?)", rows)
        cutoff = now - RETENTION_DAYS * 24 * 3600
        con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        con.commit()


def main():
    interfaces = load_interfaces()
    print(f"Sampling interfaces: {interfaces} every {INTERVAL}s", flush=True)
    reload_counter = 0
    while True:
        try:
            snap(interfaces)
            time.sleep(INTERVAL)
            # Reload interface list every ~5 minutes in case config changed
            reload_counter += 1
            if reload_counter >= (300 // INTERVAL):
                interfaces = load_interfaces()
                reload_counter = 0
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
