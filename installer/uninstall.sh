#!/usr/bin/env bash
# Awesome Router 2 — uninstaller
# Removes services, code, config. Keeps bandwidth DB unless --purge is given.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

echo "Stopping and disabling services..."
for s in awesome-router-web awesome-router-health awesome-router-apply router-stats-sampler; do
  systemctl stop "$s" 2>/dev/null || true
  systemctl disable "$s" 2>/dev/null || true
done

echo "Removing systemd units..."
rm -f /etc/systemd/system/awesome-router-*.service
rm -f /etc/systemd/system/router-stats-sampler.service
systemctl daemon-reload

echo "Removing /opt/awesome-router..."
rm -rf /opt/awesome-router

echo "Removing /usr/local/bin/router-stats-sampler.py..."
rm -f /usr/local/bin/router-stats-sampler.py

echo "Removing /etc/awesome-router.yaml..."
rm -f /etc/awesome-router.yaml /etc/awesome-router.yaml.bak-*

if [[ $PURGE -eq 1 ]]; then
  echo "Purging bandwidth DB and snapshots..."
  rm -rf /var/lib/awesome-router /var/lib/awesome-router-monitor.db
  rm -rf /run/awesome-router
  echo "Note: /etc/netplan/01-router.yaml is left in place."
  echo "      You may want to remove it and re-apply your original network config."
else
  echo "Bandwidth DB and snapshots preserved at /var/lib/awesome-router*"
  echo "Use --purge to remove them too."
fi

echo "Done."
