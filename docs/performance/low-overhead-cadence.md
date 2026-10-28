# Low-overhead measurement and sampling cadence

> **TL;DR**
> - What the service costs per collection interval (2026-10-08), and what passive Network / Disk / PSI
>   would add.
> - Neither 1 s nor 2 s collection met the 1 % budget then in force; collection and client polling are
>   separate cost knobs.
> - Recommended next: break the 1 s cost down rather than observe less.

A measurement study, not a feature: what the current platmon service costs per collection interval, what
a minimal passive Network / Disk / PSI collection would add, and which interval the numbers support.
Nothing here changes the product, its API or its defaults. The passive collection exists only as a
bench prototype (`benchmarks/passive.py`); its output appears only as `_bench_passive` in the bench
server's answers.

## Results (2026-10-08)

Baseline: `dabb89f` (product code unchanged on the bench branch). Bench tool: the branch's first commit,
`a6288e3` (copied to its own directory on each device with `git archive`). Every number below is a
measured value unless marked as computed; CPU % is of **one core**. Raw per-run records (`results.jsonl`)
are kept outside the repository; their SHA-256 (first 16 hex digits): orin `23c5f184c2ece9c5`, rpi4
`8abca757e1ad788c`, wsl `41f26b3c256ac513`. The tables were re-aggregated from those files with the
report code of the bench branch's later commits; the measurement code is still `a6288e3`. The re-check
found no timeout, pending probe call, skipped tick, dropped record, clock jump or condition mismatch,
and the CPU % recomputed from the raw rusage and elapsed time matches the recorded values.

### Environment (anonymised)

| alias | hardware | OS / kernel | Python | CPUs / RAM | clock policy | storage seen | interfaces (physical) | PSI | Wi-Fi | sensors (temp/power/fan) | run as |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| orin | Jetson Orin Nano devkit (Super), L4T R36.5.2 | Ubuntu 22.04, 5.15-tegra, aarch64 | 3.10.12, 64-bit | 6 / 7.4 GiB | nvpmodel MAXN_SUPER (mode 2), `schedutil`, CPUs at 729.6 MHz between runs | NVMe root; 7 block devices counted (NVMe + 6 zram) | 10 (5) incl. docker bridge, veth | not available | connected, `/proc/net/wireless` | 6 / 3 / 2 | native host process |
| rpi4 | Raspberry Pi 4 Model B Rev 1.2 | Debian 13, 6.18 rpi-v8, aarch64, 64-bit userland | 3.13.5, 64-bit | 4 / 3.7 GiB | `ondemand`, 1.5 GHz between runs | SD card root (mmc); 2 block devices counted (mmc + zram) | 3 (2) | not available | connected, `/proc/net/wireless` | 1 / 0 / 0 | native host process |
| wsl (supplementary) | x86-64 PC, WSL 2 | Ubuntu 24.04, 6.6 WSL2 kernel | 3.12.3, 64-bit | 20 / 15.5 GiB (VM view) | not exposed | virtual disks (repo on the Linux filesystem) | 7 (1), virtual NIC | not available | none | none exposed | WSL 2 guest |

Both ARM devices kept running their own platmon service during the study (Orin: the container on port
9797; Raspberry Pi: a native process on 9797), as on a real deployment; it was not touched (state before
and after: same container/PID, start time, restart count 0, healthy, checkout SHA, INI hash, port
answering). Other background load was idle (load average about 0.0-0.1). No run was invalid or skipped;
the observed scope (interfaces, block devices, sensors, collector states) was the same in every run of a
device. Temperatures between runs: Orin 50.0-52.9 °C, Raspberry Pi 46.3-48.7 °C; `vcgencmd
get_throttled` was `0x0` before and after every Raspberry Pi run. Budget used: Orin 30 windows / 78.5
min, Raspberry Pi 30 windows / 78.6 min (of 32 / 90).

### Table 1 — core cost per interval (1 client polling every 1 s)

B0 → B1 → B1 → B0 per interval; "final" is one extra 300 s window per variant. "Pooled" p95: nearest
rank over the samples of all valid runs of that row taken together (sample and run counts given).
"Max of per-run p95": each run's own p95, the largest of them; a different, more conservative number,
not a pooled p95. Passive: the callback's elapsed / thread-CPU time. Attempt: elapsed time of
`Sampler._attempt`. RSS: end of window, B1 minus B0 means. 2nd-half growth: RSS change over the second
half of the window, largest run.

| platform | interval s | B0 CPU % | B1 CPU % | B1-B0 pp | repeat spread pp | B1 passive p95 ms, pooled: elapsed / CPU (samples, runs) | B1 passive, max of per-run p95 ms | attempt p95 ms, pooled B0 / B1 | RSS B1-B0 MiB | 2nd-half growth KiB B0 / B1 | failed / overrun | actual interval p50 / max ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| orin | 0.5 | 3.20, 3.20 | 3.51, 3.59 | +0.350 | 0.072 | 1.33 / 1.32 (480, 2 runs) | 1.34 | 20.2 / 24.0 | -0.45 | 1332 / 4 | 0 / 0 | 500.2 / 500.8 |
| orin | 1 | 1.75, 1.74 | 1.93, 1.92 | +0.178 | 0.010 | 1.40 / 1.39 (241, 2 runs) | 1.42 | 20.3 / 22.1 | +0.88 | 8 / 0 | 0 / 0 | 1000.2 / 1000.4 |
| orin | 2 | 0.99, 1.01 | 1.14, 1.13 | +0.144 | 0.020 | 1.44 / 1.44 (120, 2 runs) | 1.46 | 20.4 / 22.6 | -0.70 | 444 / 8 | 0 / 0 | 2000.2 / 2000.4 |
| orin | 2 (final, 300 s) | 1.00 | 1.12 | +0.121 | single pair | 1.43 / 1.41 (150, 1 run) | 1.43 | 20.3 / 22.9 | +1.18 | 0 / 0 | 0 / 0 | 2000.2 / 2000.4 |
| orin | 5 | 0.55, 0.55 | 0.63, 0.61 | +0.070 | 0.028 | 1.46 / 1.45 (48, 2 runs, exploratory) | 1.46 | 20.7 / 22.9 | -0.20 | 1444 / 4 | 0 / 0 | 5000.2 / 5000.5 |
| rpi4 | 0.5 | 2.29, 2.15 | 2.72, 2.76 | +0.523 | 0.138 | 1.63 / 1.63 (480, 2 runs) | 1.63 | 9.0 / 11.2 | +0.01 | 16 / 8 | 0 / 0 | 500.2 / 500.4 |
| rpi4 | 1 | 1.39, 1.40 | 1.68, 1.61 | +0.250 | 0.069 | 1.64 / 1.64 (240, 2 runs) | 1.64 | 9.0 / 11.1 | +0.05 | 16 / 8 | 0 / 0 | 1000.2 / 1000.3 |
| rpi4 | 2 | 0.91, 0.92 | 1.12, 1.11 | +0.202 | 0.015 | 1.65 / 1.64 (120, 2 runs) | 1.65 | 9.3 / 11.2 | +0.04 | 12 / 12 | 0 / 0 | 2000.2 / 2000.4 |
| rpi4 | 2 (final, 300 s) | 0.90 | 1.11 | +0.208 | single pair | 1.64 / 1.64 (150, 1 run) | 1.64 | 8.9 / 11.5 | +0.02 | 20 / 12 | 0 / 0 | 2000.2 / 2000.3 |
| rpi4 | 5 | 0.67, 0.65 | 0.73, 0.79 | +0.100 | 0.052 | 1.90 / 1.74 (48, 2 runs, exploratory) | 1.90 | 9.6 / 11.7 | +0.04 | 12 / 16 | 0 / 0 | 5000.2 / 5000.3 |
| wsl | 0.5 | 0.67, 0.73 | 0.99, 0.82 | +0.207 | 0.169 | 1.00 / 0.99 (480, 2 runs) | 1.07 | 3.8 / 4.9 | +0.12 | 0 / 0 | 0 / 0 | 500.2 / 503.6 |
| wsl | 1 | 0.43, 0.44 | 0.54, 0.57 | +0.120 | 0.029 | 0.96 / 0.94 (240, 2 runs) | 1.13 | 3.6 / 5.1 | +0.11 | 0 / 0 | 0 / 0 | 1000.2 / 1004.6 |
| wsl | 2 | 0.33, 0.27 | 0.36, 0.38 | +0.064 | 0.056 | 0.91 / 0.90 (120, 2 runs) | 0.91 | 3.7 / 4.7 | +1.63 | 0 / 0 | 0 / 0 | 2000.2 / 2005.9 |
| wsl | 5 | 0.31, 0.24 | 0.28, 0.30 | +0.013 | 0.066 (indistinguishable) | 1.03 / 1.04 (48, 2 runs, exploratory) | 1.05 | 3.8 / 5.0 | +0.16 | 0 / 0 | 0 / 0 | 5000.2 / 5004.2 |

Per collection (B0, thread CPU p50): the product collection costs 14.5 ms on the Orin, 7.6 ms on the
Raspberry Pi and 2.0 ms on WSL; the whole attempt (collection, provenance check, publish) about 0.5-1.3 ms
more. The no-op control callback's p95 was 0.008-0.021 ms (timer and recording cost). The Orin's 2nd-half
growth values above 400 KiB are single steps of about 444 KiB in B0 runs, flat before and after (B1
runs took the same step during warmup, which is why their RSS starts higher); in the 300 s windows the
second half grew 0 KiB on the Orin and at most 20 KiB on the Raspberry Pi. 120-300 s windows cannot rule out slow growth over hours.

### Table 2 — clients (collection every 1 s)

One window per variant for the client rows (no repeats): read large differences only. CPU is the server
process; the clients' own CPU is separate. Handler: `do_GET` elapsed time, p95. Client latency:
nearest rank over the requests in the window. In the "matrix" rows (two runs per variant) the handler
and latency values are the mean of the two runs' own percentiles, not a pooled percentile.

| platform | clients × poll | B0 CPU % | B1 CPU % | server requests | handler p95 ms B0 / B1 | client latency p50 / p95 ms B0 | B1 | response KiB B0 / B1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| orin | 0 | 1.52 | 1.67 | 0 | - | - | - | - |
| orin | 1 × 2 s | 1.63 | 1.80 | 60 / 60 | 1.27 / 1.77 | 3.61 / 3.86 | 4.28 / 4.70 | 6.8 / 10.6 |
| orin | 1 × 1 s (matrix means) | 1.75 | 1.93 | 120 / 120 | 1.32 / 2.04 | 3.55 / 3.97 | 3.94 / 4.37 | 6.8 / 10.6 |
| orin | 3 × 1 s | 2.36 | 2.44 | 360 / 360 | 2.77 / 4.01 | 3.67 / 6.48 | 4.36 / 7.50 | 6.8 / 10.6 |
| rpi4 | 0 | 0.92 | 1.10 | 0 | - | - | - | - |
| rpi4 | 1 × 2 s | 1.16 | 1.42 | 60 / 60 | 2.15 / 2.90 | 6.02 / 6.10 | 6.76 / 6.85 | 3.0 / 4.3 |
| rpi4 | 1 × 1 s (matrix means) | 1.40 | 1.65 | 120 / 120 | 2.25 / 2.90 | 6.08 / 6.22 | 6.81 / 6.95 | 3.0 / 4.3 |
| rpi4 | 3 × 1 s | 2.29 | 2.78 | 360 / 360 | 5.41 / 8.19 | 6.29 / 10.17 | 11.21 / 15.27 | 3.0 / 4.3 |

Every request was answered (200, no client failure); each sample was served once per poll, so at 2 s and
5 s collection the same sample was read 2 and 5 times (counted as reads, not new samples).

### Table 3 — conditional features (standalone probe processes)

Process CPU over the 120 s window includes the probe loop; the no-op control at 1 s is the loop's own
cost (Orin 0.056 %, Raspberry Pi 0.106 %). Burst rows are 100 back-to-back calls after the window, apart
from the real cadence.

| platform | feature / provider | period s | calls | process CPU % | elapsed p50 / p95 ms | thread CPU p50 / p95 ms | state | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| orin | RSSI, `/proc/net/wireless` | 1 | 120 | 0.083 | 1.56 / 1.90 | 0.32 / 0.39 | 120/120 with a level, 0 failures | cost low; elapsed is mostly waiting |
| orin | RSSI, `/proc/net/wireless` | 2 | 60 | 0.048 | 1.62 / 2.20 | 0.37 / 0.43 | 60/60 | as above |
| orin | RSSI, `/proc/net/wireless` | 5 | 24 | 0.020 | 1.52 / 1.87 | 0.38 / 0.46 | 24/24 | exploratory (n=24) |
| rpi4 | RSSI, `/proc/net/wireless` | 1 | 120 | 0.190 | 6.23 / 6.35 | 0.79 / 0.89 | 120/120 | blocks about 6 ms per read |
| rpi4 | RSSI, `/proc/net/wireless` | 2 | 60 | 0.100 | 6.20 / 6.38 | 0.81 / 0.89 | 60/60 | as above |
| rpi4 | RSSI, `/proc/net/wireless` | 5 | 24 | 0.039 | 6.25 / 6.38 | 0.80 / 0.91 | 24/24 | exploratory (n=24) |
| orin | mountinfo + statvfs (2 local mounts) | 30 | 4 | 0.006 | 0.64 / 0.78 + 0.035 / 0.041 | same | 0 failures, nothing pending | too few samples to call |
| orin | same | 60 | 2 | 0.003 | 0.51 / 0.60 + 0.031 / 0.036 | same | as above | too few samples to call |
| orin | same, burst | - | 100 | - | 0.41 / 0.48 + 0.022 / 0.025 | same | | per-call cost only |
| rpi4 | mountinfo + statvfs (2 local mounts) | 30 | 4 | 0.008 | 1.03 / 1.05 + 0.065 / 0.071 | same | 0 failures, nothing pending | too few samples to call |
| rpi4 | same | 60 | 2 | 0.005 | 0.59 / 1.16 + 0.055 / 0.068 | same | as above | too few samples to call |
| rpi4 | same, burst | - | 100 | - | 0.47 / 0.57 + 0.033 / 0.043 | same | | per-call cost only |
| both | RTT | - | - | - | - | - | not run: no approved internal target | not verified |
| both | RSSI via nl80211 | - | - | - | - | - | not run: no direct implementation | not verified |

On WSL `/proc/net/wireless` has no interface (failures recorded, not zero cost). The RSSI value changed
over the windows (2-4 distinct levels per window), but a repeated value says nothing either way about
driver caching. The product already calls `statvfs` on the root filesystem in every collection
(`disk`), so the new cost of capacity reporting is mostly the mount-table parse.

### Where the cost goes (computed from the measured values)

- **The existing daemon is most of the cost.** With no client, B0 at 1 s costs 1.52 % (Orin) and
  0.92 % (Raspberry Pi); the product collection's thread CPU (14.5 / 7.6 ms per collection) accounts for
  almost all of it (14.5 ms per second = 1.45 %). Which parts of the collection cost the most was
  not measured: the bench times the collection as a whole.
- **The B1 increase is two parts.** The passive callback: about 1.4 ms (Orin) / 1.6 ms (Raspberry Pi)
  thread CPU per collection, i.e. 0.14 / 0.16 pp at 1 s. The bigger answer: +3.8 KiB (Orin: 10
  interfaces including docker bridge and veth) / +1.3 KiB (Raspberry Pi) JSON per request, +0.5-0.7 ms
  handler CPU per request, paid per request, not per collection. Together: about 1.9 / 2.3 ms per second
  at 1 s with one client polling every 1 s, against 1.78 / 2.5 ms measured. That is why doubling the
  interval does not halve the increase (+0.178 → +0.144 pp on the Orin).
- **Back-to-back calls do not predict the service cost.** In a tight loop the same Raspberry Pi
  callback took 0.45 ms CPU (diagnostic run, not in the tables); once a second in the service, about
  1.6 ms. Cold caches and a lower CPU clock after a second of sleep are possible explanations, not
  verified here; the in-service number is the one that counts.
- **HTTP is not free.** Each 1-per-second client adds about 0.23 pp (Orin) / 0.45 pp (Raspberry Pi) to
  the server process in B0, more than the handler's own thread CPU (1.2 / 2.2 ms): connection set-up,
  a thread per request and HTTP parsing are paid outside `do_GET`. On the Raspberry Pi one polling client
  costs about half as much as collecting every second.
- Reference model (`c_ms / (10 × T_s)`): the passive callback alone would cost 0.14 / 0.07 / 0.03 pp on the
  Orin at 1 / 2 / 5 s. It explains the callback part only, not the per-request part.

### Hypotheses

| | hypothesis (proposed budget) | Orin Nano | Raspberry Pi 4 | WSL (supplementary) |
| --- | --- | --- | --- | --- |
| H1 | passive adds ≤ 0.1 pp CPU at 1 s (budget set before the runs, not changed after) | **rejected**: +0.178 pp (spread 0.010) | **rejected**: +0.250 pp (spread 0.069) | rejected: +0.120 pp (spread 0.029) |
| H1 | passive callback p95 ≤ 2 ms | **supported**: pooled 1.40 ms elapsed (max of per-run p95 1.42) | **supported**: pooled 1.64 ms (max 1.64) | supported: pooled 0.96 ms (max 1.13) |
| H2 | whole daemon ≤ 1 % at 1 s, 1 client at 1 s | **rejected, already in B0**: 1.75 % (B1 1.93 %) | **rejected, already in B0**: 1.40 % (B1 1.65 %) | supported: 0.43 % (B1 0.55 %) |
| H2 | passive RSS increase ≤ 5 MiB | **supported**: -1.94 to +1.18 MiB | **supported**: +0.01 to +0.05 MiB | supported |
| H2 | no growth with sample count after warmup | supported in 120-300 s windows (one-off steps only); hours not tested | supported in 120-300 s windows | supported in 120 s windows |
| H3 | cost vs interval | not proportional: B0 3.20 / 1.75 / 1.00 / 0.55 % at 0.5 / 1 / 2 / 5 s | not proportional: 2.22 / 1.40 / 0.92 / 0.66 % | |
| H4 | clients change the cost | **yes, large**: 0 / 1 / 3 clients = 1.52 / 1.75 / 2.36 % (B0) | **yes, large**: 0.92 / 1.40 / 2.29 % | not run |
| H5 | RSSI 2 s / capacity 30 s / RTT 10 s | RSSI (proc) 2 s: low CPU, 1.6 ms blocking; capacity: insufficient samples; RTT: not verified | RSSI (proc) 2 s: low CPU, **6 ms blocking**; capacity: insufficient samples; RTT: not verified | RSSI: no Wi-Fi |
| H6 | CPU ≠ device impact | no throttling seen, temperature flat; **power not measured (unverified)** | `get_throttled` 0x0 throughout, temperature flat; **power not measured (unverified)** | not applicable |

Context switches were the same in B0 and B1 (Orin about 31/s, Raspberry Pi about 6/s with one client;
these are not CPU wake-ups). Minor page faults doubled in B1 on the Raspberry Pi (125 → 246 per window),
from a small base.

### Table 4 — recommendation

| function | default candidate | low-overhead candidate | measured on | evidence | not verified / constraints |
| --- | --- | --- | --- | --- | --- |
| core collection | 1 s keeps the current behaviour. **Not** a low-overhead result: it exceeds the 1 % budget on both ARM boards already without the new feature (B0 1.75 / 1.40 %) | 2 s lowers the cost but does **not** meet the 1 % budget with passive collection (final 300 s: B1 1.12 % Orin, 1.11 % Raspberry Pi; B0 1.00 / 0.90 %). 5 s is the only measured interval below 1 % with passive collection (B1 about 0.62 / 0.76 %) | Orin Nano, Raspberry Pi 4 | Table 1 | at 5 s the Raspberry Pi's passive increase (+0.100 pp, repeat spread 0.052) sits on the 0.1 pp line: H1 is not established there. Where the collection's 14.5 / 7.6 ms go is not located yet; any default change belongs in a follow-up |
| passive Network / Disk (as prototyped) | same cadence as the core | - | Orin Nano, Raspberry Pi 4 | +0.14-0.21 pp at 2 s, callback p95 ≤ 1.65 ms | fails the 0.1 pp budget at 1 and 2 s; measures the Network/Disk path plus PSI's unsupported path, not PSI's success path; integration cost (metadata, diagnostics, freshness) not measured |
| PSI | - | - | none | `/proc/pressure` missing on all three systems | parser covered by fixtures only; cost on a PSI kernel not measured |
| Wi-Fi RSSI (`/proc/net/wireless`) | 2 s (design candidate), outside the core collection's critical path | 5 s | Orin Nano, Raspberry Pi 4 | 0.3-0.8 ms CPU, but 1.6 / 6.2 ms elapsed per read | nl80211 not measured; radio/firmware power effect not measured; two drivers only, not a general result |
| filesystem capacity | 30 s (design candidate) | 60 s | Orin Nano, Raspberry Pi 4 | per call 0.4-1.0 ms mount parse + 0.02-0.07 ms statvfs, 0 failures | 4 / 2 samples per cadence: blocking safety not established; remote/FUSE mounts excluded by design, not tested |
| RTT | - | - | none | not run | no approved target |
| API polling (clients) | 1 client at 1 s | poll every 2 s | Orin Nano, Raspberry Pi 4 | 1 → 3 clients: +0.6 / +0.9 pp | single windows; a browser's own load not measured |

When the cost has to come down, slowing the clients (or the web page's poll) cuts the per-request part,
slowing the collection cuts the collection part: they are separate knobs (Table 2). The next step is to
break both down at 1 s (inside the collection, and the response's copy and JSON), not to narrow what is
observed. Container scope was not measured; the numbers are for a native host process. PSI's success
path, nl80211, RTT and container scope remain unverified.


## Method

### What runs

`benchmarks/cadence.py serve` runs the product code of the fixed baseline unchanged, in one process:
`collector.collect_recorded`, `runtime.sampler.Sampler` and the HTTP handler of
`frontends/server.make_handler`, on `127.0.0.1`. The bench only wraps them:

- **B0 (baseline)**: the product collection, followed by a timed no-op callback.
- **B1 (candidate)**: the same, with the callback reading `/proc/net/dev`, `/proc/diskstats` and the
  PSI files once each (`benchmarks/passive.py`) and adding the result to the snapshot as
  `_bench_passive`, so it is also deep-copied, serialised and sent with every `/api/stats` answer.

B0 and B1 share the wrapper, the timers, the clients and the recording, so the difference between them is
the passive callback and the bigger snapshot. The no-op callback of B0 is the timer/recording control.

The prototype computes rates over the time actually observed between two readings, gives a row with no
usable previous reading (first sight, counter going backwards) a reason and no rate (never 0), reports
whole block devices only (partitions have no `/sys/block` entry and are never summed; `loop*` and
`ram*` are skipped by name), reports bytes/s and bits/s under separate names, calls the I/O time share
`io_time_ratio` (time with I/O in flight, not device saturation) and reports a missing PSI file as
`unsupported`, not as zero pressure.

### What is recorded

All inside the bench processes, into fixed-size buffers allocated before the warmup (so recording does
not grow memory with the number of samples), written to disk once at the end of a run:

- **Process CPU**: `getrusage(RUSAGE_SELF)` user + system at the end of the 30 s warmup and at the end of
  the window, over the window's measured `CLOCK_MONOTONIC` time: `cpu_pct_one_core = 100 * ΔCPU s /
  elapsed s`, never divided by the CPU count. This includes the HTTP threads. `process_time_ns` is
  recorded as a cross-check; the bench server starts no child processes (`children_cpu_s` is recorded).
- **Stages** (each on its own thread's CPU clock, `thread_time_ns`, plus elapsed time): the product
  collection, the passive callback, `Sampler._attempt` as a whole (collection, provenance check and
  publish; its `duration_ns` is the product's `sample.duration_ms`), `Sampler.read` inside each request,
  and each request's `do_GET` (read, JSON and send). The stages overlap; they are never added up.
- **Cadence**: the start of every attempt on the Sampler's clock, so the actual interval, start delay and
  overruns come from real collections and their `sequence`, not from HTTP answers.
- **Memory**: RSS (`/proc/self/statm`) every 10 s and at both window edges, `Pss` from
  `/proc/self/smaps_rollup` at the window edges, `ru_maxrss` (a high-water mark, not current RSS),
  context switches and page faults from `getrusage`. Context switches are not CPU wake-ups.
- **Clients**: separate processes (`cadence.py client`) on the same device, each requesting
  `/api/stats` on a fixed schedule; their CPU is reported apart from the server's. A synthetic API
  client, not a browser.
- **Device state**: thermal zones, CPU clocks, load average and (Raspberry Pi) `vcgencmd get_throttled`
  between runs only, never inside a window, the same way for B0 and B1.

No subprocess, SSH session or disk write happens per sample.

### Plan and budget

Per device, one bench server at a time, native (not in a container), 30 s warmup and 120 s window:

| phase | conditions |
| --- | --- |
| matrix | interval 0.5 / 1 / 2 / 5 s, 1 client polling every 1 s, B0 → B1 → B1 → B0 per interval |
| clients | interval 1 s: 0 clients; 3 clients every 1 s; 1 client every 2 s; B0 → B1 each |
| final | the candidate interval, B0 → B1, window max(300, 120 × interval) s, at most 600 s |
| probes | standalone processes: no-op control at 1 s; `/proc/net/wireless` at 1 / 2 / 5 s; mountinfo + `statvfs` of local block filesystems at 30 / 60 s, then 100 back-to-back calls reported apart |

The runner (`cadence.py run`) stops starting new runs after 90 minutes or 32 windows per device,
counting every phase in the same output directory, and before each run checks the device: the existing
platmon service still answers, no current throttle/under-voltage bits, at least 300 MiB available memory
and 500 MiB free disk. A run that fails a check, times out or sees a clock jump is kept and marked
invalid with the reason. Every wait has a deadline, from the bench server's ready line (30 s) to the
end of the run; on a timeout the runner kills only the processes it started and waits for them to end,
and if one does not, it records that and starts no further run. A probe whose last call was still
running at the end is kept with its partial samples but marked invalid (incomplete), because the
slowest call is missing from them.

`B1 - B0` is computed per device and condition from the per-run values; it is called
**indistinguishable** when it is not larger than the spread between repeats of the same variant, and
nothing is claimed for conditions with one run per variant beyond the raw values. Percentiles are
nearest-rank over the attempts or requests in the window; the sample count is given with them.

## Reproduce

On the device, from a checkout of the bench commit in its own directory (not the service's checkout):

```bash
python3 benchmarks/cadence.py run --out ~/bench-results --alias orin --phase matrix --intervals 1,2,0.5,5 \
    --watch-url http://127.0.0.1:9797/api/status --source-sha <baseline sha>
python3 benchmarks/cadence.py run --out ~/bench-results --alias orin --phase clients --watch-url ...
python3 benchmarks/cadence.py run --out ~/bench-results --alias orin --phase probes --watch-url ...
python3 benchmarks/cadence.py run --out ~/bench-results --alias orin --phase final --final 1 --watch-url ...
python3 benchmarks/cadence.py report --md ~/bench-results/results.jsonl   # JSON without --md
```

`--watch-url` is optional (a service that must keep answering between runs). `--measure 5 --warmup 2`
shortens every window for a smoke test of the tool; such runs are not results.

## Not measured, and how to measure it

- **Power.** Neither board's total input power was measured; the Orin's per-rail readings are not a total
  and were not summed. The power effect of any cadence is unverified.
- **Container scope.** Not run (time budget; lowest priority). In a bridge-network container
  `/proc/net/dev` lists the container's interfaces, not the host's, while `/proc/diskstats` is host-wide;
  how the product would observe host network I/O from a container is an open design question, and the
  production compose files were not changed. To compare: build from the deploy/docker/Dockerfile under a separate
  project, image tag and port, run `cadence.py run --phase final --final 2` inside, and record the
  interface and mount lists next to the numbers.
- **PSI** on a kernel that has it (`/proc/pressure/*`): rerun `--phase matrix --intervals 1,2` there.
- **RTT**: needs an approved internal target; no probe exists in the tool yet.
- **RSSI via nl80211**: no direct implementation; `iw` was not used periodically.
- **Longer windows** for memory growth over hours, and repeats of the client conditions.
