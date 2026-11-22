# platmon-cli for one architecture: scripts/packaging/package_rpm.py fills in the @...@ fields and stages the files (the same as
# the .deb's) in @STAGE@; nothing is compiled here.
Name: platmon-cli
Version: @VERSION@
Release: @RELEASE@
Summary: Terminal client for the platmon platform monitor
License: Apache-2.0 AND (MIT OR Apache-2.0) AND MIT
URL: https://github.com/platmon/platmon
# One static binary, as the server: no dependencies to find, no debug package, nothing to strip.
AutoReqProv: no
%global debug_package %{nil}
%global __os_install_post %{nil}

%description
The platmon command: a live view of a platmon service's HTTP API, this
device's by default, in the terminal. One static native binary, without
Python.

%install
cp -a @STAGE@/. %{buildroot}/

%post
if [ -e /usr/local/bin/platmon ] || [ -e /usr/local/sbin/platmon ]; then
    echo "a local platmon command may shadow /usr/bin/platmon; check command -v platmon" >&2
fi

%files
/usr/bin/platmon
/usr/share/man/man1/platmon.1.gz
%doc /usr/share/doc/platmon-cli
%license /usr/share/licenses/platmon-cli

%changelog
* @DATE@ platmon maintainers <jwsung91@users.noreply.github.com> - @VERSION@-@RELEASE@
- platmon-cli @VERSION@; see /usr/share/doc/platmon-cli/CHANGELOG.md
