#!/usr/bin/env bash
# platmon candidate check: removes everything install.sh put in place (sudo bash uninstall.sh).
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 1; }
units="platmon-native-candidate.service platmon-native-candidate-fakesmi.service platmon-python-candidate.service"
systemctl stop $units
rm -f /etc/sudoers.d/platmon-candidate
for u in $units; do rm -f "/etc/systemd/system/$u"; done
rm -rf /usr/lib/platmon-candidate /etc/platmon-candidate
systemctl daemon-reload
systemctl reset-failed $units 2>/dev/null
echo "removed. platmon.service: $(systemctl is-active platmon.service)"
