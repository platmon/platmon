#!/usr/bin/env bash
# platmon candidate check without root: removes everything install_user.sh put in place (bash uninstall_user.sh,
# as the login user). Linger, if it was turned on for the run, is left as it is.
set -uo pipefail
[ "$(id -u)" != 0 ] || { echo "run it as the login user, not with sudo (that is uninstall.sh)" >&2; exit 1; }
units="platmon-server-candidate.service platmon-server-candidate-fakesmi.service"
systemctl --user stop $units
for u in $units; do rm -f "${HOME:?}/.config/systemd/user/$u"; done
rm -rf "${HOME:?}/.local/lib/platmon-candidate" "${HOME:?}/.config/platmon-candidate"
systemctl --user daemon-reload
systemctl --user reset-failed $units 2>/dev/null
echo "removed. platmon.service: $(systemctl is-active platmon.service)"
