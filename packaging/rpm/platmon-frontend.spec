# platmon-frontend for one architecture: scripts/packaging/package_rpm.py fills in the @...@ fields and stages the files (the same as
# the .deb's) in @STAGE@; nothing is compiled here.
Name: platmon-frontend
Version: @VERSION@
Release: @RELEASE@
Summary: Web page and terminal client for the platmon platform monitor
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
from /usr/share/platmon, and the platmon command, a live view of the HTTP
API in the terminal, this device's by default. The command is one static
native binary, without Python.

%install
cp -a @STAGE@/. %{buildroot}/

%post
if [ -e /usr/local/bin/platmon ] || [ -e /usr/local/sbin/platmon ]; then
    echo "a local platmon command may shadow /usr/bin/platmon; check command -v platmon" >&2
fi

%files
/usr/bin/platmon
/usr/share/man/man1/platmon.1.gz
/usr/share/platmon
%doc /usr/share/doc/platmon-frontend
%license /usr/share/licenses/platmon-frontend

%changelog
* @DATE@ platmon maintainers <jwsung91@users.noreply.github.com> - @VERSION@-@RELEASE@
- platmon-frontend @VERSION@; see /usr/share/doc/platmon-frontend/CHANGELOG.md
