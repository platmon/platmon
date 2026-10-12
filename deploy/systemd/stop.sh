#!/usr/bin/env bash
# Stop the platmon service for now. It stays enabled and starts again at boot;
# deploy/systemd/uninstall.sh removes it for good.
#   deploy/systemd/stop.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"

if [ ! -f /etc/systemd/system/platmon.service ]; then
    echo "platmon is not installed"
    exit 0
fi
systemctl stop platmon
echo "platmon stopped (still enabled: it starts again at boot; deploy/systemd/uninstall.sh removes it)"
