#!/usr/bin/env bash
# platmon candidate check: removes everything install.sh put in place (sudo bash uninstall.sh).
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 1; }
# and the units of earlier installs: platmon-native-candidate* (before the server was platmon-server) and the
# Python unit of one before 1.5.0 (1.4.0 removed the Python server it ran)
units="platmon-server-candidate.service platmon-server-candidate-fakesmi.service platmon-native-candidate.service
platmon-native-candidate-fakesmi.service platmon-python-candidate.service"
systemctl stop $units
rm -f /etc/sudoers.d/platmon-candidate
for u in $units; do rm -f "/etc/systemd/system/$u"; done
rm -rf /usr/lib/platmon-candidate /etc/platmon-candidate
systemctl daemon-reload
systemctl reset-failed $units 2>/dev/null
echo "removed. platmon.service: $(systemctl is-active platmon.service)"
