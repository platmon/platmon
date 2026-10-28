# Passive Network / Disk / PSI cost after D2 and D3

> **TL;DR**
> - Re-measured the passive Network / Disk / PSI prototype after the identity and snapshot-copy changes.
> - It still adds about +0.16 pp (Orin), above the 0.1 pp per-feature hypothesis; its callback p95 stayed
>   under 2 ms.
> - Prototype only; the product collectors are measured in network-product.md and disk-io-product.md.

Re-measurement, not a feature: what the passive prototype (`benchmarks/passive.py`) adds to the current
product, after [identity-resolution.md](identity-resolution.md) (#28) and
[snapshot-copy.md](snapshot-copy.md) (#29) made the collection and `/api/stats` cheaper. B0 and B1 are the
same product and the same bench code; the only difference is the passive callback: a timed no-op (B0) or
the whole `Passive()` call with its result added to the snapshot as `_bench_passive` (B1). Nothing in the
product, its API or its defaults changes. The earlier reports keep their own results.

## Result (2026-10-09)

| | Orin Nano | Raspberry Pi 4 |
| --- | --- | --- |
| **H1** B1 - B0 ≤ 0.1 pp, 1 client polling at 1 s | **rejected**: +0.161 pp (blocks +0.143, +0.179). The process CPU spread between repeats (0.20 / 0.30 pp) is larger than the difference, so the size is not precise; the collection thread alone adds 1.46 ms per collection (0.146 pp), and both blocks exceed 0.1 | **rejected**: +0.241 pp (blocks +0.225, +0.257), larger than the spread (0.06 / 0.05 pp) |
| **H1-latency** passive callback pooled p95 ≤ 2 ms | **supported**: 1.41 ms (480 samples, 4 runs; max of per-run p95 1.46) | **supported**: 1.69 ms (480, 4; 1.71) |
| **H2** B1 ≤ 1 % of one core | **rejected**: 1.65 % (B0 is already 1.49 %) | **rejected**: 1.44 % (B0 1.20 %) |
| **H2** RSS B1 - B0 ≤ 5 MiB, no growth, errors or overruns | **supported** in 120 s windows: -0.08 MiB, no second-half growth in B1, 0 failed collections, 0 overruns, 0 HTTP errors | **supported**: -0.18 MiB, ≤ 8 KiB second-half growth, 0 / 0 / 0 |

- The passive bundle as prototyped costs about 0.16-0.24 pp of one core at 1 s on both ARM boards, above the
  0.1 pp budget; the whole process stays above 1 % with or without it (as in D3).
- Where it goes (collection thread CPU, B1 - B0, means of runs): the passive call itself (Orin 0.93-0.98 ms,
  Raspberry Pi 1.46-1.53 ms) plus about **0.5 ms on both boards outside both the product collection and the
  passive call**, inside `Sampler._attempt`: the publish copy and handling of the larger snapshot. Per request:
  `do_GET` +0.14 ms on both (response +55 % / +45 % bytes), about 0.015 pp at one request per second.
- Diagnostic split of the passive call (Table 3): Disk about half, Network about a third, the three PSI
  lookups the rest (all unsupported on both boards; 11 % / 20 % of the parts' CPU for files that are not
  there).
- **Next (one):** design a minimal Passive Network product slice with its own off/on budget (see
  Follow-up). Network alone measured 0.026 pp (Orin) / 0.047 pp (Raspberry Pi, diagnostic) in the
  passive-parts runs, before its share of the publish and response cost.

## Setup

| | |
| --- | --- |
| Product | main `36e817c` (product hash `ac123f681e622010`) |
| Bench code | `462c5d0` on `perf/passive-cost-rebaseline` (bench hash `176dfeb5b722fa0a`), copied with `git archive` to its own directory on each device; `benchmarks/passive.py` unchanged (SHA-256 `4abc01ff52a544aa…`) |
| Main runs | `cadence.py run --phase passive-rebaseline`: interval 1 s, reference instrumentation, 30 s warmup + 120 s window, a new process and a new `Passive` per run; blocks in a fixed order c1-1 (1 client), c0-1 (0 clients), c1-2, c0-2, each B0 B1 B1 B0: 16 windows per board |
| Diagnostic runs | `--phase passive-parts`: B1, 0 clients, reference → passive-parts → passive-parts → reference: 4 windows. Never used for H1/H2 |
| WSL | `--phase passive-rebaseline --clients 1 --blocks 1`: one block (4 windows), supplementary; a second block did not fit the 20 min budget |
| Budget | Orin 20 windows / 50.3 min, Raspberry Pi 20 / 50.4 min (of 24 / 60); WSL 4 / 10.1 min (of 8 / 20) |

Environment (anonymised; as in [low-overhead-cadence.md](low-overhead-cadence.md) unless noted):

| alias | hardware, OS | Python | clock policy | observed scope (same in every run of the board) | PSI |
| --- | --- | --- | --- | --- | --- |
| orin | Jetson Orin Nano devkit (Super), L4T R36, 5.15-tegra | 3.10.12 | MAXN_SUPER, `schedutil` | 6 CPU rows, 6 temp / 3 power / 2 fan sensors, 20 source records, all 8 collectors `ok`; 10 interfaces (5 physical), 7 block devices counted | `/proc/pressure` absent |
| rpi4 | Raspberry Pi 4 Model B, Debian 13, 6.18 rpi-v8 | 3.13.5 | `ondemand`, 1.5 GHz | 4 CPU rows, 1 temp sensor, 5 source records; 3 interfaces (2 physical), 2 block devices | absent |
| wsl | x86-64 PC, WSL 2, 6.6 | 3.12.3 | not exposed | 20 CPU rows, no sensors; 6 interfaces (1 physical), 5 block devices | absent |

Each board's own platmon service kept running (both at `e1bc82a`, not the measured `36e817c`) and was
not touched: container id, image, start time, restart count 0, healthy (Orin), PID and start time
(Raspberry Pi), checkout SHA with a clean tree, INI and CLI hashes, rollback tags and folders (their
SHA256SUMS verify), port 9797 answering, power mode and governor were the same before and after. Load
average 0.0-0.6 before runs; hottest zone 50.2-53.0 °C (Orin), 50.6-52.6 °C (Raspberry Pi);
`get_throttled` 0x0 before and after every Raspberry Pi run (the Orin has no equivalent flag source).
Every run valid; no dropped record, clock jump, timeout or skipped run; the CPU % recomputed from the raw
rusage and elapsed time equals the recorded value in every run.

## Table 1 — passive off / on (CPU % of one core, 120 s windows)

| board | clients | block | B0 runs | B1 runs | B1 - B0 pp |
| --- | --- | --- | --- | --- | --- |
| orin | 1 | c1-1 | 1.440, 1.448 | 1.592, 1.583 | +0.143 |
| orin | 1 | c1-2 | 1.637, 1.433 | 1.567, 1.863 | +0.179 |
| orin | 1 | **all** | mean 1.490, spread 0.204 | mean 1.651, spread 0.296 | **+0.161** (not larger than the spread) |
| orin | 0 | c0-1 | 1.257, 1.267 | 1.642, 1.398 | +0.258 |
| orin | 0 | c0-2 | 1.233, 1.268 | 1.405, 1.393 | +0.149 |
| orin | 0 | **all** | mean 1.256, spread 0.035 | mean 1.460, spread 0.249 | **+0.203** (not larger than the spread) |
| rpi4 | 1 | c1-1 | 1.230, 1.177 | 1.411, 1.445 | +0.225 |
| rpi4 | 1 | c1-2 | 1.216, 1.170 | 1.442, 1.459 | +0.257 |
| rpi4 | 1 | **all** | mean 1.198, spread 0.060 | mean 1.439, spread 0.047 | **+0.241** (distinguishable) |
| rpi4 | 0 | c0-1 | 0.806, 0.812 | 0.992, 0.977 | +0.176 |
| rpi4 | 0 | c0-2 | 0.784, 0.810 | 1.023, 1.011 | +0.220 |
| rpi4 | 0 | **all** | mean 0.803, spread 0.028 | mean 1.001, spread 0.046 | **+0.198** (distinguishable) |
| wsl | 1 | c1-1 | 0.406, 0.462 | 0.536, 0.559 | +0.114 (spread 0.057; one block, supplementary) |

Block delta = mean of the block's two B1 runs - mean of its two B0 runs; "all" = mean of the four B1 runs -
mean of the four B0 runs; spread = largest - smallest run of one variant.

- On the Orin three runs (c1-2 B0 1.637, c1-2 B1 1.863, c0-1 B1 1.642) are higher through the product
  collection itself (13.4-13.6 ms per collection against 11.5-12.0 in the other runs, in both variants),
  not through the passive call; they were kept. They make the Orin's process-level spread larger than the
  difference. The thread-level numbers below show the same increase in every block.
- Request share, reference estimate only (different runs): Δ1 - Δ0 = -0.042 pp (Orin), +0.043 pp
  (Raspberry Pi), both inside the spread. The measured request cost grew by 0.145 / 0.142 ms per request
  (`do_GET` thread CPU), i.e. about 0.015 pp at one request per second.

## Table 2 — work per collection and per request (means of the runs)

| board | clients | variant | process CPU % | product collect CPU ms | passive CPU ms | `_attempt` CPU ms | attempt - product - passive ms | `do_GET` CPU ms (`_stats_json` incl. JSON) | requests / window, bytes | client latency p50 / p95 ms | RSS / PSS end MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| orin | 1 | B0 | 1.490 | 12.30 | 0.005 | 13.01 | 0.71 | 0.607 (0.369) | 120, 7008 | 3.00 / 3.38 | 25.07 / 17.60 |
| orin | 1 | B1 | 1.651 | 12.29 | 0.977 | 14.47 | 1.20 | 0.752 (0.511) | 120, 10860 | 3.18 / 3.60 | 24.99 / 17.04 |
| orin | 0 | B0 | 1.256 | 11.72 | 0.004 | 12.30 | 0.58 | - | 0 | - | 25.11 / 17.28 |
| orin | 0 | B1 | 1.460 | 12.23 | 0.931 | 14.32 | 1.16 | - | 0 | - | 24.80 / 17.03 |
| rpi4 | 1 | B0 | 1.198 | 6.55 | 0.011 | 7.79 | 1.23 | 1.278 (0.687) | 120, 3078 | 5.26 / 5.36 | 28.65 / 19.09 |
| rpi4 | 1 | B1 | 1.439 | 6.75 | 1.531 | 10.02 | 1.74 | 1.420 (0.828) | 120, 4466 | 5.46 / 5.70 | 28.47 / 18.91 |
| rpi4 | 0 | B0 | 0.803 | 6.38 | 0.011 | 7.58 | 1.19 | - | 0 | - | 28.40 / 19.26 |
| rpi4 | 0 | B1 | 1.001 | 6.44 | 1.455 | 9.57 | 1.68 | - | 0 | - | 28.41 / 19.27 |

| board | clients | B1 passive elapsed p95 ms, pooled (samples, runs) | max of per-run p95 | `_attempt` elapsed p95 ms, pooled B0 / B1 | actual interval p50 / max ms | failed / overrun |
| --- | --- | --- | --- | --- | --- | --- |
| orin | 1 | 1.41 (480, 4) | 1.46 | 19.0 / 21.3 | 1000.2 / 1000.5 | 0 / 0 |
| orin | 0 | 1.38 (480, 4) | 1.42 | 17.6 / 21.0 | 1000.2 / 1000.5 | 0 / 0 |
| rpi4 | 1 | 1.69 (480, 4) | 1.71 | 8.2 / 10.7 | 1000.2 / 1000.4 | 0 / 0 |
| rpi4 | 0 | 1.60 (480, 4) | 1.62 | 8.2 / 10.2 | 1000.2 / 1000.3 | 0 / 0 |
| wsl | 1 | 1.02 (240, 2) | 1.02 | 3.7 / 5.3 | 1000.3 / 1004.6 | 0 / 0 |

- Collection thread, B1 - B0: Orin +1.46 ms (1 client) / +2.02 ms (0 clients, including one slow run);
  Raspberry Pi +2.23 / +1.99 ms. Converted at 1 s: 0.15-0.22 pp, the size of the process-level
  differences. The column "attempt - product - passive" (publish copy of the snapshot, the source records,
  the bench wrapper) grows by 0.46-0.58 ms in every condition: the passive result is copied once more per
  collection when it is published. That cost belongs to adding the data to the snapshot, not to reading
  the files.
- Every collection in a B1 window called the passive callback once (120 calls per 120 collections, every
  run); B0 windows ran the no-op (the scope check after the window is outside it). Requests did not
  trigger collections: 0 clients served 0 requests, 1 client 120 requests over 120 distinct samples.
- `sample.duration_ms` is not the process CPU; `_stats_json` includes the JSON serialization, so its
  numbers are not comparable with D1's `read()` step.
- Client CPU (same device, separate process, not in the daemon CPU): Orin 0.34 s, Raspberry Pi 0.71-0.72 s
  per window in both variants.
- Memory: RSS and PSS at the end of the window did not grow with B1; growth over the second half of a B1
  window was 0 KiB (Orin) and ≤ 8 KiB (Raspberry Pi). 120 s windows do not show stability over hours.

## Table 3 — passive parts, diagnostic

B1, 0 clients, each guarded call timed on the `Passive` instance (`_guarded` included, so failed and
unsupported lookups are timed too; nothing is read twice; tested in `tests/benchmarks/test_bench_cadence.py`). Means
of the two passive-parts runs; CPU % of one core = the window's summed thread CPU of that part / elapsed.

| board | part | support | calls (per run) | CPU % (sum) | CPU mean ms | elapsed p50 / p95 ms | share of the parts |
| --- | --- | --- | --- | --- | --- | --- | --- |
| orin | Network (`/proc/net/dev`) | ok, 120 reads | 120 | 0.026 | 0.257 | 0.23 / 0.45 | 33 % |
| orin | Disk (`/proc/diskstats`) | ok, 120 reads | 120 | 0.044 | 0.435 | 0.40 / 0.75 | 56 % |
| orin | PSI cpu / memory / io | unsupported (ENOENT), 0 reads | 120 each | 0.009 | 0.044 / 0.024 / 0.020 | 0.04 / 0.07, 0.02 / 0.04, 0.02 / 0.04 | 11 % |
| rpi4 | Network | ok, 120 reads | 120 | 0.047 | 0.468 | 0.48 / 0.55 | 31 % |
| rpi4 | Disk | ok, 120 reads | 120 | 0.073 | 0.728 | 0.74 / 0.83 | 49 % |
| rpi4 | PSI cpu / memory / io | unsupported (ENOENT), 0 reads | 120 each | 0.030 | 0.138 / 0.081 / 0.076 | 0.14 / 0.15, 0.08 / 0.08, 0.08 / 0.08 | 20 % |

| board | reference runs CPU % | passive-parts runs CPU % | parts - reference | passive call CPU mean ms, reference / parts | use |
| --- | --- | --- | --- | --- | --- |
| orin | 1.448, 1.362 | 1.401, 1.384 | -0.012 pp | 0.834 / 0.854 (+2 %) | per-part values usable as shares |
| rpi4 | 0.917, 0.995 | 0.995, 1.067 | +0.075 pp (+7.8 %) | 1.385 / 1.727 (**+25 %**) | diagnostic only: the timing itself inflates the passive call |

- Parts sum to 0.78 of 0.85 ms (Orin) and 1.49 of 1.73 ms (Raspberry Pi) of the passive call; the rest is
  the call's own result building and, in these runs, the wrapper.
- PSI: the cost is three failed `open` calls and their error conversion per collection; the successful PSI
  path (parsing and rates) was not measured anywhere, and its cost is not 0.
- Elapsed and CPU time are almost equal for every part; no part waited noticeably, but this does not say
  how a slow or busy storage device would behave.
- These are the parts' costs with everything on. Network or Disk alone would still pay its share of the
  publish copy and the larger response, which this split does not attribute.

## Table 4 — then and now

| | D0 (`low-overhead-cadence.md`) | now |
| --- | --- | --- |
| product | `dabb89f` (before #28, #29) | `36e817c` |
| bench | `a6288e3` | `462c5d0` (same `passive.py`) |
| design | interval matrix, B0 B1 B1 B0 once per interval; clients phase one window per variant | two A B B A blocks per client count, 1 s only |
| Orin B1 - B0, 1 client | +0.178 pp (B0 1.75, B1 1.93) | +0.161 pp (B0 1.49, B1 1.65) |
| Orin B1 - B0, 0 clients | +0.15 pp (single windows) | +0.203 pp |
| Raspberry Pi B1 - B0, 1 client | +0.250 pp (B0 1.40, B1 1.65) | +0.241 pp (B0 1.20, B1 1.44) |
| Raspberry Pi B1 - B0, 0 clients | +0.18 pp (single windows) | +0.198 pp |
| passive pooled p95 Orin / Raspberry Pi | 1.40 / 1.64 ms | 1.41 / 1.69 ms |

The B0 level fell by D2/D3 as reported there; the passive increment did not: it is about the same size
as in D0. Product version, bench version, run order and the date differ between the two, so the change
between them is not attributed to a specific optimization. D3 removed the per-request deep copy, so the
larger snapshot no longer costs a copy per request, but it is still copied once per collection when
published (Table 2).

## Limits

- The prototype covers RX/TX bytes and packets of every interface in `/proc/net/dev`, and throughput, IOPS
  and I/O time of the block devices its name filter keeps; not errors/drops, link state, Wi-Fi RSSI, RTT or
  filesystem capacity. Its filter's generality is not verified here.
- `_bench_passive` has no diagnostics, metadata, freshness or provenance integration; a product version
  would add cost and has to be measured again. The snapshot's `oldest_current_read_start` says nothing
  about the passive values.
- PSI was unsupported on all three platforms; its success path is unmeasured.
- 1 s only: the 2 s and 5 s targets of the current main are not re-checked.
- The Orin's process-level difference is not larger than its repeat spread; its verdict rests on both
  block deltas and on the collection-thread CPU.

## Reproduce

```sh
python3 benchmarks/cadence.py run --out DIR --alias orin --phase passive-rebaseline --budget-min 60 --max-windows 24 \
    --watch-url http://127.0.0.1:9797/api/status --source-sha 462c5d0
python3 benchmarks/cadence.py run --out DIR --alias orin --phase passive-parts --budget-min 60 --max-windows 24 \
    --watch-url http://127.0.0.1:9797/api/status --source-sha 462c5d0
python3 benchmarks/cadence.py report --md DIR/results.jsonl   # Tables 1-3 plus the block and parts tables
```

Raw records (`results.jsonl`, one per board) are kept outside the repository; SHA-256 (first 16 hex
digits): orin `d29b9a4c9747d155`, rpi4 `d52147dfa221951e`, wsl `bbee93155072ac92`. The tables above were
aggregated from them with `cadence.py report` at `462c5d0`; re-running it gives the same output. They were
re-aggregated with this branch's later report code, which gives a block no delta when its runs differ in
interval, poll, clients, Python, product or bench hash or scope: every block matched, no number changed.
