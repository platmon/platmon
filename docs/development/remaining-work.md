# Remaining work

> **TL;DR**
> - What is still open, checked against the repository on 2026-10-30; the evidence for each release is in
>   [release readiness](../releases/readiness.md).
> - Windows: unreleased until the real-PC checks below pass. Linux: armhf on a real device, a 24 h soak
>   since 1.5.0, an apt upgrade from 1.5.0 on a real device, a remote probe target.
> - Python server: deprecated in 1.5.0, removed for the next minor release (below); not released yet.
> - Also recorded, not started: battery follow-ups, a desktop tray frontend, and a separate network
>   process (deferred; no Unix socket).

Merging, production rollout and releases always wait for the owner's approval. The staged plan of
2026-10-09 to 2026-10-11 (Disk I/O → UI → storage → PSI → Wi-Fi → probe → history → release readiness)
was merged and released from 1.0.0 on; its validation items that still apply are listed below, and its
checkpoints are in this file as of `b96aba8`.

## Windows build (2026-10-26)

The native server's Windows build (`x86_64-pc-windows-msvc`, native/README.md "Windows") collects
everything Linux does that Windows has: the required data, CPU clocks (every processor group, the hybrid
CPUs' own base clocks), the GPU, temperatures, network, disk I/O, all four slow groups (storage, Wi-Fi,
battery, probe) and their history, and runs as a Windows service. Pressure has no Windows source
([pressure.md](../reference/pressure.md#windows)).

**Release decision (owner, 2026-10-25):** unreleased until the real-PC checks below pass; then an
experimental zip of the binary (no installer, no package manager), named as experimental in its release
notes. The Linux packages and their release gate are unchanged by it.

Real-PC checks (a laptop: Core Ultra 7 155H, Intel Arc iGPU, WD SN810 NVMe, Windows 11 26H2, Korean display
language, non-admin unless stated). CI runs on a VM, which has none of these devices:

| check | status | evidence |
| --- | --- | --- |
| build, unit tests, `tools/windows_smoke.py` (non-admin parts) | passed | every PR from #115 to #124 |
| CPU clock against Task Manager's formula | passed after #115 | the registry base was wrong on hybrid CPUs (P-cores up to 5.85 GHz); now `% Processor Performance × Processor Frequency` |
| GPU usage and clocks | passed (#116) | usage near the busiest-engine counter; clocks 650–750 MHz / 2.25 GHz when the driver reports them |
| drive temperature | read (63–64 °C); **not compared** | the comparison (`Get-StorageReliabilityCounter`) needs an administrator shell |
| GPU temperature | **not checked** | an integrated GPU reports none (as in Task Manager); needs a discrete GPU |
| disk counters without admin | passed | |
| storage, battery, Wi-Fi against Windows's own tools | passed (#120, #121, #123) | `Get-Volume` / `Get-Partition`, WMI battery classes, `netsh wlan` |
| history on a laptop | passed after #124 | 48 series, nothing dropped |
| service: `sc.exe create`, `start`, `stop`, `delete`; `/api/status` ready, exit code 0 | **not checked** | needs an administrator shell (`windows_smoke.py` does it) |
| sleep and resume while serving | **not checked** | needs someone at the PC: data age and `clock` after resume, no repeated gap errors |
| more than 64 CPUs (several processor groups) | **not checked** | unit-tested numbering only; needs such a host, or `bcdedit /set groupsize` and a reboot |

Record for each check: Windows version, CPU, GPU and driver, drive model, and the values on both sides.
`cpu_frequency` and `temperature` should never be `error` on a healthy PC.

Known gaps: Intel's GPU driver reports no clocks while the GPU sleeps (`no_data`); one of 14 local smoke
runs failed the Wi-Fi observation check before its PR and could not be reproduced; GitHub's runners have
a virtual disk that answers the temperature query with a device error (#119 reports it as not exposed).

## Linux releases

Open since the gates in [release readiness](../releases/readiness.md):

- **armhf on a real device:** 1.6.0's armhf package was checked under QEMU only; the systemd service
  start, its health check and the sandbox are unchecked on armhf.
- **24 h soak and `hub_check.py`:** last run at 1.4.0; 1.5.0 and 1.6.0 had one-hour checks by the owner's
  choice.
- **apt upgrade on a real device:** the Orin Nano upgraded from 1.4.0 to 1.6.0 under apt and rebooted
  (2026-10-28, [release readiness](../releases/readiness.md#160-release-gate-2026-10-25)); the upgrade
  from 1.5.0 and the Hub reconnecting after it were checked only in CI's containers or not at all.
- **Probe against a remote target:** checked on loopback and with fakes only; no remote target has been
  approved ([probe.md](../reference/probe.md#verification)).
- **Orin candidate unit:** remove it (`sudo native/tools/deploy/uninstall.sh` on the Orin).
- The local worktrees `platmon-w2` and `platmon-svc` on the machine that made them.

## Python server removal

The Python server (`platmon.py`, `collector/`, `runtime/`) was deprecated in 1.5.0. The owner decided on
2026-10-24 to remove it in a later **minor** release: the versioning contract covers the HTTP API,
`platmon.ini` keys and command options, which do not change. Done on `main`, not released yet:

1. The source installation and the container run the native server: `deploy/systemd/install.sh` installs
   `native/build.sh`'s build, the Dockerfile builds the server in a build stage and runs it on
   `debian:13-slim`, with `platmon-native health` as its health check.
2. The Python service's answers are recorded in `native/tools/equivalence/golden/`; the equivalence
   checks compare with them. Since 2026-10-30 they are regression data that `--bless` rewrites from the
   server after an intended change ([ADR 0002](adr/0002-api-contract-not-python-answers.md)).
3. Its 397 tests were ported to Rust or mapped to the checks that show them:
   [Python test port](python-test-port.md).
4. The Python server, `benchmarks/` and their tests are removed; `native/tools/` is split into
   `equivalence/`, `perf/` and `deploy/`; the application version is the `VERSION` file. Renaming
   `native/` was not part of it (it needs the owner's approval).

The release that carries the removal (a minor, by the owner) still has to record it in
[release readiness](../releases/readiness.md).

## Deferred after the battery group (2026-10-11)

Left out of `feat(collector): report batteries and external power supplies` on purpose, recorded here so
the reasons do not have to be rediscovered. None of them is a defect in what shipped.

| Item | What it would take | Why it waited |
| --- | --- | --- |
| A battery level in `[thresholds]` | Done: `battery` and `wifi` levels, warning above critical, compared at or below by `low_level()` / `lowLevel` (docs/guides/interfaces.md) | — |
| `temp`, `time_to_empty_now`, `time_to_full_now` | Three more standard `power_supply` attributes in the battery reader (`native/src/collect/linux/observe/battery.rs`); absent ones already cost nothing | `temp` is a temperature and `/api/stats` already has a `temperature` table, so where it belongs is a design question, not a field to add. The two time estimates are a driver's own extrapolation, not a reading |
| What reading a battery costs the battery | A power measurement on a battery-powered host, with the group off and on | It is the reason the default interval is 60 s rather than the 10 s floor, and it is the one number behind that choice that is not measured |
| Battery in a fleet server's own history | Done in `platmon-hub` ([#31](https://github.com/platmon/platmon-hub/pull/31)): `battery` is among the observation groups its history stores | — |

### A battery behind an external device

A host can have its charge reported by a separate device — a power board, a controller — rather than by
the kernel's class, with another program on the host already reading that device. Such a source would sit
ahead of `/sys/class/power_supply` in the same group, chosen the way the GPU's sources already are
([api.md](../reference/api.md), "Where one value has several sources"), with `provider` saying which one answered
and the field model this group defines staying as it is.

It would stay a source platmon **reads**, not one that pushes to it. Three reasons:

- A pushed value has an age platmon never observed. `Sampler._check` requires every number in a snapshot
  to carry a read span from that collection and falls the whole snapshot back to
  `cycle_start_upper_bound` otherwise; a group's `data_age_ms` makes the same promise for its own data.
- The HTTP frontend is read-only GET. A write path asks who may write, and platmon has no answer: an
  injected charge level is a safety-adjacent falsehood on a vehicle, and authentication is not in scope.
- The Jetson reader (`native/src/collect/linux/jetson.rs`) already reads another program's file (`/var/lib/nvpmodel/status`), so the pattern
  exists and needs no new mechanism.

Deferred until a second real source exists, so that the format is settled by the field model rather than
by one device's payload. The external device keeps whatever else it reports — motion, actuators, anything
else on its bus — out of platmon: only the fields this group defines would be read.

## Decided: what the service serves (2026-10-30)

The owner asked for the web page and the CLI to be independent of the service, reading it, without
breaking platmon-hub. The existing `[http] bind` and `web` keys already give the three shapes (everything,
the API without the page, this host only), on Linux and Windows alike; they are documented in
[deployment](../guides/deployment.md#choosing-what-is-served).

| Option | Decision | Why |
| --- | --- | --- |
| A Unix socket for local clients | Not added | Loopback TCP (`bind = 127.0.0.1`) already keeps the API on the host, with the same code on Windows; a socket would add a second transport per OS (AF_UNIX or named pipes) for file-permission access control the read-only API does not need. Revisit if per-user access is required |
| A separate network process (a gateway on 9797 relaying a loopback core API, with the page) | Deferred | It would isolate a network-side failure from collection and keep `/proc` and `/sys` out of the process that accepts connections; it costs a second process and unit, two port settings, and a relay for `/api/replay`'s large answers. Today the server builds with `panic = "abort"`, so a panic while answering a request would end collection too; none is known. Measure its memory and latency against [budget.md](../performance/budget.md) before starting |

## Planned: desktop tray (2026-10-18)

Not started. Recorded so the shape agreed with the owner does not have to be rediscovered; starting it is
the owner's decision.

A tray icon on a Linux desktop showing the state of this host's platmon service. It is one more frontend,
like `frontends/cli.py`: a separate process that reads the local service's HTTP API, never part of the
service itself.

| Decision | Choice | Why |
| --- | --- | --- |
| Process | Separate, started in the user's desktop session (`/etc/xdg/autostart/`) | The service runs with `DynamicUser=yes` and has no access to the session bus a tray registers on |
| Source | This repository, `frontends/tray` | It reads the same `/api/status` and `/api/observations` the CLI reads and changes with them |
| Package | A second binary package `platmon-tray` (`Depends: platmon`) built from the same source | Headless boards keep installing `platmon` alone: no desktop files, no extra dependencies, no change to the measured budget |
| Protocol | StatusNotifierItem plus `com.canonical.dbusmenu` over D-Bus; no XEmbed | XEmbed trays do not work on Wayland |
| D-Bus library | jeepney (MIT, pure Python) or a standard-library implementation | GTK, libappindicator / Ayatana, pystray and Qt are LGPL or GPL |
| Scope | Read-only: a state icon, a few key readings, "open the web view" | The HTTP frontend is read-only GET; the tray does not control the service |
| Poll rate | Low (5–10 s), measured against [budget.md](../performance/budget.md) before release | Every client request is CPU on the host being monitored |

Desktop support: KDE, XFCE and Cinnamon show StatusNotifierItem icons as installed; Ubuntu's GNOME ships the
AppIndicator extension; plain GNOME (Debian) needs the user to install that extension. A tray showing several
hosts would read `platmon-hub` instead and belongs with Hub or in its own repository, not here.
