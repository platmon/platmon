# History expiry correction validation (2026-10-09)

> **TL;DR**
> - Final validation of runtime `aac7ed7` (2026-10-09) after fixing expired slow-group history points that
>   were kept internally.
> - Default CPU stayed within 2.0 % on both boards; with every optional group on, the Orin exceeded it,
>   and Storage/Wi-Fi call p95 exceeded 2 ms.
> - History retention and memory stayed bounded over the measured windows.

## Reassessment after a reproduced retention defect

The recovery-gap candidate `65bcc39` still retained expired low-frequency points internally.
With retention 600 s, a fake-clock Storage series retained 20, 40 and 60 points at 600, 1200
and 1800 s, although the latter HTTP views showed only 21 points. The shared core-cadence
deque cap was bounded, but slow publishers could keep allocating for hours after the visible
window filled. This invalidates a post-fill memory stability claim for that candidate.

#42 `a52880b77294930aa78c61d13cc2e4390e09e267` removes expired points when their publisher
appends. The cutoff is inclusive: a point exactly at the boundary remains. No HTTP mutation,
new worker, dependency, default, public field or request policy is introduced. Storage, Wi-Fi
and Probe regression cases failed before the two-line correction; focused History/Sampler
tests pass 53/53 and component full tests pass 665 with 3 skips.

The preceding board round is preserved, not extended or relabeled as validating this fix.
The user's requested scoped correctness repair and validation of the new SHA require a new
measurement. This is a reassessment after a failed correctness hypothesis, not a retry to
improve a marginal performance result. The CPU and responsiveness targets are unchanged.

## Fixed validation scope before running

- One new round, at most 60 minutes per board, after the previous runner has finished and
  released its process and port. No overlapping benchmarks or production changes.
- Archive the final integrated correction with its source and runner hashes in a new
  `~/platmon-expiry-rc-<sha>/` directory. Never edit the previous measured checkout.
- Same default/all ABBAABBA: 30 s warmup + 120 s measurement, one stats client at 1 s and
  observations/history at 10 s; default Network/Disk on, other features off.
- Same all-on 30 s warmup + 1800 s retention measurement, retention 600 s. Compare RSS/PSS
  after filling across two more 600 s windows, record ranges/slopes, bounds and identities.
- Default CPU must remain <=2% on each board. Report all-on CPU separately, including any
  miss; require zero recorded failures/drops/fallbacks and no observed collection overruns.
  Sampled timing cannot prove every attempt's duration.
- Reuse only unchanged-path evidence: browser fixtures/661 s soak and independent Disk,
  Storage/Wi-Fi cadence costs. Their optional p95 misses remain failures and defaults stay off.
- After native measurements, validate the same SHA using existing Orin compose files with
  a unique project/image, loopback port, non-root/read-only/cap-drop/no-new-privileges settings.
  The earlier prepared container run is superseded; avoid a redundant old-code build.
- Preserve raw results and rollback material; compare operational identities before/after.
  Actual privileged systemd execution, ARM PSI and external TCP remain unavailable/unapproved.

If this correction still fails a required gate, preserve the failed evidence and Draft status;
do not silently retry or change thresholds. Final CI and measured SHA are recorded with results.

## Results — completed corrected candidate

Measured commit: `aac7ed796faf2be0e387312c04a78925198fe11d`; runtime hash
`a12124188dd1db59`, benchmark hash `49a338fd5f082aa2`. Native suites ran 19:04–19:55 KST
on 2026-10-09 (3041 s Orin / 3043 s Pi), after the previous runner and ports were reclaimed.
The subsequent isolated Orin container check finished before 19:58, within the 60-minute budget.
Source archive SHA-256:
`b77711829d1adf77e3d817fbde72ba76a2f0ce8a3a3a5e719350d2f3d832f6b7`.
Native environments: Orin Python 3.10.12, 5.15.199-tegra, MAXN_SUPER/schedutil; Pi Python 3.13.5,
6.18.50+rpt-rpi-v8, ondemand. PSI remains disabled/unsupported in this matrix; TCP targets only
each candidate's own loopback listener. The optional set is Storage, Wi-Fi, Probe and History.

### CPU and runtime integrity

| CPU (% of one core) | Orin Nano | Raspberry Pi 4 |
| --- | --- | --- |
| default, four measured windows | 1.678, 1.625, 1.600, 1.600 | 1.529, 1.583, 1.583, 1.579 |
| all, four measured windows | 1.818, 1.835, 2.174, 1.860 | 1.900, 1.975, 1.851, 1.858 |
| all-on retention window | 2.078845 | 2.019434 |

All eight **default** windows pass the unchanged <=2% release target. Orin all-on window 6
is **2.174%, above target**, and long all-on CPU exceeds 2% on **both** boards. No window is
excluded or replaced; optional configurations are not labeled passing. All optional defaults stay off.
Storage/Wi-Fi independent call p95 remains 3.212/3.223 ms and 3.365/7.871 ms (Orin/Pi), above 2 ms.
Those reader paths and the separately passing Disk classifier are byte-identical to their measured SHAs.

All 18 native windows ended normally with process/port reclaimed and no forced kill. HTTP errors,
logged core failures/drops, diagnostics and stderr are zero. No overrun was observed. Short-window
maximum core duration: 23.537/14.849 ms; retention: 66.024/69.215 ms. Orin short window 1 missed
2 intermediate sequences; each board's retention poller missed 2. These are sampling gaps, not
logged collection failures, and do not establish full per-attempt duration coverage.

Retention serves 1801 stats / 181 observations / 181 history requests per board, all 200. The
complete saved response audit covers 744/738 group records and 248/246 history responses:
no stale/invalid states, reversed IDs, instance mismatch, bounds violation or series drop. Native
hardware/interface/namespace/power-mode scope is identical across all nine runs on each board.

### Retention and memory

Each board fills 600 s history. Final series counts 41/20, maximum returned points 600, full JSON
557939/230213 bytes. Storage/Wi-Fi/Probe publish independently (final IDs 62/367/184 on both).
Wi-Fi namespace and ifindex agree with core Network. Six threads stay constant throughout retention.

RSS from `smaps_rollup`, using elapsed time from the start of the candidate (warmup included):

| board / interval | RSS first → last (KiB) | min–max (KiB) | fitted KiB/min |
| --- | --- | --- | --- |
| orin / 600–1200 s | 26296 → 26808 | 26296–26808 | 19.880 |
| orin / 1200–1800 s | 26808 → 26808 | 26600–26816 | -1.248 |
| pi / 600–1200 s | 26464 → 26508 | 26464–26508 | 3.172 |
| pi / 1200–1800 s | 26508 → 26520 | 26508–26520 | 0.807 |

Final RSS/PSS is 26812/18526 KiB on Orin and 26520/16969 KiB on Pi. Pi reaches 26520 KiB
at 1280 s and remains there through 1830 s (550 s). Orin's last interval has small 4–8 KiB
steps and a temporary decrease, with equal endpoints at 1200/1800 s. The first post-fill period
still grows, especially on Orin; it is not hidden. The bounded data structures, explicit expiry
regressions and settling in the final period support the finite retention stability check. This
is not proof of an indefinitely flat RSS curve, and small allocator/page variations remain visible.
PSS in the final interval has the same endpoint change: 0 KiB Orin / +12 KiB Pi.

Synthetic 64-series × 3602-point allocation with gaps/recoveries: 21909526 bytes retained,
28179814 peak including a bounded JSON read, 902326 response bytes; within the 32/64 MiB targets.
This allocation test is separate from ARM RSS. The expiry regression additionally proves Storage
internal points remain 21 at 1200/1800 s instead of growing to 40/60 under a 600 s retention.

### Orin compose and support boundaries

Existing deploy/docker/Dockerfile + compose/Jetson override, local socket `unix:///var/run/docker.sock`,
ARM daemon 29.8.1 on the same 5.15.199-tegra kernel. Unique project/network
`platmon-expiry-aac7ed7`, image `platmon:validation-aac7ed7`, loopback port 19871. Container
Python 3.13.16, UID 65534, read-only rootfs and configured host mounts, cap-drop ALL, no-new-privileges; health is healthy.
Stats/status/observations/history return 200 with the same instance; observation schema 2, bad
history points 400, unlisted route 404. Existing local tests cover startup/stale 503 separately.

- Network is the container namespace (`lo`, `eth0`), distinct from the native host namespace.
  It is not host-wide Network or host Wi-Fi; container Wi-Fi is unavailable/not_detected.
- Host-visible CPU/GPU, six temperature readings, three power rails, two fan entries, power mode
  and `nvme0n1` Disk I/O are available. Legacy root `disk` capacity uses the read-only host-root bind.
- Storage sees NVMe partitions and mounts but returns null capacity/partial `io_error`: the grouped
  filesystem's first representative mount is file bind `/usr/sbin/docker-init`, which is not a
  directory. This documented file-bind limitation is preserved, not counted as a normal capacity pass.
- Own-loopback Probe ends ok at 0.603 ms; 2 attempts include **1 startup failure**. Observation workers
  start before frontends, so the listener need not exist for the first attempt. The recorded failure
  is retained; this is not a zero-failure container Probe result or a remote-path benchmark.

Compose down removes only the test project. Its container/network are absent and port 19871 is
closed. The built image is preserved:
`sha256:a3a7b2ee4e19ec5e62bf77aa2bf7d6a753aed46ee00e4e19b95dc1a9d9a2ad2e`.
No registry push. Existing image IDs (12) and platmon tags remain; 35 files in four Orin rollback
directories are unchanged before/after the container check, and all 24 existing manifest checks
match. Pi has no matching rollback directory in the recorded home/workspace/checkout search scope;
no nonexistent archive is claimed as verified. Production checkout/process/configuration remains intact.

Actual systemd DynamicUser execution remains unavailable due to administrator authentication.
Pi `systemd-analyze verify` passes on the unchanged unit. ARM normal PSI and external TCP paths
are unvalidated; hosted CI exercises normal PSI. No production unit, kernel, governor or power change.

### Tests, provenance and disposition

Integrated `python3 -m pytest -v`: 676 passed / 3 skipped (two profiling capabilities, local PSI).
Python 3.9 CI: 678/1; Python 3.13: 677/2, run 37913085542 (PR) / 37913078085 (push), including
Node and normal hosted PSI. Frontend, service/compose files, Disk and Storage/Wi-Fi paths were
hash-compared to their applicable earlier evidence. Chromium 153 fixtures at 1280/390 px and the
unchanged final-page 661 s soak remain valid (305 DOM nodes throughout, no JS errors).

Production before/after SHA, INI, instance, PID/start time or container/image/mounts/restarts, kernel,
governors and power mode match at `85259427f20ba09c9541c52970243fc07645d21d` after native and
container validation. All candidate workers/ports are reclaimed; no deployment, tag or Release.

**Disposition:** eligible for the authorized component/aggregate merges within this measured default
configuration and documented support scope. Optional CPU/p95 misses and unavailable environments
remain explicit limitations; this is not blanket approval of every enabled configuration. Final PR
heads and merged-main CI must still be checked when merging. No further performance round is needed.

Raw remains in `~/platmon-expiry-rc-aac7ed7/` on both boards and locally in
`.validation/expiry-rc-evidence/`; scripts, configs, state, full results and logs are preserved.
Each input manifest has 6 verified entries; native result manifests have 27/25 entries; the
separate container manifest has 12. Full SHA-256:

| board | retention JSON | native result manifest |
| --- | --- | --- |
| orin | `8f706043db3134857e4db8e5b33f3cfbe16a56547b716455305e6887d76c0f32` | `1ff3663a5162e31dc91382c32cc4b8fc8febf06eb11ddce10184adbbd2675e50` |
| pi | `694f0850ab4efda3741ce4d03207464bf74e38069a9327267315243e5cb2b416` | `c4aca538b4aaba6e3bfd1d8243da3d85a4321979dd5eebef35b3234b96ad5b8b` |

Shared input manifest: `3017c708bb7048a3fd7fff536c624d568c5c9dc2c9ea88c2d5533069f34b7594`.
Orin container manifest: `ce32aea88146715bdcdb62cccba7ea29f9aaf907656c637d1e49c2b87c892754`.
