# Remaining work

> **TL;DR**
> - What is still open, checked against the repository on 2026-11-18 (1.9.0 released); the evidence for
>   each release is in [release readiness](../releases/readiness.md).
> - 1.8.0 (owner, 2026-11-03 and 2026-11-12), released 2026-11-12: the experimental Windows zip together with
>   `.rpm` release files and battery follow-ups; 1.8.1, the frontends' design on its server, released
>   2026-11-13; its root candidate unit passed the checks, `hub_check.py` and a 24 h soak on the Orin.
> - Windows: the real-PC checks passed on 2026-11-01 except two without the hardware, accepted as known
>   gaps; one of them, a discrete GPU's temperature, passed on a desktop on 2026-11-19. Linux: armhf on a
>   real device, a remote probe target.
> - Windows as a supported platform: proposed 2026-11-16; its plan is done (owner, 2026-11-21: step 3 needs no
>   further device unless asked, an unsigned MSI, the switch in the next release): Windows 11 x86_64 is
>   supported from the release after 1.9.0; the evidence is [below](#windows-promotion-to-a-supported-platform-proposed-2026-11-16).
> - Python server: deprecated in 1.5.0, removed in 1.7.0 (below).
> - Also recorded, not started: a desktop tray frontend and a separate network process (deferred; no
>   Unix socket).

Merging, production rollout and releases always wait for the owner's approval. The staged plan of
2026-10-09 to 2026-10-11 (Disk I/O → UI → storage → PSI → Wi-Fi → probe → history → release readiness)
was merged and released from 1.0.0 on; its validation items that still apply are listed below, and its
checkpoints are in this file as of `b96aba8`.

## Next release (2026-11-03)

After 1.7.0 the owner chose not to release the Windows zip on its own but with the rest of this work, in
this order; the version is 1.8.0 (owner, 2026-11-12):

1. This file brought up to date with 1.7.0. Done (#159).
2. `.rpm` packages among the release files (GitHub Release only: no DNF repository, no signing). Done (#160).
3. Battery follow-ups ([below](#deferred-after-the-battery-group-2026-10-11)), after a design decision.
   Done: `temp_c` (#161), the rest closed or deferred by the owner on 2026-11-03.
4. On the Orin, the release candidate as the root candidate unit: `deploy_check.py checks`, a soak and
   `hub_check.py` (installing and removing the unit needs the owner's sudo). Done with 1.8.1's binary
   (2026-11-13/14, [its gate](../releases/readiness.md#181-release-gate-2026-11-13)).
5. The release, with its gate in [release readiness](../releases/readiness.md#180-release-gate-2026-11-12).
   Done: published 2026-11-12, in the APT repository, the Orin upgraded with `apt install`.

1.8.1 (owner, 2026-11-13) releases the frontends' design policy (#193), which missed 1.8.0, as a patch on
1.8.0's server; its gate is in [release readiness](../releases/readiness.md#181-release-gate-2026-11-13).
Published 2026-11-13, in the APT repository, the Orin upgraded with a whole-system `apt-get upgrade`.
Item 4 above was done with 1.8.1's binary.

1.8.2 (owner, 2026-11-14) is a patch of two display fixes found after 1.8.1 (#203, #206); its gate is in
[release readiness](../releases/readiness.md#182-release-gate-2026-11-14).
Published 2026-11-14, in the APT repository, the Orin upgraded with a whole-system `apt-get upgrade`.

1.9.0 (owner, 2026-11-18) releases what `main` gained after 1.8.2, without waiting for the Windows 24 h soak
of the promotion plan (Windows stays experimental); its gate is in
[release readiness](../releases/readiness.md#190-release-gate-2026-11-18).
Published 2026-11-18, in the APT repository.

## Windows build (2026-10-26)

The native server's Windows build (`x86_64-pc-windows-msvc`, native/README.md "Windows") collects
everything Linux does that Windows has: the required data, CPU clocks (every processor group, the hybrid
CPUs' own base clocks), the GPU, temperatures, network, disk I/O, all four slow groups (storage, Wi-Fi,
battery, probe) and their history, and runs as a Windows service. Pressure has no Windows source
([pressure.md](../reference/pressure.md#windows)).

**Release decision (owner, 2026-10-25):** unreleased until the real-PC checks below pass; then an
experimental zip of the binary (no installer, no package manager), named as experimental in its release
notes. The Linux packages and their release gate are unchanged by it.

**Since (owner, 2026-11-01):** the two checks this laptop cannot do (more than 64 CPUs, a discrete GPU's
temperature) stay unchecked and are named in the zip's README instead of holding the release. The zip:
`scripts/packaging/package_windows.py`, built by CI's `package` job and released from the next tag after
1.7.0 (native/README.md, "Windows"). Its CHANGELOG entry must call it experimental: the release workflow
refuses a tag whose entry does not (`check_changelog` in `package_windows.py`).

**Since (owner, 2026-11-14):** still experimental, but easy to start, to draw Windows testers and
contributors. Two choices of the 1.8.0 zip change: the server links the C runtime statically (`+crt-static`; no
Visual C++ Redistributable to install), and the zip registers the service: `start.cmd` (serve as the user,
open the browser), `install-service.cmd` / `uninstall-service.cmd` (administrator; `C:\Program Files\platmon`,
`sc.exe`). No installer and no package manager still. Windows reports do not count toward the APT-install
testers of #181. Before the release that brings it: a check of the zip on the owner's Windows laptop.

Real-PC checks (a laptop: Core Ultra 7 155H, Intel Arc iGPU, WD SN810 NVMe, Windows 11 26H2, Korean display
language, non-admin unless stated). CI runs on a VM, which has none of these devices:

| check | status | evidence |
| --- | --- | --- |
| build, unit tests, `tools/windows_smoke.py` (non-admin parts) | passed | every PR from #115 to #124 |
| CPU clock against Task Manager's formula | passed after #115 | the registry base was wrong on hybrid CPUs (P-cores up to 5.85 GHz); now `% Processor Performance × Processor Frequency` |
| GPU usage and clocks | passed (#116) | usage near the busiest-engine counter; clocks 650–750 MHz / 2.25 GHz when the driver reports them |
| drive temperature | passed (2026-11-01) | administrator shell: `Get-StorageReliabilityCounter` 64 and 63 °C before and after, platmon 64.0 °C between (SN810); the card reader reports 0 and platmon leaves it out |
| GPU temperature | **not checked: known gap**; passed on a desktop (2026-11-19, [below](#windows-promotion-to-a-supported-platform-proposed-2026-11-16)) | an integrated GPU reports none (as in Task Manager); needs a discrete GPU |
| disk counters without admin | passed | |
| storage, battery, Wi-Fi against Windows's own tools | passed (#120, #121, #123) | `Get-Volume` / `Get-Partition`, WMI battery classes, `netsh wlan` |
| history on a laptop | passed after #124 | 48 series, nothing dropped |
| service: `sc.exe create`, `start`, `stop`, `delete`; `/api/status` ready, exit code 0 | passed (2026-11-01) | `windows_smoke.py` in an administrator shell on `main` at `9dbe9b2`: all passed |
| sleep and resume while serving | passed (2026-11-01, Modern Standby) | asleep 8 min 36 s: `uptime` +515.8 s for +516.7 s of wall time; the first answer `stale` (data age 515.7 s, `/api/stats` 503), ready 1 s later with one `gap` reading (every CPU, interface and disk), then none in 108 polls; same instance, no failure. The laptop has no S3 or hibernation |
| more than 64 CPUs (several processor groups) | **not checked: known gap** | unit-tested numbering only; needs such a host, or `bcdedit /set groupsize` and a reboot |

Record for each check: Windows version, CPU, GPU and driver, drive model, and the values on both sides.
`cpu_frequency` and `temperature` should never be `error` on a healthy PC.

Known gaps: Intel's GPU driver reports no clocks while the GPU sleeps (`no_data`); one of 14 local smoke
runs failed the Wi-Fi observation check before its PR: most likely a link going down, which Windows lists
connected for some seconds while refusing its RSSI (#154 reports that as `unknown`); GitHub's runners have
a virtual disk that answers the temperature query with a device error (#119 reports it as not exposed).

## Windows: promotion to a supported platform (proposed 2026-11-16)

**Switched (owner, 2026-11-21):** supported on Windows 11 x86_64 from the release after 1.9.0, with the
installer and the zip (no longer `-experimental`); the release check no longer requires "experimental". Until
then it was experimental: the README's support table, native/README.md and the release check said so. After
the checks below the owner asked what promoting it would take; this was the plan.

**Evidence so far (2026-11-16, the laptop above, 1.8.2's zip):**

- Installed as documented ([Windows](../guides/getting-started.md#windows),
  #214): checksum, `config-check`, `serve` by hand, then the service from `C:\Program Files\platmon` with
  the elevated `sc.exe` line; running and started at boot.
- A 12 h run of the service with an ad-hoc script (not `deploy_check.py`, which is Linux only): `/api/status`
  every second, `/api/stats`, `/api/observations`, `/api/history` and `/text` every 10 s, the process every
  minute. No restart (one instance id and PID), 0 of 42,818 polls not ready, 0 failed requests, data age
  median 0.5 s and at most 2.7 s, sequence always rising. Private bytes 26.6 → about 30.1 MiB in the first
  4 hours, then flat (29.6 to 30.3 MiB) to hour 12: +1.79 MiB against hour 1, under the 2 MiB set before
  the run. Its last criterion, an unchanged thread count, failed as written: 9 to 12 threads (10 in 628 of
  716 readings, handles 208 or 210 with them), bounded and not growing, which the request threads explain;
  the rule was too strict. One client only, no Hub; four loop stalls of 5 to 12 s were the script's, not
  sleep. The script and its record were in a temporary directory and are not kept.
- Compared with the Orin running 1.8.2: the required data, CPU clocks, GPU usage and clocks, network,
  disk I/O, storage, Wi-Fi, battery and history answer with the same structure. Windows has no CPU
  temperature, power, fans, power mode or pressure (native/README.md, "Windows (experimental)").
- The `platmon` command crashed when piped on a non-UTF-8 code page; fixed in #213. Windows still has no
  way to install the command.

**Steps 1 and 2 on the same laptop (2026-11-16 to 18):**

- Failures (#216): a refused configuration or a panic goes to the event log (source `platmon`) and the
  service reports its stop with the error; the guide's `sc.exe failure` line restarts it after a crash.
  Restarting Windows logged the service as ended unexpectedly (event 7034) and counted a failure; fixed in
  #221, seen across real restarts on the desktop below.
- `native/tools/deploy/windows_check.py` (#217) runs `deploy_check.py`'s checks on a candidate service
  plus an upgrade from 1.8.2, and its soak, judged by `deploy_check.py report`'s rules (private bytes as
  PSS, handles as fds). The checks passed 9 of 9 on each build below. In elevated runs, starting
  `taskkill` was refused (WinError 5) in 5 of 8, cause unknown; the crash check then ends the process
  with `TerminateProcess` (#223).
- The accelerated soak (`soak --accel --hours 20`, about 6 h: 3 rounds of 4 concurrent pollers each hour,
  a restart at hour 10), as the owner chose instead of 24 h:
  - The default heap (`main` before #222): cut at hour 15 by a Windows Update restart, and already failing
    the settled PSS rule. Settled private bytes moved between 32.9 and 63.7 MiB under the same load,
    growing 653 and 3156 KiB per round against 64. The data held was constant and the peak flat, so it
    was the heap keeping freed memory, not growth; a test program reproduced it, and the segment heap
    did not. #222 asks for the segment heap.
  - With the segment heap (`a34e4d8`), twice. Settled private bytes stayed between 25.6 and 27.9 MiB
    in both; threads 10 to 14, handles 208 or 210, no lost or failed requests.
    - Run 1 (2026-11-17/18) failed the rule by a hair: 15 KiB per round in hours 0 to 9, 66 in hours 10
      to 19 (limit 64), from a rise of about 1.2 MiB over hours 15 to 19.
    - Run 2 (2026-11-18), the same build and settings: passed, 24 and 5 KiB per round.
    - The rule stays as set before the runs: run 2 shows no repeated growth, so run 1's rise reads as
      noise.
- `hub_check.py` on Windows and a 24 h soak: done on the desktop below.

**A second device: a desktop (2026-11-19 to 21, 1.9.0's zip):**

- Intel Core i5-13600KF (hybrid, 20 threads), NVIDIA GeForce RTX 4070 Ti (driver 32.0.16.1088), two NVMe
  drives (WD_BLACK SN850X, Samsung 980 PRO), two SATA drives (Samsung 850 EVO, a WD20EZRZ hard disk), Intel
  Wi-Fi 6 AX201, no battery; Windows 11 Home 25H2 (10.0.26200), Korean display language; WSL2 with mirrored
  networking and Docker Desktop installed.
- Installed as documented: checksum, `sc.exe create`, the recovery actions, the event log source, the user
  `PATH`. A platmon running in WSL2 held port 9797, since mirrored networking shares the host's ports: the
  service could not listen (event 1, exit code 1) and the recovery actions started it once that platmon was
  stopped.
- Against Windows's own tools, 12 readings 5 s apart: the GPU's temperature 40.1–41.8 °C against
  `nvidia-smi`'s 40–42, its clock the same as `nvidia-smi`'s at the same moment (210 MHz in P8, 2610 MHz in
  P0), the Wi-Fi signal within 2 dB of `netsh wlan`'s RSSI (−60 to −64 dBm), the NVMe drives' temperatures
  as `Get-StorageReliabilityCounter`'s (44 and 45 °C). The SATA drives answer
  `StorageDeviceTemperatureProperty` with `ERROR_INVALID_FUNCTION`, so platmon reports no temperature for
  them (`not_exposed`); in an administrator shell Windows shows the hard disk's 31 °C from SMART, which
  would be a new source.
- Real restarts: Windows Update restarted the PC three times between 02:48 and 02:50 on 2026-11-20, with
  the service running since the evening before. The System log has no 7034 or 7031 for `platmon`, and it
  was running after: #221 across a real restart.
- Sleep and resume while serving, woken by a wake timer, with an ad-hoc script polling every 0.5 s (kept
  with the records below), same instance throughout:
  - S3 (hybrid sleep, this desktop's setting), asleep 127 s: uptime +128.0 s for +125.6 s of wall time
    between the polls around it. The first two answers `stale` (data age 127 s, `/api/stats` 503), ready
    1.0 s after resume with gap readings for one second, then none in 159 polls.
  - Hibernation, asleep 197 s: uptime +196.7 s for +195.5 s; ready at once with one gap reading, then none
    in 161 polls.
- `windows_check.py checks --previous` 1.8.2's binary: 9 of 9 (`taskkill` started this time; back in
  5.35 s).
- `hub_check.py` in its Windows mode (#227) with platmon-hub 1.2.0 against 1.9.0 as the candidate service,
  the full 13 min: passed. 4 of 4 instances stored, `agent_restart_unknown_extent` 3 per stream, 56 core
  publications from the Hub's 60 s down stored after it, the core cursor at the agent's latest id.
- `windows_check.py soak --hours 24` (the full phases, not `--accel`; 2026-11-20/21, the PC kept awake
  by the tool's wrapper): **PASS**. Settled private bytes 24.3 to 25.7 MiB, −32 and −28 KiB per round in
  hours 0 to 11 and 12 to 23 (limit 64); concurrency peaks 54 to 78 MiB; threads 9 to 11, handles 167 to
  169; CPU 1.3 to 1.7 % of a core per hour (one hour without a reading); no loss, no automatic restart.
- Raw records on that PC: `%LOCALAPPDATA%\platmon-measure\windows-desktop-1.9.0-20261119\` (SHA256SUMS
  there).

**What a supported platform lacks, against the Linux gate:**

| | Linux (the release gate) | Windows now |
| --- | --- | --- |
| devices | x86_64 PC, Orin Nano, Raspberry Pi 4 | a laptop (Intel, Windows 11 26H2) and a desktop (Intel, NVIDIA GPU, Windows 11 25H2) |
| deployment checks | `deploy_check.py checks`: restart, crash recovery, failed start and the rest (9) | `windows_check.py checks`: the same 9 with an upgrade, passed on both (above) |
| soak | `deploy_check.py soak --hours 24`: concurrent pollers, Hub-like requests, settled PSS rule | `windows_check.py soak --hours 24` and `hub_check.py` passed on the desktop (1.9.0); `--accel` on the laptop |
| failures | systemd `Restart=`, the journal | `sc.exe failure` (the guide), the event log (#216) |
| install, upgrade, removal | apt, the edited `platmon.ini` kept | copied by hand and `sc.exe`; the binary replaced under the service by `windows_check.py`'s upgrade check, no installer |
| `platmon` command | `platmon-cli` package | in the zip from 1.9.0 (#219), with Python 3.9+ on `PATH`; no installer |
| unchecked | named in native/README.md | Windows 10, more than 64 CPUs, an AMD CPU, SATA drives' temperatures (SMART) |

**Plan, in order:**

1. **Failures:** the service restarts after a crash (a recovery action: `sc.exe failure`, or the
   installer's) and reports a failed start and its reason to the Windows event log.
2. **Deployment checks and soak on Windows:** restart, crash recovery, failed start and upgrade checked by
   a tool (a Windows mode of `deploy_check.py`, or its own), then a 24 h soak with concurrent pollers and
   `hub_check.py`; thread and handle counts judged by bounds and growth, not equality.
3. **A second device:** AMD, or a desktop with a discrete GPU (its temperature is a known gap). Whether
   Windows 10 is in scope is the owner's call; it is out of Microsoft's support.
4. **Installation:** an MSI or scripts in the zip, including the `platmon` command (`frontends/cli.py`
   with Python 3.9+, as `platmon-cli` on Linux). An MSI built with WiX using only MSI's own tables
   (`ServiceInstall`, `ServiceControl`, `Environment`), no custom action DLLs, so that none of WiX's code
   ships in it; WiX's license (MS-RL) is to be checked against the dependency rules first. An unsigned
   installer meets SmartScreen warnings: signing needs a certificate and its key in CI (owner).
   **Decided (owner, 2026-11-21):** an MSI, unsigned. WiX is MS-RL, outside the dependency rules, and its
   binaries carry a separate maintenance-fee agreement (OSMF), so the MSI is built with Windows Installer
   itself (`msi.dll` through ctypes, `makecab.exe`; `scripts/packaging/package_msi.py`) from MSI's own
   tables, no custom actions. Step 3 needs no further device unless a user asks for one (owner, 2026-11-21).
5. **The switch:** the support table, native/README.md, the zip's name and README, and the release check
   that requires "experimental" changed together, with the supported scope and its known gaps (no CPU
   temperature among them) stated (owner). **Done** for the release after 1.9.0: the scope is Windows 11
   x86_64; not checked: Windows 10, more than 64 CPUs, an AMD CPU.

All five steps are done: 1 and 2 on the laptop and the desktop, 3 with the desktop (no further device unless a
user asks), 4 as the MSI, 5 for the release after 1.9.0.

## Linux releases

Open since the gates in [release readiness](../releases/readiness.md):

- **armhf on a real device:** 1.6.0's armhf package was checked under QEMU only; the systemd service
  start, its health check and the sandbox are unchecked on armhf.
- **24 h soak and `hub_check.py`:** both last ran at 1.4.0; 1.5.0 and 1.6.0 had one-hour checks by the
  owner's choice. On 2026-10-30/31 `main` at `0ba5e2c` (the Python server removed; aarch64 build
  `40a73583`) ran `deploy_check.py soak --hours 24` on the Orin Nano as the candidate unit, after its
  checks (9/9): **PASS** (`report --settled-mib 14`). 105,960 Hub-like requests, no error, loss or walk
  problem, no automatic restart; CPU 0.77–0.90 % of a core per hour; settled PSS 10.6 → 11.4 MiB (budget
  14; 1.4.0's 24 h run rose 10.5 → 11.3), concurrency peaks up to 12.3 MiB; 5 threads, 4 descriptors
  throughout. Raw: the Orin's `~/platmon-soak-0ba5e2c/` (`checks-orin.jsonl`, `soak-orin.jsonl`). Later
  changes on `main` (the HTTP edges and `/text` 500, the binary's commands, the package split, the
  rename) ran after it; 1.7.0's aarch64 binary then passed `--user checks` (9/9) and
  `--user soak --hours 24` on 2026-11-01/02 as a user unit, not the root unit (its gate). 1.8.1's aarch64
  binary passed the root unit's checks (9/9), `hub_check.py` with platmon-hub 1.1.1 and
  `soak --hours 24` on 2026-11-13/14 ([its gate](../releases/readiness.md#181-release-gate-2026-11-13));
  the raw records are on the development PC in `~/.local/state/platmon-measure/item4-1.8.1-20261113/`
  (SHA256SUMS there; the Orin's copy and its candidate units were removed on 2026-11-14).
- **apt upgrade on a real device:** the Orin Nano upgraded from 1.4.0 to 1.6.0 under apt and rebooted
  (2026-10-28, [release readiness](../releases/readiness.md#160-release-gate-2026-10-25)), and from 1.6.0
  to 1.7.0 with `apt install platmon`, which also installed platmon-cli (2026-11-03,
  [its gate](../releases/readiness.md#170-release-gate-2026-10-31)), and from 1.7.0 to 1.8.0 with
  `apt install platmon platmon-cli` (2026-11-12, [its gate](../releases/readiness.md#180-release-gate-2026-11-12)),
  and from 1.8.0 to 1.8.1 with a whole-system `apt-get upgrade`, the PC's Hub following the restarted
  service (2026-11-13, [its gate](../releases/readiness.md#181-release-gate-2026-11-13)); the upgrade from
  1.5.0 was checked only in CI's containers.
- **Probe against a remote target:** checked on loopback and with fakes only; no remote target has been
  approved ([probe.md](../reference/probe.md#verification)).
- The local worktrees `platmon-w2` and `platmon-svc` on the machine that made them (not the Linux PC).

## Python server removal

The Python server (`platmon.py`, `collector/`, `runtime/`) was deprecated in 1.5.0. The owner decided on
2026-10-24 to remove it in a later **minor** release: the versioning contract covers the HTTP API,
`platmon.ini` keys and command options, which do not change. Released in 1.7.0:

1. The source installation and the container run the native server: `deploy/systemd/install.sh` installs
   `native/build.sh`'s build, the Dockerfile builds the server in a build stage and runs it on
   `debian:13-slim`, with `platmon-server health` as its health check.
2. The Python service's answers are recorded in `native/tools/equivalence/golden/`; the equivalence
   checks compare with them. Since 2026-10-30 they are regression data that `--bless` rewrites from the
   server after an intended change ([ADR 0002](adr/0002-api-contract-not-python-answers.md)).
3. Its 397 tests were ported to Rust or mapped to the checks that show them:
   [Python test port](python-test-port.md).
4. The Python server, `benchmarks/` and their tests are removed; `native/tools/` is split into
   `equivalence/`, `perf/` and `deploy/`; the application version is the `VERSION` file. Renaming
   `native/` was not part of it (it needs the owner's approval).

1.7.0 carries the removal; its gate is in [release readiness](../releases/readiness.md#170-release-gate-2026-10-31).

## Deferred after the battery group (2026-10-11)

Left out of `feat(collector): report batteries and external power supplies` on purpose, recorded here so
the reasons do not have to be rediscovered. None of them is a defect in what shipped.

| Item | What it would take | Why it waited |
| --- | --- | --- |
| A battery level in `[thresholds]` | Done: `battery` and `wifi` levels, warning above critical, compared at or below by `low_level()` / `lowLevel` (docs/guides/interfaces.md) | — |
| `temp` | Done (owner, 2026-11-03): `temp_c` in each battery's entry, from `temp` on Linux and `BatteryTemperature` on Windows ([battery.md](../reference/battery.md#fields)), not in `/api/stats`'s `temperature` table | — |
| `time_to_empty_now`, `time_to_full_now` | Closed (owner, 2026-11-03): not added | A driver's own extrapolation, not a reading; a viewer can work it out from the charge and the draw |
| What reading a battery costs the battery | A power measurement on a battery-powered host, with the group off and on | It is the reason the default interval is 60 s rather than the 10 s floor, and it is the one number behind that choice that is not measured. Deferred again (owner, 2026-11-03): the effect, one 101 ms firmware call a minute, is likely below a gauge's noise |
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
