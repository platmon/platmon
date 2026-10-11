# Performance budget

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

How platmon's CPU cost is judged before a feature ships or becomes a default. These are internal
product targets for a lightweight monitor, derived from the measurements in this directory. They are not
an industry standard, a safety limit for the host's own workload, or a real-time guarantee: average CPU
share says nothing about a task's worst-case latency or deadlines.

## Policy (since 2026-10-09)

| Level | Target | Role |
| --- | --- | --- |
| **Default configuration (release target)** | daemon CPU (user + system, the whole process) **≤ 2.0 % of one core** with every feature that is on by default | the gate for what runs by default |
| **Feature collection cost (review criterion)** | the increase in collector-thread CPU per collection (`Sampler._attempt`, so the feature's own call plus checking and publishing its data) **about ≤ 2 ms per collection**, reported next to the increase per request (`do_GET`) | finds expensive features early and starts a design review; not an automatic pass or fail |
| **Responsiveness** | a periodic passive collection's own call: elapsed p95 ≤ 2 ms | a low average must not hide a slow call |
| **Regressions** | no lasting memory growth, new collection failures, overruns, data-age fallbacks or data-ownership problems | correctness, apart from the numbers |

Conditions of the release target, judged **per board** (never averaged across boards, so one board
cannot hide another's excess): native process, collection every 1 s, one client reading `/api/stats`
every 1 s, reference instrumentation, 30 s warmup + 120 s windows in A B B A order, with the power mode,
governor, Python version and observed scope (sensors, interfaces) recorded. Boards: Jetson Orin Nano
and Raspberry Pi 4. The target is checked under these conditions; it is not a limit platmon holds under
any request rate, interface count or host load.

### Units

CPU % (pp for a difference) is `100 × Δ(user + system CPU s) / elapsed s` of the process over a measured
window, never divided by the CPU count. It is the number that is judged. Per collection and per
request costs (thread CPU in ms) explain where it goes:

    Δ CPU (pp) ≈ collections/s × Δ ms per collection / 10 + requests/s × Δ ms per request / 10 + other

This is an approximation for explaining a result, not a way to compute one. A longer interval does not
halve the cost if clients keep polling at the same rate, and the ms per collection also depend on the
board and the cadence.

### How a feature is decided

1. Measure the feature off / on in one tree (same SHA, only the feature's setting differs).
2. Check the feature criteria (collection ms, request ms, call p95, regressions). Above about 2 ms per
   collection, review the design before going further.
3. Check the release target for the configuration the feature would ship in. On by default only if
   the default configuration stays ≤ 2.0 % on every board; otherwise it ships off by default (opt-in)
   with its measured cost stated, or is reworked.
4. Write the measured cost in the feature's documentation.

Targets are set before a feature is measured. A feature that misses them does not get the target
raised for it. Estimates from adding up earlier per-part numbers are fine for planning, never for a
release decision: the integrated cost is measured on the configuration that includes everything already
on by default.

## Earlier policy (until 2026-10-09)

The earlier targets were a whole process ≤ 1 % of one core and a feature's increase ≤ 0.1 pp. Their
results stand as recorded:

- The whole process ≤ 1 % is not met by the current default without Network (Orin 1.42 %, Raspberry Pi
  1.23 %, [network-product.md](network-product.md) second round) nor in earlier reports. It remains a
  long-term improvement goal, no longer a release condition.
- Network's ≤ 0.1 pp is **not met** (`8cc82b0`: +0.141 / +0.197 pp; `31b4bbc`: +0.160 / +0.175 pp),
  with differences larger than the spread between repeats. That result is not changed by this policy.

Why it changed: with a fixed per-feature increase, the total is not controlled as features add up, while
most useful features would end up opt-in for missing a margin close to the measurement noise (repeats
differ by 0.02-0.09 pp), each needing about 30 minutes of windows per board to judge. A total for the
default configuration controls what users actually run; the feature criteria keep each addition visible.

## Network under this policy

Re-evaluated from the `31b4bbc` measurements; not a new experiment and not a new improvement.

| | Orin Nano | Raspberry Pi 4 |
| --- | --- | --- |
| whole process with Network on (release target ≤ 2.0 %) | 1.582 % | 1.402 % |
| collector-thread CPU per collection, off → on | 12.37 → 13.83 ms (+1.46) | 8.07 → 9.65 ms (+1.58) |
| request CPU (`do_GET`), off → on | 0.60 → 0.75 ms (+0.15) | 1.28 → 1.46 ms (+0.18) |
| the network call itself, mean thread CPU | 0.790 ms | 1.255 ms |
| network call elapsed p95 (≤ 2 ms) | 1.338 ms | 1.370 ms |
| regressions | none | none |
| headroom to 2.0 % (means) | about 0.42 pp | about 0.60 pp |

Network is therefore **on by default**, with `[network] enabled = no` to turn it off; an INI that already
says `no` stays off. Further features are measured on top of this configuration, and the headroom above
is not a reserved share for any of them.

## Disk I/O under this policy

Measured on top of Network on ([disk-io-product.md](disk-io-product.md), `e184f8c`, after one design
review: the first round missed the call p95 criterion on the Raspberry Pi).

| | Orin Nano | Raspberry Pi 4 |
| --- | --- | --- |
| whole process with Network and Disk I/O on (≤ 2.0 %) | 1.750 % | 1.558 % |
| collector CPU per collection, off → on | +1.46 ms | +1.12 ms |
| request CPU (`do_GET`), off → on | +0.05 ms | +0.08 ms |
| disk call elapsed p95 (≤ 2 ms) | 0.937 ms | 1.176 ms |
| regressions | none | none |
| headroom to 2.0 % (means) | about 0.25 pp | about 0.44 pp |

Disk I/O is therefore **on by default**, with `[disk_io] enabled = no` to turn it off.

## Device-wake sensors on x86 hosts (2026-10-11)

Not a feature decision and not a new experiment: a finding about a host class the conditions above do
not cover. Both reference boards are measured with their own sensors, and neither a Jetson Orin Nano nor
a Raspberry Pi 4 has an NVMe hwmon channel or an Intel Wi-Fi thermal zone, so the responsiveness
criterion had never been checked against a sensor whose read wakes a device. The release target, the
feature criteria and the recorded board results above are unchanged.

Measured on an x86_64 PC (22 logical CPUs, Ubuntu 26.04, kernel 7.0, NVMe root, Intel Wi-Fi) with every
feature on by default and `[core] interval = 1.0`, after the thermal-zone twin fix. Two of the 102 reads
of a collection cost 13.3 ms of its 16.7 ms of reading; the other 100 cost 3.5 ms together.

| | nvme `temp1_input` | iwlwifi zone `temp` |
| --- | --- | --- |
| elapsed per read at the 1 s cadence (criterion: p95 ≤ 2 ms) | 9.5 ms | 3.5 ms |
| elapsed per read in a tight loop, the device never idle | 1.70 ms | 0.14 ms |
| thread CPU of that elapsed time | 0.371 ms (4 %) | 0.275 ms (10 %) |
| elapsed per read at a 5 s / 15 s cadence | 8.84 / 8.64 ms | 4.55 / 4.52 ms |

Reading either one wakes the device: an NVMe admin command to a drive in an APST low-power state, and a
firmware command to the Wi-Fi card. The cost is waiting, not computing, and it does not fall with a
longer interval — only how often it is paid does.

What that does and does not reach, on the same host:

| | result |
| --- | --- |
| whole process, default configuration, idle (release target ≤ 2.0 %) | about 0.8 % over 4 h 45 m |
| collection elapsed, median / p95 | 18.8 / 27.7 ms |
| cadence at `interval = 1.0` | 30 collections in 30.1 s; no overruns, no stale snapshots |
| cadence at `interval = 0.1`, the shortest the config allows (other groups at their minimums) | 299 collections in 30.0 s (100 %), 4.6 % of one core |
| responsiveness, a call's elapsed p95 ≤ 2 ms | **not met** by these two channels |
| regressions: memory, failures, overruns, data-age fallbacks | none; 10 088 collections at a compressed cadence left no lasting growth |

**Accepted as it is.** The release target is met with margin precisely because the cost is wait and not
CPU, the cadence holds at every interval the config allows, and the alternatives are each worse. The
first two were measured on this host; the third was assessed against the API and the viewers:

- Leaving a channel out of the snapshots that did not read it keeps the data age honest
  (`data_age_basis` stays `oldest_current_read_start`, no log entries), but the web page's
  "Hottest of N sensors" then alternates between 26 and 28, the `platmon` screen's TEMP line gains and
  loses two entries from second to second, and the channel loses its history entirely: an identity that
  is not present every time never wins a slot back once `MAX_SERIES` is reached, because established
  series win ties (`collector/history.py`, `_fitting`).
- Carrying the last value forward keeps the key and stops the flicker, but every snapshot that carries
  one falls back to `cycle_start_upper_bound`, its `sensor_meta` says `read: null`, and the service logs
  `a value without a valid read` about 700 times an hour. The snapshot would stop saying when its own
  temperature was read.
- Moving the two channels to a low-frequency group would solve both, at the cost of taking two keys out
  of `/api/stats` and of both viewers merging two sources: a breaking change in exchange for 13 ms of
  waiting a second. Not measured.

The criterion stays as written. It is a review trigger for the work platmon does, and it did its job
here: it is what made these two channels visible. A channel whose cost is a device wake is recorded
here rather than quietly exempted, so the next host that shows one is compared against this.

Not measured: the power cost of waking an NVMe drive once a second, which matters on a battery-powered
host. If that is judged to matter, the low-frequency group becomes the better trade and this decision
should be revisited.
