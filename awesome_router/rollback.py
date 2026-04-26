"""Snapshot and rollback of routing state."""
from __future__ import annotations
import json
import os
import subprocess
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

SNAPSHOT_DIR = "/var/lib/awesome-router/snapshots"
MAX_SNAPSHOTS = 50


def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=timeout
        ).decode(errors="ignore").strip()
    except Exception:
        return ""


def snapshot() -> str:
    """Capture current routing state. Returns snapshot path."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(SNAPSHOT_DIR, f"{ts}.json")

    data = {
        "timestamp": ts,
        "epoch": int(time.time()),
        "rules": _run(["ip", "rule", "show"]),
        "routes_main": _run(["ip", "route", "show", "table", "main"]),
        "routes_all_tables": {},
        "nftables": _run(["sudo", "nft", "list", "ruleset"]),
        "rt_tables": _safe_read("/etc/iproute2/rt_tables"),
        "sysctl": _safe_read("/etc/sysctl.d/99-router.conf"),
        "nftables_conf": _safe_read("/etc/nftables.conf"),
    }

    # Capture all custom routing tables
    for line in data["rt_tables"].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            tid = parts[0]
            if tid not in ("0", "253", "254", "255"):
                name = parts[1]
                data["routes_all_tables"][name] = _run(
                    ["ip", "route", "show", "table", name]
                )

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    _prune_old_snapshots()
    return path


def list_snapshots() -> list[dict]:
    """List available snapshots (newest first)."""
    if not os.path.isdir(SNAPSHOT_DIR):
        return []
    result = []
    for name in sorted(os.listdir(SNAPSHOT_DIR), reverse=True):
        if not name.endswith(".json"):
            continue
        path = os.path.join(SNAPSHOT_DIR, name)
        try:
            with open(path) as f:
                data = json.load(f)
            result.append({
                "filename": name,
                "path": path,
                "timestamp": data.get("timestamp", name),
                "epoch": data.get("epoch", 0),
            })
        except Exception:
            pass
    return result


def restore(snapshot_path: str) -> list[str]:
    """Restore routing state from a snapshot. Returns list of actions taken."""
    with open(snapshot_path) as f:
        data = json.load(f)

    actions = []

    # 1. Restore nftables (atomic)
    nft_content = data.get("nftables", "")
    if nft_content:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".nft", delete=False) as tf:
            tf.write(nft_content)
            tf_path = tf.name
        try:
            rc = subprocess.call(["sudo", "nft", "-c", "-f", tf_path],
                                  stderr=subprocess.DEVNULL, timeout=5)
            if rc == 0:
                subprocess.check_call(["sudo", "nft", "-f", tf_path], timeout=5)
                actions.append("Restored nftables ruleset")
            else:
                actions.append("WARNING: nftables validation failed, skipped restore")
        finally:
            os.unlink(tf_path)

    # 2. Restore nftables.conf
    nft_conf = data.get("nftables_conf", "")
    if nft_conf:
        _safe_write("/etc/nftables.conf", nft_conf)
        actions.append("Restored /etc/nftables.conf")

    # 3. Restore sysctl
    sysctl = data.get("sysctl", "")
    if sysctl:
        _safe_write("/etc/sysctl.d/99-router.conf", sysctl)
        subprocess.call(["sudo", "sysctl", "--system"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        actions.append("Restored sysctl settings")

    # 4. Flush and restore ip rules
    _restore_ip_rules(data.get("rules", ""))
    actions.append("Restored ip rules")

    # 5. Restore routing tables
    for table_name, routes_text in data.get("routes_all_tables", {}).items():
        _restore_routes(table_name, routes_text)
        actions.append(f"Restored routes for table {table_name}")

    return actions


def _restore_ip_rules(rules_text: str):
    """Flush non-system ip rules and restore from snapshot."""
    # Delete all custom rules (priority < 32766)
    current = _run(["ip", "rule", "show"])
    for line in current.splitlines():
        parts = line.split(":")
        if not parts:
            continue
        try:
            prio = int(parts[0].strip())
        except ValueError:
            continue
        if prio in (0, 32766, 32767):
            continue
        # Delete this rule
        rule_spec = line.split(":", 1)[1].strip()
        subprocess.call(["sudo", "ip", "rule", "del", "pref", str(prio)],
                        stderr=subprocess.DEVNULL, timeout=5)

    # Re-add from snapshot
    for line in rules_text.splitlines():
        parts = line.split(":")
        if not parts:
            continue
        try:
            prio = int(parts[0].strip())
        except ValueError:
            continue
        if prio in (0, 32766, 32767):
            continue
        rule_spec = line.split(":", 1)[1].strip()
        # Parse: "from X lookup Y"
        tokens = rule_spec.split()
        cmd = ["sudo", "ip", "rule", "add", "pref", str(prio)] + tokens
        subprocess.call(cmd, stderr=subprocess.DEVNULL, timeout=5)


def _restore_routes(table: str, routes_text: str):
    """Flush and restore routes for a table."""
    subprocess.call(["sudo", "ip", "route", "flush", "table", table],
                    stderr=subprocess.DEVNULL, timeout=5)
    for line in routes_text.splitlines():
        line = line.strip()
        if not line:
            continue
        cmd = ["sudo", "ip", "route", "add"] + line.split() + ["table", table]
        subprocess.call(cmd, stderr=subprocess.DEVNULL, timeout=5)


def _safe_read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""


def _safe_write(path: str, content: str):
    import tempfile
    dirname = os.path.dirname(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=dirname, delete=False) as tf:
        tf.write(content)
        tmp = tf.name
    os.replace(tmp, path)


def _prune_old_snapshots():
    """Keep only the newest MAX_SNAPSHOTS files."""
    if not os.path.isdir(SNAPSHOT_DIR):
        return
    files = sorted(
        [f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json")],
        reverse=True
    )
    for old in files[MAX_SNAPSHOTS:]:
        try:
            os.unlink(os.path.join(SNAPSHOT_DIR, old))
        except Exception:
            pass
