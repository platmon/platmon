#!/usr/bin/env bash
# Install or update platmon as a systemd service from this checkout, enable it at boot and start it,
# and install the `platmon` command (/usr/local/bin) to look at it. The server is this checkout's native
# build for this machine: native/build.sh (amd64, arm64) or native/build-armhf.sh (32-bit ARM) first.
#   deploy/systemd/install.sh        (run it again after a git pull and a new build to update)
# Code:   /opt/platmon                       the server and its web page; staged before replacing, previous
#                                            install retained
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
if command -v rpm >/dev/null && rpm -q --quiet platmon 2>/dev/null; then
    echo "platmon is managed by rpm; update it with dnf, not this source installer" >&2
    exit 1
fi

# both ways serve port 9797; use one at a time
if command -v docker >/dev/null && [ -n "$(docker ps -q --filter label=com.docker.compose.project=platmon 2>/dev/null)" ]; then
    echo "platmon is running in Docker; stop it first: deploy/docker/stop.sh" >&2
    exit 1
fi

# the build for this machine (the Debian package's targets, scripts/packaging/package.py ARCHES)
case "$(uname -m)" in
    x86_64) target=x86_64-unknown-linux-musl build=native/build.sh ;;
    aarch64 | arm64) target=aarch64-unknown-linux-musl build=native/build.sh ;;
    armv6l | armv7l | armv8l) target=arm-unknown-linux-musleabihf build=native/build-armhf.sh ;;
    *) echo "no native build for $(uname -m)" >&2; exit 1 ;;
esac
binary=$repo/native/target/$target/release/platmon-server
if [ ! -x "$binary" ]; then
    echo "no server built for this machine: run $build first (it needs Docker), then this again" >&2
    exit 1
fi
version=$(python3 "$repo/scripts/packaging/package.py" --version)

# check the config with this checkout's server before replacing anything
candidate_config=$config
[ -f "$config" ] || candidate_config=$repo/platmon.ini
checked=$("$binary" config-check "$candidate_config") || true  # exit 1 with {"error": ...} when refused
case $checked in
    '{"error":'*) echo "invalid config: $(printf '%s' "$checked" | sed 's/^{"error":"//; s/"}$//')" >&2; exit 1 ;;
esac

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
mkdir -p "$work/code/frontends"
install -m 755 "$binary" "$work/code/platmon-server"
ln -s platmon-server "$work/code/platmon-native"  # its name up to 1.6.0, for scripts that call it there
cp -r "$repo/frontends/web" "$work/code/frontends/"
chmod -R a+rX "$work/code"
# the Debian package's unit, with this install's paths and version
sed -e 's|/usr/lib/platmon/|/opt/platmon/|g' -e 's|/usr/share/platmon/|/opt/platmon/|g' -e "s|@VERSION@|$version|g" \
    "$repo/packaging/debian/platmon.service" > "$work/unit.new"
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
