# CLI and web interfaces

> **TL;DR**
> - The terminal client (`platmon [host] [interval]`, `--once`, `--view`) and the web dashboard show the
>   same seven views of the published snapshot and share the `[thresholds]` levels.
> - Neither triggers a collection: they read what the service last published; `/text` gives the plain
>   screen over HTTP.

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
web viewer's warning/critical thresholds from `[thresholds]`. Under the view tabs, two
lines are kept for status on every view: the **Needs attention** entries, a partial
collection's reason, or the error banner over the previous data when the service cannot
be reached; the tab of each view with a warning is marked ▲. Bars stay grey while their
reading is fine and take the warning or critical color when it is not.

It offers the same seven views as the web dashboard:
**Overview, Resources, Network, Storage, Temperature, Power & battery, System**.
Overview shows what the web Overview shows: CPU (with its busiest core), RAM, GPU,
root filesystem and the hottest temperature, then the busiest interfaces; it fits
80×24 and a phone's SSH window. Dedicated views expose full details, including cores,
disk I/O, pressure, Wi-Fi, TCP probe times, swap, temperatures, power rails (with
their sum) and fans; System also names the client's version and the service it reads.
The header gives the data's age only when it is late, and the footer the service's
address only when it is not this device.

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
capacity and the hottest temperature, in one row on a wide screen and as rows
of one card on a phone, so all five are on its first screen. CPU is the mean
across measured cores; its detail names the busiest core. Bars stay grey while
their reading is fine and take the warning or critical color when it is not.
Network summarizes up to four non-loopback interfaces, the busiest over the last
few updates, and indicates omitted interfaces; it neither guesses an uplink nor
sums overlapping interfaces.

The **Needs attention** strip lists readings at or above their warning
threshold and links to the relevant view; that view's tab carries a dot in the
warning's color. It sits in a status slot under the tabs on every view, which
the not-current and partial-collection banners share (a partial collection's
banner carries the warnings), and which keeps its height when empty, so the
views never move as warnings come and go. By default, CPU, GPU, memory and
filesystem levels are warning at 80% and critical at 90%. Temperature defaults
are Jetson Orin 85/95 °C, Raspberry Pi 75/80 °C and other platforms 80/90 °C.
Battery charge and the connected Wi-Fi signal are the other way round, since their low reading is the
bad one: a reading **at or below** a level counts, by default battery 20%/10% and Wi-Fi −70/−80 dBm.
The Wi-Fi defaults follow a common vendor rule of thumb, not a standard: about −70 dBm is the weakest
signal for reliable data, and below −80 dBm a connection is unreliable
([MetaGeek, Understanding RSSI](https://metageek.com/training/resources/understanding-rssi/)).
The battery defaults are this project's choice. Both depend on the device: one far from its access point,
or one that must reach a charger in time, may need others.
These are instantaneous thresholds, not alerts based on persistence;
change them in the service's `[thresholds]` configuration.
Other filesystem, battery and Wi-Fi warnings use the cached Storage, Battery and Wi-Fi observations only
while they are current; an empty bay or a disconnected interface raises nothing. Staying on Overview does
not refresh those observations; open Storage, Network or Power & battery to fetch them. Their warnings
disappear when the observation becomes stale. The Power & battery and Network views color the charge and
the signal by the same levels; the signal's bar runs from −90 dBm (empty) to −30 dBm (full) in both viewers.

The **Resources, Network, Storage, Temperature** and
**Power & battery** views show details and related recent history.
Hovering or touching a chart gives the nearest value and its age. Percentages
and temperatures are drawn on the 0–100 scale of their bars; values without a
top (bytes/s, W) use their own range. More than eight cores are drawn as one
chart of their mean and the busiest core, each core's own chart on request.
Temperatures, interfaces (loopback last) and filesystems keep the server's order;
partitions are folded under one line a disk; Power & battery sums the rails.
**System** shows platform, uptime, OS, kernel, architecture, hostname and
board-specific information (such as Jetson L4T); power mode stays under
Power & battery. System only refreshes changed values and does not make
optional API requests.

A shared header presents the model, host, uptime and **Live / Partial /
Offline** state on every tab. The status tooltip includes sample number and
data age. When requests fail, a banner names the error and age of retained
data, and the views are dimmed. The data's age appears beside the status only
when it is older than two updates. System lists the running version and the
service the page reads. On a phone the header names the host instead of the model.

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
