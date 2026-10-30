# platmon-cli: scripts/packaging/package_rpm.py fills in the @...@ fields and stages the files (the same as
# the .deb's) in @STAGE@; nothing is compiled here.
Name: platmon-cli
Version: @VERSION@
Release: @RELEASE@
Summary: Terminal client for the platmon platform monitor
License: Apache-2.0
URL: https://github.com/platmon/platmon
BuildArch: noarch
Requires: python3 >= 3.9
# One Python file on the standard library: its interpreter is the only dependency, given above.
AutoReqProv: no
%global __os_install_post %{nil}

%description
The platmon command: a live view of a platmon service's HTTP API, this
device's by default, in the terminal. One Python file that uses the host's
Python standard library only.

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
