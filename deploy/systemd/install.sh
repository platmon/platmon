#!/usr/bin/env bash
# Install or update platmon as a systemd service from this checkout, enable it at boot and start it,
# and install the `platmon` command (/usr/local/bin) to look at it.
#   deploy/systemd/install.sh        (run it again after a git pull to update)
# Code:   /opt/platmon                       staged before replacing; previous install retained
# Config: /etc/platmon/platmon.ini           created once from platmon.ini, never overwritten
# Unit:   /etc/systemd/system/platmon.service
set -euo pipefail

[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
prefix=/opt/platmon
config=/etc/platmon/platmon.ini
unit=/etc/systemd/system/platmon.service

if command -v dpkg-query >/dev/null && [ "$(dpkg-query -W -f='${db:Status-Status}' platmon 2>/dev/null)" = installed ]; then
    echo "platmon is managed by dpkg; update it with apt, not this source installer" >&2
    exit 1
fi

# both ways serve port 9797; use one at a time
if command -v docker >/dev/null && [ -n "$(docker ps -q --filter label=com.docker.compose.project=platmon 2>/dev/null)" ]; then
    echo "platmon is running in Docker; stop it first: deploy/docker/stop.sh" >&2
    exit 1
fi

# check the config with this checkout's code before replacing anything
candidate_config=$config
[ -f "$config" ] || candidate_config=$repo/platmon.ini
python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from platmon import load_config
try:
    load_config(sys.argv[2])
except ValueError as e:
    sys.exit(f"invalid config: {e}")
' "$repo" "$candidate_config"

# Prepare the entire candidate before stopping the service or moving its installed files.
work=$(mktemp -d "$(dirname "$prefix")/.platmon-install.XXXXXX")
switched=false
unit_changed=false
enable_attempted=false
was_active=false
was_enabled=false
systemctl is-active --quiet platmon && was_active=true
systemctl is-enabled --quiet platmon && was_enabled=true

rollback() {
    status=$?
    trap - EXIT
    if [ "$status" -ne 0 ]; then
        set +e
        if $switched; then
            systemctl stop platmon
            rm -rf "$prefix"
            [ ! -d "$work/previous" ] || mv "$work/previous" "$prefix"
        fi
        if $unit_changed; then
            if [ -f "$work/unit.previous" ]; then
                install -m 644 "$work/unit.previous" "$unit"
            else
                rm -f "$unit"
            fi
            systemctl daemon-reload
        fi
        if $enable_attempted && ! $was_enabled; then
            systemctl disable --quiet platmon
        fi
        if $switched && $was_active; then
            systemctl start platmon || echo "previous files restored, but the service needs attention" >&2
        fi
        echo "installation failed; previous code and unit restored where present" >&2
        # Keep recovery material even if a restore command itself failed.
        echo "recovery directory: $work" >&2
    fi
    exit "$status"
}
trap rollback EXIT
mkdir "$work/code"
cp -r "$repo/platmon.py" "$repo/collector" "$repo/runtime" "$repo/frontends" "$work/code/"
find "$work/code" -name __pycache__ -prune -exec rm -rf {} +
chmod -R a+rX "$work/code"
sed 's|/usr/share/platmon/|/opt/platmon/|g' "$repo/deploy/systemd/platmon.service" > "$work/unit.new"
[ ! -f "$unit" ] || install -m 644 "$unit" "$work/unit.previous"

if [ -f "$config" ]; then
    echo "keeping existing config $config"
else
    install -D -m 644 "$repo/platmon.ini" "$config"
    echo "created config $config"
fi

echo "installing code to $prefix"
if [ -f "$unit" ]; then
    systemctl stop platmon
fi
[ ! -d "$prefix" ] || mv "$prefix" "$work/previous"
switched=true
mv "$work/code" "$prefix"

unit_changed=true
install -m 644 "$work/unit.new" "$unit"
systemctl daemon-reload
enable_attempted=true
systemctl enable --quiet platmon
echo "enabled at boot"
# A failed readiness check runs the EXIT trap and restores the previous install.
"$here/start.sh"
trap - EXIT
if [ -d "$work/previous" ] || [ -f "$work/unit.previous" ]; then
    echo "previous installation kept in $work (see docs/reference/packaging.md for rollback)"
else
    rm -rf "$work"
fi
# the `platmon` command to look at it; a failure here must not stop the service install
"$repo/scripts/install-cli.sh" || echo "the platmon command was not installed (see above)" >&2
