# CLI and web interfaces

The collector publishes a snapshot in the background. The standalone terminal client,
web dashboard and plain-text endpoint display those published values without launching
new metric reads for each request.

## Terminal client

```sh
platmon                          # this device, live view
platmon 192.168.55.53            # remote device, default port 9797
platmon 192.168.55.53 2          # refresh every 2 seconds
platmon --once                   # one full plain screen for scripts/SSH/logs
platmon --view network           # start with the Network view
```

On an interactive Linux/macOS terminal, the live UI fits the window and shares the
web viewer's warning/critical thresholds from `[thresholds]`. It includes a
**Needs attention** line, sparklines of the last 24 updates and an error banner
over the previous data when the service cannot be reached.

It offers the same seven views as the web dashboard:
**Overview, Resources, Network, Storage, Temperature, Power & battery, System**.
Overview fills the available terminal with summary readings, cores and as many
details as fit. Dedicated views expose full details, including disk I/O, pressure,
Wi-Fi, TCP probe times, swap, temperatures, power rails and fans.

| Key | Action |
| --- | --- |
| `1`–`7`, `Tab`, `Left`, `Right` | Switch views |
| `Up`, `Down`, `PgUp`, `PgDn`, `Home`, `End` | Scroll a long view |
| `q` or `Ctrl+C` | Quit |

The header names the views, with shorter labels on narrow terminals.
When a view is taller than the screen, its last line indicates the hidden
content above and below. Switching views renders already received data;
it makes no additional service request.

Set `NO_COLOR=1` for monochrome output or `PLATMON_ASCII=1` for terminals
where box/block glyphs occupy two columns. With `--once`, redirected output
or Windows, the client uses a plain screen containing every section,
matching `/text`; `--view` is intended for the interactive UI.

The device's `platmon` command is installed automatically by the package
(`/usr/bin`), source-systemd installer (`/usr/local/bin`) or Docker
start script (`~/.local/bin`). For a monitoring-only PC, install
`frontends/cli.py` via [the CLI-only instructions](getting-started.md#view-a-running-device).

## Web dashboard

Open `http://<device>:9797` (with `[http] web = yes`, the default).
The **Overview** shows CPU, memory, available GPU, root filesystem
capacity and the hottest temperature. CPU is the mean across measured cores.
Network summarizes up to four non-loopback interfaces and indicates omitted
interfaces; it neither guesses an uplink nor sums overlapping interfaces.

The **Needs attention** strip lists readings at or above their warning
threshold and links to the relevant view. By default, CPU, GPU, memory and
filesystem levels are warning at 80% and critical at 90%. Temperature defaults
are Jetson Orin 85/95 °C, Raspberry Pi 75/80 °C and other platforms 80/90 °C.
These are instantaneous thresholds, not alerts based on persistence;
change them in the service's `[thresholds]` configuration.
Other filesystem warnings use the cached Storage observation only while it is current. Staying on
Overview does not refresh that observation; its warnings disappear when it becomes stale.

The **Resources, Network, Storage, Temperature** and
**Power & battery** views show details and related recent history.
Hovering or touching a chart gives the nearest value and its age.
**System** shows platform, uptime, OS, kernel, architecture, hostname and
board-specific information (such as Jetson L4T); power mode stays under
Power & battery. System only refreshes changed values and does not make
optional API requests.

A shared header presents the model, host, uptime and **Live / Partial /
Offline** state on every tab. The status tooltip includes sample number and
data age. When requests fail, a banner names the error and age of retained
data, and the views are dimmed. The footer shows the running version.

Arrow-key focus and Enter/Space activate tabs. On narrow screens the
tabs move to the bottom. A selected tab appears in the URL fragment
(e.g. `#system`), so reloads or shared links reopen that view; an unknown
fragment falls back to Overview. Tab selection replaces the current history
entry. Only the active view renders, and detail views share cached history.
A hidden browser page suspends its polling until visible again, **without**
pausing collection or in-memory history on the device.

## HTTP and plain-text access

The service answers on port 9797 by default. From any machine with an HTTP
client, use:

```sh
curl -s http://<device>:9797/api/status
curl -s http://<device>:9797/api/stats
curl -s http://<device>:9797/text
```

On systems with `watch`, `watch -n1 curl -s http://<device>:9797/text`
provides a repeating plain-text view. `/text` is useful without installing
the Python terminal client.

| Path | Purpose |
| --- | --- |
| `/api/stats` | Latest metric snapshot |
| `/api/status` | Collector health, state and freshness |
| `/api/observations` | Independently timed slow readings |
| `/api/history` | Recent numeric series held in memory |
| `/api/samples`, `/api/observation-samples` | Bounded full-publication replay for a Hub |
| `/text` | Plain-text client view |
| `/` | Web dashboard when enabled |

For exact schemas, disabled endpoints and freshness semantics, consult the
[HTTP API reference](../reference/api.md). See
[observations](../reference/observations.md),
[history](../reference/history.md) and
[full-sample replay](../reference/replay.md) for their distinct retention
and cursor contracts. Replay is bounded; it is not persistent storage.

## Data coverage and limitations

platmon detects Jetson Orin/Xavier, Raspberry Pi, PCs and other Linux hosts
and reports available data without inventing readings for unsupported hardware.
It reads kernel `/proc` and `/sys` sources directly for CPU, memory,
network, disk I/O, storage, pressure and compatible sensors.

GPU data comes from a board-specific source when supported, otherwise from
a compatible DRM class reader (including Intel `i915` or `amdgpu`).
NPU data can come from the Intel `intel_vpu` accel class. On NVIDIA
systems without a supported DRM reader, including WSL2, the optional
driver `nvidia-smi` command may provide GPU readings. The collector also
reports OS release, kernel, architecture and hostname, and L4T on Jetson.

Metric-specific constraints are in the
[reference index](../README.md#reference) and individual reference
documents. Data availability and collection overhead vary by platform;
consult the [performance reports](../performance/README.md) for
measured evidence rather than assuming a universal CPU budget.
