#!/usr/bin/env bash
# platmon candidate check, once per board, by the owner: sudo bash install.sh (in the staged directory).
# Installs the candidate binary, its INI (/etc/platmon/platmon.ini with bind 127.0.0.1 and port 19797), two
# units that are not enabled or started, and a sudoers file for the login user. platmon.service, /etc/platmon
# and /usr/share/platmon are read, never changed. Refuses if anything of a previous install is there.
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 1; }
user=${SUDO_USER:?run it with sudo from the login user}
units="platmon-server-candidate.service platmon-server-candidate-fakesmi.service"
for p in /usr/lib/platmon-candidate /etc/platmon-candidate /etc/sudoers.d/platmon-candidate; do
  [ ! -e "$p" ] || { echo "$p exists: run uninstall.sh first" >&2; exit 1; }
done
for u in $units; do [ ! -e "/etc/systemd/system/$u" ] || { echo "/etc/systemd/system/$u exists" >&2; exit 1; }; done
[ "$(command -v systemctl)" = /usr/bin/systemctl ] || { echo "systemctl is not /usr/bin/systemctl" >&2; exit 1; }
sha256sum -c platmon-server.sha256
grep -qx 'bind = 0.0.0.0' /etc/platmon/platmon.ini && grep -qx 'port = 9797' /etc/platmon/platmon.ini \
  || { echo "/etc/platmon/platmon.ini: no 'bind = 0.0.0.0' or 'port = 9797' line to change" >&2; exit 1; }
sed "s/@USER@/$user/" sudoers.in > sudoers.checked
visudo -cf sudoers.checked
install -d -m 0755 /usr/lib/platmon-candidate /usr/lib/platmon-candidate/fakebin /etc/platmon-candidate
install -m 0755 platmon-server measure /usr/lib/platmon-candidate/
install -m 0755 nvidia-smi /usr/lib/platmon-candidate/fakebin/
sed -e 's/^bind = 0.0.0.0$/bind = 127.0.0.1/' -e 's/^port = 9797$/port = 19797/' /etc/platmon/platmon.ini > /etc/platmon-candidate/platmon.ini
chmod 0644 /etc/platmon-candidate/platmon.ini
for u in $units; do install -m 0644 "$u" /etc/systemd/system/; done
install -m 0440 sudoers.checked /etc/sudoers.d/platmon-candidate
rm sudoers.checked
systemctl daemon-reload
echo "installed for $user; nothing started. platmon.service: $(systemctl is-active platmon.service)"
