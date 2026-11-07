# Process group: design and validation plan (draft)

> **TL;DR**
> - Built on `feat/processes-group` (2026-11-07), **off by default**: a `processes` slow group in
>   `/api/observations` with the top N processes by CPU and by memory, plus host-wide counts, every 5 s from
>   `/proc/<pid>/stat` ([contract](../reference/processes.md)).
> - Orin Nano, whole service, A B B A: **+0.21 / +0.23 pp** of one core (target 0.2 pp: missed by 0.02,
>   accepted by the owner); the default configuration with it on is 0.87 %, far below the 2.5 % budget. Memory with full replay:
>   +0.5 MiB PSS. Correct against `ps` and known loads on x86 and the Orin.
> - Not checked: the Raspberry Pi (unavailable), a DynamicUser system unit (needs sudo). No history
>   (series slots already full on the PC), no command lines.

Status: design review requested by the owner on 2026-11-07, then built and checked on the PC and the Orin
the same day ("Results" below). The contract sections describe what was built, with the changes the checks
led to noted there; the owner's decisions are at the end.

## Why

The core snapshot says *that* the host is busy (CPU, memory, pressure); it does not say *which* process.
Answering that today needs a shell on the device. A top-N list next to the existing metrics answers it
remotely, and in replay and the fleet server it answers it *after the fact*: what was running when an
anomaly happened, which is the input the post-processing goals (anomaly detection, failure prediction)
lack. A full process manager (sorting every process, killing, tree views) is out of scope: `top` and
`htop` already do that on the device.

## Constraints found

| Constraint | Evidence | Consequence |
| --- | --- | --- |
| Cost grows with the process count | x86 probe below: about 8.5 µs of thread CPU per process per observation | not in the 1 s core collection; a slow group at 5 s or more |
| Unprivileged service | `packaging/debian/platmon.service` uses `DynamicUser=yes`; `/proc/1/io` answers `EACCES` to a normal user (checked on the PC), and so do `smaps_rollup` and `environ` of other users | CPU times, RSS, state, threads, `comm` and `cgroup` only (all world-readable); no per-process I/O or PSS |
| Unauthenticated listener | `[http] bind = 0.0.0.0` by default ([deployment.md](../guides/deployment.md)) | no `cmdline` (arguments can hold passwords and tokens); names from `comm` (at most 15 bytes) and the cgroup unit |
| HTTP never triggers reads | [architecture.md](architecture.md), [observations.md](../reference/observations.md) | no on-demand `/api/processes`; the group publishes, requests read the last publication |
| Fleet server contract | Hub 1.1.1 validates observation groups generically (`hub/protocol.py` `observations()`), fetches replay only for its known streams (`hub/history/raw.py` `STREAMS`) and projects history only for `OBSERVATION_GROUPS` | a new group is ignored by the released Hub, not refused (to confirm with `hub_contract.py` in CI); capturing it needs a Hub release |
| Hub response limit | `HttpAgentClient(max_bytes=512 * 1024)` | top N bounded (x86 probe: about 2.7 KiB for N = 10) |
| Containers | Compose does not share the host PID namespace ([docker-scope.md](../reference/docker-scope.md)) | the container sees only its own processes: reported as such (below), not presented as the host |
| PID churn | PIDs are reused, processes appear and vanish between listing and reading | identity `(pid, starttime)`; a process gone mid-read is skipped, not an error |

## Proposed contract

Configuration, with the same keys and checks as the other slow groups:

```ini
[processes]
; top processes by CPU and by memory, read from /proc/<pid>/stat on their own thread
enabled = no        ; default decided after the board measurement (open decision 1)
interval = 5        ; seconds between observations, 2 to 3600
top = 10            ; rows per ranking, 1 to 50; the two rankings are merged
```

`/api/observations` gains `groups.processes` with the usual envelope (`state`, `interval_ms`,
`stale_after_ms`, `observation`, `collector`). Its `data`:

```json
{
  "scope": "host",
  "total": 512, "running": 1, "blocked": 0, "zombie": 0, "threads": 2036,
  "top": 10,
  "processes": [
    {"pid": 6696, "name": "gnome-shell", "kernel": false, "state": "S", "cpu_percent": 5.2,
     "rss_bytes": 317444096, "threads": 26, "unit": "org.gnome.Shell@ubuntu.service"}
  ]
}
```

- **Rows:** the union of the top `top` by `cpu_percent` and the top `top` by `rss_bytes` (at most
  2 × `top`), sorted by CPU and then by RSS. Kernel threads are included, flagged by `kernel` (the
  `PF_KTHREAD` flag in `stat`): a busy `kworker` or `irq/…` thread is a real answer.
- **`cpu_percent`:** user + system ticks over the time between the two observations, × 100, in **% of one
  core** (as [budget.md](../performance/budget.md); it can exceed 100 for a multithreaded process). For a
  process first seen in this observation: its ticks since it started over its age. `null` in the very
  first observation (no previous reading). With 100 Hz ticks and 5 s, the resolution is 0.2 %.
- **`rss_bytes`:** `VmRSS` of `/proc/<pid>/status`, read for the selected rows only; ranking uses `stat`
  field 24. Changed while building: on kernel 7.0 `stat`'s count is the approximate per-CPU counter, below
  `VmRSS` by up to 3.5 MiB per process (217 of 218 processes differed on the PC), so the live check against
  `ps` failed for small processes. Shared pages count in every process; PSS is not readable without
  privileges.
- **`name`:** `comm` (field 2, between the first `(` and the last `)`), bytes that are not UTF-8 replaced
  by U+FFFD. **`unit`:** the last component of the cgroup v2 path (`0::/system.slice/ssh.service` →
  `ssh.service`), read only for the selected rows; `null` when unreadable or on cgroup v1 without a
  unified line.
- **Counts:** `total` processes listed, `running` (R), `blocked` (D, uninterruptible: usually waiting
  for storage), `zombie` (Z), `threads` (sum of field 20).
- **`scope`:** `host`, or `container` when the service does not run in the initial PID namespace (the
  container sees only its own processes); `hidepid` limiting the view is a `partial` collector state
  with a reason.
- **States:** `ok`; `partial` when some processes could not be read for reasons other than exiting, or
  `/proc` is mounted with `hidepid`; `error` when `/proc` cannot be listed. Windows: the group is not
  started (`NtQuerySystemInformation(SystemProcessInformation)` would give the same fields without
  administrator rights; not designed in this round).

Not collected: command lines, environment, owners (user names), per-process I/O, PSS, open files,
network sockets, process trees, events for processes that start and exit between two observations.
Short-lived processes are invisible to a sampler; the host's CPU total in `/api/stats` already counts
them.

History and replay:

- **History:** none (changed while building). Per-process series would grow without bound, and even the
  five counts would cost other series their slots: on the development PC `/api/history` already holds its
  64 series and drops 30 (platmon 1.3.0 there), and `observation/` ranks above `temperature/`, so the counts
  would push out temperatures. The Orin holds 42.
- **Replay:** the whole observation, as for the other groups. At 5 s and the default 600 s retention:
  120 records of about 3 KiB, about 360 KiB of the 16 MiB shared replay capacity. This counts in the
  memory budget and is measured (below).

Clients: a Processes view in the web page and the terminal client (`/text`), starting in
`frontends/ui-spec.json`. No thresholds or Needs attention entries in the first version.

## Implementation outline

- `native/src/collect/linux/observe/processes.rs`: list `/proc`, read each `stat` with the existing
  `with_file` path (raw `openat`, read to EOF, `close`), parse, keep a `HashMap<(pid, starttime), ticks>`
  of the previous observation only (replaced every time, so it holds live processes and nothing else),
  select, read `cgroup` for the selected rows, build the `Value`. The reader closure owns the map
  (`slow::Reader` is `FnMut`).
- `config.rs` section and ranges, `main.rs` wiring like `battery`, `history.rs` points for the counts.
- `ENOENT`/`ESRCH` on a PID's file: the process exited, skipped silently. Other errors on one PID:
  counted, the group `partial` with the first reason.

## Probe (2026-11-07)

### x86

A standalone Rust probe doing exactly one observation as outlined (same raw syscalls as `with_file`,
static musl build with rust 1.88, as the server is built). Source, binaries and raw output:
`~/.local/state/platmon-measure/processes-20261107/` (SHA256SUMS there). Host: the development PC
(Core Ultra 7 155H, 22 CPUs, kernel 7.0, governor powersave, desktop session running), run as a normal
user, 3 warmup observations then 60 at 1 s (24 at 5 s), thread CPU per observation:

| mode | processes | thread CPU mean / p95 (ms) | elapsed p95 (ms) | payload | pp of one core at 5 s |
| --- | --- | --- | --- | --- | --- |
| `stat` only (lower bound) | 503 | 3.21 / 3.91 | 3.91 | — | 0.064 |
| design (top 10, 1 s) | 504 | 4.29 / 4.99 | 4.99 | 2.7 KiB (max 3.2) | 0.086 |
| design, `openat` relative to `/proc`, one read per file | 514 | 4.20 / 4.58 | 4.59 | 2.7 KiB | 0.084 |
| design (top 10, 5 s) | 519 | 4.45 / 5.00 | 5.00 | 2.7 KiB | 0.089 |

- The read is about 75 % of the cost (kernel work in procfs); parsing, the map and selection the rest.
  The relative-`openat` variant saves little (2 %) and is not worth a second code path.
- The interval barely changes the cost per observation (4.29 vs 4.45 ms), so cost per second scales with
  1 / interval.
- Probe peak RSS 0.5–0.6 MiB; `cgroup` readable for every selected row as a normal user.
- An observation's elapsed p95 (5 ms on x86) is above the 2 ms responsiveness criterion, which is written
  for periodic calls in the core collection. On its own thread it delays no other reading (as storage and
  Wi-Fi, whose call p95 is also above 2 ms, [budget.md](../performance/budget.md)); recorded as an
  exception to accept, not a pass.

### Orin Nano (V1, 2026-11-07)

Same probe binary (aarch64, SHA-256 `1b682c9e…`), same runs, as the user `jorin`. Host: L4T R36.5.2,
kernel 5.15.199-tegra, 6 CPUs, MAXN_SUPER, governor schedutil, the production `platmon.service` (1.7.0-1)
and a GitHub Actions runner service running alongside, 302 processes. Files removed from the board
afterwards; raw output in the same directory (`orin.jsonl`).

| mode | processes | thread CPU mean / p95 (ms) | elapsed p95 (ms) | payload | pp of one core at 5 s |
| --- | --- | --- | --- | --- | --- |
| `stat` only (lower bound) | 302 | 7.68 / 8.30 | 8.36 | — | 0.154 |
| design (top 10, 1 s) | 302 | 12.67 / 13.36 | 13.44 | 1.7 KiB (max 2.1) | 0.253 |
| design, `openat` relative to `/proc`, one read per file | 302 | 12.14 / 12.87 | 12.93 | 1.7 KiB | 0.243 |
| design (top 10, 5 s) | 303 | 13.95 / 14.78 | 14.90 | 2.1 KiB (max 2.7) | **0.279** |

- **Below the 0.3 pp stop line, above the 0.2 pp V2 target** at 5 s: per the plan, reassess before V2.
- About 42 µs per process (x86: 8.5 µs). The work outside the read is a larger share than on x86: 5.0 ms
  of 12.7 (40 %, x86 25 %); the probe's parse allocates per process (a field vector, the `comm` string,
  SipHash map). A parse that keeps only byte offsets and builds names for the selected rows only is the
  obvious next step; the read itself (7.7 ms, 0.15 pp at 5 s) is the floor at this process count.
- A 5 s interval costs 10 % more per observation than 1 s (13.95 vs 12.67 ms), most likely colder caches.
- At 10 s the measured design is 0.14 pp.

### Allocation-free parse (reassessment, 2026-11-07)

Mode `mvp2` of the same probe (source SHA-256 `860fd80d…`, aarch64 `06159b09…`): the same reads (absolute
paths, read to EOF, as `with_file`), but `/proc` listed with `getdents64` into a reused buffer, `stat`
parsed by byte offsets, `comm` kept inline (16 bytes), reused vectors and maps with a simple hasher, and a
partial selection (`select_nth_unstable`). Mode `check` runs both parsers on the same bytes: equal for
every process on x86 (517 of 517, three runs) and the Orin (304 of 304; 302 of 304 with 2 exited between
listing and reading, skipped as designed), and the `getdents64` listing equal to `read_dir`.

Orin, same conditions as above, 5 s, in A B B A order in one session (`orin2.jsonl`):

| order | parser | thread CPU mean / p95 (ms) | elapsed p95 (ms) | pp of one core at 5 s | at 10 s |
| --- | --- | --- | --- | --- | --- |
| A | `mvp` (above) | 14.05 / 14.70 | 14.83 | 0.281 | 0.141 |
| B | `mvp2` | 8.45 / 8.77 | 8.82 | **0.169** | 0.085 |
| B | `mvp2` | 8.40 / 8.73 | 8.79 | **0.168** | 0.084 |
| A | `mvp` | 14.02 / 14.71 | 14.80 | 0.280 | 0.140 |

- 40 % less per observation; now below the 0.2 pp V2 target at 5 s, with the probe alone. The service adds
  publication, replay and history on top: V2 decides.
- What is left is the read: 8.4 ms against the 7.7 ms `stat`-only floor. At 1 s `mvp2` is 7.56 ms
  (0.76 pp), so the 1 s core collection stays ruled out.
- The implementation follows `mvp2`; `mvp` stays as the record of why.

The Raspberry Pi 4 was not reachable on this network (no address for `samchi-pi.local`); its V1 run is
still open.

## Validation plan

Defined before measuring (CLAUDE.md: baseline, target, allowed regressions, budget).

**Baseline:** the released server's default configuration (A) against the same build with
`[processes] enabled = yes` at the default interval (B), per board (Orin Nano, Raspberry Pi 4), x86 as a
third host.

| # | Check | Target (proposal) | How |
| --- | --- | --- | --- |
| V1 | Probe cost on the boards | per observation thread CPU and elapsed p95, process count recorded; decides the interval | the probe above, `stat` / design × 1 s and 5 s, about 6 min per board, files removed afterwards |
| V2 | Whole-service cost | B − A ≤ 0.2 pp per board at the chosen interval; if on by default, the whole default configuration ≤ 2.5 % per board | budget.md conditions: 1 s collection, one `/api/stats` client, 30 s warmup + 120 s, A B B A |
| V3 | Memory | settled PSS B − A ≤ 1 MiB with full replay buffers; within the release's PSS and VmHWM budgets | `native/tools/service_ab.py` windows with full buffers, smaps_rollup |
| V4 | Churn | no growth in the previous-observation map or RSS while 200 short processes start and exit per second for 10 min; the observation's cost tracks the live count | a fork loop started and stopped by its saved PID |
| V5 | Correctness on fake `/proc` | equal to the expected answer for: `comm` with spaces, `(`, `)` and newlines; non-UTF-8 `comm`; a PID gone between listing and reading; PID reuse with a new start time; zombie; kernel thread; first observation (`null` CPU); ties; `top` larger than the process count | a fixture scenario in `native/tools/equivalence/fixture.sh`, mutants caught |
| V6 | Correctness live | RSS and state equal to `ps -o pid,stat,rss`; CPU within 1 percentage point of `pidstat 5 1` for the five busiest processes under a known load (`stress-ng --cpu 1`, or a busy loop started by PID) | x86, Orin, Pi |
| V7 | Privacy | no `cmdline`, environment or user field in the answer, the replay page or `/text`; checked by a unit test over the keys | unit test |
| V8 | Contract | `hub_contract.py` with the pinned Hub 1.1.1 passes; `/api/observations` with `top = 50` stays below 64 KiB | CI `native` job |
| V9 | Deployment contexts | `DynamicUser` package unit sees every PID (`scope: host`); Docker without `pid: host` answers `scope: container`; `hidepid=invisible` answers `partial` with its reason | package service check, container image, a mount with `hidepid` in the throwaway container |

**Allowed regressions:** none in the core collection (overruns, data-age fallbacks, collection
failures) or in existing groups.

**Budget:** V1 and V2 together one session per board. If V1 puts the design above about 0.3 pp at 5 s on
either board, stop and reassess (a 10 s default, a smaller `top`, or opt-in only) before V2.

**Not planned:** optimizing the scan before V1 says it is needed; Windows; Hub capture (a Hub release
after the agent ships).

## Results (2026-11-07, `feat/processes-group`)

Server builds from the branch (x86_64 `7155cd9c…`, aarch64 `503893d2…`, `native/build.sh`); scripts and raw
output in `~/.local/state/platmon-measure/processes-20261107/` (`livecheck.py`, `servicebench.py`,
`orin-service.txt`). Orin: as above, the production 1.7.0-1 service running alongside; files removed afterwards.

| # | Check | Result |
| --- | --- | --- |
| V1 | probe cost | Orin 8.4 ms per observation at 5 s (above); Pi not measured |
| V2 | whole service, Orin, A B B A, budget.md conditions | A 0.650 / 0.650 %, B 0.858 / 0.875 %: **+0.208 / +0.225 pp, target 0.2 pp missed by 0.017 (mean)**. The default configuration with the group on is 0.87 % (budget 2.5 %). In the service an observation takes 10.1 ms (p95 10.6), 1.7 ms more than the probe: the `status` reads for the rows and building the answer |
| V3 | memory, A and B together for 660 s (replay full) | settled PSS A 9080 KiB, B 9616 KiB: **+536 KiB** (target ≤ 1 MiB); the replay stream holds 120 records, 345 KB (2.9 KB each). Client errors 0 |
| V4 | churn, about 200 short processes per second for 300 s, replay and history off | VmRSS flat: x86 2224–2236 KiB, Orin 1640 → 1504 KiB; every observation `ok`. With replay and history on, the first 600 s grow by the buffers' filling, as without the group |
| V5 | unit tests on fake `/proc` | 9 tests (names with spaces and parentheses, bad lines, overflow, kernel threads, first observation `null`, deltas, new processes, PID reuse, exits between listing and reading, other errors, unlistable `/proc` resetting deltas, container scope, `hidepid`, VmRSS, churn bound, no command line or environment in the answer); 12 of 12 mutants caught. Not in `fixture.sh`: its recorded answers hold the four older groups |
| V6 | live against `ps` and known loads | x86 three runs and the Orin: a busy loop 99.5–100 %, a half-busy loop 50.0 %; name, RSS (±5 %), unit equal to `ps` and `/proc/<pid>/cgroup` for every row; no process above the memory floor left out; `total` equal to `/proc` |
| V7 | privacy | unit test over the keys; `cmdline = yes` is refused as an unknown key |
| V8 | fleet server contract | `hub_contract.py` with the defaults passes. With the group on, Hub 1.1.1's validators accept every answer and the capture through a restart passes, but the check fails on "groups the Hub does not store: processes": Hub ignores it |
| V9 | deployment contexts | Docker without `--pid=host`: `scope: container`, 3 processes; with it: `host`, 557, units resolved (both as `nobody`). `hidepid=invisible` and `hidepid=2`: `partial` / `permission_denied` on `/proc`. A user unit with `PrivateUsers=yes` (as `install_user.sh`): every process, `ok`. A DynamicUser system unit: not run (sudo) |
| — | regressions | `fixture.sh` 11/11, `runtime_fixture.py` 7/7, `config_fixture.py` 68/68 after `--bless` (the diff: the `processes` defaults in 13 recorded settings, and the observation-samples group message), `live_http.py` no difference, unit tests 273, `pytest` 261 passed, `cargo check` for Windows |

## Decisions (owner, 2026-11-07)

Accepted as built ("받아들이고 진행"):

1. **Default:** off. The Orin's +0.217 pp (V2) is accepted against the 0.2 pp feature target; turning it
   on by default waits for a Raspberry Pi measurement.
2. **Responsiveness:** an observation takes about 10 ms on the Orin, above the 2 ms criterion; accepted as
   for storage and Wi-Fi, since it runs on its own thread.
3. **Command lines:** never (an unknown key).
4. **Docker:** Compose unchanged; the container scope is reported and documented.

Still open: the release it ships in, the web page and `platmon` command views, and platmon-hub storing the
group (a Hub release).
