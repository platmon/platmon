# Changelog

Versions use MAJOR.MINOR.PATCH. The application version is defined once in
`frontends/cli.py`; Debian packaging revisions use a separate `-N` suffix.
API schema versions are independent of release versions.

## 1.3.0 — 2026-10-17

- Battery charge and the connected Wi-Fi signal get warning and critical levels, `[thresholds] battery`
  (default 20, 10 %) and `wifi` (default -70, -80 dBm). Their low reading is the bad one, so the warning
  is above the critical level and a reading at or below a level reaches it. The web page and the
  `platmon` command color them and list them under Needs attention while their observation is current;
  `/api/status` `levels` carries both, and clients that do not know them ignore them.

- Begin Ubuntu 20.04–26.04 qualification: replace Python 3.9-only runtime and packaging
  operations, pin test tools and expand interpreter CI to 3.8, 3.9, 3.10, 3.12, 3.13 and 3.14.
  CI checks extracted packages using all four Ubuntu LTS default interpreters, plus package
  lifecycle on 22.04, 24.04 and 26.04. Python 3.8 and Ubuntu 20.04 are CI-only observation targets, with no official support or
  ongoing compatibility commitment. The native package minimum stays at Python 3.9. See docs/development/ubuntu-lts-qualification.md.

- Full published core snapshots and independent observations can be replayed through
  `/api/samples` and `/api/observation-samples`. A configurable bounded memory buffer
  retains complete values and provenance for Hub catch-up, reports unavailable ID ranges,
  and rejects cursors from a previous process. Live endpoints and numeric history keep
  their existing contracts. Replay is enabled by default (up to 600 s / 16 MiB of
  encoded payloads); restarting clears it. See docs/reference/replay.md.

- The HTTP server allows at most 32 active connections and closes excess connections before a request
  thread is created; request reads and response writes time out after 10 seconds without progress, so
  a stalled or disconnected client releases its slot. The overview's cached storage warnings expire with
  their observation, and one probe target's socket error no longer affects the others.

- Source checkout files are grouped by responsibility. Deployment commands move from `scripts/systemd`
  and `scripts/docker` to `deploy/systemd` and `deploy/docker`, the Dockerfile and compose files move
  to `deploy/docker`, and package build and check tools move to `scripts/packaging`. Installed
  commands, service names, configuration paths, the HTTP API and `platmon.ini` keys are unchanged;
  a source or Docker installation runs the same commands from their new paths (docs/reference/packaging.md).

- Debian packages include a CLI manual, compressed upstream and packaging changelogs,
  copyright metadata and file checksums. Default reproducible timestamps come from
  the packaging changelog instead of 1970; `SOURCE_DATE_EPOCH` still overrides them.
  CI checks both package revisions with Lintian and rejects errors and warnings.

## 1.2.1 — 2026-10-12

- NVIDIA GPU usage and graphics clocks are collected through the driver's optional `nvidia-smi`
  command when no supported DRM reader is available, including on WSL2. The service also finds the
  WSL command outside PATH. Queries time out after 0.5 seconds; unsupported fields remain null and
  failures never reuse previous readings. GPU UUIDs identify history sources and choose one GPU
  consistently when more than one is present. Python runtime dependencies remain unchanged.
- Power rails are kept in the recent history, as `power/<source id>` series in watts
  (docs/reference/history.md), and drawn in the web page's Power & battery view. They were the only numeric
  group of `/api/stats` with no series at all, which left the rails — what a consumption reading is
  made of — impossible to read back over a window. A rail is identified by its `sensor_meta` source
  id exactly as a temperature channel is, so a renamed rail or a replaced chip starts a new series.
  Rails rank above the temperature channels when the 64-series limit is reached: a board labels a few
  of them against dozens of nearly identical temperature channels.
- The battery joins them: `observation/battery/<name>/percent`, `charge_now_uah`, `energy_now_uwh` and
  `power_uw` series, drawn on the same view, so a drain is a difference over a window rather than two
  readings a minute apart. The bay's name is the identity, so a cell swapped into the same bay
  continues its series. Only the fields a reading has get a series — a gauge counts charge or energy
  and not both, a driver may report no instantaneous power, and an empty bay reports none of them — so
  a series that could never hold a value does not take a slot from one that can.

## 1.2.0 — 2026-10-11

- Batteries and external power supplies are reported, as a low-frequency group `battery` at
  `/api/observations` (docs/reference/battery.md), on by default every 60 seconds. Per battery: charge percent,
  the driver's status, the charge or energy family its gauge counts, voltage, current or power, cycle
  count and the wear its full charge shows against the design charge; per external supply, whether it
  is online. The web page shows them in a Battery card, the `platmon` live view in its Power & battery
  view with a `BATT` summary line on a wide Overview, and the plain screen (`--once`, pipes, `/text`)
  in a `BATT` block; a narrow terminal wraps the readings onto their own lines rather than dropping
  one. `/api/stats` and its schema are unchanged, and a viewer that does not know the group ignores it.
  Read on its own slow cadence because on an ACPI host one reading of a battery is a 101 ms firmware
  call behind the driver's 1 s cache, which the 1 s core interval would hit every collection, and
  because reading a battery costs the battery.
- The Temperature & power view is now two: **Temperature**, and **Power & battery** with the power
  rails, the fans, the board's power mode and the batteries. Both viewers gain the view and the keys
  become `1`-`7`; the live view's tab bar gains a shortest label set (`1 Ov 2 Re …`) so a 40-column
  terminal keeps every tab named instead of falling back to the position alone. What the board draws
  and what it has left are read together, which is how consumption is read: the draw over a window
  against the charge that window cost. It also stops a host with dozens of temperature channels from
  pushing them off the screen — on the reporting PC the battery sat on line 36 of 43 of the combined
  view and was off a 24-row terminal entirely. The Power & battery view has no Recent history panel:
  the power rails and the battery have no history series yet
  ([remaining work](docs/development/remaining-work.md)).
- A thermal zone that also appears as an hwmon chip is now recognised by the zone's path instead of its
  name, so its temperature is reported once. The kernel may name the chip after the zone rather than
  after the zone type (`acpitz_0` for a zone of type `acpitz`), and then the name no longer matched: on
  an x86_64 PC `/api/stats` listed `acpitz`, `acpitz (thermal_zone1)` and `iwlwifi_1` a second time as
  `acpitz_0 temp1`, `acpitz_1 temp1` and `iwlwifi_1_8 temp1`. Reading a channel twice also paid twice
  for a slow sensor: the duplicate Wi-Fi read alone cost about 2 ms of every collection.
- The HTTP frontend now gives a client ten seconds to send its request. A connection that sent nothing
  held its thread, and the memory behind it, for as long as it stayed connected, so a client whose
  network dropped or a port scan left threads parked for good; 150 such connections cost about 18 MB
  that did not come back, and nothing bounded them but the service's task limit. Answering is
  deliberately not bounded: a socket deadline covers a whole write, so a shared one would cut a large
  `/api/history` response off a client that reads slowly but steadily.

## 1.1.0 — 2026-10-11

- The GPU of any host is read from the standard DRM class and the NPU from the accel class, with one
  short reader per driver: `i915` reports the share of the window the GPU was not idle with its clock
  and ceiling, `amdgpu` the busy share it publishes itself, and `intel_vpu` the share the NPU worked.
  A board module is still asked first, so a Jetson keeps its own source. A card whose driver publishes
  no load is reported by name without numbers, and a host without one says `not_detected`.
- `/api/stats` gains an `npu` object and a `npu` collector entry beside `gpu`; `gpu/usage` and
  `npu/usage` join the history. `schema_version` stays 1 and every existing key is unchanged, so an
  older client ignores the additions. A GPU without a load reading is now possible, and both viewers
  handle it.
- The web page's Recent history draws every series the endpoint gives it, instead of the first twelve
  with a dropdown for the rest. A row of buttons names the kinds of series the view holds so a viewer
  can take one out, and each kind gets its own labelled section.
- `platmon` live view shows one view at a time, the same six the web page has as tabs: Overview,
  Resources, Network, Storage, Temp & power and System. `1`-`6`, Tab and the arrow keys switch them,
  `--view` picks the first one, and a tab bar in the header names them. Switching makes no request.
- Overview keeps the screen it had. The detail views show their kind in full on any terminal width, so
  a narrow terminal no longer loses disk I/O, temperatures, power rails, fans, pressure, Wi-Fi or TCP
  connect times; swap, partition counts and the GPU's maximum clock reach the live view for the first
  time. Long sensor, rail and path names keep their column; the bars give up the room instead.
- A view longer than the terminal scrolls: the up and down arrows move a line, PgUp/PgDn a page, Home
  and End its ends, and the last line says how much is out of sight. Before, a host with many sensors
  or cores ended in `+N more lines · enlarge the terminal` with no way to reach the rest.
- The offline banner wraps instead of being cut, so the errno, the retry interval and the name of the
  script to start survive on a terminal narrower than the 111 columns it needs.
- The detail beside each Overview reading takes the width the terminal has free, instead of a fixed 16
  columns that cut `x86_pkg_temp · hottest of 38` to `x86_pkg_temp · h`.
- `--once`, pipes, Windows and `/text` are unchanged: the plain screen with every section.
- Five fixes from running 1.0.0's package on an x86_64 PC (22 logical CPUs, 31 temperature channels, an
  `acpi_fan` without a tachometer): Storage reports the filesystem's read-only state rather than this
  process's mount; history keeps every core's series inside its 64-series limit; a fan speed the
  firmware withholds is reported as `no_data` instead of failing the group; the live view draws no more
  lines than the terminal has rows; and the service integration tests no longer read the host's sensors.
- The README says what to check when another machine cannot reach the service.

## 1.0.0 — 2026-10-10

First release. From this version the HTTP API (its `schema_version` contracts), the `platmon.ini` keys and
the `platmon` command's options follow MAJOR.MINOR.PATCH: a breaking change needs a new major version.
A `v0.1.0` tag was cut while validating the packaging and never published; 1.0.0 includes everything below.

- First versioned distribution of the existing platform monitor.
- Debian package with a systemd service, standalone CLI and preserved INI configuration.
- Server and CLI `--version`, source archive, build manifest and SHA-256 checksums.
- Source installer stages updates and restores previous code/unit on startup failure.
- Package and release validation workflows; an explicit version tag creates a draft release.
- Release tags must reference commits included in main; existing version tags are protected from updates/deletion.
- Web viewer redesign: sticky header with a Live/Partial/Offline status, banners for connection loss and partial
  collection, warning/critical levels with threshold ticks, a Needs attention strip, tables, chart hover readout,
  a bottom tab bar on narrow screens and the running version in the footer.
- `[thresholds]` in `platmon.ini` sets the web page's warning and critical levels; an empty temperature keeps
  the board default.
- `platmon` live terminal view on interactive terminals: colored levels from the service's `[thresholds]`,
  Needs attention, core grid, tables, sparklines, header status and an offline banner; `q` quits,
  `NO_COLOR` and `PLATMON_ASCII` are honored. `--once`, pipes and `/text` are unchanged.
- `/api/status` reports the configured `levels`; an empty `[thresholds]` temperature is sent as the board's
  default, decided once by the server.
- Network rows on the web Overview and in `platmon` list the busiest interfaces first.
