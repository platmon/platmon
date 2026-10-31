# Release readiness

> **TL;DR**
> - Release gates and evidence per version, newest first (1.7.0 at the top).
> - Each gate lists what was verified (tests, native equivalence, packages, devices, soak) and what was not.
> - Not verified for 1.7.0: its own soak and deployed-unit checks (the 24 h soak ran on `main` at `0ba5e2c`,
>   before the later changes), `hub_check.py`, armhf on a real device under systemd, an apt upgrade to it
>   on a real device.

## 1.7.0 release gate (2026-10-31)

Changes since 1.6.0: the Python server removed, the source installation and the container on the native
server (#129 to #132, #136); the `platmon` command in its own package `platmon-cli`, so `platmon` no longer
depends on python3 (#142, #143; the APT repository accepts the new layout with platmon/platmon.github.io#7,
merged); the server binary named `platmon-server` with a `platmon-native` link (#144); the server's
`--version`, `--help`, `config-check` exit code and web page log (#140); `/text` 500 and a status line on
every HTTP answer (#141); the recorded answers as regression data (#139, ADR 0002); the CLI's cleanup
(#134); `.rpm` packages, built and checked in CI and not among the release files (#147, #148); the Windows
build's service, clocks, GPU, temperatures, slow groups and history (#112 to #127; not released).

A minor release by the owner's choice (docs/development/remaining-work.md, "Python server removal"): the
HTTP API's paths and fields, `platmon.ini` keys and defaults, command options and schema versions are
unchanged. Changed at their edges, as ADR 0002 allows: `config-check` exits 1 for a refused file, `/text`
answers 500 where it closed the connection, a request without a version gets 400 and HTTP/0.9 is not
answered. The server's bytes change (local `native/build.sh` / `build-armhf.sh` of the release tree):
x86_64 `5a4aadf80bbd90ae`, aarch64 `e508086baf6d69e9`, armhf `ef0cd153b112911b`.

Verified for this release:

| check | result |
| --- | --- |
| native equivalence on the release tree (x86_64): fake trees, configurations, runtime, live HTTP | `fixture.sh` 11/11, `config_fixture.py` 68/68, `runtime_fixture.py` 7/7 (57/57 answers; `/text` and the CLI's screen the same), `live_http.py` no difference |
| `pytest`, Rust unit tests | 242 passed, 2 skipped (the `.rpm` builds, without `rpmbuild` on the host); 262/262 |
| `package.py` 1.7.0-1 and -2 (amd64, arm64, armhf, platmon-cli), `SHA256SUMS`, `check_package.py` (amd64, the PC), Lintian 2.117 (Ubuntu 24.04, `--pedantic --fail-on error,warning`) | built, `sha256sum -c` OK, smoke passed, clean |
| on the Jetson Orin Nano (Python 3.10), in a temporary directory beside the production 1.6.0 service | `check_package.py` on `platmon_1.7.0-1_arm64.deb` and platmon-cli: smoke passed; the aarch64 server's `live_http.py`: no difference; `collect` reads the board and the three passive groups |
| CI on `main` (`99d201e`): the dpkg lifecycle, and the service under systemd upgraded from 1.3.0 (amd64, arm64 on Ubuntu 22.04, 24.04, 26.04); armhf under QEMU (Debian 13, Ubuntu 24.04); the `.rpm` lifecycle (Fedora 44, AlmaLinux 9, CentOS Stream 10) | passed |
| `deploy_check.py soak --hours 24` on the Orin, `main` at `0ba5e2c` (the Python server removed) as the candidate unit, 2026-10-30/31 | PASS: 105,960 Hub-like requests, no error, loss or walk problem, no automatic restart; CPU 0.77–0.90 % of a core per hour; settled PSS 10.6 → 11.4 MiB (budget 14), concurrency peaks up to 12.3 MiB; 5 threads, 4 descriptors |

Not verified, and therefore open:

- **The 24 h soak predates most of this release:** #134 to #148 (the binary's commands, the HTTP edges and
  `/text`, the package split, the rename) ran after it, and the release binaries did not run as the
  candidate unit (`deploy_check.py checks` needs the owner's sudo to install them). The Orin check above
  ran them as a user process only.
- **No `hub_check.py`** since 1.4.0.
- **armhf on a real device and its systemd service**, as at 1.6.0.
- **No real device upgrade** from 1.6.0 to 1.7.0 under apt, where `apt upgrade` should also install
  platmon-cli (checked in containers: `apt-get upgrade` keeps `platmon` back until `apt install platmon`).

## 1.6.0 release gate (2026-10-25)

Changes since 1.5.0: the native collectors moved into `native/src/collect/` (#105), the Windows build's
clocks, collector, network, probe and disk counters (#106 to #108, #110, #111; built and
checked in CI, not released), and the armhf package released again (#109; the APT repository accepts it
with platmon/platmon.github.io#6, merged). A minor release by the owner's choice: the HTTP API,
`platmon.ini` keys and defaults, command options, status codes and schema versions are unchanged. The
server's bytes change: x86_64 `efb24bd170ae5575`, aarch64 `bc6b7e8854cf1df0`, armhf `9ed50c864e019973`.

Verified for this release:

| check | result |
| --- | --- |
| native equivalence on the release tree (x86_64): fake trees, configurations, runtime, live HTTP | `fixture.sh` 11/11, `config_fixture.py` 67/67, `runtime_fixture.py` 7/7, `live_http.py` no difference |
| `pytest`, Rust unit tests | 1008 passed, 2 skipped; 30/30 |
| `package.py` 1.6.0-1 (amd64, arm64, armhf), `SHA256SUMS`, `check_package.py` (amd64, the PC), Lintian (Ubuntu 24.04, `--pedantic --fail-on error,warning`) | built, `sha256sum -c` OK, smoke passed, clean |
| `check_package.py` arm64 on the Jetson Orin Nano (Python 3.10), unpacked in a temporary directory | smoke passed; the packaged server is `bc6b7e8854cf1df0` |
| armhf under QEMU (CI, #109): Debian 13 and Ubuntu 24.04 armhf, package smoke and dpkg lifecycle; by hand, Debian 13 armhf from the released `platmon_1.3.0-1_all.deb` to `platmon_1.6.0-1_armhf.deb` | passed: install, config-preserving upgrade and downgrade, disabled and masked state, remove and purge, migration refusal, invalid config recovery |
| deployed form on the Jetson Orin Nano: the release aarch64 binary as the candidate unit beside the production 1.4.0 | `deploy_check.py checks` 9/9 (start and restart ready in 0.1 s, back 5.35 s after SIGKILL, the held port, every stop), as 1.5.0's |
| `deploy_check.py soak --hours 1` on the Orin (the same candidate unit; the owner limited it to one hour): 40 min of Hub-like polling, 480 s away, the reconnect, one round of 4 concurrent pollers, 120 s settle | PASS: 3,701 requests, no error, loss or walk problem; 2,881 replayed collections, longest step 1.0 s, no gap; CPU 0.84 % of a core (1.5.0: 0.88); settled PSS 10.6 MiB (budget 14; 1.5.0: 10.6), round peak PSS 11.4 MiB (46); 5 threads, 4 descriptors, no automatic restart |
Not verified, and therefore open:

- **armhf on a real device and its systemd service:** no armhf board was available (the owner's
  choice to release on CI and QEMU); systemd does not start under qemu-user, so the service start, its
  health check and the sandbox are unchecked on armhf. The release notes say so.
- **No 24 h soak and no `hub_check.py`** at this version, as at 1.5.0: the owner limited the check to an
  hour; raw: the Orin's `~/platmon-release-1.6.0/`.
- **No real device upgrade** from 1.5.0 to 1.6.0 under apt.

After the release, on the Jetson Orin Nano (L4T R36.5.2, Ubuntu 22.04), the production install upgraded
under apt from the APT repository, checked read-only (without sudo) on 2026-10-28:

| check | result |
| --- | --- |
| `apt-get upgrade` by the owner, then a reboot | `platmon` 1.4.0-1 (`all`) → 1.6.0-1 (`arm64`, the native server), from the Python server of 1.4.0's `all` package |
| the service after the reboot | `platmon.service` enabled and active, `NRestarts=0`, no warning in its journal; the packaged binary as its DynamicUser with `ProtectSystem=strict` |
| configuration | `/etc/platmon/platmon.ini` kept: its MD5 is the one dpkg records for the conffile |
| API and clients | `/api/stats`, `/api/status`, `/api/observations`, `/api/history`, `/api/samples` and the web page 200; every group `ok` except PSI, NPU and battery, which the board does not have (`unavailable`); `platmon --once` prints the screen |

Not checked there: the Hub reconnecting, and an upgrade from 1.5.0 (the board went from 1.4.0 directly).

## 1.5.0 release gate (2026-10-24)

Changes since 1.4.0: the native server's OS calls behind `native/src/platform/` and a Windows build in CI
that does not collect (#101), a flaky unit test fixed (#102), the candidate service checks on the native
server alone (#103), the APT source's `Architectures` field in the guides (#99), the package checks' OS tools
installed once in CI (#100) and the Python server's deprecation (removal in a later release). A minor release by the
owner's choice: the HTTP API, `platmon.ini` keys and defaults, command options, status codes and schema
versions are unchanged. The server's bytes change (code moved for the platform boundary, the start line no
longer ends in "(step 3 runtime)"): x86_64 `d7b491c5ce844222`, aarch64 `0acd11a091ffe094`. armhf is still
not released (planned with the Windows work).

Verified for this release:

| check | result |
| --- | --- |
| native equivalence on the release tree (x86_64): fake trees, configurations, runtime, live HTTP | `fixture.sh` 11/11, `config_fixture.py` 67/67, `runtime_fixture.py` 7/7, `live_http.py` no difference |
| `pytest`, Rust unit tests | 1007 passed, 2 skipped; 28/28 (60 suite runs without a failure after #102) |
| `package.py` 1.5.0-1, `SHA256SUMS`, `check_package.py` (amd64, the PC), Lintian (Ubuntu 24.04, `--pedantic --fail-on error,warning`) | built, `sha256sum -c` OK, smoke passed, clean |
| deployed form on the Jetson Orin Nano: the release aarch64 binary as the candidate unit beside the production 1.4.0 | `deploy_check.py checks` 9/9 (start, restart, SIGKILL, held port, every stop) |
| `check_package.py` arm64 on the Jetson Orin Nano (Python 3.10), unpacked in a temporary directory | smoke passed; the packaged server is `0acd11a091ffe094` |
| `deploy_check.py soak --hours 1` on the Orin (the same candidate unit; the owner limited it to one hour): 40 min of Hub-like polling, 480 s away, the reconnect, one round of 4 concurrent pollers, 120 s settle | PASS: 3,701 requests, no error, loss or walk problem; 2,881 replayed collections, longest step 1.0 s, no gap; CPU 0.88 % of a core; settled PSS 10.6 MiB (budget 14; candidate 1's hour 0 was 10.5), round peak PSS 11.7 MiB (46); 5 threads, 4 descriptors, no automatic restart |

Not verified, and therefore open:

- **No 24 h soak and no `hub_check.py`** at this version: the owner limited the check to an hour. The
  memory trend over a day and the released Hub's handling rest on 1.4.0's runs, whose server logic this
  release keeps; raw: the Orin's `~/platmon-release-1.5.0/` (`checks-orin.jsonl`, `soak-orin.jsonl`).
- **No real device upgrade** from 1.4.0 to 1.5.0 under apt; CI's container checks cover the upgrade from
  1.3.0 and the package lifecycle.
- **No CPU or memory comparison with 1.4.0**: the change moves code and adds no work per collection.

## 1.4.0 release gate (2026-10-22)

Changes since 1.3.0: the Debian package runs the static native server (Rust, `native/`) instead of the
Python server, one package per architecture (`_amd64.deb`, `_arm64.deb` in place of `_all.deb`), with a
health check after install and upgrade (#94; the experiments behind it are #92 and #93). A minor
release by the repository's rule: the HTTP API, `platmon.ini` keys and defaults, command options,
status codes and schema versions are unchanged (compared byte for byte or by shape against the Python
service, native/README.md). The package's architecture and server change; armhf hosts get no 1.4.0 and
stay on 1.3.0, and the source installation and the container keep the Python server.

Verified for this release (candidate 1, binaries `027e35b1c9251b9d` x86_64 and `ff97aa9ad4e9dd28`
aarch64, rebuilt byte for byte in CI):

| check | result |
| --- | --- |
| whole-service A/B with 1.3.0 (step 3C, PC, Orin Nano, Raspberry Pi 4) | CPU −27..−38 % (PC), −60..−64 % (Orin), −59..−65 % (Pi); PSS with full buffers −47 % (PC), −61 % (Orin), −75 % (Pi) |
| final comparison of candidate 1 (A B B A, the same hosts) | every candidate run within the absolute PSS budget; serving one backlog to 4 clients at once costs more CPU than 1.3.0 (+33 to +240 % a round) |
| deployed form: hardened unit beside the production one, swap, restart, kill, stop (PC, Orin, Pi) | 11/11 checks on each |
| 24 h under a Hub-like load with a restart (Orin, Pi) | PASS: no error or loss, no automatic restart; Orin settled PSS 10.5 → 11.3 MiB (under its +10 % limit), Pi flat |
| the released Hub 1.1.1 through agent restart, SIGKILL, Hub down 60 s, agents down 30 s (PC, Orin, Pi) | PASS, the same records as with 1.3.0 |
| package: Lintian, reproducible builds, dpkg lifecycle, upgrade from the released `1.3.0-1_all.deb` under a booted systemd (failed start, downgrade recovery, disabled service, refused INI, restart) | CI on amd64 and arm64 runners, Ubuntu 20.04–26.04 |

Not verified, and therefore the gates still open before this draft is published:

- **APT repository:** `apt/tools/update.sh` accepts per-architecture releases only with
  platmon/platmon.github.io#4, which must be merged before the `apt` workflow adds 1.4.0.
- **No real device upgrade or reboot:** the upgrade and boot ran in containers; the plan is one Orin Nano
  first (apt upgrade, reboot, `/api/status`, the Hub reconnecting), the others after.
- **Long memory trend:** the Orin's settled PSS rose about 0.7 MiB in 24 h. An accelerated soak
  (2026-10-22/23, `deploy_check.py soak --accel`, 20 cycles of 18 min, each a reconnect after 480 or
  720 s away and 3 rounds of 4 concurrent pollers, a restart at cycle 10; raw
  `~/.local/state/platmon-measure/native-20261023-accel/`) put about 60 such rounds into 6 h:
  - Orin: settled PSS 11.33 → 11.92 MiB over cycles 1–9 and 11.14 → 11.76 MiB over cycles 11–19, about
    25–30 KiB per round, the same per round as the 24 h soak (0.7 MiB, 24 rounds with 40 minutes of
    polling an hour). So it grows with the backlog work, not with time or steady polling, and did not level
    off within 6 h. The tool's report says FAIL: its limit is cycle 0 + 10 %, and cycle 0 here is read
    before the buffers fill (541 records against about 1,194 in later cycles); against cycle 1 every
    cycle is within 10 %. Highest settled 11.9 MiB (budget 14), highest concurrent peak 13.0 MiB (46). No
    error, loss or automatic restart.
  - Pi: 6.16 → 6.61 and 6.30 → 6.51 MiB; PASS.
  - Meaning for a release: a Hub that stays connected polls small pages and rarely makes a backlog round,
    so the growth there should be small; it shows when Hubs reconnect after downtime, at about 1 MiB per
    35 to 40 rounds on the Orin. The canary's PSS over its first days is the next evidence; going back
    is `apt install --allow-downgrades platmon=1.3.0-1`.
  - Cause (2026-10-23, branch `diag/alloc-growth`: a build counting the bytes the program holds, and
    `growth_probe.py`, 60 backlog rounds of 4 clients against a local server): on the PC and the Orin,
    Anonymous grew about 2.2 MiB while the live bytes stayed within 10 KiB (PC 14.83 MB, Orin 8.37 MB).
    It is musl's allocator keeping freed pages, not data the server keeps: no leak. The settled reading
    cannot pass the process's high-water mark, which is the concurrent rounds' peak (VmHWM in the
    accelerated soak: Orin 40.6 to 40.9 MiB, Pi 22.0 to 22.2 MiB, within the 46 and 27 MiB peak
    budgets), though that peak also crept about 0.3 MiB over 27 rounds. Raw:
    `~/.local/state/platmon-measure/native-20261023-growth/`.
- **Known difference:** a request line with `HTTP/2.0` or an invalid version gets the body without a
  status line, as from CPython 3.13.5 and 3.14.4 (newer 3.13 releases answer with a status line).
- **Not covered:** nvidia-smi under a native Linux NVIDIA driver (verified under WSL2 and fake trees).

## 1.3.0 release gate (2026-10-17)

Changes since 1.2.1: full-publication replay (#77), bounded HTTP connections and stalled writers
with isolated probe errors and expiring storage warnings (#79), battery and Wi-Fi levels (#81, #82),
Ubuntu LTS and Python CI qualification (#80), Debian package policy (#76) and the source tree grouped
by responsibility (#75). A minor release by the repository's rule: `/api/samples` and
`/api/observation-samples` are new endpoints, `[replay]` is a new config section,
`[thresholds] battery` and `wifi` are new keys and `/api/status` `levels` gains two entries; no
endpoint, key, option, status code or schema version is removed or changed. #75 is marked breaking
for source checkouts only: their deployment and packaging scripts move, while installed commands,
service names and configuration paths stay. The versioning contract covers the HTTP API,
`platmon.ini` keys and command options, so the owner chose 1.3.0 over 2.0.0, with the moves listed in
the changelog.

Verified for this release:

| check | result |
| --- | --- |
| `pytest` at the release commit, locally (Python 3.14.4) | 989 passed, 2 skipped |
| CI on merged `main` | pass at #81, #82 and #83 (`5c761b0`, the release base) |
| `scripts/packaging/package.py` then `scripts/packaging/check_package.py` | `platmon_1.3.0-1_all.deb` smoke pass (server, CLI, API, web and SVG) |
| `SHA256SUMS` of the built files | `sha256sum -c` OK for the `.deb`, the archive and `BUILD.json` |
| dpkg lifecycle, `1.3.0-1` → `1.3.0-2`, locally in disposable containers | pass on `ubuntu:22.04`, `ubuntu:24.04` and `ubuntu:26.04`: install, config-preserving upgrade and downgrade, disabled/masked state, remove/purge, migration refusal and invalid-config recovery |
| the fleet server's contract (`platmon-hub` `main` at `17b8429`), against this release's source on an x86_64 PC | status, stats and observations accepted with no core or optional error; replay captured for core, storage, Wi-Fi, battery and probe with no losses and no rejected page; schema 7 history healthy with no invalid record; battery and Wi-Fi alert inputs present. Agent restart handling (new instance, cursor reset) was checked the same day against Hub `a48232a` and Agent `54a7885`, which already contained replay |

Not verified, and therefore the gates still open before this draft is published:

- **Lintian was not run locally** (not installed on the release host). CI runs it for both packaging
  revisions on this change; its result is the evidence.
- **No real systemd service run of 1.3.0.** The lifecycle check does not boot systemd; the x86 PC was
  not upgraded to the package for this release.
- **No ARM device acceptance**, as at 1.1.0 and 1.2.0: Orin Nano and Raspberry Pi startup, sandboxing
  and readings are unverified at this version.
- **Replay memory on boards is unmeasured.** The default 16 MiB budget counts encoded payloads, not
  process RSS; its resident cost and CPU on an ARM board have not been measured.

Publishing the draft therefore needs, at minimum, the CI Lintian result and the owner's decision on the
open ARM and service gates. The draft release and its artifacts are the review point, not a release
decision.

### After publication (2026-10-17)

CI, Lintian included, passed on the tagged commit, and the owner published 1.3.0 with the ARM gates
open. The same day the x86_64 PC (Ubuntu 26.04) was upgraded by the owner from
`1.2.0-2` to the published `platmon_1.3.0-1_all.deb`, which closes the service gate for x86 only:

| check | result |
| --- | --- |
| installed version | `dpkg-query` `1.3.0-1`; `platmon --version` and the server both report 1.3.0; `dpkg -V platmon` clean |
| service | restarted by the upgrade, `active`, `NRestarts=0`, `ExecMainStatus=0`; no journal entry at warning or above |
| sandbox | `DynamicUser=yes`, `User=platmon`, `ProtectSystem=strict`, `NoNewPrivileges=yes` |
| configuration | the unedited 1.2.0-2 `platmon.ini` was replaced by the 1.3.0 default (adds `[replay]` and the battery and Wi-Fi levels) with no `.dpkg-old` or `.dpkg-dist` left; preserving an *edited* file was not exercised here and rests on the container lifecycle check |
| API | `/api/status` ready, `levels` includes `battery` and `wifi`; `/api/samples` 200 (404 before the upgrade); `/api/observation-samples` 200 for storage, Wi-Fi and battery, 404 for probe (no target configured, as documented) |

Replay memory on this PC, sampled every minute from the restart with the default `[replay]` settings
(600 s, 16 MiB) and one core publication per second:

| minutes after start | RSS | cgroup `MemoryCurrent` | replay records |
| --- | --- | --- | --- |
| 0.7 | 35.6 MiB | 24.5 MiB | 40 |
| 5 | 42.3 MiB | 31.3 MiB | 340 |
| 9 | 48.5 MiB | 37.5 MiB | 581 |
| 10 | 49.1 MiB | 36.8 MiB | 600 (full) |
| 11 | 49.1 MiB | 38.0 MiB | 600, IDs 102–701 |

RSS grew about 1.5 MiB a minute while the buffer filled and stopped growing once it held 600 s, so
the default buffer costs about 13.5 MiB of resident memory on x86_64. The whole service used 9 s of
CPU in 11 min 46 s (about 1.3 % of one core), collection included; replay was not isolated. This is
one PC over 12 minutes: long runs and every ARM board remain unmeasured, and the ARM acceptance
gate stays open.

The journal also showed one storage line below warning level, `permission_denied` for a removable
disk mounted under `/run/media/<user>`, most likely because the sandboxed service user cannot read
that mount.

### ARM acceptance (2026-10-18)

The owner upgraded both boards to the published `platmon_1.3.0-1_all.deb` (SHA-256 checked against the
release's `SHA256SUMS`) with `apt-get install -o Dpkg::Options::=--force-confnew`. Both boards' INIs were
comment-free files with every value at its default, written on 2026-10-10; they were backed up and
replaced by the 1.3.0 default, the old file kept as `.dpkg-old`.

| check | Jetson Orin Nano devkit (Super) | Raspberry Pi 4 Model B |
| --- | --- | --- |
| system | L4T R36.5.2, Ubuntu 22.04.5, MAXN_SUPER, `schedutil`, Python 3.10.12 | Debian 13, 6.18 rpi-v8, `ondemand`, Python 3.13.5 |
| upgrade | `1.2.1-2` → `1.3.0-1` | `1.0.0-5` → `1.3.0-1` |
| installed files | `dpkg -V`: only `/usr/share/man/man1/platmon.1.gz` missing, because the image's `/etc/dpkg/dpkg.cfg.d` excludes `/usr/share/man/*` for every package | `dpkg -V` clean |
| service | `active`, `enabled`, `NRestarts=0`, `ExecMainStatus=0`; no journal entry at warning or above; the server logs version 1.3.0 and board module `collector.jetson` | the same; board module none, as at 1.0.0 |
| sandbox | `DynamicUser=yes`, `User=platmon`, `ProtectSystem=strict`, `NoNewPrivileges=yes` | the same |
| API | `/api/samples` 200 (404 before); `/api/observation-samples?group=storage` 200 | the same |
| fleet server | registered in `platmon-hub` 1.1.0 (raw capture on) on the x86 PC: Live, every core publication from ID 1 captured, no replay loss | the same |

Replay memory, sampled every minute from the restart, default `[replay]` (600 s, 16 MiB), with the
fleet server polling the service. The Pi kernel runs with `cgroup_disable=memory`, so it has no cgroup
figure.

| minutes after start | Orin RSS | Orin cgroup `MemoryCurrent` | Pi RSS | replay records (Orin / Pi) |
| --- | --- | --- | --- | --- |
| 1.6–2.2 | 29.7 MiB | 23.6 MiB | 32.2 MiB | 134 / 94 |
| 5.3 | 35.2 MiB | 28.4 MiB | 33.8 MiB | 318 / 277 |
| 10.4 / 9.7 | 37.5 MiB | 31.6 MiB | 35.4 MiB | 600 (full) / 584 |
| 13.5 / 12.8 | 37.5 MiB | 31.6 MiB | 35.6 MiB | 600 / 600 |
| 17.5 / 16.9 | 37.5 MiB | 31.7 MiB | 35.6 MiB | 600 / 600 |

RSS stopped growing once the buffer held 600 s: the default buffer costs about 12.6 MiB on the Orin
(from 24.9 MiB at 54 s) and about 4 MiB on the Pi, in proportion to the core publication size (about
10.3 KB on the Orin and 5.3 KB on the Pi against 19.4 KB on the x86 PC). Over the last 6–7 minutes RSS
moved by +12 KiB (Orin) and +100 KiB (Pi). The fleet server's capture lag over ten minutes was p50
2.6 s and p95 5.0 s on both boards, its 5 s replay poll.

CPU, measured as in the [budget](../performance/budget.md): `v1.3.0` (`e767176`) from `git archive`,
natively on loopback port 19797 next to the boards' own services (which the fleet server kept
polling), the shipped INI with only `[http]` bind/port changed (A) and additionally
`[replay] enabled = no` (B), `benchmarks/service_cpu.py` with 30 s warmup + 120 s per window, one
client polling `/api/stats` every second. Both boards ran at the same time, 15:08–15:18 KST.

| board | 1: A (replay on) | 2: B (off) | 3: B (off) | 4: A (on) | replay (A − B means) | release target |
| --- | --- | --- | --- | --- | --- | --- |
| Orin Nano | 1.884 % | 1.769 % | 1.767 % | 1.875 % | +0.11 pp | every window ≤ 2.5 % |
| Raspberry Pi 4 | 2.108 % | 1.800 % | 1.942 % | 2.067 % | +0.22 pp | every window ≤ 2.5 % |

Under the previous 2.0 % target the Pi's replay-on windows would fail; the owner raised the target to
2.5 % the same day ([budget](../performance/budget.md#release-target-raised-to-25--2026-10-18)). Replay
adds about 2.2 ms per collection on the Pi, at the per-feature review criterion of about 2 ms. All 963
`/api/stats` requests answered 200; no collection failure, overrun or missing sample; collection took
at most 22 ms (Orin) and 16 ms (Pi); every window exited normally and released its port. These windows
have no replay reader: the fleet server's `/api/samples` reads add to this, and the boards' services
used 2.42 % (Orin) and 2.64 % (Pi) over the 15 minutes they were polled by it.

This closes the ARM service and replay-memory gates for 1.3.0. Still open: replay memory over hours,
and the fleet server's 24-hour compression ratio on these boards (its ADR-0006).

## 1.2.0 release gate (2026-10-11)

Changes since 1.1.0: batteries and external power supplies as the low-frequency group `battery` (#68),
the Temperature / Power & battery view split (#69), a thermal zone reported once instead of twice (#67)
and a request deadline on the HTTP frontend (#67). A minor release by the repository's rule: the
additions are a new `/api/observations` group within schema **2**, a new `[battery]` config section and
a new view, all compatible. `/api/stats` keeps every documented key, unit and type and
`schema_version` 1; what changes there is the membership of `temperature`, which the API already
declines to promise (a channel that cannot be read is left out). No endpoint, status code, 503 rule,
`no-store` header or packaging layout changes.

Verified for this release:

| check | result |
| --- | --- |
| `pytest` at the release commit, locally | 897 passed, 2 skipped (the Python 3.14 profile modes) |
| CI on Python 3.9 and 3.13, plus the package job | pass on #67, #68, #69, #70 and on merged `main` at each |
| `scripts/packaging/package.py` then `scripts/packaging/check_package.py` | `platmon_1.2.0-1_all.deb` smoke pass (server, CLI, API, web, SVG) |
| `SHA256SUMS` of the built files | `sha256sum -c` OK for the `.deb`, the archive and `BUILD.json` |
| dpkg install, upgrade and downgrade | the CI package job (`scripts/packaging/check_package_install.py`) on every change above |
| x86_64 PC (Ubuntu 26.04, kernel 7.0, 22 cores, 28 temperature channels after #67, NVMe, Intel Wi-Fi, one battery) | the package installed and restarted as the real systemd service over four revisions (1.1.0-1 → -2 → -3 → -4) with the config replaced without a prompt; `/api/observations` served the `battery` group `ok` with `duration_ms` 101.8 on its own thread, and the core collection unchanged |
| the battery group's cost, on that PC | one reading 101.20 ms elapsed and 0.594 ms thread CPU, behind the ACPI driver's 1000 ms cache; at the 60 s default about 0.001 pp of one core, with the core cycle untouched ([budget](../performance/budget.md)) |
| both viewers on that PC | the owner confirmed the battery in the `platmon` command and, after the polling fix (#70), in the web page; the page in this commit is byte-identical to the one they checked |
| the fleet server's contract (`platmon-hub`) | its own `protocol.observations()` accepts the new group, its alert rule reports no optional-endpoint error, and `/api/observations` is 4.2 kB against its 512 kB bound |

Not verified, and therefore the gates still open before this draft is published:

- **No ARM device acceptance for 1.2.0.** As at 1.1.0, neither an Orin Nano nor a Raspberry Pi 4 was
  available. The service's sandboxing, startup and the boards' own readings are unverified at this
  version, and the 1.0.0 lifecycle evidence below does not carry over.
- **No ARM CPU measurement with the battery group.** On a board with no power supply class the group
  reports `unavailable` and its thread does one listing per interval, so the added cost should be
  negligible there — but that is reasoning, not a measurement, and the 2.0 % target was last measured
  per board at 1.0.0.
- **No board with power rails ran the new Power & battery view.** `VDD_IN`, the fans and `MAXN_SUPER`
  are covered by fixtures and by the x86 PC's empty rail set, not by a Jetson.
- **The power cost of reading a battery is unmeasured.** It is the reason the default interval is 60 s
  rather than the 10 s floor, and the one number behind that choice with no measurement under it.
- **The power rails and the battery have no history series**, so neither appears in `/api/history` or in
  the Power & battery view's Recent history panel, which the view therefore does not have. Recorded in
  [remaining work](../development/remaining-work.md); not a regression, and not closed by this release.

Publishing the draft therefore needs the ARM package acceptance and a CPU window per board, recorded
here as 1.0.0's were. The draft release and its artifacts are the review point, not a release decision.

## 1.1.0 release gate (2026-10-11)

Changes since 1.0.0: generic GPU and NPU collectors (#63), the web page's history sections (#62), the
live view's six views with scrolling (#64, #65) and five fixes found by running 1.0.0 on a PC (#61).
`/api/stats` gains `npu` within `schema_version` 1; no `platmon.ini` key, endpoint or packaging layout
changes, and `--view` is the only new command option.

Verified for this release:

| check | result |
| --- | --- |
| `pytest` at the release commit, locally | 859 passed, 2 skipped (the Python 3.14 profile modes) |
| CI on Python 3.9 and 3.13, plus the package job | pass on #61, #62, #63, #64, #65 and this change |
| `scripts/packaging/package.py` then `scripts/packaging/check_package.py` | `platmon_1.1.0-1_all.deb` smoke pass (server, CLI, API, web, SVG) |
| `SHA256SUMS` of the built files | `sha256sum -c` OK for the `.deb`, the archive and `BUILD.json` |
| dpkg lifecycle, 1.0.0-1 → 1.1.0-1 in a disposable Ubuntu 24.04 container | install, config-preserving upgrade **and downgrade**, disabled/masked state, remove/purge, source-unit refusal and invalid-config recovery all pass |
| x86_64 PC (Ubuntu 26.04, 22 cores, 38 thermal zones, 4 interfaces) | the new live view driven through a pty against the installed 1.0.0 service: six views, every switching and scrolling key, resize 30–119 columns, `NO_COLOR`/`PLATMON_ASCII`, the offline banner over cached data, `q` restoring the terminal |
| GPU and NPU readers (#63), on that PC | Intel Arc (`i915`) 4.7 % idle / 43.7 % under a VA-API load; Intel AI Boost (`intel_vpu`) 0 % idle with a 1400 MHz ceiling; service CPU 0.83 % of one core, measured against 0.96 % before |

Not verified, and therefore the gates still open before this draft is published:

- **No ARM device acceptance for 1.1.0.** The 1.0.0 package was installed, run as the real systemd
  service and purged on an Orin Nano and a Raspberry Pi 4 (below); none of that was repeated for this
  version, because neither board was available to the change. The service's sandboxing, startup and the
  boards' own readings are unverified at 1.1.0.
- **No ARM CPU re-measurement.** #63 adds two collectors that run on every collection. Their cost was
  measured only on the PC above, where the whole process fell from 0.96 % to 0.83 % of one core. The
  2.0 % target was last measured on the boards at 1.0.0, before those collectors existed, so the ARM
  result does not carry over. A Jetson keeps its own GPU source and has no accel class, so its added
  path is a `not_detected` check rather than a reader, but that is reasoning, not a measurement.
- The new live view has not been run on a Jetson or a Raspberry Pi: its Jetson-specific rows (L4T,
  power rails, `MAXN_SUPER`, the 85/95 °C levels) are covered by fixtures only.
- The web page's history sections (#62) were checked in the harness and a real Chromium during #62, not
  again at this release commit.

Publishing the draft therefore needs the ARM package acceptance and a CPU window per board, recorded
here as 1.0.0's were. The draft release and its artifacts are the review point, not a release decision.

## 1.0.0 release gate (2026-10-10)

The default configuration since the enablement policy (every feature on; Probe idle without targets;
PSI on where the kernel provides it, now including the Raspberry Pi) was measured on both boards against
the unchanged target: the whole process at most 2.0 % of one core.

- Runtime: `9617806b9945dfe5acc2b3e7aceb6f75c0f211ac` (main after #59), from `git archive`, natively on a
  loopback test port next to the boards' own services. The INI is the shipped `platmon.ini` with only
  `[http]` bind/port changed (sha256 `a7ae2251538e…`, same on both boards).
- Method: `benchmarks/service_cpu.py`, 4 windows per board, each 30 s warmup + 120 s measurement, one
  client polling `/api/stats` every second. Both boards ran at the same time, 13:31–13:41 KST.
- The 1.0.0 release commit adds the web history label fix, the version and documentation on top of this
  runtime. Neither changes collection or `/api/stats`.

| board | window 1 | window 2 | window 3 | window 4 | result |
| --- | --- | --- | --- | --- | --- |
| Orin Nano | 1.742 % | 1.650 % | 1.702 % | 1.867 % | every window <= 2.0 % |
| Raspberry Pi 4 | 1.802 % | 1.579 % | 1.675 % | 1.675 % | every window <= 2.0 % |

All 963 `/api/stats` requests answered 200; no collection failure was logged; every window exited
normally (no forced kill) and released its port. RSS at the end of a window: 22.8–29.0 MiB (Orin),
25.5–28.1 MiB (Pi). The production services on port 9797 kept answering throughout.

This gate covers the default configuration only. The earlier optional all-on results above 2 % (with
configured Probe targets and history retention filled) stay as recorded below, and Storage/Wi-Fi call
p95 above 2 ms is unchanged. Raw files: `~/platmon-gate-1.0-9617806/default-{1..4}.json` on each board
(sha256 prefixes Orin `3db58330 863cd659 a23036ae 3bf2e287`, Pi `3f297379 207c587f f392f202 d01f2366`).

### Package acceptance on the devices (2026-10-10)

`platmon_1.0.0-1_all.deb`, built from the 1.0.0 release branch (head `1f97497`), installed with APT on both
boards and run as the real systemd service, then purged. The service got a loopback test INI on port
19797, placed before the install and kept by dpkg. An `/etc/platmon` left by an earlier source install on
the Orin was moved aside and restored unchanged. The package's preinst refuses to install next to a
running Docker platmon, as documented, so the Orin's Docker service was stopped for the test (about one
minute) and the same container was started again.

| check | Orin Nano (Ubuntu 22.04, Python 3.10) | Raspberry Pi 4 (Debian 13, Python 3.13) |
| --- | --- | --- |
| service active and enabled | pass | pass |
| DynamicUser (not root), NoNewPrivileges | pass | pass |
| `/api/status` ready, `/api/stats`, observations, history, web page `v1.0.0` | pass | pass |
| `platmon --version` = `platmon 1.0.0` | pass | pass |
| readings as the sandboxed user | 6 temperatures, 3 power rails, 2 fans, GPU, `MAXN_SUPER`, CPU frequency; PSI unavailable (kernel) | CPU temperature, PSI; no GPU/power/fans (board) |
| `levels.temperature` | `[85.0, 95.0]` | `[75.0, 80.0]` |
| purge: package gone, port released, prior state back | pass | pass |

Both logs end in `RESULT: PASS` (`~/platmon-acceptance-1.0.0/acceptance.log` on each board). The first Orin
attempt stopped at the preinst check before anything was installed; it left only a not-installed dpkg
entry, which the passing run removed. The Pi had rebooted at 14:47 for an unrelated reason; its
hand-started platmon (no autostart) was down before the test and was restarted afterwards.

## Earlier records

The pre-1.0.0 candidate records (2026-10-09: container visibility, the default enablement policy,
the final and resumed candidates and their integration evidence) are in this file as of `b96aba8`.
