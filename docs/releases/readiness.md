# Release readiness

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

## Container visibility and Pi PSI update (2026-10-09)

Storage now skips file bind mounts when a valid directory on the same filesystem is available.
The owner approved Docker host networking to restore host interfaces and Wi-Fi; configured HTTP
ports also drive the image health check and start-script output. CPU/GPU/sensors/memory/Disk I/O
were already visible; additional filesystems still need explicit directory binds. The complete
comparison and actual-image checks are in [the Docker scope audit](../reference/docker-scope.md).

Pi PSI has been activated with `psi=1`, rebooted, and verified through the three kernel files and
API (`ok`). Orin kernel changes remain deferred. No previous CPU/p95 miss is relabeled as passing.
[PR #48](https://github.com/jwsung91/platmon/pull/48) records the final merge/CI/deployment state;
older checkpoints below preserve their historical configurations and limitations.

## Default enablement policy update (2026-10-09)

After #40–#46 merged at `dc2875428892e99055e4dfe9c9afab0c2f7f1c52`, the owner requested
all monitoring features enabled by default. Network, Disk I/O, Storage, Wi-Fi, PSI, Probe and
History now default to enabled in both code and the shipped INI. Existing explicit off values
remain effective; omitted sections adopt the new defaults. Probe with no configured targets
stays idle without a worker, observation group or connection; targets require a restart to apply.
Unsupported hardware retains its existing unavailable result. No production configuration is changed.

This is an explicit policy choice, not a new performance pass. The previous supported all-on
measurement (PSI excluded, loopback Probe) reached 2.174% of one core in one Orin short window,
and 2.079% / 2.019% on Orin/Pi over retention. Storage/Wi-Fi call p95 still exceeds 2 ms.
The 2% target and raw results remain unchanged; the new exact default configuration has not
been ARM-benchmarked. Historical off-by-default statements below describe their measured revisions.

## Final candidate disposition (2026-10-09)

**Eligible for the authorized aggregate merge within the measured default configuration and
recorded support scope.** #40, #43, #41, #42 and #44 are merged at component main
`e3ff8bbd565fd8d080e104fe7cfac02f6a97a2e8`. The additional benchmark diagnostics, browser validation and these
reports land through [#46](https://github.com/jwsung91/platmon/pull/46), which records the final
merge SHA and merged-main CI. This document is the pre-merge evidence checkpoint, not deployment.
Six validation stages are complete; the seventh closes with that aggregate merge and its main CI.

The exact measured runtime is `aac7ed796faf2be0e387312c04a78925198fe11d`; later reconciliation and
documentation preserve its runtime/benchmark/tests. Two reproduced History defects are fixed:
recovery from one failed core attempt now breaks the graph, and publishers release expired points.
The [final report](../performance/history-expiry-validation.md) includes reproduction, exact hashes,
all CPU windows, RSS/PSS trajectories, actual Orin compose scope and operational preservation.
Earlier reports below are explicitly historical and do not substitute for the final measurements.

| Release evidence | Outcome |
| --- | --- |
| Default whole-process CPU, two ABBA blocks per board | Orin 1.600–1.678%; Pi 1.529–1.583%; every window <=2% |
| Optional whole-process CPU | Orin short window 2.174%; retention Orin 2.079%, Pi 2.019%: **not met**, retained in the report |
| History 600 s retention in a 1830 s run | bounds/IDs/expiry pass; final-period RSS endpoints 26808→26808 KiB Orin, 26508→26520 KiB Pi; finite stability with small variations, no indefinite claim |
| Automated integration | local 676/3; Python 3.9 CI 678/1, Python 3.13 CI 677/2; Node and normal hosted PSI included |
| Actual Chromium final page | 1280/390 px fixtures and 661 s soak; zero JS errors, constant 305-node DOM |
| Native / actual Orin container | API/identity/health/non-root/read-only and cleanup pass within the observed scope |
| Production / rollback | both boards stay `8525942`; identities/settings unchanged; Orin 12 prior images and 35 rollback files preserved |

Network/Disk remain on. Storage/PSI/Wi-Fi/TCP/history remain off. Storage and Wi-Fi call p95 exceed
2 ms; no cost threshold is raised. Container Network is a separate namespace; Storage file-bind
capacity remains null/partial, Wi-Fi is unavailable and one startup loopback Probe failure is recorded.
Host-visible GPU/sensors/Disk and legacy root capacity work in the tested Orin compose configuration.
The API compatibility boundary is explicit: observations schema **2**, nullable Wi-Fi connectivity;
new CLI/web accept v1/v2, while stats/history remain schema 1.

Remaining support limits: actual systemd DynamicUser execution needs administrator authentication;
Pi unit static verification passes. Normal PSI is unsupported on these ARM kernels. External TCP
has no approved target. Sampled duration coverage is not full per-attempt accounting. Pi has no
matching rollback archive in the recorded search scope; its live checkout/configuration is unchanged.
These limits do not imply tests passed in an unavailable environment.

No deployment, tag, GitHub Release, registry push, power/governor/kernel change or rollback deletion.
A later rollout must use a fixed verified merged SHA, retain the current image/configuration/source
and follow the existing backup/rollback procedure below with separate authorization. The monitoring
CPU result is conditional on the tested cadence/scope, not a guarantee under arbitrary request load.

## Historical resumed candidate status (2026-10-09)

**Review candidate; not fully release-ready.** #46 combines #40–#44 and the concurrent #45 follow-up
without modifying main. Exact component HEADs and eight-stage status are in
[remaining-work.md](../development/remaining-work.md). The previous candidate report is preserved as
[historical context](../development/post-integration-validation.md); its pending ARM entries are
superseded by the [new measured results](../performance/archive/resumed-integration-results.md).

The measured service/collector tree is `e919baf72a5c5100dd5468188fcc97b119b287f6`. Subsequent #45
reconciliation changes web age rendering, tests, benchmark diagnostics and docs; its collector,
server, CLI and service entrypoint are identical to the measured tree. Reuse is limited to those
unchanged paths. Final branch HEAD is available on PR #46; documentation commits are not measurement
SHAs. No main merge, production rollout, tag, Release or registry image publication.

### Changes and compatibility

Disk classifications are invalidated on identity changes/read failure and failed sysfs access is
retried. UI uses bounded optional requests, validates return epochs and keeps ages advancing.
Storage verifies the opened directory's device identity before capacity reads. Wi-Fi validates
carrier and ifindex instead of inferring connectivity from wireless update punctuation. History
stores independent observations once, represents missing intervals explicitly and bounds requests,
series and storage. No new runtime dependency, general scheduler or per-request collection.

`/api/stats` and `/api/history` retain schema 1. `/api/observations` becomes **schema 2** because
Wi-Fi `connected` can now be null and has an explicit `connection_state`; see
[the version boundary](../reference/observations.md). New CLI/web handle v1/v2. Legacy root `disk`, 503/no-store
and remote standalone CLI remain intact. Network/Disk stay on; Storage/PSI/Wi-Fi/TCP/history stay off.

### Validation and limits

- `pytest -v`: final reconciled local tree **672 passed / 3 skipped**. Skips: two profiling capability
  checks and unsupported local PSI. CI before reconciliation: Python 3.9 672/1, Python 3.13 671/2
  (run 37898089620), including Node and real hosted-runner PSI. Final exact-HEAD CI is attached to #46;
  raw logs distinguish push HEAD from the PR merge ref.
- Real Chromium after reconciliation: desktop 1280 px / narrow 390 px, logo, escaping, missing fields,
  pressure/Wi-Fi fixture panels, 503, actual 5 s timeout and simulated visibility-return event. This
  is an actual browser with fixtures, not actual hardware PSI or a real OS tab lifecycle test.
- A live container page at measured `e919baf` ran 661 s: 661 stats and 65 each observations/history
  requests, zero JS/page errors, 289 live DOM nodes throughout. Renderer task time 4.405 s and final
  JS heap 2.86 MB are browser metrics, not daemon CPU. This long run predates the final history-age
  repaint; the reconciled page passed bounded repeated-render fixture checks, not another long soak.
- Native legacy INI, explicit Network/Disk off, each optional group and standalone copied CLI passed.
  Enabled TCP without targets is rejected at startup (exit 2), before listener/probe creation.
  Six valid services exited normally and released their isolated ports.
- Existing deploy/docker/Dockerfile was built locally at `e919baf`, then run non-root (UID 65534), read-only,
  cap-drop ALL, no-new-privileges on an isolated loopback port. Core, history and own-loopback TCP
  worked; Storage reported null/io_error on file bind mounts, overlay was excluded, Wi-Fi/PSI were
  unsupported. Namespace scope was inspected; this container is not evidence of whole-host visibility
  or of Orin-specific container hardware support. No production compose/network was changed.
- ARM two-block default/all matrix: default **1.59–1.64% Orin / 1.54–1.60% Pi**, every window ≤2%.
  Storage elapsed p95 **3.212/3.223 ms**, Wi-Fi **3.365/7.871 ms** exceed 2 ms: off remains. The cost
  criterion was not changed. The integrated tool does not count every collection overrun/drop;
  separate Disk reference instrumentation does not remove that combined-path evidence limitation.
- Both boards completed 660 s history retention with unique publication IDs and bounded responses.
  Only about one minute follows the first full window; **long-term RSS plateau remains unverified**.
  Maximum-allocation synthetic memory evidence is separate from real RSS. No budget extension.
- Production before/after checkout, INI, instance and process/container identities matched at
  `8525942`. Private raw/hash manifests, scripts and screenshots are preserved; see the result report.

Open gates: actual DynamicUser/ProtectSystem/ProtectHome systemd execution is blocked by existing
administrator authentication; normal PSI on the ARM kernels is unsupported; remote TCP targets are
not approved. Longer post-fill history and final-page soak need a new bounded validation plan.
These limits keep #46 Draft; successful fixtures are not substituted for missing environments.

The historical installation/rollback procedure below remains a proposal for a separately approved
fixed merged SHA. Preserve existing images, INI, CLI and checkout before any switch. No step was
executed by this resumed task. New installs use defaults above; existing explicit off values remain.

## Historical integration evidence

Status of the features built on top of main `85259427f20b` (#31), checked together on the
`integration/release-candidate` branch before they were merged. All of them are on main now (#32 → #33 →
#34 → #36 → #37 → #38, then #35; main `007a422`); each was merged on its own PR with its tree checked equal to
the PR head and main's CI green after it. Nothing here is released, tagged or deployed: the devices run
`8525942` (#31), see below.

## Contents and defaults

| PR | Feature | Default | API | Docs |
| --- | --- | --- | --- | --- |
| #31 (merged) | Network counters | **on** | `/api/stats` `network` | [network.md](../reference/network.md) |
| #32 | Disk I/O counters | **on** | `/api/stats` `disk_io` | [disk-io.md](../reference/disk-io.md) |
| #33 | Network / Disk I/O in the web page, `platmon` command, `/text` | - | - | [api.md](../reference/api.md#web-page) |
| #34 | Storage capacity; low-frequency groups | off | `/api/observations` (new) | [storage.md](../reference/storage.md), [observations.md](../reference/observations.md) |
| #35 | Pressure stall information | off | `/api/stats` `pressure` | [pressure.md](../reference/pressure.md) |
| #36 | Wi-Fi signal (passive) | off | `/api/observations` `wifi` | [wifi.md](../reference/wifi.md) |
| #37 | TCP connect probe (active) | off, no targets | `/api/observations` `probe` | [probe.md](../reference/probe.md) |
| #38 | Recent history | off | `/api/history` (new) | [history.md](../reference/history.md) |

Merged in that order on 2026-10-09; #35 last, after #38, so its conflicts with the storage to history
features were resolved once (taking the integration branch's resolution, checked against main).

## Compatibility

- `/api/stats` keeps every existing key, unit and type and `schema_version` 1. New keys: `network`,
  `disk_io` (on by default), `pressure` (when enabled), and the matching `collectors` entries. `disk` (root
  filesystem usage) is unchanged and separate from `disk_io`.
- `/api/status`, `/text`, `/` and their status codes, 503 rules and `no-store` are unchanged; `/text`
  shows more lines. New read-only endpoints: `/api/observations` (always 200), `/api/history` (404 while
  history is off). Older clients ignore them; the new page and command handle servers without them
  (nothing shown, not asked again after a 404).
- Config: new sections `[network]`, `[disk_io]` (on), `[pressure]`, `[storage]`, `[wifi]`, `[probe]`,
  `[history]` (off). An existing INI without them gets the defaults; an explicit `enabled = no` is kept;
  unknown keys are still refused.
- The `platmon` command stays one standard-library file; it never imports the collector.

## Cost (integrated, `c2752c1`; later commits change only the display of long mount lists and docs)

The real service (`python3 platmon.py INI`, native) polled like the web page (`/api/stats` every 1 s,
`/api/observations` and `/api/history` every 10 s) by `benchmarks/service_cpu.py` from its own process;
the service's user + system CPU from `/proc/<pid>/stat`; 30 s warmup + 120 s window; order default, all,
all, default.

| board | default (Network + Disk I/O on) | all on (+ storage, Wi-Fi, probe to loopback, PSI, history) | target |
| --- | --- | --- | --- |
| Jetson Orin Nano | 1.583, 1.669 % (mean 1.626) | 1.817, 1.742 % (mean 1.779) | ≤ 2.0 %: met by both |
| Raspberry Pi 4 | 1.567, 1.595 % (mean 1.581) | 1.835, 1.860 % (mean 1.847) | ≤ 2.0 %: met by both |

RSS at the end of the windows 23.8–26.5 MiB in every run. `/api/stats` answered 120–121 times per window
without an error (largest latency 4.3 ms Orin, 5.6 ms Raspberry Pi); with history off `/api/history` is the
expected 404. History was not full in a 150 s run: its bound is 3.5–3.7 MiB at 600 s
([performance/low-frequency.md](../performance/low-frequency.md)). PSI is unsupported on both boards, so the
"all on" runs include its unsupported path only.

Per-feature results and the criteria not met:

| feature | per-feature result | criteria |
| --- | --- | --- |
| Network | +0.160 / +0.175 pp (`31b4bbc`) | met under [budget.md](../performance/budget.md); the earlier 0.1 pp target is recorded as not met |
| Disk I/O | +0.152 / +0.123 pp, call p95 0.94 / 1.18 ms (`e184f8c`) | met |
| Storage | 2.8–3.1 ms per observation every 30 s (≈ 0.01 pp) | call p95 ≤ 2 ms **not met** (3.1–3.4 ms); runs on its own thread; off by default |
| Wi-Fi | 0.6–0.8 ms CPU per 5 s observation, elapsed p95 2.2 / 6.4 ms | call p95 **not met** (waiting on the driver); own thread; off by default |
| PSI | 1.8 µs per collection on the unsupported path (WSL tight loop) | normal path unmeasured on the boards (no PSI there); off by default |
| Probe | 0.3–0.7 ms loopback connect | real targets not approved: unmeasured; off by default |
| History | ≤ 3.7 MiB at its bound | integrated run within 2.0 %; off by default |

## Verified and not verified

Verified: full `pytest` and the web harness on every branch, CI on Python 3.9 and 3.13 (push and
pull_request) for every PR and this branch; Orin Nano and Raspberry Pi 4 natively on loopback test ports
(functional checks of every feature; per-feature cost; the integrated runs above); the web page in a real
browser (Chromium headless shell, 1280 px and 390 px) for #33's panels. Each device run left the boards'
own services (`e1bc82a`) untouched, compared by state records before and after (see the performance
reports for which runs have both). The integrated page with storage, the probe (to loopback) and history on
was also checked in that browser on WSL at 1280 px and 390 px; that check found a filesystem with 45 bind
mounts making the Storage panel and the `STORAGE` line unreadable, fixed in #34 (`efc6e59`: the first mount
point and "+N more").

Not verified: a container deployment of any new feature (the scope differences are documented, not run);
systemd installation of this branch; WSL for the low-frequency groups; PSI's normal path and the probe's
real targets on the boards; the Wi-Fi and pressure panels in a real browser (WSL has neither; harness
only); history filled to its retention on a board.

## Upgrade and rollout (for after approval; not done)

New installs get the defaults above. An existing install keeps its INI: Network and Disk I/O turn on
unless its INI says `enabled = no`; everything else stays off until enabled.

For each device, at a fixed merged SHA (the procedure used for earlier rollouts):

1. Record the running state: SHA, container id and image (Orin) or PID and start time (Raspberry Pi), INI,
   CLI, health.
2. Keep rollback material: tag the running image `platmon:rollback-<old sha>`, and copy the compose files,
   INI, CLI, container inspect output, ROLLBACK.md and SHA256SUMS to `~/platmon-rollback-<old sha>-<time>/`.
3. Build the candidate from `git archive <sha>` locally (`--provenance=false`); never pull or push images.
4. Smoke-test the candidate under its own compose project on `127.0.0.1:19797`. With the current
   `network_mode: host`, a separate project does not isolate listening ports and `ports:` cannot remap
   them. First confirm that 19797 is free. Copy the operational INI to a candidate-only `smoke.ini`,
   preserving its collector settings, and change only its `[http]` listener:

   ```ini
   [http]
   bind = 127.0.0.1
   port = 19797
   ```

   Mount that file read-only at `/opt/platmon/platmon.ini` in the candidate's resolved Compose config,
   replacing the normal INI mount at that target; do not edit the operational INI. Use a distinct
   project name and candidate image tag. Before starting, inspect `docker compose ... config` to
   confirm the candidate image, host networking, candidate INI source and absence of published ports.
   The image health check reads the same INI. Check healthy, `/api/status` ready and `/api/stats` 200
   with `network` and `disk_io`, and compare source ids with the running service. Remove only the
   candidate project, confirm 19797 is released, and confirm the operating service's identity and
   health are unchanged. An intentional bridge-mode test instead needs host networking removed and
   its own port mapping; network namespace source ids then differ from the host. See
   [Docker observation scope](../reference/docker-scope.md#networking).
5. Switch: check out the SHA, tag the candidate as `platmon:local`, recreate only the platmon service;
   on the Raspberry Pi, fast-forward its checkout and restart the process the same way it was started.
6. After: health, `/api/status` `ready`, CPU of the service compared with the integrated numbers above.
   Roll back (ROLLBACK.md) on a failed health check, a non-ready status for more than a minute, or CPU
   well above 2 % of one core.

Rolled out on 2026-10-09: #31 (`8525942`) on the Orin (14:09 KST, container) and the Raspberry Pi
(14:20 KST, native process), with this procedure ([development/remaining-work.md](../development/remaining-work.md)).
#32–#38 are merged but not rolled out.

## Raw data

On each device: `~/platmon-rc-c2752c1/` (`run-1-default.json` … `run-4-default.json`, `ops-before.txt`,
`ops-after.txt`), plus the per-feature directories listed in the performance reports.

| board | run-1 default | run-2 all | run-3 all | run-4 default |
| --- | --- | --- | --- | --- |
| orin | `9f48f3c5…` | `09dd3239…` | `b92c63e7…` | `5ba67ce9…` |
| rpi4 | `088ae0e2…` | `c5763765…` | `dffe1d52…` | `0b58d724…` |
