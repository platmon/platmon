#!/usr/bin/env bash
# platmon candidate check without root, where the owner cannot run install.sh: bash install_user.sh (in the
# staged directory, as the login user). The same two units in the login user's systemd, on the same INI and
# port; the binary under ~/.local/lib/platmon-candidate, the INI under ~/.config/platmon-candidate. The units are
# install.sh's with the login user in its own user namespace (PrivateUsers=yes) in place of DynamicUser, and
# ProtectHome=read-only in place of yes (the binary is in the home directory). Nothing is enabled or started;
# platmon.service, /etc/platmon and /usr/share/platmon are read, never changed. Refuses if anything of a
# previous install is there. deploy_check.py --user drives these units.
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" != 0 ] || { echo "run it as the login user, not with sudo (that is install.sh)" >&2; exit 1; }
lib=${HOME:?}/.local/lib/platmon-candidate etc=$HOME/.config/platmon-candidate dir=$HOME/.config/systemd/user
units="platmon-server-candidate.service platmon-server-candidate-fakesmi.service"
for p in "$lib" "$etc"; do [ ! -e "$p" ] || { echo "$p exists: run uninstall_user.sh first" >&2; exit 1; }; done
for u in $units; do [ ! -e "$dir/$u" ] || { echo "$dir/$u exists: run uninstall_user.sh first" >&2; exit 1; }; done
[ "$(command -v systemctl)" = /usr/bin/systemctl ] || { echo "systemctl is not /usr/bin/systemctl" >&2; exit 1; }
systemctl --user show-environment >/dev/null || { echo "no systemd user manager to install into" >&2; exit 1; }
sha256sum -c platmon-server.sha256
grep -qx 'bind = 0.0.0.0' /etc/platmon/platmon.ini && grep -qx 'port = 9797' /etc/platmon/platmon.ini \
  || { echo "/etc/platmon/platmon.ini: no 'bind = 0.0.0.0' or 'port = 9797' line to change" >&2; exit 1; }
for u in $units; do
  sed -e 's|/usr/lib/platmon-candidate|%h/.local/lib/platmon-candidate|g' \
      -e 's|/etc/platmon-candidate|%h/.config/platmon-candidate|g' \
      -e 's/^DynamicUser=yes$/PrivateUsers=yes/' -e 's/^ProtectHome=yes$/ProtectHome=read-only/' "$u" > "$u.user"
  grep -qx 'PrivateUsers=yes' "$u.user" && grep -qx 'ProtectHome=read-only' "$u.user" \
    || { echo "$u: no 'DynamicUser=yes' or 'ProtectHome=yes' line to change" >&2; exit 1; }
done
install -d -m 0755 "$lib" "$lib/fakebin" "$etc" "$dir"
install -m 0755 platmon-server "$lib/"
install -m 0755 nvidia-smi "$lib/fakebin/"
sed -e 's/^bind = 0.0.0.0$/bind = 127.0.0.1/' -e 's/^port = 9797$/port = 19797/' /etc/platmon/platmon.ini > "$etc/platmon.ini"
chmod 0644 "$etc/platmon.ini"
for u in $units; do install -m 0644 "$u.user" "$dir/$u" && rm "$u.user"; done
systemctl --user daemon-reload
echo "installed for $(id -un); nothing started. platmon.service: $(systemctl is-active platmon.service)"
echo "linger: $(loginctl show-user "$(id -un)" -p Linger --value) (the units stop with the user's last login without it)"
