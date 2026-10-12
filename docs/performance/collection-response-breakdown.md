# Collection and response CPU cost breakdown

Follow-up to [low-overhead-cadence.md](low-overhead-cadence.md) (D0). D0 measured what the service costs
at a 1 s interval; this study splits that cost into the code paths that spend it, to choose what a later
change should touch. Nothing here changes the product: no optimisation, no fewer readings, no new
interval. Every number is a measurement on the devices unless marked as computed.

## Summary

- **Collection is most of the cost, and inside it three measured stages dominate on both ARM boards**:
  `Group.read` (opening, reading, text handling and parsing one value: Orin about 6.2 ms per collection
  over 50 calls; Raspberry Pi 4 about 1.9 ms over 8), `sysfs.canonical` (resolving sysfs paths for source
  identity: Orin 3.7 ms over 17 calls, Raspberry Pi 1.3 ms over 3, i.e. 25 % / 15 % of a collection) and
  `sysfs.source` (building the descriptor, its JSON, the SHA-256 and the result: 1.2 / 0.8 ms). The first
  is the observation itself; the second and third are recomputed every collection for paths and
  descriptors that did not change.
- **A request costs 2.0 ms (Orin) / 4.0 ms (Raspberry Pi) of worker-thread CPU, plus 0.45 / 0.7 ms on the
  listener thread.** The largest measured step is not JSON but the two `copy.deepcopy` calls in
  `Sampler.read` (0.60 / 0.93-0.98 ms); `json.dumps` is 0.25 / 0.39 ms.
- **B1's bigger answer costs mostly more copying**: per request +0.37 / +0.43 ms deepcopy and +0.13 /
  +0.13 ms JSON.
- **Instrumentation is not free.** Full stage timing added 10-32 % CPU over the reference runs; the one
  allowed retry with fewer stages (stages-lite) added 7.9 % on the Orin (within the 10 % quality line) but
  still 17.9 % on the Raspberry Pi. The Orin stages-lite numbers are the primary ones; every Raspberry Pi
  stage number, and the per-read numbers on both boards (full stages only), are diagnostic.
- **Next change to test (one):** reuse sysfs path identities between collections instead of resolving
  them every time (Table 4), with the conditions for resolving again still to be designed. Reference
  values, not a saving: the instrumented stage costs 3.7 ms per collection on the Orin (converted:
  0.37 pp at 1 s) and 1.3 ms on the Raspberry Pi (0.13 pp, diagnostic).
- **Profiles:** only the Orin's (Python 3.10) are used. The Raspberry Pi's (Python 3.13) are kept but
  excluded: cProfile there records every thread, so they cannot be read as one thread's profile, not even
  for the order of functions. The profile modes now refuse to run on Python 3.12 and later.

## Setup

| | |
| --- | --- |
| Product code | `04990c0` (main after #26; collector, frontends, `platmon.py` identical to `dabb89f`), source hash `3b2b21434c4de83e` |
| Bench code | `b103d52` (stages, profile; bench hash `85508079dc7c7c64`) for 18 windows per board; `b6cbcf9` (adds stages-lite; `3bbbb6f59608c1ea`) for the 4-window retry. Results of the two are reported apart. The tables were re-aggregated with the later report code of this branch, which compares only runs equal in interval, poll, clients, Python, product and bench hash and scope, and keeps stages and stages-lite apart: every comparison set matched, no number changed. |
| Raw records | kept outside the repository; SHA-256 (first 16 hex digits) orin `bfb51d4a69e68f7f`, rpi4 `3b5e7bdd78049b0e`, wsl `a180d70742bfd931` |
| Boards | Orin Nano devkit (Super), L4T R36.5.2, MAXN_SUPER, `schedutil`, Python 3.10.12, 6/3/2 temperature/power/fan sensors; Raspberry Pi 4 Model B, Debian 13, `ondemand`, Python 3.13.5, 1/0/0 sensors; WSL 2 (supplementary), Python 3.12.3, no sensors exposed |
| Conditions | native process next to each board's own platmon service, collection 1 s, synthetic client(s) polling `/api/stats` every 1 s (new connection per request, as in D0), 30 s warmup, 120 s windows |
| Clocks | elapsed: `CLOCK_MONOTONIC`; stage CPU: `CLOCK_THREAD_CPUTIME_ID` (`thread_time_ns`), resolution 1 ns as reported; process CPU: `getrusage` user + system over the window, % of one core |
| Runs | Orin 22 windows / 53.1 min, Raspberry Pi 22 / 53.3 min (budget 24 / 60), WSL 7 / 17.6 min (budget 10 / 20). No invalid run, no dropped record, no clock jump, the same scope (sensors, interfaces, collector states) in every run of a board. Raspberry Pi `get_throttled` 0x0 before and after every run; temperatures between runs: Orin 56.0-58.6 °C (D0: 50-53 °C), Raspberry Pi 46.7-50.1 °C. |

The reference runs reproduce D0: Orin B0 with one client 1.73 % (D0 1.75 %), Raspberry Pi 1.30-1.39 %
(D0 1.40 %).

## Method

`cadence.py serve --instrumentation` adds three modes to D0's runner (variant B0/B1 keeps its meaning):

- **reference**: D0's coarse timers only.
- **stages** (`benchmarks/breakdown.py`): product call sites wrapped in place for the bench process only,
  through the module attributes the product actually calls (including names imported with
  `from ... import`), restored on exit. Per unit (a collection on the sampler thread, a request on its
  worker thread, an accept on the listener thread) each stage records calls, inclusive elapsed time,
  inclusive thread CPU and self CPU (inclusive minus the stages nested in it). A stage that recurses into
  itself counts once. `copy.deepcopy` and `json.dumps` are wrapped only where `runtime.sampler` and
  `frontends.server` call them, so deepcopy's own recursion and the bench's own JSON are not counted.
  Totals go to fixed-size buffers when a unit ends, never per call.
- **stages-lite**: the same without the per-value wrappers (`Group.read`, `Group.sensor`,
  `Group.status`, `entries`, `hwmon_chips`).
- **profile-collect / profile-request**: cProfile on the thread CPU clock, one profiler per thread,
  enabled only in the window (50 s), merged afterwards. Used to find candidates, never for CPU %. Only
  valid where each thread has its own profile function (Python 3.11 and older; checked by tests on 3.9 in
  CI and on the Orin's 3.10). From Python 3.12 cProfile uses `sys.monitoring`, whose events are global:
  a profile then also records other threads (with this timer, even negative times) and a second one
  cannot be enabled while one is active. The modes now refuse to start there and the report marks such
  results as not usable.

Stages are nested: the "parts" (`common.thermal_zones`, `common.hwmon_sensors`, ...) and the "kinds of
work" (`work.Group.read`, `work.canonical`, ...) are two classifications of the same time and are never
added together. Inclusive values of a parent and its children are not added either. CPU per unit is the
sum over the window's units divided by their number; elapsed p50/p95 are nearest rank over the units in
which the stage ran. Units are assigned to the window by their start (none crossed the window end on
the boards except one B1 collection on the Orin).

Process CPU is split by thread: collections (each attempt's thread CPU), request workers (each worker
thread's whole CPU, read when it ends), listener (each accept and dispatch) and an unattributed rest.

## Table 1 — instrumentation impact

Reference and staged windows alternate (reference → staged → staged → reference). Wrapper cost is a
no-op timed 20 000 times at the start of each staged run (thread CPU); in the service, the cost per call
is higher than that loop suggests (clock reads are system calls, caches are cold).

| platform | mode | variant | clients | reference CPU % | staged CPU % | staged - reference | actual interval ms, mean of the runs' p50 / largest max (ref; staged) | wrapper ns/call | quality |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| orin | stages | B0 | 1 | 1.73, 1.73 | 2.01, 2.01 | +0.280 pp (16.2 %) | 1000.2 / 1000.4; 1000.2 / 1000.5 | 3120 | diagnostic |
| orin | stages | B0 | 0 | 1.51, 1.51 | 1.73, 1.73 | +0.217 pp (14.3 %) | 1000.3 / 1000.5; 1000.2 / 1000.4 | 3108 | diagnostic |
| orin | stages | B0 | 3 | 2.11, 2.18 | 2.48, 2.46 | +0.327 pp (15.2 %) | 1000.2 / 1000.5; 1000.2 / 1000.5 | 3101 | diagnostic |
| orin | stages | B1 | 1 | 1.95, 1.93 | 2.21, 2.18 | +0.259 pp (13.4 %) | 1000.2 / 1000.4; 1000.2 / 1000.5 | 3110 | diagnostic |
| orin | **stages-lite** | B0 | 1 | 1.73, 1.74 | 1.87, 1.87 | +0.138 pp (7.9 %) | 1000.3 / 1000.5; 1000.2 / 1000.4 | 3139 | **ok (< 10 %)** |
| rpi4 | stages | B0 | 1 | 1.36, 1.39 | 1.68, 1.68 | +0.303 pp (22.0 %) | 1000.2 / 1000.3; 1000.3 / 1000.3 | 8160 | diagnostic |
| rpi4 | stages | B0 | 0 | 0.93, 0.82 | 1.15, 1.15 | +0.275 pp (31.5 %) | 1000.2 / 1000.3; 1000.3 / 1000.3 | 8281 | diagnostic |
| rpi4 | stages | B0 | 3 | 2.26, 2.46 | 2.54, 2.67 | +0.245 pp (10.4 %) | 1000.2 / 1000.3; 1000.2 / 1000.4 | 8290 | diagnostic |
| rpi4 | stages | B1 | 1 | 1.62, 1.52 | 1.92, 1.90 | +0.340 pp (21.7 %) | 1000.2 / 1000.5; 1000.3 / 1000.4 | 8072 | diagnostic |
| rpi4 | stages-lite | B0 | 1 | 1.30, 1.38 | 1.57, 1.59 | +0.239 pp (17.9 %) | 1000.2 / 1000.3; 1000.3 / 1000.4 | 8156 | diagnostic (retry used) |
| wsl | stages | B0 | 1 | 0.46, 0.51 | 0.62, 0.61 | +0.132 pp (27.1 %) | 1000.2 / 1004.6; 1000.3 / 1002.4 | 709 | diagnostic |
| wsl | stages | B0 | 0 | 0.30 | 0.35, 0.35 | +0.051 pp (17.0 %) | 1000.2 / 1003.7; 1000.3 / 1002.8 | 704 | diagnostic |

The 10 % line is this study's quality bar for attributing absolute cost, not a product budget. The
instrumentation cost was not subtracted from any number below. The Raspberry Pi stays above the line
even without the per-value wrappers; where its remaining cost goes (the stages left, the unit
bookkeeping, or clock reads being slower there) was not measured. Its numbers below are relative
evidence (which path is large), not absolute costs of the uninstrumented service.

## Table 2 — collection (B0, 1 client, per collection)

Orin: stages-lite (quality ok). Raspberry Pi: stages-lite (diagnostic). "Reads" come from the full
stages runs (diagnostic on both), because stages-lite does not time them. Self CPU of a part includes
the reads and other unwrapped work it does itself.

| stage | Orin calls | Orin CPU ms (self) | Orin elapsed p50 / p95 ms | Orin CPU % | Raspberry Pi calls | Raspberry Pi CPU ms (self) | Raspberry Pi elapsed p50 / p95 ms | Raspberry Pi CPU % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| whole collection (`collect._collect`) | 1 | 14.93 (0.07) | 19.12 / 20.62 | 1.49 | 1 | 8.32 (0.14) | 8.35 / 8.44 | 0.83 |
| `common.collect` | 1 | 12.26 (1.18) | 16.39 / 16.89 | 1.23 | 1 | 8.01 (1.60) | 8.03 / 8.12 | 0.80 |
| `common.thermal_zones` | 1 | 5.03 (3.03) | 5.95 / 6.10 | 0.50 | 1 | 1.40 (0.81) | 1.39 / 1.49 | 0.14 |
| `common.hwmon_sensors` | 1 | 4.62 (3.23) | 7.81 / 8.00 | 0.46 | 1 | 2.13 (1.14) | 2.14 / 2.19 | 0.21 |
| `jetson.extend` (gpu, power mode, L4T, tach fans) | 1 | 2.52 (0.29) | 2.50 / 3.96 | 0.25 | - | - | - | - |
| `common.cpu_sample` (`/proc/stat`) | 1 | 0.65 | 0.65 / 0.77 | 0.07 | 1 | 1.07 | 1.08 / 1.15 | 0.11 |
| `common.read` (uptime, model, os-release) | 3 | 0.16 | 0.16 / 0.19 | 0.02 | 3 | 0.56 | 0.57 / 0.63 | 0.06 |
| `common.meminfo` | 1 | 0.14 | 0.14 / 0.16 | 0.01 | 1 | 0.47 | 0.47 / 0.52 | 0.05 |
| `common.disk_usage` (statvfs) | 1 | 0.03 | 0.03 / 0.04 | 0.00 | 1 | 0.06 | 0.06 / 0.07 | 0.01 |
| `collect.summarize` + `collect.groups` + `collect.sensor_entries` | 1 + 1 + 1 | 0.19 | - | 0.02 | 1 + 1 + 1 | 0.31 | - | 0.03 |
| publish `deepcopy` (in `_attempt`) | 1 | 0.26 | 0.17 / 0.39 | 0.03 | 1 | 0.59 | 0.60 / 0.65 | 0.06 |
| `sampler._provenance` / `_check` | 1 | 0.35 | 0.25 / 0.51 | 0.03 | 1 | 0.49 | 0.49 / 0.54 | 0.05 |
| *by kind of work, nested in the parts above:* | | | | | | | | |
| `work.Group.read` (open, read, text handling, parse) — full stages | 50 | 6.21 | 10.71 / 11.14 | 0.62 | 8 | 1.90 | 1.95 / 2.03 | 0.19 |
| `work.canonical` (realpath, relpath, exists) | 17 | 3.69 | 3.65 / 4.37 | 0.37 | 3 | 1.27 | 1.28 / 1.35 | 0.13 |
| `work.chip_identities` (calls canonical per chip) | 2 | 1.82 (0.21) | 1.69 / 2.38 | 0.18 | 1 | 0.99 (0.16) | 0.99 / 1.05 | 0.10 |
| `work.source` (descriptor, JSON, SHA-256, result) | 20 | 1.18 | 1.25 / 1.37 | 0.12 | 5 | 0.76 | 0.79 / 0.86 | 0.08 |
| `work.entries` (directory listings) — full stages | 8 | 0.49 | 0.50 / 0.56 | 0.05 | 3 | 0.29 | 0.31 / 0.35 | 0.03 |
| `work.Group.sensor` / `Group.status` — full stages | 20 / 7 | 0.09 / 0.03 | - | 0.01 | 5 / 7 | 0.06 / 0.13 | - | 0.02 |

Per call (computed): a read costs about 124 µs CPU on the Orin and 238 µs on the Raspberry Pi; a
`canonical` 217 / 423 µs; a `source` 59 / 151 µs. On the Orin, reads take 10.7 ms elapsed for 6.2 ms
CPU; the difference is waiting or scheduling, not shown to be sensor I/O.

**What the Orin profile hints at** (Python 3.10, 50 collections; cProfile slows Python code more than C
functions, so these are leads, not cost shares of the uninstrumented service): inside `canonical`, the
time is mostly in Python path handling (`posixpath._joinrealpath`, `join`, `normpath`, `relpath`); the
`lstat` system calls themselves are 0.097 s of 1.09 s cumulative.
`chip_identities` runs twice per collection on the Jetson (from `hwmon_sensors` and `tach_fans`) over
the same chip listing. `sysfs.numbered` (a regular expression per directory entry, per channel kind) is
about 0.29 s of 2.95 s and sits in `hwmon_sensors`' self time. The profile's share for the publish
`deepcopy` (9 %) is higher than its stage cost (0.26 ms of 14.9 ms): many small calls, which the profiler
slows down most. The Raspberry Pi profiles are not used (see Method).

## Table 3 — requests (1 client, per request)

| step | Orin B0 CPU ms | Orin elapsed p50 / p95 | Raspberry Pi B0 CPU ms | Raspberry Pi elapsed p50 / p95 | Orin B1 CPU ms | Raspberry Pi B1 CPU ms |
| --- | --- | --- | --- | --- | --- | --- |
| worker thread, whole (thread start, socket set-up, handling, close) | 1.93-2.02 | - | 3.97-3.99 | - | 2.43-2.46 | 4.53-4.55 |
| `http.handle` (request line and headers, `do_GET`) | 1.62 | 1.72 / 1.78 | 3.30 | 3.41 / 3.49 | 2.13 | 3.84 |
| `http.do_GET` | 1.28 | 1.33 / 1.41 | 2.53 | 2.54 / 2.61 | 1.79 | 3.08 |
| `sampler.read` | 0.71 | 0.72 / 0.76 | 1.28 | 1.29 / 1.35 | 1.08 | 1.70 |
| `deepcopy` (stats and sensor_meta, 2 calls) | 0.60 | 0.61 / 0.63 | 0.98 | 0.99 / 1.06 | 0.97 | 1.41 |
| `json.dumps` | 0.25 | 0.25 / 0.28 | 0.40 | 0.40 / 0.42 | 0.38 | 0.53 |
| `http.reply` (headers, write) | 0.22 | 0.26 / 0.30 | 0.59 | 0.57 / 0.67 | 0.22 | 0.58 |
| listener thread (accept, start the worker) | 0.43-0.45 | - | 0.70 | - | 0.45 | 0.69-0.72 |
| response size | 6.8 KiB | | 3.0 KiB | | 10.6 KiB | 4.3 KiB |

From full stages runs (diagnostic): 240 requests per condition. Orin stages-lite gives the same
request numbers within 0.01 ms (`handle` 1.61, `deepcopy` 0.60, `json.dumps` 0.25). Computed: outside
`handle` the worker thread spends about 0.3-0.4 ms (Orin) / 0.7 ms (Raspberry Pi), the listener another
0.45 / 0.7 ms; these are measured thread totals minus measured stages, not named causes. `sampler.read`
minus its deepcopy (age, status and metadata) is about 0.11 / 0.30 ms. The UTF-8 `encode()` of the body
is inside `do_GET`'s self time (0.10 / 0.27 ms together with the rest of `do_GET`), not timed apart.

With three clients the per-request CPU stays the same (Orin `handle` 1.64 ms, Raspberry Pi 3.31 ms); only
elapsed p95 grows (3.9 / 8.5 ms), from requests overlapping.

**Process accounting** (stages runs, % of one core over the window): the threads above explain
everything but 0.04-0.07 % (Orin) and 0.07-0.27 % (Raspberry Pi), e.g. Orin B0 one client: process
2.01 = collections 1.73 + workers 0.20 + listener 0.04 + unattributed 0.04.

**B1's increase**: per request +0.52 ms (Orin) / +0.54 ms (Raspberry Pi) in `handle`, of which deepcopy +0.37 (Orin) /
+0.43 (Raspberry Pi) and JSON +0.13 / +0.13; per collection the passive callback itself (D0: about 1.4 /
1.6 ms).

## Table 4 — candidates

At most three; the first is the one proposed for the next PR. "Room" is the instrumented stage cost and
its conversion at 1 s: a reference value, not a guaranteed upper bound of the saving in the
uninstrumented service and not a prediction (a replacement costs something too). The Orin's 7.9 % total
instrumentation cost does not bound the error of each stage; the Raspberry Pi values are diagnostic.

| candidate | direct evidence | both boards? | room (instrumented stage cost) | contracts to keep | unknown |
| --- | --- | --- | --- | --- | --- |
| **1. Reuse sysfs path identities between collections** (`sysfs.canonical` from `thermal_zones`, `chip_identities`, `jetson.gpu`; `chip_identities` twice per Jetson collection) | stage timing: 17 calls, 3.7 ms (Orin, quality ok); 3 calls, 1.3 ms (Raspberry Pi, diagnostic); Orin profile hint: Python path handling rather than `lstat` | yes (25 % / 15 % of a collection) | 3.7 / 1.3 ms per collection, converted 0.37 / 0.13 pp at 1 s | source ids unchanged (same descriptor, same basis); hotplug, a renumbered `hwmonN`, a chip appearing or going away, aliases and test trees still resolve as today; chips still listed every collection | when to resolve again: the same chip listing does not mean the same devices (a symlink can point elsewhere under unchanged names); what a check costs; not "resolve once and keep forever" |
| 2. Copy less per request (`Sampler.read` deep-copies stats and sensor_meta for every request) | 0.60 / 0.98 ms per request, the largest step of a request; most of B1's increase | yes | 0.60 / 0.98 ms per request, converted 0.06 / 0.10 pp per client polling at 1 s | `read()` returns new objects callers may change; age, status and metadata stay per request; no cached JSON | what a cheaper copy would cost; the server's own use of the result |
| 3. Compute each source id once per descriptor (`sysfs.source`: descriptor, JSON, SHA-256 and result for an unchanged descriptor, every collection) | 20 calls 1.2 ms (Orin), 5 calls 0.8 ms (Raspberry Pi) | yes | 1.2 / 0.8 ms, converted 0.12 / 0.08 pp at 1 s | same id for the same descriptor (a pure function of it), unresolved stays `id: null` | lookup cost of a memo |

Not candidates: the reads themselves (the observation; the elapsed/CPU gap is not explained),
excluding sensors or interfaces, caching values (freshness), caching whole responses (age and status
change per request), and slowing the collection or the clients (D0's knobs, a product decision).

**Next PR, candidate 1, how to show it**: the same ABBA runs at 1 s (reference vs changed, B0, one
client, 120 s, both boards), the whole-process CPU as the result and the collection's thread CPU as
the mechanism; tests that every source id and identity basis is the same before and after on the
existing fixture trees, including renumbered and removed chips.

## Not measured

- Kernel-level costs of the reads (syscall counts, time in the sysfs drivers): no strace/perf run.
- Why the Raspberry Pi's instrumentation stays above 10 % with stages-lite.
- Profiles on the Raspberry Pi: both of its profile runs (Python 3.13) are excluded, including the
  collect profile whose total was positive; a valid per-thread profile there would need another method
  (not attempted). Their text is kept with the raw records, marked as not for analysis.
- WSL: only B0 with one and no client (stages); the profile, three-client and B1 runs and one
  reference window were skipped by its 20 min budget. Supplementary only, not averaged with the boards.
- D0's other intervals, RSSI, statvfs probes, RTT, PSI kernels, containers: out of scope here.

## Reproduce

From a checkout of the bench commit in its own directory on the device:

```bash
python3 benchmarks/cadence.py run --out ~/bench-d1 --alias orin --phase breakdown --budget-min 60 --max-windows 24 \
    --watch-url http://127.0.0.1:9797/api/status --source-sha <product sha>
python3 benchmarks/cadence.py run --out ~/bench-d1 --alias orin --phase breakdown-lite --budget-min 60 --max-windows 24 ...
python3 benchmarks/breakdown.py report --md --profiles ~/bench-d1/profiles ~/bench-d1/results.jsonl
```
