# platmon

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="frontends/web/assets/brand/platmon-logo-dark.svg">
  <img src="frontends/web/assets/brand/platmon-logo-light.svg" alt="platmon" width="440">
</picture>

Lightweight platform monitor for Jetson, Raspberry Pi, PCs and other Linux hosts.
One collector core runs on the device as a background service with an HTTP API; a terminal client and a web page show what it collects.

- No dependencies: Python 3.9+ standard library only
- Reads `/proc` and `/sys` directly (CPU, GPU, NPU, memory, disk, temperatures, power rails, fans); the
  GPU from the board where it has its own source, otherwise from the DRM class (Intel `i915`, `amdgpu`),
  and the NPU from the accel class (Intel `intel_vpu`); NVIDIA GPUs, including WSL2, use the
  driver's optional `nvidia-smi` command when no supported DRM reader is available
- Shows the OS release, kernel, architecture and hostname (plus the L4T release on Jetson)
- Detects the platform (Jetson Orin / Xavier, Raspberry Pi, PC, Linux) and hides what the board lacks

## Install on a device

Ubuntu/Debian-derived systems use a `.deb` package with a systemd service and the `platmon`
command. Download a release's files from the GitHub Releases page (verify `SHA256SUMS`), or build
the package from this checkout:

```sh
platmon_version=$(python3 scripts/packaging/package.py --version)
platmon_dist="dist/${platmon_version}-1"
python3 scripts/packaging/package.py --output "$platmon_dist"
python3 scripts/packaging/check_package.py "$platmon_dist/platmon_${platmon_version}-1_all.deb"
sudo apt install "./${platmon_dist}/platmon_${platmon_version}-1_all.deb"
platmon --version
platmon --once
```

Requires the host's Python 3.9+ and systemd. The package preserves edited
`/etc/platmon/platmon.ini`; use APT to update, remove or purge it. Code is installed in
`/usr/share/platmon`, and the client in `/usr/bin/platmon`. Existing source/Docker installs
need the explicit migration procedure before installation. See [packaging and releases](docs/reference/packaging.md)
for versions, checksums, migration, rollback and validation. Other Linux systems can use
the versioned source archive and source scripts below.

## Usage

platmon runs as a background service on the device (see "Run at boot": systemd or Docker), and you look at
it with the `platmon` command, a browser, or `curl`:

```sh
platmon                          # this device: live view, q or Ctrl+C to quit
platmon 192.168.55.53            # another device (host[:port], default port 9797)
platmon 192.168.55.53 2          # update every 2 s
platmon --once                   # one snapshot without clearing the screen (scripts, ssh, logs)
platmon --view network           # start on a view other than Overview
```

On an interactive terminal (Linux, macOS) the live view fits the terminal and colors readings with the
web page's warning and critical levels (the service's `[thresholds]`), with a Needs attention line,
sparklines of the last 24 updates, and a banner over the last data while the service cannot be reached.
It shows one view at a time, the same seven the web page has as tabs: Overview, Resources, Network,
Storage, Temperature, Power & battery and System. `1`-`7`, Tab and the left and right arrows switch them, the up and down
arrows, PgUp/PgDn and Home/End scroll a view longer than the terminal, `q` quits, and the tab bar in the
header names the views (shorter labels on a narrow terminal). When a view does not fit, its last line
says how much is out of sight above and below. Overview is the whole screen as before:
the web Overview's readings, the cores, and as much of each detail kind as the terminal has room for.
The detail views show their own kind in full instead, so a narrow terminal reaches every reading —
disk I/O, temperatures, power rails, fans, pressure, Wi-Fi, TCP connect times and swap among them — by
switching views rather than being enlarged. Switching asks the service for nothing: every view is drawn
from the data already in hand. `NO_COLOR=1` turns colors off; `PLATMON_ASCII=1` draws with ASCII only
(for terminals that draw block and line characters two columns wide). `--once`, pipes and Windows keep
the plain screen with every section, the same as `/text`; `--view` is for the live view only.

If the service is not running, `platmon` says so and how to start it. On the device the command comes
with the service: the Debian package puts it in `/usr/bin`, `deploy/systemd/install.sh` in `/usr/local/bin`, `deploy/docker/start.sh` in
`~/.local/bin` (no sudo), and both update it when run again. On a PC that only watches devices, install
just the command with `scripts/install-cli.sh` (or `--user`, no sudo); it is one standard-library Python
file (`frontends/cli.py`).

In a browser: `http://<host>:9797`. Without Python, from any machine with curl (Windows and macOS too):
`watch -n1 curl -s <host>:9797/text` shows the same screen as `platmon`. Raw data: `http://<host>:9797/api/stats`, collector state:
`http://<host>:9797/api/status` (fields and freshness rules: [docs/reference/api.md](docs/reference/api.md); per-interface
network counters: [docs/reference/network.md](docs/reference/network.md); disk I/O counters: [docs/reference/disk-io.md](docs/reference/disk-io.md); storage capacity: [docs/reference/storage.md](docs/reference/storage.md); Wi-Fi signal:
[docs/reference/wifi.md](docs/reference/wifi.md); battery and external power: [docs/reference/battery.md](docs/reference/battery.md); TCP connect probe to configured targets: [docs/reference/probe.md](docs/reference/probe.md); recent history:
[docs/reference/history.md](docs/reference/history.md); pressure stall information: [docs/reference/pressure.md](docs/reference/pressure.md)). The API has no
authentication. Run it on trusted networks only.

If another machine cannot reach the service while the device itself answers (`curl -s localhost:9797/api/status`),
the collector is running and something between the two is not. The service listens on every IPv4 address
by default (`[http] bind = 0.0.0.0`; `ss -ltn` shows `0.0.0.0:9797`), so check, in this order: the host's
own firewall, which usually denies incoming connections and needs the port opened for the network you
trust (`sudo ufw allow from 192.168.1.0/24 to any port 9797 proto tcp`, adapted to your subnet); whether
both machines are on that subnet; and client isolation on the access point or switch, which blocks the
connection whatever the firewall says (`ping <device>` fails too). IPv6 is not served.

The web page opens with an Overview of CPU, memory, available GPU, root filesystem capacity and the
hottest temperature, each colored by level (warning from 80 %, critical from 90 %; temperatures
from 85/95 °C on Jetson Orin, 75/80 °C on Raspberry Pi and 80/90 °C elsewhere; instantaneous values;
set other levels in `[thresholds]` of `platmon.ini`).
A Needs attention strip lists every reading at or above its warning level and links to its tab.
CPU is the mean of measured cores; Network lists separate readings
for up to four non-loopback interfaces, shows any omission count and links to the full Network tab.
It does not infer an uplink or sum overlapping interfaces. Resources, Network, Storage and
Temperature and Power & battery tabs show details and related recent history; hovering or touching a chart shows
the nearest point's value and age. System lists the platform, uptime,
OS, kernel, architecture, hostname and available board-specific information such as L4T; power mode
remains in Power & battery. System updates only changed values and makes no optional API requests.
The common header keeps the model, host, uptime and a Live/Partial/Offline status visible in every view;
the status tooltip shows the sample number and data age. When requests fail, a banner names the error
and the age of the data still shown, and the views are dimmed. The footer shows the running version.
Tabs support arrow-key focus and Enter/Space activation.
On narrow screens the tabs move to a bar at the bottom. Selecting a tab updates the URL
fragment (for example, `#system`), so reloading or opening that link restores the view; unknown
fragments fall back to Overview. Selecting tabs replaces the current history entry.
Only the active view renders; detail tabs share cached history, and a hidden browser page suspends
polling until it returns. This does not pause the device's collector or history recording.

To run the server in the foreground instead (e.g. while developing):
`python3 platmon.py [platmon.ini]`. `platmon.ini` lists every option with its default (collection
interval, collector on/off switches, HTTP bind/port, web page on/off, web page warning levels).
All monitoring features default to enabled. Existing explicit `enabled = no` values still override
the defaults; omitted sections adopt the new defaults. Unsupported hardware keeps its unavailable
state. History uses memory only and retains 600 seconds by default. Probe stays idle until
you configure targets and restart; no target is selected automatically.
The [performance report](docs/performance/history-expiry-validation.md) records roughly 2% of
one core with the supported features enabled, including windows above the 2% target. The new
default policy does not turn those misses into passes; normal ARM PSI remains unmeasured. Which
reports are current and which are superseded: [docs/performance](docs/performance/README.md).

### Layout

```
platmon.py          server configuration and startup
collector/         platform detection, metric readers, collection records and metadata
runtime/           background sampling, low-frequency observations and recent history
frontends/         HTTP server, standalone terminal client and web HTML/CSS/JavaScript
deploy/systemd/    source installation scripts and service unit
deploy/docker/     Dockerfile, Compose configuration and start/stop scripts
packaging/debian/  Debian control files and lifecycle hooks
scripts/packaging/ package build and verification tools
scripts/           standalone CLI installer
benchmarks/        performance measurement tools
tests/             collector/, runtime/, frontends/, packaging/ and benchmarks/
docs/              reference/, development/, releases/ and performance/
```

See the [architecture guide](docs/development/architecture.md) for module boundaries and the
[documentation index](docs/README.md) for user contracts and development evidence. Source checkout
commands use the paths above; installed commands, service names and configuration paths are unchanged.

### Run at boot

Two ways: systemd (the package above, or the source scripts below) or Docker. Both serve port 9797, so use one at a time; the scripts refuse to start
one way while the other is running (or, for systemd, still enabled at boot).

**systemd from source** (the scripts ask for sudo; packaged installs use APT/systemctl):

```sh
deploy/systemd/install.sh              # install or update (after git pull), enable at boot, start, add `platmon`
deploy/systemd/start.sh                # start, or restart to apply config changes; checks /api/stats
deploy/systemd/stop.sh                 # stop for now; still enabled, so it starts again at boot
deploy/systemd/uninstall.sh            # stop, disable, remove /opt/platmon, the unit and `platmon`; keeps the config
deploy/systemd/uninstall.sh --purge    # ... and remove /etc/platmon too
```

`install.sh` stages the complete code, then stops and replaces `/opt/platmon` and installs a
unit adapted to that path in `/etc/systemd/system/`. A failed replacement or readiness check
restores the previous code/unit; a successful update keeps the previous files in the printed
recovery directory. See [rollback](docs/reference/packaging.md#rollback). It creates
`/etc/platmon/platmon.ini` from `platmon.ini` only if that file does not exist yet, so your edits are
kept. It checks the config first and changes nothing if it is invalid. Logs: `journalctl -u platmon`.
The default port was 8080 before; an existing `/etc/platmon/platmon.ini` keeps its port, so edit it
(or `uninstall.sh --purge` and install again) to move to 9797. `start.sh` prints the port in use.

### Docker

No image is published: build it yourself on the device (see "Container image licensing" below).
`up` builds the image `platmon:local` from this checkout the first time; add `--build` after a `git pull`.
On native Linux Docker Engine, the container uses host networking so Network and Wi-Fi see the
host interfaces. It also needs read-only host mounts (all set in `deploy/docker/compose.yaml`):

```sh
deploy/docker/start.sh    # build and start; on Jetson adds deploy/docker/compose.jetson.yaml (nvpmodel power mode); waits until healthy
deploy/docker/stop.sh     # stop and remove the container (the image stays)
```

`start.sh` also installs the `platmon` command in `~/.local/bin`; `stop.sh` leaves it, since it can watch other devices too
(`scripts/install-cli.sh --user --uninstall` removes it).

Without the scripts: `BUILDX_NO_DEFAULT_ATTESTATIONS=1 docker compose -f deploy/docker/compose.yaml up -d --build` (Jetson: add
`-f deploy/docker/compose.jetson.yaml` after the base `-f` option). Run from the checkout root.
The variable keeps the image id stable when nothing changed;
without it some Docker versions give every build a new id, and the running container's image can be
deleted out from under it.

- `/sys/firmware` → board detection. Docker hides the device tree, so without this mount a Jetson
  shows up as "PC" and loses its GPU, power mode and fan data. The startup log line
  `platmon: platform …` shows what was detected.
- `/` (not recursive) → the host's root filesystem: disk usage, and the OS release (`/etc/os-release`)
  and L4T release so they show the host, not the image. The host's `/proc`, `/sys` and `/run` are not
  exposed through it.
- `./platmon.ini` → config. Set `[http] bind` and `port` here; the listener uses the host port
  directly, without a Docker port mapping. The health check and start script follow these settings.
- Host networking shares the host network namespace: Network includes host interfaces, Wi-Fi reads
  the host wireless link, and Probe loopback means the host. It does not share the PID or mount
  namespace or require privileged mode. A bridge-network override sees only its own interfaces.
- Storage reads the host root via `/host/root`. Separate filesystems such as `/boot/efi` or an
  external data disk need explicit read-only directory binds; the root bind stays non-recursive.
  See [Docker observation scope](docs/reference/docker-scope.md) for the audited coverage and remaining limits.
- Runs as an unprivileged user, needs no NVIDIA container runtime, and is marked unhealthy while
  `/api/stats` has no current data.

#### Container image licensing

platmon itself is Apache-2.0 and uses only the Python standard library. The image it builds on,
`python:3.13-slim`, is Debian-based and contains GPL/LGPL software (e.g. bash, coreutils, apt,
glibc), as any Linux base image does. So:

- This repository ships only the `deploy/docker/Dockerfile` and compose files; it does **not** publish images.
  Building and running the image on your own machine is not distribution.
- If you push an image you built to a registry or hand it to others, you distribute those
  components and take on their license obligations (offering the corresponding source, keeping
  license notices). platmon stays Apache-2.0: it is only packaged alongside them, not linked into them.
- The image deliberately uses a plain Python base, not an NVIDIA L4T one, so it bundles no NVIDIA
  binaries; platmon reads `/proc` and `/sys` only.

This is a summary of the project's policy, not legal advice.

## Test

```sh
pip install pytest   # test-only; runtime stays dependency-free
pytest
```

## License

Apache-2.0

[Brand assets and colors](frontends/web/assets/brand/README.md)
