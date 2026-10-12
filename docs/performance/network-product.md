# Product Network off / on

What the product's own Network collection ([../network.md](../reference/network.md)) costs, measured as the same
product and bench code with `[network]` off (A) and on (B). Not the passive prototype: in these runs
`benchmarks/passive.py` is never called, and its earlier numbers
([passive-cost-rebaseline.md](passive-cost-rebaseline.md), Network part 0.026 / 0.047 pp) are not used as
a prediction or a pass.

## Decision

- The per-feature 0.1 pp target is **not met** in either round below (`31b4bbc`: +0.160 pp Orin,
  +0.175 pp Raspberry Pi) and stays recorded as not met. No further micro-optimization is planned.
- First decision (`671e2c1`): Network opt-in (default off) under that target.
- Superseded the same day by a new budget policy, [budget.md](budget.md): the default configuration's
  whole process ≤ 2.0 % of one core per board, with per-collection and per-request costs as review
  criteria. Re-evaluated under it from the `31b4bbc` measurements, Network on is 1.582 % (Orin) and
  1.402 % (Raspberry Pi), so it is **on by default** again, with `[network] enabled = no` to turn it off.

The defaults changed after the measurements, in later commits. The code paths that were measured
(`collect_recorded` with or without a `NetworkCounters`, selected explicitly by the bench's
`--product-network`) did not change, so the off/on numbers were not measured again on those commits;
they are the measurements of `8cc82b0` and `31b4bbc`.

## Result (2026-10-09, product `8cc82b0`)

| | Orin Nano | Raspberry Pi 4 |
| --- | --- | --- |
| **Main** on − off, 1 client polling at 1 s, collection at 1 s, ≤ 0.1 pp | **not met**: +0.141 pp (blocks +0.143, +0.138), larger than the spread between repeats (0.023 pp) | **not met**: +0.197 pp (blocks +0.203, +0.190), larger than the spread (0.061 pp) |
| on − off, 0 clients | +0.152 pp (one block; spread 0.053) | +0.199 pp (one block; spread 0.012) |
| Network call p95 ≤ 2 ms (thread CPU / elapsed, pooled) | **met**: 1.47 / 1.47 ms (480 calls, 4 runs; max of per-run p95 1.48) | **met**: 1.55 / 1.56 ms (480, 4; 1.57) |
| RSS on − off ≤ 5 MiB, no new errors, overruns or growth | **met**: +0.39 MiB (1 client), −1.87 MiB (0 clients); 0 failed, 0 overruns, 0 data-age fallbacks, 0 HTTP/client errors; second-half RSS growth ≤ 16 KiB except one on-run at 448 KiB (not repeated) | **met**: −0.16 / +0.03 MiB; 0 / 0 / 0 / 0; ≤ 12 KiB |
| Whole process ≤ 1 % of one core (separate target) | not met before Network either: off 1.45 %, on 1.59 % | not met before Network either: off 1.19 %, on 1.38 % |

- Network as built costs about **0.14-0.15 pp of one core on the Orin and 0.20 pp on the Raspberry Pi**
  at a 1 s interval, above the 0.1 pp budget on both boards, with or without a client. The difference
  is larger than the spread between repeats in every condition (distinguishable).
- Where it goes, per collection (means): the network call itself is 0.88 ms thread CPU on the Orin
  (10 interfaces) and 1.40 ms on the Raspberry Pi (3 interfaces), i.e. 0.088 / 0.140 pp at 1 Hz. The
  rest of the difference is in `Sampler._attempt` outside the call (Orin +1.25 ms in total, Pi +1.78 ms:
  checking the read, the publish copy of the larger snapshot) and per request (`do_GET` +0.14 /
  +0.16 ms CPU, response +3.5 / +1.3 KiB, about 0.015 pp at one request per second).
- The Pi with 3 interfaces costs more per call than the Orin with 10, so the interface count alone does
  not explain the difference between the boards (their CPUs, Python versions and clock policies differ
  too); how the cost grows with the number of interfaces on one board was not measured. A diagnostic
  split outside the windows (below) shows it spread over every step, with no duplicate read or
  measurement error. No optimization was tried on `8cc82b0`; one limited round follows in
  [Second round](#second-round-31b4bbc).

## Setup

| | |
| --- | --- |
| Product and bench | `8cc82b0` on `feat/passive-network` (product hash `a60c529904caebd8`, bench hash `40b8714324bd9ccd`), copied with `git archive` (SHA-256 of the archive `a4ee4af6…`) to its own directory on each device |
| Runs | `cadence.py run --phase network-product`: variant B0 (passive callback a no-op) in every run, `--product-network off` or `on`; collection 1 s, reference instrumentation, 30 s warmup + 120 s window, a new process and a new `NetworkCounters` per run; `--clients 1 --blocks 2` (c1-1, c1-2: off on on off each), then `--clients 0 --blocks 1` (c0-1) |
| What on runs | the product path: `collect_recorded(cpu, {}, clock, NetworkCounters(clock, max_gap))`: namespace, index list, counter read, rates, `collectors.network`, `network.read` checked by the Sampler. Each `NetworkCounters.sample` call is timed (two clock reads around it) |
| What off runs | `collect_recorded` without a `NetworkCounters`: no namespace, index or counter read in the window (0 network calls recorded in every off run). One reading after the window gives the run's interface scope |
| Comparable | per block and condition: same alias, interval, client count, Python, product and bench hash, sensor counts, collector states (without `collectors.network`, the intended difference), namespace id, interface count and names: no mismatch in any block |
| WSL | `--clients 1 --blocks 1`, supplementary, not combined with ARM |
| Budget | Orin 12 windows / 30.2 min of runs, Raspberry Pi 12 / 30.3 min (of 16 / 50, plus about 3 min each for the functional check and the diagnostic); WSL 4 / 10.1 min (of 8 / 20) |

Environment (anonymised):

| alias | hardware, OS | Python | clock policy | scope in every run |
| --- | --- | --- | --- | --- |
| orin | Jetson Orin Nano devkit (Super), L4T R36, 5.15-tegra, native | 3.10.12 | MAXN_SUPER, `schedutil` | 6 CPU rows, 6 temp / 3 power / 2 fan sensors, 20 source records, all collectors `ok`; host network namespace, 10 interfaces (loopback, Ethernet, Wi-Fi, CAN, USB gadget, Docker and L4T bridges, one veth) |
| rpi4 | Raspberry Pi 4 Model B, Debian 13, 6.18 rpi-v8, native | 3.13.5 | `ondemand` | 4 CPU rows, 1 temp sensor, 5 source records; host namespace, 3 interfaces |
| wsl | x86-64 PC, WSL 2, 6.6 | 3.12.3 | not exposed | 20 CPU rows, no sensors; 6 interfaces |

The measured runs are native processes in the host namespace. platmon's own Docker deployment uses a
bridge network: there it reports the container's namespace (its `eth0` and `lo`), which was not
benchmarked here.

Each board's own platmon kept running at `e1bc82a` and was not touched. Before and after: Orin
container id, image, start time, restart count 0, healthy; Raspberry Pi PID and start time; checkout
SHA with a clean tree and the INI hash; both services' `instance_id` (no restart), port 9797 answering
200, the rollback tags and folders (SHA256SUMS verify where present), power mode and governor.
`get_throttled` 0x0 before and after every Raspberry Pi run; hottest zone after each run 52.6-53.1 °C (Orin),
51.1-52.6 °C (Pi). Every run valid; no skipped run, timeout, clock jump or dropped record. The bench's
servers, clients and the diagnostic were the only processes started, and they have all exited; the test
ports 19797/19798 are closed.

## Table 1 — Network off / on (CPU % of one core, user + system, 120 s windows)

| board | clients | block | off runs | on runs | on − off pp |
| --- | --- | --- | --- | --- | --- |
| orin | 1 | c1-1 | 1.444, 1.461 | 1.594, 1.598 | +0.143 |
| orin | 1 | c1-2 | 1.447, 1.443 | 1.590, 1.575 | +0.138 |
| orin | 1 | **all** | mean 1.449, spread 0.019 | mean 1.589, spread 0.023 | **+0.141** (distinguishable) |
| orin | 0 | c0-1 | 1.252, 1.265 | 1.437, 1.384 | **+0.152** (spread 0.053, distinguishable) |
| rpi4 | 1 | c1-1 | 1.182, 1.199 | 1.371, 1.417 | +0.203 |
| rpi4 | 1 | c1-2 | 1.159, 1.201 | 1.356, 1.384 | +0.190 |
| rpi4 | 1 | **all** | mean 1.185, spread 0.042 | mean 1.382, spread 0.061 | **+0.197** (distinguishable) |
| rpi4 | 0 | c0-1 | 0.807, 0.819 | 1.011, 1.012 | **+0.199** (spread 0.012, distinguishable) |
| wsl | 1 | c1-1 | 0.491, 0.470 | 0.556, 0.523 | +0.058 (spread 0.033; supplementary) |

CPU % = 100 × Δ(user + system CPU seconds of the server process) / measured elapsed seconds, never
divided by the CPU count. Block delta = mean of its two on runs − mean of its two off runs; "all" = mean
of the four on runs − mean of the four off runs; spread = largest − smallest run of one arm;
distinguishable = the difference is larger than both arms' spread. Values are measured; deltas are
calculated from them.

## Table 2 — Where it goes (means per collection or request; pooled percentiles over all calls)

| board | clients | network call thread CPU mean / p50 / p95 ms | elapsed p95 / max ms | `_attempt` CPU off → on ms | `_attempt` p95 off → on ms | `do_GET` CPU off → on ms | response KiB off → on | client latency p95 off → on ms | actual interval p50 / max ms (on) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| orin | 1 | 0.876 / 0.761 / 1.468 | 1.472 / 2.434 | 12.63 → 13.89 | 17.78 → 19.66 | 0.60 → 0.75 | 6.8 → 10.3 | 3.46 → 3.59 | 1000.2 / 1000.5 |
| orin | 0 | 0.876 / 0.728 / 1.463 | 1.477 / 2.346 | 12.35 → 13.87 | 17.60 → 20.85 | - | - | - | 1000.2 / 1000.5 |
| rpi4 | 1 | 1.405 / 1.450 / 1.552 | 1.563 / 1.714 | 7.83 → 9.61 | 8.20 → 10.26 | 1.20 → 1.36 | 3.0 → 4.3 | 5.41 → 5.56 | 1000.2 / 1000.3 |
| rpi4 | 0 | 1.398 / 1.433 / 1.530 | 1.532 / 1.634 | 7.70 → 9.66 | 8.15 → 10.26 | - | - | - | 1000.2 / 1000.3 |
| wsl | 1 | 0.547 / 0.591 / 0.806 | 0.806 / 1.570 | 2.76 → 3.34 | 3.83 → 4.94 | 0.58 → 0.61 | 6.1 → 8.3 | 3.68 → 3.97 | 1000.3 / 1003.7 |

Pooled percentiles are over all calls of the condition's on runs (480 per 1-client row, 240 per 0-client
row); the largest of the runs' own p95 is in the Result table. `_attempt` CPU is the collector thread's
CPU for a whole collection, including the network call.

## Table 3 — Diagnostic split of the network call (outside the windows)

A separate process on each board after its runs, calling the product's `NetworkCounters.sample` 60
times at 1 Hz with each step wrapped in a thread-CPU timer (the wrappers add their own cost; the total
is above the bench's value). Diagnostic only, never used for the result.

| step (thread CPU, mean µs) | Orin | Raspberry Pi |
| --- | --- | --- |
| `stat("/proc/self/ns/net")` | 71 | 121 |
| namespace id (JSON + SHA-256) | 128 | 261 |
| read `/proc/self/net/dev` | 248 | 321 |
| parse (header, rows, counters) | 413 | 340 |
| `socket.if_nameindex()` | 368 | 314 |
| rates, output, diagnostics (the rest) | 441 | 482 |
| **total** | 1669 | 1839 |

The same code in a tight loop on WSL takes 68 µs per call; at one call per second it takes about ten
times that, as on the boards. Why was not measured: cold caches or CPU clocks dropping between calls
are hypotheses, not findings. Tight-loop numbers are therefore not a substitute for the periodic runs.

Candidates for a follow-up within the same contract (not tried on `8cc82b0`; the first is part of the
second round below, together with a single `int()` per counter): compute the
namespace id only when the namespace's device/inode changes; a leaner row parser; building the output
with fewer intermediate objects. Their combined effect on the 0.14-0.20 pp is unknown; none removes
the publish-side cost of the larger snapshot.

## Second round (`31b4bbc`)

One limited optimization, then the same off/on comparison on the new SHA. The `8cc82b0` results above
stay as they are; they are the result for that SHA.

- Change (`31b4bbc`): the namespace is still looked up with `stat` on every reading, but its id string
  is only hashed again when the device/inode differs from the last successful lookup (one pair per
  `NetworkCounters`; a failed lookup still gives `id: null`). Each counter field is converted with
  `int()` once instead of twice; the accepted inputs are unchanged. Nothing else: same interfaces,
  fields, reads, identity lookups and publish path.
- Runs: product and bench `31b4bbc` (product hash `7be8055be68d6fd5`, bench hash `40b8714324bd9ccd`,
  unchanged), `--phase network-product --clients 1 --blocks 2`: 8 windows per board, 30 s warmup + 120 s,
  20.2 min of runs each (budget 30 min / 8 windows). Same boards, Python, scope (10 / 3 interfaces) and
  services; every run valid, every block comparable, 0 network calls in off runs, 0 failed collections,
  overruns, data-age fallbacks, HTTP or client errors; `get_throttled` 0x0 before and after every
  Raspberry Pi run. The services' state: the full record taken before this round was not kept (it was
  in a temporary directory that is gone, and none was written on the devices). What remains is an excerpt
  printed when it was taken (Orin container id, image, start time, restart count; both checkout SHAs;
  power mode; governors; both services' `instance_id`; the Raspberry Pi PID; `get_throttled`), which the
  state after the round matches. The INI hash, the rollback SHA256SUMS and the images after the round
  match the first round's record. That is a comparison with earlier records, not a direct before/after
  comparison of one full record.

| board | block | off runs | on runs | on − off pp |
| --- | --- | --- | --- | --- |
| orin | c1-1 | 1.417, 1.430 | 1.572, 1.589 | +0.157 |
| orin | c1-2 | 1.405, 1.435 | 1.574, 1.593 | +0.163 |
| orin | **all** | mean 1.422, spread 0.030 | mean 1.582, spread 0.021 | **+0.160** (distinguishable) |
| rpi4 | c1-1 | 1.216, 1.199 | 1.434, 1.389 | +0.204 |
| rpi4 | c1-2 | 1.258, 1.238 | 1.440, 1.347 | +0.145 |
| rpi4 | **all** | mean 1.228, spread 0.060 | mean 1.402, spread 0.093 | **+0.175** (distinguishable) |

| board | network call thread CPU mean / p50 / p95 ms | elapsed p95 / max ms (max of per-run p95) | `_attempt` CPU off → on ms | `do_GET` CPU off → on ms | RSS end off → on MiB |
| --- | --- | --- | --- | --- | --- |
| orin | 0.790 / 0.710 / 1.331 | 1.338 / 1.460 (1.361) | 12.37 → 13.83 | 0.60 → 0.75 | 25.81 → 25.59 |
| rpi4 | 1.255 / 1.258 / 1.359 | 1.370 / 1.711 (1.453) | 8.07 → 9.65 | 1.28 → 1.46 | 29.24 → 29.07 |

- **Main budget (≤ 0.1 pp): still not met** on either board: +0.160 pp (Orin), +0.175 pp (Raspberry Pi).
- The network call itself got cheaper: mean thread CPU 0.876 → 0.790 ms (Orin), 1.405 → 1.255 ms
  (Raspberry Pi); p95 1.47 → 1.33 / 1.55 → 1.36 ms. That is about 0.009 / 0.015 pp at 1 Hz, smaller than
  the run-to-run spread, so the process-level difference does not show it: the Orin's on − off is
  higher than on `8cc82b0` (+0.141), the Pi's lower (+0.197). These are separate sessions on different
  SHAs; the comparison with the first round is for reference only, not a measured effect of the change.
- Call p95 ≤ 2 ms, RSS ≤ 5 MiB and no new errors or overruns: still met.

## Reproduce

```bash
python3 benchmarks/cadence.py run --out DIR --alias NAME --phase network-product --clients 1 --blocks 2 \
    --budget-min 50 --max-windows 16 --watch-url http://127.0.0.1:9797/api/stats --source-sha 8cc82b0
python3 benchmarks/cadence.py run --out DIR --alias NAME --phase network-product --clients 0 --blocks 1 \
    --budget-min 50 --max-windows 16 --watch-url http://127.0.0.1:9797/api/stats --source-sha 8cc82b0
python3 benchmarks/cadence.py report --md DIR/results.jsonl
```

Raw files stay on each device in `~/platmon-netbench-8cc82b0/raw-<alias>/results.jsonl` (not in the
repository):

| alias | `results.jsonl` SHA-256 |
| --- | --- |
| orin | `20e3de9ee33c259a5ea5e6f7c9edee7db3d3140745460e75ceab0183730d3876` |
| rpi4 | `5aadf23268a1c5f969f0c3f7aae87f8e1f483122c6fc96a751bf90440d9ebc74` |
| wsl | `5f676487b97b75daaa4ae0aac11776a633096439d573a5ce84d566fb0549a44e` |

Second round, in `~/platmon-netbench-31b4bbc/raw-<alias>/results.jsonl`:

| alias | `results.jsonl` SHA-256 |
| --- | --- |
| orin | `6f236b7aefd6a5f815b8269e49e18f396391f2a23b3a422dd80f64c428b7f30e` |
| rpi4 | `6e07e7a7441f39752ecf34974bba6749e435435b7203f84a80f146dc3188447e` |
