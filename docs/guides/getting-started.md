# Getting started

> **TL;DR**
> - Install the `.deb` from the signed APT repository (check the key fingerprint first) on an Ubuntu or
>   Debian-derived host with systemd; the `platmon` command (`platmon-cli`, installed with it) needs Python 3.9+.
> - Then run `platmon`, open `http://<device>:9797`, or `curl` `/api/status`.
> - Settings live in `/etc/platmon/platmon.ini`; the API has no authentication.
> - Windows 11 (x86_64) has an experimental, unsupported zip of the server: [Windows (experimental)](#windows-experimental).

platmon is a device-side monitor: one background service reads host metrics and publishes
snapshots. The `platmon` terminal client, web dashboard and HTTP clients all read the same
published data. Collection is not triggered by requests.

## Requirements and installation

The primary distribution is a `.deb` package for Ubuntu and Debian-derived Linux hosts
(including suitable Jetson and Raspberry Pi systems), built for **amd64**, **arm64** and **armhf** (32-bit ARM).
It requires **systemd**. The service is one static native binary without Python. The `platmon` command is
the separate package `platmon-cli`, which `platmon` recommends, so apt installs it by default; it needs
**Python 3.9+** on the host and uses only the Python standard library. A headless device can leave it out
with `sudo apt install --no-install-recommends platmon` and read the API or the web page instead.
A package for an architecture does not imply that every OS or kernel on it has been validated.

Install from the signed APT repository at <https://platmon.github.io/apt>. It holds
the same `.deb` files as GitHub Releases. Before running `apt update`, check that
`gpg --show-keys` prints the fingerprint
`6D42 C14A BFB9 412F 34BA 4BEF F38B 9BB5 26A3 B0C6`:

```sh
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://platmon.github.io/apt/platmon-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/platmon.gpg > /dev/null
gpg --show-keys /etc/apt/keyrings/platmon.gpg
sudo tee /etc/apt/sources.list.d/platmon.sources > /dev/null <<EOF
Types: deb
URIs: https://platmon.github.io/apt
Suites: stable
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/platmon.gpg
EOF
sudo apt update
sudo apt install platmon
systemctl status platmon --no-pager
platmon --version
platmon --once
```

Later updates arrive with `sudo apt update && sudo apt upgrade`.

`Architectures:` limits the source to the host's own architecture. Without it, a host with a foreign
architecture enabled (for example i386 on a PC) gets the notice
`Skipping acquire of configured file 'main/binary-i386/Packages' ... doesn't support architecture 'i386'`
on every `apt update`; it is harmless. To add the line to an existing source:

```sh
echo "Architectures: $(dpkg --print-architecture)" | sudo tee -a /etc/apt/sources.list.d/platmon.sources
```

Without the repository, download a fixed release and its `SHA256SUMS` from
[GitHub Releases](https://github.com/platmon/platmon/releases). In the directory
containing all of the release files, verify checksums before installing the chosen
`.deb`:

```sh
sha256sum -c SHA256SUMS
sudo apt install ./platmon_<version>-<revision>_<arch>.deb ./platmon-cli_<version>-<revision>_all.deb
systemctl status platmon --no-pager
platmon --version
platmon --once
```

Replace the angle-bracket placeholders with the actual version and revision from
the release filenames and the host's architecture (`dpkg --print-architecture`); do not
run the placeholder command literally. Releases up to 1.3.0 have one `_all.deb` instead.

To build the package from a source checkout instead, use a Linux host with Python 3.9+,
`dpkg-deb` (dpkg 1.19+) and Docker, which builds the server in a throwaway Rust container
(`native/build.sh`, both architectures):

```sh
native/build.sh
platmon_version=$(python3 scripts/packaging/package.py --version)
platmon_arch=$(dpkg --print-architecture)
platmon_dist="dist/${platmon_version}-1"
python3 scripts/packaging/package.py --output "$platmon_dist"
python3 scripts/packaging/check_package.py "$platmon_dist/platmon_${platmon_version}-1_${platmon_arch}.deb"
sudo apt install "./${platmon_dist}/platmon_${platmon_version}-1_${platmon_arch}.deb" \
  "./${platmon_dist}/platmon-cli_${platmon_version}-1_all.deb"
```

The `platmon` package installs the server in `/usr/lib/platmon/platmon-server`, the web files in
`/usr/share/platmon`, and configuration in `/etc/platmon/platmon.ini`; `platmon-cli` installs the
`platmon` command in `/usr/bin`.
APT preserves an edited config on upgrade. If an existing **source or Docker**
installation is present, follow the
[migration procedure](../reference/packaging.md#migrate-a-source-or-docker-installation)
before installing the package. Use APT to upgrade, remove or purge a packaged
installation; do not update it through the source installer.

Other Linux systems can use the release source archive and the
[source-systemd or Docker instructions](deployment.md). See
[packaging and releases](../reference/packaging.md) for signing/checksums,
installation lifecycle, migration and rollback.

To watch several devices in one place, run [platmon-hub](https://github.com/platmon/platmon-hub)
on another computer. Which Hub release reads which platmon release, and what checked each pairing, is in
[platmon-hub compatibility](../reference/hub-compatibility.md).

## Windows (experimental)

Since 1.8.0 each release also has `platmon-<version>-windows-x86_64-experimental.zip`: the server
(`platmon-server.exe`), the web page, a `platmon.ini` that listens on `127.0.0.1` only, the licenses and a
`README.txt`. It is **not supported**: it was checked in CI and on one Windows 11 laptop. It needs
Windows 11 x86_64 (Windows 10 was not checked) and the
[Visual C++ 2015-2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe), which the zip
does not include; without it the server does not start (`vcruntime140.dll` was not found). There is no
installer.

Download the zip and `SHA256SUMS` from [GitHub Releases](https://github.com/platmon/platmon/releases), compare
the hash in PowerShell, and unpack it:

```powershell
(Get-FileHash .\platmon-<version>-windows-x86_64-experimental.zip -Algorithm SHA256).Hash.ToLower()
Select-String windows-x86_64 .\SHA256SUMS
Expand-Archive .\platmon-<version>-windows-x86_64-experimental.zip -DestinationPath .
cd .\platmon-<version>-windows-x86_64
.\platmon-server.exe serve platmon.ini --web web
```

Then open `http://127.0.0.1:9797/`; Ctrl+C stops it. `.\platmon-server.exe config-check platmon.ini` prints
the settings it would use.

To run it as a Windows service, first copy the folder's contents to `C:\Program Files\platmon`. The service
runs as LocalSystem, so it must not run from a folder your user can write (Downloads, Desktop): a program
running as you could replace `platmon-server.exe` or `platmon.ini` there. Then, in a Command Prompt (cmd) run
as administrator:

```bat
sc.exe create platmon start= auto binPath= "\"C:\Program Files\platmon\platmon-server.exe\" service \"C:\Program Files\platmon\platmon.ini\" --web \"C:\Program Files\platmon\web\""
sc.exe start platmon
```

`sc.exe stop platmon` stops it and `sc.exe delete platmon` removes it; delete the folder afterwards. A service
has no console and no log: a configuration error shows only as the service stopping, so run `config-check`
after editing `platmon.ini`, then restart the service. Stop anything else on port 9797 first (a server
started by hand), or the service stops at once.

The API has no authentication. Before setting `bind = 0.0.0.0`, allow port 9797 only from trusted
addresses in Windows Defender Firewall; the firewall does not ask about a service.

Windows reads CPU usage and clocks, memory, the system volume, disk I/O, network, the first GPU's usage and
clocks, drive and GPU temperatures, storage, Wi-Fi and battery. It has no CPU temperature, power, fans,
power mode or pressure: those groups are `unavailable` (`unsupported_platform`) and pressure is left out
([native/README.md, "Windows (experimental)"](../../native/README.md#windows-experimental)).

The `platmon` command has no Windows installer. It is one standard-library file: take `frontends/cli.py`
from the release source archive (`platmon-<version>.tar.gz`) and run it with Python 3.9+, for example
`python cli.py --once` or `python cli.py 192.168.55.53`. On Windows it shows the plain screen with every
section, redrawn in place, instead of the live view. Up to 1.8.2, piping or redirecting its output fails
with `UnicodeEncodeError` on a code page without block characters (cp949, for example); run it as
`python -X utf8 cli.py --once` there.

## View a running device

On the device:

```sh
platmon                  # live terminal view
platmon --once           # one plain-text snapshot, useful in scripts
curl -s localhost:9797/api/status
curl -s localhost:9797/api/stats
```

From another machine:

```sh
platmon 192.168.55.53       # default HTTP port: 9797
platmon 192.168.55.53 2     # refresh every two seconds
platmon --view network      # start the interactive client in Network
```

Open `http://<device-ip>:9797` to use the dashboard, or
`http://<device-ip>:9797/text` to read a plain-text snapshot. The main JSON
endpoints are `/api/stats` (readings) and `/api/status` (collector state).
See [CLI and web interfaces](interfaces.md) for views, keyboard shortcuts
and the remaining endpoints.

To install **only the terminal client** on a machine watching other devices,
run `scripts/install-cli.sh` from a checkout (`/usr/local/bin/platmon`, asks
for sudo), or `scripts/install-cli.sh --user` (`~/.local/bin/platmon`,
no sudo). The client is one standard-library file, `frontends/cli.py`.
An interactive Unix terminal provides the live view; the plain `--once`
view also works with pipes and on Windows. Make sure the chosen install
directory is on `PATH`.

If `platmon` reports that its local service is not running, check
`systemctl status platmon` for a packaged/source-systemd installation,
or `docker compose -f deploy/docker/compose.yaml ps` from the repository
for a Docker installation. For remote connectivity problems, see
[Network troubleshooting](deployment.md#network-troubleshooting).

## Configuration

The distributed [`platmon.ini`](../../platmon.ini) documents all options and their
defaults: sampling cadence, per-feature switches, low-frequency intervals, HTTP
address and port, web enablement, threshold values and memory retention.

| Installation | Configuration |
| --- | --- |
| Debian package or source systemd | `/etc/platmon/platmon.ini` |
| Docker from this checkout | `platmon.ini` in the checkout root, mounted read-only |
| Windows zip (experimental) | `platmon.ini` in the unpacked folder, or `C:\Program Files\platmon\platmon.ini` for the service |
| Foreground source run | the supplied config path; defaults to `platmon.ini` |

To run the service in the foreground while developing:

```sh
native/build.sh   # once: the server for this checkout, in Docker
native/target/x86_64-unknown-linux-musl/release/platmon-server serve [platmon.ini] --web frontends/web
```

The optional square-bracket argument indicates a configuration path; without it the built-in defaults
apply. On an Arm host the build is under `aarch64-unknown-linux-musl` (or `native/build-armhf.sh`'s
`arm-unknown-linux-musleabihf` on 32-bit ARM).
Restart the running service/container to apply edits.

All monitoring features are enabled by default, but unavailable hardware still
reports an unavailable state. An explicit `enabled = no` continues to disable
a feature; omitted configuration sections use built-in defaults. The
TCP connect probe is **idle until targets are configured** and the service
is restarted. No target is selected automatically.

History and full-sample replay are **memory-only**, with 600 seconds of default
retention; replay has additional capacity limits and is cleared on restart.
Long-term storage belongs to the consuming application. See
[history](../reference/history.md) and [replay](../reference/replay.md)
for precise behavior.

## Security and support

The HTTP API has **no authentication**. The default `[http]` bind is
`0.0.0.0:9797` (IPv4); run only on trusted networks or restrict its
bind address and network exposure. IPv6 is not served.
Device-specific and optional metric availability is explained in
[CLI and web interfaces](interfaces.md#data-coverage-and-limitations)
and [HTTP API](../reference/api.md).
