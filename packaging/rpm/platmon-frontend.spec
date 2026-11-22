# platmon-frontend for one architecture: scripts/packaging/package_rpm.py fills in the @...@ fields and stages the files (the same as
# the .deb's) in @STAGE@; nothing is compiled here.
Name: platmon-frontend
Version: @VERSION@
Release: @RELEASE@
Summary: Web page, terminal client and tray icon for the platmon platform monitor
License: Apache-2.0 AND (MIT OR Apache-2.0) AND MIT
URL: https://github.com/platmon/platmon
# platmon-cli up to this release: dnf replaces it with this package. The web page came from platmon before
# this release; rpm moves those files when both are upgraded in one transaction.
Obsoletes: platmon-cli < @VERSION@
Provides: platmon-cli = %{version}-%{release}
# One static binary, as the server: no dependencies to find, no debug package, nothing to strip.
AutoReqProv: no
%global debug_package %{nil}
%global __os_install_post %{nil}

%description
The frontends of a platmon service: the web page the platmon server sends
from /usr/share/platmon, the platmon command, a live view of the HTTP API
in the terminal, this device's by default, and platmon-tray, its state as
a tray icon (StatusNotifierItem; started at login only for the users who
ask for it). They are static native binaries, without Python.

%install
cp -a @STAGE@/. %{buildroot}/

%post
if [ -e /usr/local/bin/platmon ] || [ -e /usr/local/sbin/platmon ]; then
    echo "a local platmon command may shadow /usr/bin/platmon; check command -v platmon" >&2
fi

%files
/usr/bin/platmon
/usr/bin/platmon-tray
/usr/share/man/man1/platmon.1.gz
/usr/share/man/man1/platmon-tray.1.gz
/usr/share/platmon
%doc /usr/share/doc/platmon-frontend
%license /usr/share/licenses/platmon-frontend

%changelog
* @DATE@ platmon maintainers <jwsung91@users.noreply.github.com> - @VERSION@-@RELEASE@
- platmon-frontend @VERSION@; see /usr/share/doc/platmon-frontend/CHANGELOG.md
