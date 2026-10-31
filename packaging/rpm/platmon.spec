# platmon for one architecture: scripts/packaging/package_rpm.py fills in the @...@ fields and stages the
# files (the same as the .deb's) in @STAGE@; nothing is compiled here.
Name: platmon
Version: @VERSION@
Release: @RELEASE@
Summary: Lightweight read-only Linux platform monitor
License: Apache-2.0 AND (MIT OR Apache-2.0) AND MIT
URL: https://github.com/platmon/platmon
Requires: systemd
Recommends: platmon-cli
# The server is one static binary: no dependencies to find, no debug package, nothing to strip.
AutoReqProv: no
%global debug_package %{nil}
%global __os_install_post %{nil}

%description
Collects platform metrics with a static native server and exposes an HTTP
API and web viewer. The terminal client, the platmon command, is in
platmon-cli.

%install
cp -a @STAGE@/. %{buildroot}/

%pre
# Leave administrator-owned units intact; migrate the manual install first.
if [ "$1" -eq 1 ]; then
    if [ -e /etc/systemd/system/platmon.service ] || [ -L /etc/systemd/system/platmon.service ]; then
        echo "local platmon.service exists; follow docs/reference/packaging.md migration first" >&2
        exit 1
    fi
fi
if command -v docker >/dev/null 2>&1 &&
   [ -n "$(docker ps -q --filter label=com.docker.compose.project=platmon 2>/dev/null || true)" ]; then
    echo "platmon is running in Docker; stop that instance before installing" >&2
    exit 1
fi

%post
# Enabled on the first install, as the .deb does; an upgrade keeps the administrator's choice.
if [ "$1" -eq 1 ]; then
    systemctl --no-reload enable platmon.service >/dev/null 2>&1 || :
fi

%preun
if [ "$1" -eq 0 ]; then
    systemctl --no-reload disable --now platmon.service >/dev/null 2>&1 || :
fi

%postun
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || :
fi

%posttrans
# After the whole transaction (an upgrade's old files are gone): validate the preserved config, then start
# or restart the service and check that it answers. rpm cannot roll back here; a failure is reported and
# the running server, if any, is left alone.
status=0
why=$(/usr/lib/platmon/platmon-server config-check /etc/platmon/platmon.ini 2>&1 >/dev/null) || status=$?
if [ "$status" -eq 1 ]; then
    echo "invalid platmon config: ${why#*error: }; platmon.service was not restarted" >&2
    exit 1
elif [ "$status" -ne 0 ]; then
    echo "platmon config check failed (exit $status): $why; platmon.service was not restarted" >&2
    exit 1
fi
[ -d /run/systemd/system ] || exit 0
systemctl daemon-reload
if ! systemctl -q is-enabled platmon.service && ! systemctl -q is-active platmon.service; then
    echo "platmon.service is not enabled (disabled or masked); not started, health check skipped"
    exit 0
fi
systemctl restart platmon.service || :
# The service must stay running and answer /api/stats on the configured address (`platmon-server health`:
# a current snapshot, as /api/status says ready) within 20 s.
state() { systemctl show platmon.service -p "$1" --value; }
restarts=$(state NRestarts) why="no answer" healthy=false
deadline=$(($(date +%s) + 20))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ "$(state ActiveState)" != active ] || [ "$(state NRestarts)" != "$restarts" ]; then
        why="the service stopped ($(state ActiveState)/$(state SubState), result $(state Result))"
        break
    fi
    if why=$(/usr/lib/platmon/platmon-server health /etc/platmon/platmon.ini 2>&1 >/dev/null); then
        healthy=true
        break
    fi
    sleep 0.5
done
if [ "$healthy" != true ]; then
    echo "platmon.service failed its health check: ${why#platmon-server health: }; see journalctl -u platmon." \
        "To go back, install the previous package: sudo dnf downgrade ./platmon-VERSION.ARCH.rpm" >&2
    exit 1
fi

%files
%dir /usr/lib/platmon
/usr/lib/platmon/platmon-server
/usr/lib/platmon/platmon-native
/usr/lib/systemd/system/platmon.service
%dir /etc/platmon
%config(noreplace) /etc/platmon/platmon.ini
/usr/share/platmon
%doc /usr/share/doc/platmon
%license /usr/share/licenses/platmon

%changelog
* @DATE@ platmon maintainers <jwsung91@users.noreply.github.com> - @VERSION@-@RELEASE@
- platmon @VERSION@; see /usr/share/doc/platmon/CHANGELOG.md
