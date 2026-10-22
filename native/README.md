# platmon-native (experimental)

A Rust port of the core of `collector.collect_recorded`, to measure what a native collector would cost
and to check whether it can produce the same output. It is next step 1 of
[rust-feasibility.md](../docs/performance/rust-feasibility.md). Step 3 (below) builds a Rust service
candidate on it that keeps the service's contracts, to compare the whole process. **Not part of the
product:** the service, the packages and the source archive do not include it, and nothing here changes
the release.

## Scope

Ported, with the same output as the Python collector:

- CPU usage per core and `cpu_sampling`
- CPU clocks, thermal zones, hwmon temperatures, power (including INA3221 rails) and fans
- memory, disk, `system`, `model`
- the Jetson extension: GPU load and clocks, nvpmodel power mode, pwm_tach, L4T release
- the DRM GPU (i915, amdgpu, other drivers by name), then the nvidia-smi fallback, and the accel NPU
  (intel_vpu)
- `collectors` states, reasons and issues
- sensor source ids (SHA-256 descriptors), the JSON Pointers of `sensor_meta`, every value's read span and
  the required reads' spans
- the passive groups, wired as the service enables them by default (max_gap 5 s): Network
  (`/proc/self/net/dev`, the namespace id, ifindex from `if_nameindex`), Disk I/O (`/proc/diskstats`, physical
  disks by their `/sys/block` link) and Pressure (`/proc/pressure`), with their read spans

Not ported in steps 1 and 2: the slow groups (storage, Wi-Fi, battery, probe), the nvidia-smi fallback,
and everything in `runtime/` (Sampler, history, replay, HTTP). So the collector of steps 1 and 2 is the 1 s
core collection used on the three hosts checked, extended with Network, Disk I/O and Pressure, with the
same output on the fixtures defined here; not the whole default collection of the product. Step 3 (below)
adds the slow groups and `runtime/`, and the support scope step the nvidia-smi fallback and `--frontends`
([Support scope](#support-scope)).

Behaviour kept from Python where bypassing std made it explicit:

- **Clock:** collection times use `CLOCK_BOOTTIME`, `CLOCK_MONOTONIC` where it is missing, as
  `sampler.pick_clock` does. std's `Instant` is not used for them: on Linux it is `CLOCK_MONOTONIC`, which
  stops during suspend, so a window across a suspend would look shorter than it was and not count as a gap.
- **Interrupted calls:** the raw `openat` and `read` (see below) retry on `EINTR`, as Python and `std::fs` do.
  `close` is not retried.
- **Listings** are all or nothing, as `os.listdir`: an error while iterating fails the listing and is noted,
  instead of returning the entries read so far.
- **Text that is not UTF-8:** a group read notes `invalid_data`. A plain read (model, os-release, uptime, the
  device tree's compatible) fails the collection, or the start, as Python's `UnicodeDecodeError` does.
- **Counters are Python ints:** 64-bit counters, and bytes (sectors × 512) beyond 64 bits, are written as
  the exact integer. A rate converts the delta to a float before multiplying, as `delta * 1e9 / window`
  does in Python, so a large delta rounds the same way.
- **Names:** `str.split`, `str.strip` and `str.isprintable` as Python defines them. `isprintable` is exact
  for ASCII; beyond it Python follows its Unicode database, which this does not carry (see
  [Not covered](#equivalence-on-fake-trees)).

Dependencies: `libc` (MIT or Apache-2.0, no dependencies of its own) for system call numbers, flags and
struct layouts per target. JSON output and SHA-256 are implemented here, because the output has to match
Python's float repr and key order exactly, and both are tested against Python. Builds target `x86_64` and
`aarch64-unknown-linux-musl` as static binaries.

The allocator cache measured on 2026-10-18 (`src/alloc.rs`) is behind the `cache-alloc` feature, and
mimalloc, compared on 2026-10-20, behind the `mimalloc` feature (`build-mimalloc.sh`); neither is a
candidate. The default build uses the system allocator; see [Results](#results).

## Step 3: the runtime

The goal is a Rust service candidate that keeps the existing contracts, so that the whole process can be
compared with the Python service, not a switch to Rust. The Python service and its packages stay as they
are; nothing changes in the product until the comparison is in and decided. Default system allocator, the
same dependency policy (only `libc`); the `cache-alloc` build is not part of step 3.

| Stage | Scope | Done when |
| --- | --- | --- |
| 3A, publication and live API | Sampler (one writer, immutable publications, states and freshness, data age from the read records), live HTTP (`/api/stats`, `/api/status`, `/api/observations`, the web page), the slow groups' execution structure (one thread each, a blocked read stalls only its group) | the publication, failure, recovery and concurrent request contracts hold with set collections and clocks; slow readers may be fakes |
| 3B, service features | history, replay, the real slow readers, `/api/history`, `/api/samples`, `/api/observation-samples`, `/text`, `platmon.ini`; compatible with the existing clients | every feature of the default configuration to be compared exists on both sides |
| 3C, whole-service A/B | Python service against the Rust candidate, same configuration and request load | CPU, memory, response times, collection cadence, retention and recovery, judged together |

Status (2026-10-22, #93; the candidate under verification: [CANDIDATE.md](CANDIDATE.md)): 3A, 3B and 3C done ([results](#whole-service-ab-step-3c)), 3C judged again and the PC's replay cost split ([results](#replay-cost-after-3c)); the system allocator kept against mimalloc, which breaks the memory rule on every host ([results](#system-against-mimalloc)); the nvidia-smi fallback and `--frontends` ported, the support scope written down ([Support scope](#support-scope)); candidate 1 passes the final comparison on every host ([results](#final-comparison-candidate-1)) and the deployed form (service checks on three hosts, 24 h on both boards; [results](#final-comparison-candidate-1)); nvidia-smi checked on a GPU through WSL2. No switch decided.
With 3B the candidate has every feature of
the default configuration on the three hosts compared (x86 PC, Orin, Raspberry Pi), checked against the
Python service below. Missing then, ported since: the nvidia-smi fallback of the GPU and the `--frontends`
option.

**Contracts 3B must keep** (from the Python service and docs/reference): requests never collect; a slow
group has its own id and times, and a blocked read neither blocks the core nor queues more work for its
group. History keeps failures, gaps and counter resets as null and broken segments, never 0; a changed
device identity starts a new series; series that went away are dropped after the retention. Replay keeps
`instance_id`, a cursor per stream, `gap_before`, `missing_ranges` and `next_after`, answers 409 after a
restart, and moves the cursor on also when everything asked for has expired. Published data can be reused,
but never a finished answer: ages and `stale` are computed per answer, also for replayed samples.

**3C, set before it was run (2026-10-20).** `tools/service_ab.py`, one A B B A round per host:
- A: the Python service of v1.3.0 (`e767176`, the code the 1.3.0-1 package runs), `python3 platmon.py
  INI` from a `git archive` of the tag, with the host's own python3. B: `platmon-native serve INI --web
  TREE/frontends/web --app-version 1.3.0`, native source `35cdbdf`, rebuilt to the same sha256 as recorded
  for 3B, default system allocator. Both as the same login user, one at a time, on 127.0.0.1:19797, with the
  same INI: the defaults with `[core] interval = 1.0`. The packaged service and the production Hub keep
  running beside it, as in the 1.3.0 board runs.
- Load: 0-150 s the budget client (`/api/stats` every 1 s), w1 = 30-150 s, the release window as
  budget.md has it; the same client until w2 = 1290-1410 s, the release window again with history and
  replay full and expiring (two retention periods in); then a poller doing what platmon-hub does for one
  node (status and stats every 5 s, observations every 10 s, replay turns of 100 over every stream), w3 =
  1440-1560 s; then no client for 480 s (idle window 1590-1710 s); then the poller reconnects from its
  cursors and fetches the 480 s backlog, five times from the same cursors, then polls 120 s more.
- Recorded: CPU over each window (whole process, /proc ticks), response times, the cadence from the
  published samples and the replayed records, requests, records and bytes the poller got, its data lag,
  every replay page checked by `walk.py`; VmRSS every 5 s (50 ms during the reconnect) and smaps_rollup
  RSS/PSS with the replay ranges and history points held at w1's end, with full buffers, after the poller,
  before and after the reconnect and at the end.
- Judged per host, never averaged, A against B: w1 and w2 against the 2.5 % release target each; the
  proposed gates of docs/performance/rust-feasibility.md (whole-service CPU at least 30 % lower, RSS/PSS
  at least 50 % lower) from w2 and w3 and the full-buffer memory, w1 shown beside them, not used alone; the
  reconnect by its time, CPU seconds and peak memory for the same backlog.
- Not allowed for B, whatever the CPU: an error answer, a loss (a missing range after a stream's first
  page, or in the backlog), a walk problem, a missing collection id, `gap_before`, a cadence step over
  1.5 s, a response p95 above A's in the same window by more than 1 ms.
- Budget: one round per host, about 2.5 h, the three hosts at once. A run is repeated once at most, and
  only when it is invalid (the tool failed, the host was disturbed); the candidate is not changed during
  3C. If B breaks a rule above, 3C stops there and is reported, not fixed inside 3C.
- Not in 3C: the 24 h stability gate, other hosts (a PC with nvidia-smi), the `cache-alloc` build.

**After 3C, set before it was run (2026-10-20, after the sixth review).** The 3C judgement first, then the
PC's replay cost.
- The judgement: `ab_report.py` passed a run whose poller thread had died (`join()` never re-raises), a
  window with one answer, a reconnect with no collection seen, and never compared w3's `/api/stats` p95.
  Now a run is first checked for completeness: the threads' end (`tools/worker.py`, shared with
  `live_http.py`), the requests, answers, collections and records each phase implies, every reconnect
  round done, every memory point; an incomplete run is invalid, not passed. The stored 3C runs are judged
  again with it; what they did not record (the threads' end) is named, not assumed.
- The PC's replay: `platmon-native replay-cost FILE` against `tools/replay_cost_py.py FILE TREE`, on the
  same core publications: one page from each host's service (Orin and Pi: the 1.3.0 packages, default
  settings; PC: its local service), 80 records each, kept outside the repository with their hashes. Per
  record, the median of 7 rounds of 20 pages: decoding, decoding and writing back, a whole page (B:
  `Replay.view`; A: `Replay.view` and the server's `json.dumps`). Every input on every host.
- Hypothesis: on the PC's 20 KB records B decodes or writes slower than A; the split says which. If
  neither is slower, stop: the cost is elsewhere and is not guessed at.
- Candidates, at most three, in this order and only where the split points: an object written without a
  temporary list of its members when the keys are not sorted; a float written without a temporary
  string; the parser's search for a repeated key. Each must keep every answer byte for byte (the same
  key order, numbers, ages and `stale` per answer, the 2 MiB limit), checked on the three inputs, with
  `cargo test` and `live_http.py`. No other JSON library, no allocator change, no cached answers.
- Target: B's page at most A's per record on the PC's input, without a slower page on the boards' inputs;
  then one PC A B B A run of `service_ab.py` with the changed candidate, judged as 3C was (its reconnect
  CPU per round against A's). The boards' A/B and the 24 h run wait for the final candidate.
- Budget: the three candidates and that one PC run (about 2.5 h). If B is still above A after them, stop
  and report what is left.

**System against mimalloc, set before it was run (2026-10-20, after the seventh review).** One outside
allocator, compared with the default; the default build stays the system allocator, and `cache-alloc`
stays a diagnosis, never a candidate. Nothing else changes: no JSON or replay change meanwhile.
- The allocator: mimalloc 3.3.2 (MIT) through the `mimalloc` crate `=0.1.52` (MIT; `libmimalloc-sys`
  0.1.49, MIT; built with `cc` 1.5.1, `find-msvc-tools` 0.1.14, `shlex` 2.0.1, MIT OR Apache-2.0; every
  version at least two weeks old, pinned in Cargo.lock), as `#[global_allocator]` behind the `mimalloc`
  feature (Rust's allocations; libc's own malloc is not replaced). Its defaults: no crate feature, no
  `MIMALLOC_*` setting, no purge or huge page tuning. Its C is compiled by `musl-gcc` (Debian's
  `musl-tools`) in the same `rust:1.88-slim` image, natively on each architecture (aarch64 on the
  Orin's docker), with the compiler's default CPU baseline (x86-64, armv8-a), never `-march=native`; the
  compile commands are recorded.
- Stage 1, the measure: `replay-cost` and `replay_cost_py.py` decode a page keeping the 80 records alive
  and free them together on both sides, and time writing alone on records decoded beforehand (same
  data on both sides); the whole page stays as it is. Elapsed times; the inputs of the replay cost run.
- Stage 2, fixed pages: System against mimalloc on the three inputs, on the three hosts, twice each.
  On to stage 3 only if the PC's page on the PC input gets faster by more than the larger of 5 % and the
  spread of the two repeats, and no host and input gets slower by more than its spread.
- Stage 3, the whole service: `service_ab.py` with the two binaries (S = system, M = mimalloc), S M M S
  per host, the three at once, the 3C phases plus, after the reconnect rounds, three rounds of four
  pollers fetching the same backlog at once, and 120 s with no client to settle. Recorded as in 3C, plus
  threads and open file descriptors at every point. Rules for M against S: the 3C rules (complete, no
  error, loss, missing id, `gap_before`, cadence step over 1.5 s, p95 more than 1 ms above S's in the
  same window, the concurrent pages too); threads and descriptors back to their count before the
  reconnect after settling; PSS with full buffers, at the concurrent peak and after settling at most
  25 % above S's (the memory saving against Python is part of why Rust).
- Decision: mimalloc only if stage 3 keeps every rule and its CPU (w2, w3, reconnect, concurrent) is
  lower than S's beyond the spread of the repeats; otherwise the system allocator stays. Either way, the
  3C results and the PC's earlier misses stay as recorded; nothing is estimated from page numbers.
- Budget: one allocator, one configuration, stage 2 once, stage 3 one round per host (about 2.8 h).
  If a stage fails its condition, stop and report; no tuning, no JSON redesign, no `cache-alloc`.

**Support scope and the features not ported, set before the work (2026-10-20, after the eighth review).**
The system allocator is the candidate's; allocator and JSON experiments end here. In order: the support
scope written down (below, [Support scope](#support-scope)), the two features not ported, then the
candidate fixed (features, build configuration), then the integrated comparison and the deployed form's
long run, install, upgrade and recovery. This step is the first two.
- nvidia-smi: the Python behaviour, nothing new (no NVML). Done when, on fake trees with a fake command
  against `collector/gpu.py`, equal for: discovery (absent, on PATH, a DRM reader first), several GPUs
  (the smallest UUID), numbers and their rounding, units, `N/A` and not supported, source ids and read
  spans; a timeout, a nonzero exit, a signal, output that is not UTF-8 or not the CSV expected, a command
  no longer executable or gone; the next collection after a failure recovers without the old values; no
  child process left behind. Mutants of each of these must be caught. Without an NVIDIA GPU here: "fixture
  verified, not verified on NVIDIA hardware".
- `--frontends`: the order of `platmon.load_config` kept (defaults, the file and its checks, then
  `--frontends`, then "nothing to run"); compared with the same inputs: precedence over `enabled =`,
  unknown and empty names, an empty value, the file's own errors first; and the refusals of the two
  servers as processes (exit 2, the same message). No new frontend system.
- The `platmon` command (`frontends/cli.py`) stays the Python client; it is not rewritten.
- Build: the release path builds the system allocator only (`build.sh`: no features, `--locked`, both musl
  targets, SHA-256, and a check of the allocator each binary carries); `cache-alloc` and `mimalloc` are
  built only by their own scripts, as records.

**Final comparison, set before it was run (2026-10-20, after the ninth review).** Candidate 1
([CANDIDATE.md](CANDIDATE.md): commit `7f066f0`, x86_64 `027e35b1c9251b9d`, aarch64 `ff97aa9ad4e9dd28`)
against the Python service 1.3.0, `service_ab.py` as there, A B B A on each host, the three at once (about
3 h). Nothing in the candidate or the tools changes during the runs; what comes out is the result. Each
run is judged on its own, each host on its own; no averaging hides one run.
- Complete, else invalid (no verdict): `complete()` as before, now also the collections during the
  concurrent rounds from the after walk (`collected`: as many as the seconds less 4).
- Contracts (a breach fails the run): no error answer, loss, walk problem, missing id, `gap_before` or
  cadence step over 1.5 s in any window, the concurrent rounds included (`collected`, which closes the
  step that stayed unverified in the allocator runs); every reconnect round done; threads and open
  descriptors back to their count before the reconnect 120 s after the rounds.
- Budgets of the candidate (a breach fails the run): the release target, w1 and w2 at most 2.5 % of one
  core; PSS at most the budget of [Support scope](#support-scope) with full buffers, at the four pollers'
  peak and 120 s after (PC 22 / 87 / 24, Orin 13 / 46 / 14, Pi 7 / 27 / 8 MiB). Judged only with the
  same data: replay records per stream and history points as the Python runs held them (one record per
  stream and one point per series fewer at most, for the moment of reading), the same history series,
  the same concurrent backlog; otherwise the run is invalid for the budget. The peak is the highest
  reading: one before round 1, samples every 50 ms (at least 5 a second on average, else invalid), one
  after each round; a sampled peak, not a bound in continuous time. Python's runs are not held to it.
  The 25 % rule against a baseline is not used here (it chose the allocator).
- Reported, not pass or fail: CPU and PSS against Python (the proposed −30 % / −50 % gates), response p95
  more than 1 ms above Python's (as performance exceptions), the reconnect cost.
- Changed from 3C, and seen before it was set: in 3C, a p95 more than 1 ms above Python's broke a rule.
  Here it is reported as a performance exception, following the review's classification (contracts,
  completeness and budgets are pass or fail; relative performance is reported). A short `--quick` run on
  the PC (A B, to try the budget judgement) had already shown the candidate's concurrent page p95 above
  Python's (108 against 65 ms, smaller backlog).
- Budget: one round per host. A run that is invalid because of the tools: fix the tool, record it, run
  that host again once. Then the deployed form (a service unit, 24 h, install, upgrade, recovery), only
  for this same candidate.

**What the candidate has** (`platmon-native serve [platmon.ini] [--frontends LIST] --web DIR --app-version V`):

3A (`src/runtime.rs`, `src/slow.rs`, `src/http.rs`):
- One writer thread collects every interval. A publication is immutable: its stats (with the passive
  groups' `read` set from the records), `collectors` and `sensor_meta` are serialized once, before it is
  published. Each answer writes only what depends on its moment around them: `sample` with the ages,
  `state`, `ready`. The state lock is held only to count the attempt, give the sequence and swap the
  publication, and to read it; the replaced publication is let go after the lock (a unit test stops a
  writer inside serialization and reads the previous snapshot meanwhile).
- States, freshness, `collection_failed`, `collection_too_slow`, consecutive failures and the data age with
  its basis follow `runtime/sampler.py`, including every reason to fall back to the collection start.
- JSON as `json.dumps` writes it (`", "`, `": "`, non-ASCII escaped), so the answers are byte for byte the
  Python service's for the same state.
- HTTP/1.0 on std sockets, one thread per connection, at most 32 (excess ones closed before a thread
  starts), backlog 5, 10 s per request read, 10 s per response write without progress, 64 KiB writes;
  request line and header limits (the blank line that ends the headers counts), errors and their status
  lines as `http.server` gives them; HEAD gets the 501's headers without its page; HTTP/0.9, also when
  said, gets the body alone.
- Slow groups: one thread each, the next observation an interval after the last one started; their entries
  as `runtime/slow.py` writes them.

3B:
- History (`src/history.rs`): one point per publication from its publisher, after it is published and
  outside the Sampler's lock; gaps as null (failed or stalled publishers, values without a rate, missing
  identities), identities as series ids, the 64-series priority, expiry, gap-keeping downsampling; reads
  copy points under the lock and build the answer outside it.
- Replay (`src/replay.rs`): each publication serialized once by its publisher outside the replay lock;
  requests take references to the payloads under the lock and decode them outside it; ages and `stale` per
  answer; cursors, missing ranges, 409s and the byte, record and response bounds as `runtime/replay.py`.
- `/api/history`, `/api/samples`, `/api/observation-samples` with `server.py`'s query parsing
  (`parse_qs`, its limits and messages); `/text` (`src/render.rs`, the `platmon` command's `render()`).
- `platmon.ini` (`src/config.rs`): configparser's syntax (sections, `=`/`:`, comments, continuation lines,
  DEFAULT, duplicate and parsing errors with its messages), `check()`, `levels()`, the probe targets, the
  same refusals; groups, history, replay and the web page on and off as configured.
- The slow readers (`src/observe.rs`): storage (partitions of physical disks, local filesystems by device
  number, statvfs through a pinned descriptor), Wi-Fi (`/proc/net/wireless`, the namespace and ifindex,
  the carrier), battery (`/sys/class/power_supply`), the TCP connect probe.

## Support scope

Written down 2026-10-20 (after the eighth review), for the candidate as it is on #93: `platmon-native serve`
built by `build.sh`, the system allocator. Four kinds: **implemented** (in the source, compared with the
Python service on fixtures), **verified on hardware** (also run live on a host below), **unverified**
(implemented, not shown on hardware or not by a fixture either), **unsupported** (not in the candidate).

| area | implemented and compared | verified on hardware | unverified / unsupported |
| --- | --- | --- | --- |
| targets | `x86_64` and `aarch64-unknown-linux-musl`, static, one binary per architecture | x86_64 PC (Ubuntu 26.04, kernel 7.0, i915 GPU, intel_vpu NPU, a battery); Jetson Orin Nano (Ubuntu 22.04 L4T, kernel 5.15); Raspberry Pi 4 (Debian 13, kernel 6.18) | other boards and distributions unverified; 32-bit ARM and other architectures unsupported |
| core collection | every group of `collect_recorded` with the passive groups (fixtures, 8 scenarios) | the three hosts (`compare.py`, `live_http.py`) | the fixtures' "not covered" lists below |
| GPU | DRM: i915, amdgpu, other drivers by name; Jetson's own reader first; then the nvidia-smi fallback (3 scenarios, 32 collections, the Orin's real answer replayed) | i915 on the PC, the Jetson GPU on the Orin; the Orin's nvidia-smi (`nvidia-l4t-nvml`) answered the query once, read by hand: no `GPU-` UUID, every field `[N/A]` (both sides: `invalid_data`); it is not reached there, the Jetson reader answers first; **the fallback on a real NVIDIA GPU under WSL2** (GeForce RTX 4070 Ti, Windows driver 610.88, `/usr/lib/wsl/lib/nvidia-smi`; [results](#nvidia-smi-on-an-nvidia-gpu-wsl2)) | the nvidia-smi fallback with a native Linux NVIDIA driver on a PC or server: **not verified on such hardware** (WSL2 and the fixtures only; NVIDIA does not promise a stable nvidia-smi output); amdgpu fixture only; NVML unsupported; clocks of 2^128 Hz and more refused (below) |
| slow groups | storage, Wi-Fi, battery, probe (fixture `slow`) | what the three hosts have | |
| configuration | `platmon.ini` as `load_config` reads and checks it, `--frontends` after it (67 cases, also the two servers' refusals) | on the three hosts (Python 3.14, 3.10, 3.13) | Unicode digits and whitespace in numbers, non-UTF-8 file names and files; argparse's abbreviations (`--front`) and a value that starts with `-` are unsupported |
| HTTP API, web page, `/text`, history, replay | as `live_http.py` and `runtime_fixture.py` check them | the three hosts, no difference | which connections of a burst are refused (timing); Python versions' error page markup |
| clients | the HTTP contracts the Hub and the web page use | the Hub-like poller of 3C | the `platmon` command (`frontends/cli.py`) stays the Python client and is not rewritten: the daemon runs without Python, a package that also installs the command still needs Python 3 for it (packaging decides later) |

**Load and memory.** Verified: the default configuration with the 3C load (1 s collections, a Hub-like
poller, a 480 s reconnect backlog fetched five times) and four pollers fetching the same backlog at once,
three rounds (the allocator comparison). Not verified: more concurrent replays. The HTTP server's limit of
32 handler threads bounds threads, not the memory 32 replay pages at once would hold; it is not a
verified load. The candidate's PSS there (system allocator, 2026-10-20, mean of two runs), and the budget
for the integrated run, set now, before it (measured + 25 %, to the MiB above; the owner may change it
before that run):

| PSS MiB | PC measured / budget | Orin | Pi |
| --- | --- | --- | --- |
| buffers full | 17.3 / 22 | 9.7 / 13 | 5.4 / 7 |
| four pollers' peak | 69.6 / 87 | 36.7 / 46 | 21.4 / 27 |
| 120 s after | 18.7 / 24 | 10.7 / 14 | 5.9 / 8 |

The 25 % rule against S chose an allocator; these absolute numbers are the candidate's own budget for
the load above. Python's PSS with full buffers was 32.8 / 25.4 / 21.8 MiB (3C); its four-poller peak was
not measured.

**Python versions.** The comparisons run against the Python service on 3.14 (PC), 3.10 (Orin) and 3.13
(Pi). Where Python itself differs, the candidate follows 3.11 and later: a NUL inside nvidia-smi's CSV is a
character (3.10's `csv` refuses the whole answer, 3.11 and later refuse only that field's number).

**A deliberate difference.** A clock nvidia-smi gives as 2^128 Hz or more (over 3.4 × 10^32 MHz) is null
with `invalid_data`; Python publishes the integer. No GPU reports such a clock; the candidate refuses it
rather than publish a number it cannot hold (a float to integer cast saturates).

**Build.** `build.sh` is the candidate's build: the system allocator only (`--no-default-features`; `--all-features`
is never a build of it, and the two allocator features cannot be built together), `Cargo.lock` as
committed (`--locked`), both musl targets, each binary checked to carry the system allocator's mark
(`platmon-native build-info` prints it), then the SHA-256. `build-cache-alloc.sh` and
`build-mimalloc.sh` build the two records, never the candidate. The candidate under verification, with
its source, build, binaries, running conditions and comparison, is fixed in [CANDIDATE.md](CANDIDATE.md).

## Use

```sh
native/build.sh                     # in a rust:1.88 container; unit tests, then the system allocator build for both targets
native/build-cache-alloc.sh         # the cache-alloc build (a record, for bench.sh's C), into target/cache-alloc
native/tools/fixture.sh             # deterministic equivalence on fake trees, in a container
PYTHONPATH=. python3 native/tools/compare.py native/target/x86_64-unknown-linux-musl/release/platmon-native
PYTHONPATH=. native/tools/bench.sh native/target/x86_64-unknown-linux-musl/release/platmon-native out.jsonl \
    native/target/cache-alloc/x86_64-unknown-linux-musl/release/platmon-native
python3 native/tools/runtime_fixture.py native/target/x86_64-unknown-linux-musl/release/platmon-native
PYTHONPATH=. python3 native/tools/config_fixture.py native/target/x86_64-unknown-linux-musl/release/platmon-native
PYTHONPATH=. python3 native/tools/live_http.py native/target/x86_64-unknown-linux-musl/release/platmon-native
python3 native/tools/service_ab.py --tree PYTHON_TREE --binary BINARY --out ab.jsonl   # about 2.5 h; --quick to try it
python3 native/tools/ab_report.py ab.jsonl
BINARY replay-cost core.jsonl 20; python3 native/tools/replay_cost_py.py core.jsonl PYTHON_TREE 20
python3 native/tools/hub_check.py native|python --hub HUB_EXECUTABLE --hosts pc,orin=USER@HOST --out DIR   # 13 min
```

On a board, use the installed collector as the reference: `PYTHONPATH=/usr/share/platmon`.

- `fixture.sh` / `fixture.py`: **the equivalence test.** Fake `/sys` and `/proc` trees are mounted over the
  real ones in a throwaway container (a local Debian 13 image with python3, built on first use and never
  pushed). Both collectors run as `nobody`, so file modes apply, on the same clocks (`platmon-native script`
  and `fixture_py.py`; the elapsed clock advances 1 µs per call, wall time is fixed). Every output value is
  compared exactly: JSON types, key order, numbers, read spans, required reads, `collectors` and source ids.
  No tolerance. Each step states whether it expects a collection or a failed collection; both sides must
  give that and exit with 0 at the end, and a side that exits, hangs or writes something else fails the
  run. The timeout (30 s) is one deadline for the whole answer line, not for its first byte or each piece,
  and a line over 4 MiB is not read further. Before the scenarios, the harness checks itself against fake
  sides (`fixture.py --self-test`, no container needed): both failing where a collection is expected, a
  nonzero exit, a hang (also after part of an answer, or an answer completed in pieces past the deadline),
  output that is not JSON and an oversized line must each be reported. Interfaces are real ones in the
  container's own network namespace (empty bridges, created and removed by ioctl, so the kernel gives them
  their ifindex); the container has `NET_ADMIN` there and no network.
- `compare.py`: **a live smoke test** on a real host, both collectors at the same moment. Structure, types,
  identities, `collectors`, source ids, which values have a span, the clock source, and spans within 5 s of
  each other are exact. Readings that move between two reads are only checked to be plausible (a GPU clock
  is not negative, a usage is within 0–100 %): this does not show numeric equivalence.
- `bench.sh`: the Python collector (A) against platmon-native (B), and optionally a second build (C), over
  the same scope: A B B A, or A B C C B A, with 30 s warmup and 120 s windows. Each window reports its own
  CPU (`getrusage`), RSS at the start, every 30 collections and the end, collection time p50/p95/max,
  missed 1 s deadlines, how often each group was in each state, the number of source ids of the last
  collection, and its read scope: the SHA-256 of the sorted source ids and the interfaces, disks and
  pressure resources it read, with how many different scopes came up in the window. stderr goes to
  `OUT.jsonl.log`.
- `runtime_fixture.py`: **the runtime equivalence test.** The service's Sampler, slow groups, History,
  Replay and HTTP server (`runtime_py.py`, wired as `platmon.py` wires them) and `platmon-native
  runtime-script` get the same operations: a collection begins, a view is taken while it runs, it ends
  with a given collection or error; the same for observations; raw HTTP requests to each side's real
  server. Every answer must be the same text: the `/api/stats`, `/api/status` and `/api/observations`
  bodies, and whole HTTP answers without their `Date` and `Server` headers and without the markup of error
  pages (it differs between Python versions; the status line with the message stays). Runs on the host,
  reads no tree; the same self-test and deadlines as `fixture.py`.
- `config_fixture.py`: platmon.ini files read by `platmon.load_config` and `platmon-native config-check`:
  every setting with its type, the levels and the probe targets, or the same refusal message, also with
  a `--frontends` value; then refusals of `platmon.py` and `platmon-native serve` as processes.
- `live_http.py`: **a live check of the service.** Starts the Python service and `platmon-native serve`
  with the same platmon.ini (the defaults, each on its own local port), checks both the same way and
  compares them (see its docstring). It stops only the two processes it started, by their handles.
- `service_ab.py` / `ab_report.py`: **the whole-service A/B of 3C** (see their docstrings). Each run is
  judged complete first (its threads' end through `worker.py`, the counts its phases imply), then by
  the rules; an incomplete run is invalid. Both, `walk.py` and `worker.py` run their self-tests first.
- `replay-cost` / `replay_cost_py.py`: what a replay page costs per record, by stage, on fixed core
  publications, Rust and Python; both can write the page, to compare it byte for byte.

## Results

### Equivalence on fake trees

`fixture.sh`, 2026-10-20 with the nvidia-smi scenarios: 11 of 11 scenarios, 72 collections and observations
equal and the 2 expected failures on both sides (the x86_64 build of the support scope step). Before, 2026-10-19, x86_64 build: 8 of 8 scenarios, no difference: 40 collections and observations
equal and the 2 expected failures (below) failed on both sides; every side exited with 0. Harness
self-test: 15 of 15 controls reported as they must be.

| scenario | what it covers |
| --- | --- |
| `pc` | i915 GPU (0 MHz in RC6, RC6 share, a reset, a share above 100 % clamped to 0), intel_vpu NPU, ACPI zones with a hwmon twin, duplicate chip names, two chips on one device, a fan with and without a tachometer, INA3221 rails and a sum channel; CPU warmup, usage, a regressed counter, no ticks, the same clock twice (invalid interval), a gap over 5 s, a core coming online, a fan chip removed |
| `failures` | unreadable files and directories (`permission_denied`), a chip that disappeared (dangling link), invalid numbers and text that is not UTF-8 (`invalid_data`), empty files, the os-release fallback, an unknown DRM driver next to amdgpu, no accel class; `/proc/stat` missing (an expected failure) and back (warmup again), a model that is not UTF-8 (an expected failure), class directories that cannot be listed |
| `jetson` | Tegra device tree, devfreq GPU, nvpmodel mode by name, L4T release, INA3221 rails, pwmfan and pwm_tach; then an invalid nvpmodel status, an unreadable rpm and a GPU without load |
| `bare` | a host with nothing optional: every group unavailable with its reason |
| `pi` | Raspberry Pi device tree, a zone whose hwmon twin is named with `_`, a DRM card (`v3d`) named but not read |
| `passive` | interfaces with and without an ifindex (one named beyond ASCII), physical, virtual and partition disks, a disk with the 11 fields of older kernels; rates, counters reset (no rate, a new baseline), in flight falling (a gauge, not a reset), an interface and a disk removed and others added, an interface back under its name with a new ifindex, a counter at 2^64−1 and one beyond (not a counter), bytes beyond 64 bits, a delta whose rate depends on the float conversion, windows of 1.7 s and 0.3 s, a gap, the same clock twice, the network namespace changed and the rates after it, a disk whose `/sys/block` link went away (its classification is kept) |
| `slow` | the slow groups' readers (`observe-script` against `observe_py.py`), 5 steps: partitions of physical, virtual and missing disks, a row listed twice; mountinfo with the real device numbers of the container's mounts, a bind mount, an escaped path, read only from the super-options, a file bind listed before a directory (`ENOTDIR`, skipped), a mount point gone, another device behind a path (`mount_changed`), one nobody cannot reach, non-local and non-`/dev` mounts, lines that do not parse; Wi-Fi rows connected, down, with a moved ifindex, an unreadable carrier, no ifindex, a level that is not dBm and one of 0, out of range, not a row; batteries present, an empty bay, invalid numbers and text, an unreadable attribute, a UPS, external supplies, one without a type; TCP connects accepted, refused and unreachable (their times only checked to be ≥ 0); then mountinfo unreadable, files gone, files broken, nothing there |
| `nvidia` | a card no DRM reader knows (driver `nvidia`) and a fake `nvidia-smi` on PATH: two GPUs (the smallest UUID, 12.25 % rounded, MHz to Hz), `[N/A]` and `[Not Supported]`, out of range, `nan`, `inf`, underscores and exponents, quoted fields, CRLF and lone CR, a field over two lines, a NUL, no rows, an empty line, three fields, not a GPU UUID, a quote not closed, stdout and stderr that are not UTF-8, the Orin's real answer; past the 0.5 s limit, exit 3 after an answer, killed by a signal, the answer after (nothing kept), not executable, removed, back; then no process left (`/proc` of the container: no fake, `sleep` or zombie) and every run with the query's arguments |
| `nvidia_alone` | no DRM card: nvidia-smi answers for the GPU; exit 3 (no GPU entry), no rows, back |
| `nvidia_drm_first` | an i915 card and nvidia-smi present: the DRM reader answers, nvidia-smi is never run |
| `passive_failures` | a header that is not `/proc/net/dev`'s, diskstats that is not UTF-8, a pressure file that cannot be read, one that is not ASCII, one without `some`; then bad rows (an interface listed twice, 15 fields, invalid names, a line without `:`, a negative counter; a disk listed twice, too few fields, a later field that is not a counter, a major that is not a number, a line without a name, a `/sys/block` entry that is not a link), a namespace that cannot be identified, `some` twice, CRLF line ends; then every file gone, back (warmup), empty files, recovered |

The harness was checked against itself: deliberate defects in a copy of the Rust code were each reported.
Core groups, six: rail rounding, the span of a computed value, the RC6 clamp, the gap limit, `disappeared`
vs `not_exposed`, a 0 MHz clock dropped. Passive groups, eleven: `window_ms` dropped on a reset, in flight
taken for a counter, a namespace change ignored, no reuse of a disk's classification, CPU `full` kept,
counters written as 64-bit signed, the I/O ratio rounded to 3 digits, the pressure span ending at its
first read, duplicate interfaces kept, line numbers off by one, the rate computed without Python's float
conversion. The RC6 clamp, the I/O ratio and the float conversion were first missed (every window was
exactly 1 s), which added the clamped case to `pc` and the 1.7 s / 0.3 s windows and the large delta to
`passive`. nvidia-smi, ten: the largest UUID taken, a timed-out query not reaped, usage over 100
accepted, `[N/A]` not known, the span ending at its start, a quoted field not continued on the next line,
PATH not searched, stderr's UTF-8 not checked, no newline translation, a 6 s limit. The last but two were
first missed, which added stdout that is valid with stderr that is not, and rows ended by a lone CR.

Not covered by the fixtures: `ENODATA` and `EIO` (a tmpfs cannot return them; the reason mapping has a
unit test), an error in the middle of a listing, a file that vanishes while it is read, a failing
`if_nameindex`, interface names that are not UTF-8 (Python keeps them with surrogate escapes, this replaces
the bytes), names beyond ASCII that Python's Unicode database does not call printable (unassigned code
points), and a pressure total beyond 128 bits. `EINTR` has a
unit test (a FIFO opened while a signal without `SA_RESTART` arrives); it fails when the retry is removed.

### Live smoke test

`compare.py`, 2026-10-19, with Network, Disk I/O and Pressure: no difference, three runs per host. Both sides
used `boottime`, had the same network namespace id, and timed the same passive reads.

| host | sensors with a source id | with a read span | temperatures | power | fans | CPUs | interfaces | disks | pressure | covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Jetson Orin Nano Super (L4T R36.5.2) | 20 | 20 | 6 | 3 (INA3221 rails) | 2 (pwmfan, pwm_tach) | 6 | 9 | 1 (NVMe) | none: the L4T kernel has no PSI (both sides `not_exposed`) | Tegra GPU, nvpmodel `MAXN_SUPER`, L4T, CAN and USB gadget interfaces |
| Raspberry Pi 4 (Debian 13) | 5 | 5 | 1 | 0 | 0 | 4 | 3 | 1 (SD card) | cpu, memory, io | DRM card `v3d` named, not read |
| x86 PC (Ubuntu 26.04) | 58 | 56 | 28 | 0 | 1 | 22 | 4 | 2 (NVMe, USB) | cpu, memory, io | i915 GPU, intel_vpu NPU |

Every interface and disk had a rate on both sides (the second collection, 1 s after the first).

### CPU with the passive groups

2026-10-19 (step 2B), the 1 s collection the service makes by default without the slow groups: the core
groups plus Network, Disk I/O and Pressure. A B C C B A, 30 s warmup and 120 s windows; A the Python collector, B platmon-native with the
system allocator (the default build), C the `cache-alloc` build. User + system % of one core from each
program's own `getrusage`. The board services (platmon 1.3.0-1) and the Hub polling them kept running.
Every window: 120 collections, none failed, no 1 s deadline missed, no group changed state, nothing logged,
and one read scope throughout, the same on all six windows of a host (source ids, 9 / 3 / 4 interfaces,
the disks, the pressure resources; the Orin's kernel has no PSI).

| host | Python (A, A) | Rust, system allocator (B, B) | Rust, cache-alloc (C, C) |
| --- | --- | --- | --- |
| Orin | 1.436, 1.432 | 0.570, 0.569: −60 % | 0.505, 0.513: −65 % |
| Raspberry Pi 4 | 1.103, 0.943 | 0.402, 0.406: −61 % | 0.369, 0.369: −64 % |
| x86 PC | 0.756, 0.756 | 0.521, 0.562: −28 % | 0.491, 0.436: −39 % |

Collection time (p50 / p95, ms) and memory (VmRSS at the end of the window):

| host | Python | Rust, system allocator | Rust, cache-alloc |
| --- | --- | --- | --- |
| Orin | 18.7 / 19.6–19.8, 16.0 MiB | 9.8–9.9 / 10.5, 0.7 MiB | 9.3 / 9.8–9.9, 0.9 MiB |
| Raspberry Pi 4 | 10.2–11.8 / 11.2–12.0, 20.1 MiB | 4.2 / 4.4–4.5, 0.7 MiB | 3.9 / 3.9–4.0, 0.8 MiB |
| x86 PC | 18.9–21.6 / 24.4–25.0, 22.1 MiB | 15.7–16.9 / 21.5, 0.9 MiB | 15.4–16.1 / 20.5–21.0, 1.2 MiB |

- **The saving holds with the passive groups:** −60–61 % on the boards with the default build, as in 2A.
  The means of 2B are above those of 2A by 0.08 (Orin) and 0.24 (Raspberry Pi) pp of one core for Python,
  0.06 and 0.10 pp for Rust. That is a difference between two runs, not the groups' own cost: [budget.md](../docs/performance/budget.md)
  measures a feature on one tree with only its setting changed, which was not done here.
- **Every Rust window is below every Python window** on each host. The Raspberry Pi's two Python windows
  differ by 0.16 pp, so its −61 % is from their mean.
- **The allocator cache** lowers CPU by a further 0.06 pp of one core on the Orin and 0.035 pp on the
  Raspberry Pi (5 and 3 percentage points more saving against Python). On x86 the two B windows differ by
  0.04 pp and the two C windows by 0.06 pp, so its figure is not settled. It is not needed for the saving.
- Collection only: no HTTP, no response JSON, no history or replay, no slow groups.

### CPU, core groups only (step 2A)

2026-10-19, A B C C B A, 30 s warmup and 120 s windows; A the Python collector, B platmon-native with the
system allocator (the default build), C the `cache-alloc` build. User + system % of one core from each
program's own `getrusage`; use the totals, the user/system split is tick-sampled on these kernels. The board
services (platmon 1.3.0-1) and the Hub polling them kept running. 120 collections per window, none failed,
no 1 s deadline missed, no group changed state in any window, no problem logged, and the last collection
of every window had the same number of source ids (20 / 5 / 58). That the scope matched is shown by the
fixtures and the live smoke test; during the bench only these counts were recorded, not the ids.

| host | Python (A, A) | Rust, system allocator (B, B) | Rust, cache-alloc (C, C) |
| --- | --- | --- | --- |
| Orin | 1.266, 1.433 | 0.516, 0.510: −62 % | 0.434, 0.438: −68 % |
| Raspberry Pi 4 | 0.774, 0.785 | 0.292, 0.310: −61 % | 0.260, 0.263: −66 % |
| x86 PC | 0.613, 0.662 | 0.500, 0.379: −31 % | 0.327, 0.358: −46 % |

Collection time (p50 / p95, ms) and memory (VmRSS at the end of the window):

| host | Python | Rust, system allocator | Rust, cache-alloc |
| --- | --- | --- | --- |
| Orin | 17.2–18.6 / 17.5–19.6, 15.8 MiB | 9.4 / 9.7, 0.6 MiB | 8.6 / 9.0, 0.8 MiB |
| Raspberry Pi 4 | 8.0 / 8.2, 19.9 MiB | 2.9 / 3.1–3.2, 0.6 MiB | 2.5–2.6 / 2.6, 0.7 MiB |
| x86 PC | 16.5–17.8 / 22.7–23.2, 21.7 MiB | 16.1–17.2 / 20.3–21.1, 0.7–0.8 MiB | 12.8–14.9 / 19.3–20.6, 1.0–1.1 MiB |

- **The default build already saves most of it.** The system allocator and the raw reads are enough for
  −61–62 % on the boards. The allocator cache lowers CPU by a further 0.08 pp of one core on the Orin and
  0.04 pp on the Raspberry Pi (the saving against Python grows by 5–6 percentage points). On x86 it is
  0.10 pp, but the two B windows there differ by 0.12 pp (a desktop host, the noisiest of the three), so
  that figure is not settled. It is not needed for the saving.
- **Collection time** halves on the Orin and falls to about a third on the Raspberry Pi. On the x86 PC it is
  about the same: there it is spent waiting for device reads (GPU, NPU, ACPI), not in the collector.
- **RSS over the window** stays flat on every side (sampled every 30 collections). RSS of the cache-alloc
  build is higher, as expected from a cache that keeps what it holds.
- These figures cover collection without network, disk_io or pressure, and not the service: no HTTP, no
  JSON for responses, no history or replay.

### Runtime equivalence (steps 3A and 3B)

`runtime_fixture.py`, 2026-10-19, on the x86_64 build and on the aarch64 build on the Orin and the
Raspberry Pi: 7 of 7 scenarios, 317 answers the same text. Harness self-test: 6 of 6 controls.
`config_fixture.py`: 49 of 49 configurations the same on all three hosts.

| Scenario | What it covers |
| --- | --- |
| `core` | starting, a running first collection at and past `stale_after`, one dropped as too slow and one taking exactly `stale_after` (kept), ready, the data age at `stale_after` and 1 ns past it (stats withheld), failures (degraded, counted, stale) and recovery, a snapshot degraded by its own optional group, `core` in the collection's `collectors` replaced, `collectors` not a dict or missing, odd wall times, empty stats; non-ASCII keys and text, `1e-05`, `1e+16`, `-0.0`, integers beyond 64 bits |
| `provenance` | records that check out, and each reason to count the data age from the collection start: another clock, a required read missing or extra, spans before or after the collection, reversed, of floats, booleans or three values, required reads not a dict, a record for a value that is not there or not a dict, a number without its record or with a read outside, a record without a span, records not a dict, passive values without a read (and none without values), a passive read outside, a passive read without values; reads exactly at the collection's start and end (valid) |
| `observations` | groups starting, running at and past `stale_after`, ok, partial, error, unavailable, an exception (`internal_error`, data null), 40 issues (32 kept, targets cut to 64 characters), stale with its data kept, an observation inside a core collection, a second observation's id |
| `history` | `/api/history` after core and slow publications: CPU, memory, GPU, temperatures and rails by source id or name, interfaces by namespace and ifindex, disks; gaps from values without a rate, a break after a failure and after 9 s without a publication, missing series as null, another namespace, storage tokens, Wi-Fi only while connected, battery fields that are there, probe targets (IPv6 bracketed), an observation taking over 3 intervals and one in error as null, one started before its last one ignored, expiry, 70 temperatures and 8 CPUs against the 64-series limit (dropped counted once, kept series keeping their slots against newcomers); prefix, seconds, points (gap-keeping buckets, 1 point) and 16 invalid or edge queries |
| `replay` | `/api/samples` and `/api/observation-samples`: from the start, pages, a cursor at and ahead of the latest, another instance (409), 14 invalid or edge queries, a failed and a too slow collection (`gap_before`), a record over `max_bytes` (a missing range, the latest id known at once), capacity evicting the oldest, observation records stale later, a group without a stream (404), everything expired (the cursor still moves on) |
| `features_off` | history and replay off (404s), status with levels, stats, observations, an unknown path, HEAD |
| `text` | `/text` before a snapshot (503), four snapshots (CPU rows and the sampling note, GPU and NPU with and without load, swap, a full disk, temperatures, rails, fans, interfaces with errors and over 12 rows, disks, PSI) with storage, Wi-Fi, battery and probe observations of every kind, a stale snapshot, observations no longer current, and three snapshots or observations Python cannot render (no answer on both) |

Eighteen deliberate defects in copies of the runtime code (state boundaries, failure counting, the
degraded rule, span limits, the clock check, the passive rule, the fallback, `core` handling, the place of
`read`, observation staleness and errors, ages, ASCII escaping) were each caught; three were first missed
and added the boundary cases (exactly `stale_after`, a read at the collection's start, `read` not last).

Thirty-six more deliberate defects in history, replay, config, the slow readers and `/text` (gap markers,
missing-series nulls, the tie-break, gap-keeping buckets, number bounds, stale observations, out-of-order
publications, the prefix, the cursor, the latest id of an oversized record, expiry on read, the instance
check, ages, `gap_before`, the field limit; config ranges, continuation, case, duplicate targets; ESTALE,
the ENOTDIR skip, the dBm sign, an empty bay, super-options; bar rounding, unit edges, row limit, the blank
line) were caught except two that cannot be seen from outside: replay setting `collecting_for_ms` to null
that is null already (as Python does too), and DEFAULT values, which `check()` always refuses first as
unknown keys. Three more were first missed and added cases (a tie in priority, an out-of-order
observation, a record over `max_bytes` read at once), and two (a level of 0, a bar on a tie) after them.

Rust unit tests also run the threads: a slow reader that blocks keeps its group at one read in flight
(stale, `collecting_for_ms` growing) while another group publishes; a writer stopped inside serializing a
record does not hold up a reader, who gets the previous snapshot; 16 clients × 25 requests never collect
and never see a sequence go back; excess connections are closed; a silent client is dropped after the
request timeout; a client that stops reading a 64 MiB answer is dropped after the write timeout, freeing
its slot while collecting goes on; request-line, HEAD and header-limit answers as `http.server` gives them.

### Live HTTP check

3B, 2026-10-19, both services with the same platmon.ini (the defaults: every group, history and replay
on), x86 PC once, Orin and Raspberry Pi twice each: no difference. Slow groups storage, Wi-Fi and battery
in the same states on both (battery `unavailable` on the boards); the same `/api/history` series ids
(64, 42, 20: the x86 PC at the limit) and cadences; replay walked from the start with every id returned or
missing; the `/text` sections the same; 320 concurrent requests verified on each; 200 connections at once
raised the handler threads by 32 at most (6 → 38 Python, 5 → 37 Rust), the servers answered again about
1 s after they closed, and the threads went back; after a restart the old cursor got 409
`instance_changed` on both.

3A, `live_http.py` with the slow groups, history and replay off, on the x86 PC, the Orin and the Raspberry
Pi: no difference (3 runs each, and one more each after the fixes below). Every path gave the same status code, Content-Type and
Cache-Control except `/text` (not ported); the web page and its files were the same bytes;
`/api/stats`, `/api/status` and `/api/observations` had the same keys, order and types. Raw requests at the
protocol's edges (HEAD, POST, HTTP/0.9 said or implied, HTTP/00.9, 2.0 and a bad version, 99 and 100
headers, a long header, a long request line, an empty line, `//path`) gave the same answer shape. On both
servers: 320 concurrent requests, all 320 verified with no client error (last run), one instance and no
sequence going back, at most 2 snapshots published while they ran; of 40 idle connections 8 closed, a
request refused while 32 were held and answered after; a cut-off request line closed after 10.0–10.2 s.

Fixed after the fifth review (2026-10-20): the replay walk accepted duplicate ids, a cursor that stayed
while `has_more`, a last page short of `latest_id`, a changed instance, an id both returned and missing,
and ran out of pages without failing. `tools/walk.py` checks every page and fails a walk that does not end
(17 synthetic cases; the old walker passed 8 of the 11 that must fail). Rerun on the x86 PC: no
difference, both walks of 4 pages complete.

Fixed after review: the concurrent check counted the requests it planned, and an exception in a client
thread (a timeout, an answer that was not JSON or had no sample) was lost with that thread; it now counts
attempted, verified and failed answers, collects every client's errors, and passes only with all verified.
Its self-test feeds it each of those failures. The Sampler serialized a record under its state lock;
HEAD got the 501 page; HTTP/0.9 said in the request line got headers; the blank line that ends the headers
was not counted. 414's reason is `URI Too Long` (Python 3.13+; 3.10 says `Request-URI Too Long`, which the
check treats as the same).

Not covered: which connections of a burst are refused (timing: with 40 opened at once the Python server
answered a request while the client held 40; the limit check opens them 20 ms apart, the burst check
looks at bounds and recovery only), Python versions' error page markup (the shape of the answer is
compared, not the page), a reader that stops in the live check (a unit test covers it with a 64 MiB
answer: no live answer is larger than the socket buffers), the nvidia-smi fallback with a native Linux
NVIDIA driver (checked under WSL2 since, [below](#nvidia-smi-on-an-nvidia-gpu-wsl2)),
configparser and
`float()`/`int()` beyond the forms listed (Unicode digits, other whitespace), non-UTF-8 file names and
config files, IPv6 scope ids and Python 3.10's text for IPv4-mapped addresses in probe targets.

### Whole service A/B (step 3C)

2026-10-20, `tools/service_ab.py` as set above, one A B B A round per host, the three at once; tables from
`tools/ab_report.py`. A = Python 1.3.0, B = `platmon-native serve`; the mean of the two runs of each, % of
one core.

| | Orin A | Orin B | | Pi A | Pi B | | x86 A | x86 B | |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| w1, release window, buffers filling (CPU %) | 1.871 | 0.671 | −64 % | 2.042 | 0.829 | −59 % | 1.021 | 0.637 | −38 % |
| w2, release window, buffers full (CPU %) | 1.970 | 0.783 | −60 % | 2.304 | 0.808 | −65 % | 1.058 | 0.754 | **−29 %** |
| w3, Hub polling (CPU %) | 2.125 | 0.783 | −63 % | 2.784 | 1.021 | −63 % | 1.171 | 0.800 | −32 % |
| idle, no client (CPU %) | 1.841 | 0.729 | −60 % | 1.909 | 0.675 | −65 % | 0.867 | 0.637 | −27 % |
| reconnect, 480 s backlog, per round (s) | 0.502 | 0.433 | −14 % | 0.490 | 0.450 | −8 % | 0.247 | 0.263 | +6 % |
| reconnect CPU per round (s) | 0.315 | 0.209 | −34 % | 0.375 | 0.286 | −24 % | 0.162 | 0.196 | **+21 %** |
| reconnect peak VmRSS, highest sample¹ (MiB) | 41.7 | 16.8 | −60 % | 34.6 | 9.1 | −74 % | 56.0 | 29.9 | −47 % |
| full buffers RSS / PSS (MiB) | 33.1 / 25.4 | 9.8 / 9.8 | −70 / −61 % | 30.6 / 21.8 | 5.4 / 5.4 | −82 / −75 % | 44.6 / 32.8 | 17.4 / 17.4 | −61 / **−47 %** |
| end PSS (MiB) | 33.1 | 10.3 | −69 % | 25.1 | 5.6 | −78 % | 42.9 | 18.0 | −58 % |
| w2 `/api/stats` p95 (ms) | 3.36 | 2.21 | | 5.64 | 3.02 | | 3.04 | 2.04 | |
| w3 replay page p95 (ms) | 7.06 | 4.05 | | 12.49 | 10.57 | | 4.47 | 4.73 | |
| w2 collection p95 (ms) | 20.4 | 10.7 | | 13.7 | 4.8 | | 24.7 | 22.1 | |

¹ The highest value sampled, which may be below the true peak: the sampler missed the reconnect's first
seconds (see "Found later" below). The fixed points (full buffers, end) and the CPU windows are not
affected.

These are the 3C candidate's numbers (source `35cdbdf`); the JSON writing changed after them (`fc63a02`),
and nothing since compared Python with the current candidate, so the PC's +21 % reconnect is a record of
that candidate, not the current one's rate.

Repeats: A's two runs within 0.15 pp (the Pi's w1), B's within 0.06 pp except on the Orin, where the
first B run was about 0.2 pp higher in w2, w3 and idle (0.883 / 0.683, 0.875 / 0.692, 0.825 / 0.633).

- Release target (≤ 2.5 % in w1 and w2): both pass on every host; with full buffers the Pi's Python
  service is at 2.30 %, the candidate at 0.81 %. Hub polling (w3) is not the release condition: the Pi's
  Python service is at 2.78 % there.
- Rules for B: the report as run found none broken on any host (no error answer, loss, walk problem,
  missing collection id, `gap_before` or cadence step over 1.5 s; no response p95 more than 1 ms above
  A's), nor for A. That report could miss a run that did not do its load; judged again below, all twelve
  runs are complete.
- The same data on both: 600 core, 20 storage, 120 Wi-Fi and 10 battery records held with full buffers,
  the same history points (11 300 Orin, 4 700 Pi, 16 760 PC); the backlog was 483–485 core records and
  3 065–3 068 records in all over the five rounds (Orin 25.9 MB, Pi 13.8 MB, PC 49.1 MB of answers).
- Memory (VmRSS): flat on B once the buffers filled (Orin 9–10 MiB, Pi 5 MiB, PC 16–18 MiB from 600 s
  on). A steps up once more when the poller starts (Orin 30–33 → 37–41, Pi 30 → 33–34, PC 44 → 53–56 MiB)
  and is flat after; neither grows from round to round of the reconnect (after each round: Orin A 37–41,
  B 9–10; Pi A 33–34, B 5; PC A 54–56, B 18 MiB).
- The slowest collection while the backlog was fetched: Python 23 and 49 ms on the Orin, 61 and 74 ms on
  the Pi (at most 24 and 15 ms in w3), the candidate 11–13 ms and 5 ms; on the PC 24–25 and 21–22 ms. No
  cadence step over 1.02 s anywhere.
- Proposed gates (docs/performance/rust-feasibility.md): CPU at least 30 % lower and RSS/PSS at least
  50 % lower hold on the Orin and the Pi in every window. On the PC they do not quite: w2 −29 %, PSS with
  full buffers −47 % (RSS −61 %), and fetching the backlog takes the candidate 21 % more CPU than Python
  (the first round +22 %, rounds 2–5 +21 %: not a first-round effect). The PC's core records are the
  largest (about 20 KB; 64 history series, at the limit); the replay cost below shows that the record
  size alone does not explain the reversal.
- Not measured here: 24 h, other hosts, the `cache-alloc` build, a candidate changed after these runs.

Raw (outside the repository, `~/.local/state/platmon-measure/native-20261020-3c/`), SHA-256 first 16 hex
digits: Orin `5aec9a9779e7f0ba`, Pi `60999a7d8e28e89d`, PC `0f6671002b378032`; tool `service_ab.py`
`2ce211c8700b22c8` as run.

**Judged again (after the sixth review).** `ab_report.py` as run passed synthetic runs it must not: a
poller thread that died after one page, a window with one answer, a reconnect with no collection seen,
and a w3 `/api/stats` p95 of 52 ms against A's 2 ms (w3's `/api/stats` p95 was not compared). With the
completeness check and the w3 `/api/stats` rule (`python3 ab_report.py`: 14 synthetic runs, all judged as
expected; of the ten that must not pass and that the old report could judge, it passed seven: the dead
poller with and without thread states, the one-answer window, no collection seen, the w3 p95, too few
reconnect records, a memory sampler that stopped), the twelve stored runs: all complete, no rule broken, w3 `/api/stats` p95 lower on B on every host (Orin 2.59 → 1.28, Pi
4.78 → 2.56, PC 1.90 → 1.19 ms). What they cannot show: the threads' end was not recorded then; the
counts stand for it (w3: 24 stats, 12 observations, 62 replay requests and 115–117 replayed collections
in every run; the reconnect: 3 064–3 071 records for five rounds of a 604–608 record backlog, every round
done). A short run of the new tool on the PC records the threads' end (`workers`).
Found later (the seventh review's run): the memory sampler went from every 5 s to every 50 ms only when
its wait under way ended, so the reconnect's first seconds (up to 5) were sampled only by the reading
after each round. The reconnect peaks above may miss a peak there; fixed since (`Rss.speed`).

### Replay cost (after 3C)

2026-10-20, as set before it was run (above). µs per record, decoding / writing back / a whole page, the
mean of two medians of 7 × 20 pages; every page of every binary byte for byte equal to Python's. Inputs:
80 core records from each host's service (PC 19.7 KB, Orin 10.3 KB, Pi 5.4 KB a record). base = 3C's
candidate with only `replay-cost` added; c2 = candidates 1 and 2 (now in the source).

| host | input | A (Python) | base | c2 | c2, page vs A |
| --- | --- | ---: | ---: | ---: | ---: |
| PC (Python 3.14) | PC | 124 / 116 / 262 | 189 / 168 / 393 | 192 / 145 / 352 | +34 % |
| | Orin | 72 / 69 / 153 | 110 / 93 / 217 | 112 / 78 / 201 | +32 % |
| | Pi | 43 / 38 / 87 | 56 / 50 / 130 | 57 / 44 / 113 | +30 % |
| Orin (Python 3.10) | PC | 333 / 307 / 813 | 280 / 219 / 552 | 283 / 179 / 505 | −38 % |
| | Orin | 184 / 192 / 468 | 164 / 108 / 300 | 164 / 88 / 273 | −42 % |
| | Pi | 105 / 112 / 266 | 83 / 58 / 161 | 83 / 45 / 146 | −45 % |
| Pi (Python 3.13) | PC | 760 / 515 / 1548 | 689 / 493 / 1367 | 703 / 398 / 1259 | −19 % |
| | Orin | 442 / 333 / 906 | 421 / 253 / 767 | 429 / 198 / 705 | −22 % |
| | Pi | 265 / 187 / 537 | 205 / 135 / 427 | 210 / 102 / 391 | −27 % |

- The relative cost follows where it runs: on the PC the candidate's page is slower than Python's for
  every input tried, the Pi's 5 KB records too; on the boards it is faster for every input, the PC's
  20 KB records too. So the PC's large records alone do not explain the reversal (the 3C note suggested
  they did). Size and structure still set the absolute cost (on the PC 113 µs a Pi record, 352 µs a PC
  record). "Where it runs" is the CPU and also the Python version and build (3.14, 3.10, 3.13) and the
  system: not narrowed down to the instruction set.
- How to read the stages: decoding / writing are not like for like. Python's decoding keeps a page of
  80 decoded records alive and frees them together, the Rust one frees each record at once; writing is
  (decoding and writing) − decoding, an estimate of what writing adds, not writing timed alone. All are
  elapsed times, not CPU profiles. The whole page is like for like (each side's own Replay, the same
  page byte for byte).
- Candidate 1 (objects in order without a member list): about 4 % off the PC's writing, within the
  runs' spread. Candidate 2 (floats written in place): with 1, writing 12–24 % less than base on every host and input.
  Candidate 3 (repeated keys found by a key hash first): no change in decoding (199–200 µs both), so
  taken out again; its test of nested repeated keys stays.
- Target not met: c2's page is still 30–34 % above Python's on the PC, so, as set, no PC A/B run.
  c2 keeps every answer: `cargo test` 23 passed, `live_http.py` no difference, the pages above equal.
- Added after the three candidates, a diagnosis, not a candidate: the same c2 source built with
  `cache-alloc` decodes the PC's input in 109 µs instead of 192 (page 254 µs, −3 % against Python;
  −3 to −8 % on the PC for every input, −54 to −62 % on the Orin, −32 to −41 % on the Pi). So on the PC
  the allocations the decoding asks for (one per key, string, object and array), with how the system
  allocator (musl's) serves and frees them, are a large part of the cost. `cache-alloc` changes more than
  the time of a malloc (size classes, blocks kept and reused, never returned), so this shows that
  changing that path can help; it does not split the cost between the parser and the allocator. Nothing
  here turns `cache-alloc` on.

Raw (outside the repository, `~/.local/state/platmon-measure/native-20261020-replay/`): the inputs
(`core-*.jsonl`, SHA-256 first 16 hex digits PC `30561610b9ca81e2`, Orin `f4a990e77926add0`, Pi
`bc48e15822041bde`), `cost-{x86,orin,pi}.txt`. Binaries x86_64 / aarch64: base `334e5440ee866210` /
`260de2a0d5c7082f`, c2 `93bc8944f1f4ccf4` / `f10dc3f46ec4f92e` (the source as committed rebuilds to these).

### System against mimalloc

2026-10-20, as set before it was run (above). Source `6c26614` (feature `mimalloc`), tools `56236c2`.
S = system allocator, M = mimalloc; x86_64 S `40b6db573533e64c`, M `14dcca1967608f81`; aarch64 S
`3b8369bb7d38c867` (cross-built as `build.sh` does), M `b88ec453038df9e6` (on the Orin's docker, then
the image removed). mimalloc's C: `x86_64-linux-musl-gcc -O3 ... -m64` / `aarch64-linux-musl-gcc -O3 ...`,
no `-march` (gcc 12.2 defaults x86-64 / armv8-a); `cargo test` 23 passed with M on both.

Stage 1: decoding now keeps a page of records and frees them together on both sides, writing is timed
on records decoded beforehand. Stage 2, fixed pages, µs per record (decoding / writing / whole page),
the mean of two medians; every page byte for byte equal to Python's:

| host | input | A (Python) | S | M | M page vs S |
| --- | --- | ---: | ---: | ---: | ---: |
| PC | PC | 123 / 126 / 260 | 208 / 133 / 345 | 107 / 121 / 234 | −32 % |
| | Orin | 76 / 74 / 154 | 119 / 78 / 199 | 60 / 70 / 134 | −33 % |
| | Pi | 42 / 42 / 86 | 66 / 42 / 111 | 32 / 38 / 74 | −34 % |
| Orin | PC | 349 / 371 / 836 | 338 / 169 / 513 | 157 / 144 / 310 | −40 % |
| | Orin | 190 / 224 / 482 | 185 / 88 / 277 | 86 / 74 / 166 | −40 % |
| | Pi | 107 / 125 / 264 | 100 / 45 / 146 | 48 / 38 / 89 | −39 % |
| Pi | PC | 781 / 728 / 1553 | 866 / 409 / 1251 | 418 / 351 / 783 | −37 % |
| | Orin | 451 / 425 / 909 | 491 / 223 / 702 | 234 / 188 / 431 | −39 % |
| | Pi | 265 / 243 / 536 | 274 / 119 / 387 | 132 / 98 / 238 | −39 % |

- The condition to go on holds: the PC input on the PC −32 % (repeats 337 / 353 µs for S, 230 / 238
  for M), every host and input faster, none slower.
- With decoding freed a page at a time, S decodes slower than when each record was freed at once (PC
  input on the PC 208 against 192 µs): the earlier stage numbers did depend on the lifetimes. M decodes
  in about half S's time on every host and input; writing takes 9–18 % less.
- On the PC, M's page is below Python's for every input (−10 to −14 %).

Stage 3, the whole service, S M M S on each host, the three at once (each value the mean of two runs,
M against S):

| | PC | Orin | Pi |
| --- | --- | --- | --- |
| CPU %, buffers full (w2) | 0.750 → 0.650 (−13 %) | 0.696 → 0.587 (−16 %) | 0.804 → 0.692 (−14 %) |
| CPU %, Hub-like polling (w3) | 0.792 → 0.688 (−13 %) | 0.700 → 0.617 (−12 %) | 0.975 → 0.837 (−14 %) |
| reconnect, CPU s per fetch | 0.187 → 0.131 (−30 %) | 0.199 → 0.126 (−37 %) | 0.239 → 0.155 (−35 %) |
| concurrent, CPU s per round (4 pollers) | 2.22 → 0.55 (−75 %) | 5.19 → 0.49 (−91 %) | 2.33 → 0.75 (−68 %) |
| concurrent page p95 ms | 200 → 64 | 469 → 148 | 235 → 133 |
| **PSS MiB, buffers full** | 17.3 → 32.2 (**+86 %**) | 9.7 → 23.3 (**+140 %**) | 5.4 → 6.7 (+24 %) |
| **PSS MiB, concurrent peak** | 69.6 → 174.9 (**+151 %**) | 36.7 → 124.2 (**+239 %**) | 21.4 → 43.9 (**+105 %**) |
| **PSS MiB, 120 s after** | 18.7 → 33.3 (**+78 %**) | 10.7 → 24.2 (**+127 %**) | 5.9 → 7.0 (+17 %) |
| threads / fds, 120 s after | 5 / 4 → 5 / 4 | 5 / 4 → 5 / 4 | 5 / 4 → 5 / 4 |

- **M breaks the memory rule on every host** (PC and Orin at all three points, Pi at the concurrent
  peak). Of what was recorded, neither S nor M broke another rule; each collection step during the
  concurrent rounds was not recorded in these runs and stays unverified (below). Leaving M out rests on
  the memory breaches alone, which were recorded directly. S's memory is 3C's B again (17.4 / 9.8 /
  5.4 MiB then).
  M is lower in CPU everywhere, beyond the spread, and its replay pages are faster; but with full
  buffers on the PC and the Orin it holds about what Python held in 3C (32.8 / 25.4 MiB, another day's
  runs), which takes away most of what Rust saves in memory there.
- **Decision: the system allocator stays.** In this version (mimalloc 3.3.2), its default configuration
  and these loads, mimalloc lowered CPU and response times but does not fit platmon's memory budget, so
  it is not adopted. That is a trade-off measured in this configuration, not a finding about mimalloc or
  musl in general: memory settled and threads and descriptors came back (no leak seen), and the return
  policy, fragmentation and caches were not separated. The `mimalloc` feature stays in the source only
  as the record of this comparison, off by default; nothing was tuned (no purge, huge page or other
  setting), and as set before the run, no second allocator, JSON redesign or `cache-alloc` follows from
  it. The PC's misses (3C gates, the 3C candidate's +21 % reconnect, the replay page above Python's)
  stay as recorded.
- S itself, at the concurrent peak of four pollers, holds about four times its full-buffer PSS (PC 69.6,
  Orin 36.7, Pi 21.4 against 17.3, 9.7, 5.4 MiB); that is a load the support scope has to state and give
  a budget ([Support scope](#support-scope)). The 25 % rule chose between allocators; it is not the
  product's memory limit.
- A measuring defect found in these runs: the concurrent pollers stop at the ids they started for, so
  the collections made during the rounds are not theirs to see; the completeness check counted them
  there and refused both boards' S runs (5 collections over 13 s, where the PC's faster rounds passed).
  `service_ab.py` now records them from the main poller's after walk (`concurrent.collected`), and
  `ab_report.py` judges that. These runs predate it: for them the core sequence's advance over the
  rounds (9 to 13 ids, as many as the seconds) and the after walk's losses (none) stand for it, marked
  unverified. Judged that way, the six S runs (two per host) are complete with no recorded rule broken,
  and the six M runs break only the memory rule. What the stand-in cannot show is each step: a 1.5 s
  step could hide in an advance that matches the seconds. The candidate's integrated run records
  `collected` and checks it.

Raw: `~/.local/state/platmon-measure/native-20261020-mimalloc/`, `cost-{x86,orin,pi}.txt` (SHA-256 first
16: `ba80e600bee7222d`, `57d46cc58e9667c5`, `e5b2635794a48d37`), `ab-{x86,orin,pi}.jsonl` (`ddac5318ddec7486`,
`c97188ba3dab8c0b`, `48a16cebe2b28997`) with each run's stderr in `ab-*.err`.

### Support scope step: nvidia-smi, `--frontends`, the build

2026-10-20, as set before the work (above). Binaries from `build.sh`: x86_64 `30a8985e52dc15cb`, aarch64
`dc1a281ccae9502b` (SHA-256, first 16), both with the system allocator's mark; `cargo test` 26 passed.
- nvidia-smi: `fixture.sh` 11 of 11 scenarios, of them `nvidia`, `nvidia_alone` and `nvidia_drm_first`
  with 32 collections equal; no process left after the timeout and the signal; every run with the
  query's arguments. Ten mutants, all caught (two only after two steps were added; see
  [Equivalence on fake trees](#equivalence-on-fake-trees)). **The fallback with a PC or server NVIDIA GPU
  that answers normally: fixture verified, not verified on such hardware.** (Since checked on an NVIDIA
  GPU under WSL2: [below](#nvidia-smi-on-an-nvidia-gpu-wsl2).) The Orin has nvidia-smi (`nvidia-l4t-nvml`): its answer, read by hand once (30 ms, exit 0),
  is replayed in `nvidia`; the Jetson reader answers there first, so the fallback is not reached.
- `--frontends`: `config_fixture.py` 67 of 67 (49 configurations as before, 12 with `--frontends`, 6
  refusals of the two servers as processes) on the PC, the Orin and the Pi. Seven mutants: six caught
  (sorting and repeats of unknown names, an empty value taken as a name, `--frontends` checked before
  the file, the first value counting, `--frontends=` not read); one cannot be seen: with `http` the only
  frontend, a valid list always names it, so leaving `enabled` alone for frontends not named changes
  nothing. A live start with `[http] enabled = no` and `--frontends http`: 200 on `/api/status`.
- `live_http.py` with the new binaries: no difference on the PC, the Orin and the Pi (the v1.3.0 tree as
  the Python side on the boards). `runtime_fixture.py` 7 of 7.
- `build.sh` refused a binary without the mark and the `cache-alloc` build (its mark names it).

Raw: `~/.local/state/platmon-measure/native-20261020-scope/` (`live-orin.json` `ca5c7cccc48d00fc`,
`live-pi.json` `8c8f289f71991e21`). The board copies were removed.

### Final comparison (candidate 1)

2026-10-20 23:29 to 2026-10-21 02:50 KST: candidate 1 ([CANDIDATE.md](CANDIDATE.md)) against the Python service
1.3.0, A B B A on each host, the three at once, judged as set before ("Final comparison, set before it was
run", above) with `ab_report.py --pss-budget` (self-test 45 of 45). Every run is complete and none was
run again; nothing in the candidate or the tools changed. **Candidate 1 passes on every host and in every
run**: no contract broken, the release target and the PSS budget kept.

The candidate's runs, one value per run (MiB, % of one core):

| host | full buffers PSS | concurrent peak PSS | settled PSS | budget | w1 / w2 CPU | `collected` in the concurrent rounds |
| --- | --- | --- | --- | --- | --- | --- |
| PC | 17.4, 17.3 | 65.0, 65.6 | 18.8, 18.6 | 22 / 87 / 24 | 0.67 / 0.72, 0.63 / 0.73 | 11 over 8.6 s, 11 over 8.7 s |
| Orin | 11.8, 9.8 | 41.5, 40.0 | 12.7, 10.6 | 13 / 46 / 14 | 0.67 / 0.66, 0.66 / 0.68 | 15 over 13.0 s, 14 over 12.7 s |
| Pi | 5.5, 5.5 | 21.5, 21.0 | 6.0, 6.0 | 7 / 27 / 8 | 0.73 / 0.82, 0.78 / 0.80 | 12 over 9.9 s, 13 over 10.2 s |

- `collected`: no missing id, no `gap_before`, the longest step 1.0 s in every candidate run; Python's
  longest 1.02 to 1.09 s. The step left unverified in the allocator runs is closed.
- The peak: 165 to 226 PSS readings per run (one before round 1, every 50 ms, one after each round).
- Same data in every run: the replay records, history series and points, and the concurrent backlog of
  the Python runs (PC 7501, Orin 7520, Pi 7532 records over 3 rounds of 4 pollers).
- Threads 5 and open descriptors 4 after settling, as before the reconnect (Python 6 and 4).
- The closest margin is the Orin with full buffers: 11.8 MiB of 13 in the first run, 9.8 in the second.

Reported, not pass or fail (the candidate against Python, the means of its two runs):

| | PC | Orin | Pi |
| --- | --- | --- | --- |
| CPU w1 / w2 / w3 / idle | −35 / −33 / −32 / −26 % | −66 / −69 / −68 / −65 % | −63 / −63 / −64 / −63 % |
| PSS full buffers / concurrent peak / settled | −47 / −13 / −74 % | −58 / −32 / −77 % | −77 / −46 / −85 % |
| reconnect, per round: seconds / CPU seconds | +5.5 / +19.7 % | −16 / −37 % | −19 / −35 % |
| concurrent rounds: seconds / CPU seconds | +18 / +240 % | −4.5 / +179 % | −31 / +33 % |
| concurrent page p95 | 196 against 146 ms (+35 %) | 455 against 438 ms (+3.8 %) | 231 against 327 ms (−29 %) |

- Against the proposed gates: CPU −30 % is met in every window but the PC's idle (−26 %); PSS −50 % with
  full buffers on the boards, not on the PC (−47 %); settled PSS is −74 to −85 % everywhere.
- Performance exceptions (p95 more than 1 ms above Python's): the concurrent page on the PC (both runs)
  and the Orin (both runs). The other response p95 are lower than Python's on every host.
- The cost of serving one large backlog to several clients at once is the candidate's weak side: 2.3 to 5.1
  CPU seconds a round against 0.7 to 1.8 for Python, and on the PC the reconnect +20 % CPU (+21 % in 3C).
  Not optimized here (the allocator and JSON work is closed); it is the input for a later decision.

Raw: `~/.local/state/platmon-measure/native-20261020-final/` (SHA-256, first 16 hex digits: `ab-x86.jsonl`
`e0c4a83ba5c6cdd9`, `ab-orin.jsonl` `4b36f42d655d2d94`, `ab-pi.jsonl` `1d2fa6e601d5c096`; the `.err` logs
beside them). Each record names the binary's SHA-256 and the tools'. The board copies were removed; the
production `platmon` service on both boards stayed active (200).

**The deployed form, set before it was run (2026-10-21).** Candidate 1 as a service, on the Orin and the
Pi (the long run) and the PC (the short checks only); not on a production service's place. The owner ran
`native/tools/deploy/install.sh` once per host (root); it installs, beside `platmon.service`:
- three units with the product unit's hardening (`DynamicUser=yes`, `ProtectSystem=strict`,
  `ProtectHome=yes`, `NoNewPrivileges=yes`, `Restart=on-failure`, `RestartSec=5`), none enabled, each in
  `Conflicts=` with the others: `platmon-python-candidate` (the installed Python 1.3.0),
  `platmon-native-candidate` (candidate 1, its SHA-256 checked at install), and
  `platmon-native-candidate-fakesmi` (the same with a slow fake nvidia-smi first on `PATH`);
- one INI for the three, `/etc/platmon-candidate/platmon.ini`: a copy of `/etc/platmon/platmon.ini` with
  `bind = 127.0.0.1` and `port = 19797` (nothing else changed); the binary under `/usr/lib/platmon-candidate/`, the installed web assets;
- a sudoers file letting the login user start, stop, restart and kill these units and run a read-only
  `measure` (PSS, threads, descriptors and processes of a DynamicUser process); `platmon.service` is not
  named. `uninstall.sh` removes all of it.

`native/tools/deploy_check.py` drives it (its docstring has every step):
- `checks` (each host; a check passes with no problem): Python → candidate on the same INI and port
  (`Conflicts=` stops the other, a new `instance_id`, 409 for the previous instance's cursor, `/api/status`
  the same but for its moment, the INI unchanged); `restart`; SIGKILL brought back by `Restart=on-failure`;
  the port held: the unit keeps restarting and serves on its own once released; rollback to Python; stops at
  7 offsets from a collection's start, during replay answers to 4 clients (whole or an error, no hang), and
  3 times while the fake nvidia-smi runs: each within 2 s, `Result=success`, no process left, the port free;
  at the end `platmon.service` active and answering.
- `soak --hours 24` (Orin and Pi at once), one record per hour: 2400 s of Hub-like polling, then no client
  for 480 s (720 s in odd hours: the oldest replay records expire), the reconnect, one round of 4 pollers on
  the same backlog, 120 s settle, a reading; one `systemctl restart` after the twelfth hour's reading
  (hour 11). Every reading is taken at least 2900 s after a start, with full buffers (600 s retention).
- Pass, per hour, every hour: no HTTP error, loss or walk problem (in odd hours a missing range only on the
  reconnect's first page, from the cursor up to below the oldest held record); reconnect and round done;
  every replayed collection step at most 1.5 s, no `gap_before`; the unit active with no automatic restart;
  after the restart, 409 for the old cursor; settled threads, descriptors, sockets and processes equal to
  hour 0's; settled PSS at most hour 0's + 10 % (at least + 1 MiB); settled PSS within the budget (Orin 14,
  Pi 8 MiB) and the round-end PSS readings within the concurrent budget (46, 27 MiB; no 50 ms PSS sampling
  here). `--quick` (420 s hours, to try the tool) reads before the buffers are full: its PSS rule does not
  apply. At the end the tool's own workers ended without error, its VmRSS reported, the stop clean,
  `platmon.service` active and answering. A run stopped by the tools: fix, record, run that host again once.
- Not here: nvidia-smi on an NVIDIA GPU (the fake only; the WSL2 check is separate, below), the package's
  postinst, a reboot.

**The deployed form: results (2026-10-21 to 22).** Judged as set above; nothing in the candidate, the
units or the tools changed after the criteria were committed (`18b8f8a`), and no run was repeated. **Candidate 1
passes on every host**: `checks` 11 of 11 on the PC, the Orin and the Pi, and the 24 h run on the Orin and
the Pi. The production `platmon` service stayed active and answering on every host throughout.

`checks` (2026-10-21, about 07:00 KST, the three at once; the INI's SHA-256 the same on all three):

| | PC | Orin | Pi |
| --- | --- | --- | --- |
| Python → candidate (`Conflicts=`) | ready 0.11 s, 409 | ready 5.39 s, 409 | ready 5.40 s, 409 |
| `restart` | ready 0.1 s, 409 | ready 0.1 s, 409 | ready 0.1 s, 409 |
| SIGKILL | back in 5.13 s, `NRestarts` 0 → 1 | 5.26 s | 5.24 s |
| port held | `exit-code`, `auto-restart`; serves once released | the same | the same |
| rollback to Python | ready 0.16 s, 409 | 0.31 s | 0.61 s |
| stops at 7 collection offsets | 0.02 s each, clean | 0.03 s | 0.06 to 0.12 s |
| stop with 4 replay answers in flight | 200, `RemoteDisconnected`, refused; no hang | the same | the same |
| stop during the fake nvidia-smi | not reached (i915 answers first) | not reached (the Jetson reader) | reached 3 times: 3 processes at the stop, none left |

- "ready": seconds from `systemctl start` to the first answer. 5.4 s for the swap on the boards against
  0.1 s on the PC; no rule on it, not looked into further.
- Clean stop each time: within 2 s, `Result=success`, no process left in the unit's cgroup, the port free.

The 24 h run (2026-10-21 07:39 to 2026-10-22 07:40 KST, the Orin and the Pi at once), 24 hourly records each:

| | Orin | Pi |
| --- | --- | --- |
| Hub-like requests / errors | 105 948 / 0 | 105 942 / 0 |
| replayed collections / longest step / `gap_before` | 84 283 / 1.001 s / 0 | 84 284 / 1.004 s / 0 |
| CPU (% of one core, per hour) | 0.81 to 0.89 | 0.93 to 1.01 |
| settled PSS: hour 0, range, hour 23 (limit, budget) | 10.5, 10.5 to 11.3, 11.2 MiB (11.6, 14) | 5.9, 5.8 to 6.2, 6.1 MiB (6.9, 8) |
| round-end PSS, highest (budget) | 12.3 MiB (46) | 6.7 MiB (27) |
| threads / descriptors (sockets, processes the same every hour) | 5 / 4 every hour | 5 / 4 every hour |
| reconnect, per hour | 0.39 to 0.54 s, done | 0.39 to 0.52 s, done |
| round of 4 pollers: seconds / CPU seconds | 1.7 to 2.6 / 3.5 to 6.1 | 1.1 to 1.4 / 1.8 to 2.4 |
| replay / stats response p95, highest hour | 4.6 / 1.5 ms | 8.8 / 2.9 ms |
| `restart` after hour 11 | 409 for the old cursor, ready 0.1 s | the same |
| automatic restarts | 0 | 0 |
| end: stop | 0.03 s, clean | 0.07 s, clean |

- Odd hours: the missing range is only on the reconnect's first page, from the cursor + 1 up to below the
  oldest held record (core 125 records each time, the time the client was away past retention).
- The Orin's settled PSS rose about 0.7 MiB over the first 23 hours (10.5 to 11.3 MiB) and did not come back
  after the restart (10.8 MiB at hour 12); it stayed under hour 0 + 10 % (11.6 MiB) every hour, and
  hour 23 was lower than hour 22. A slow rise within the limit; 24 h does not show whether it levels off.
  The Pi's stayed within 5.8 to 6.2 MiB and fell back after the restart.
- The round-end PSS is read once after the round, not sampled during it: it is not the concurrent peak.
  The backlog per round was the same as in the final comparison (core about 480 to 496 records, 4 pollers),
  but one round an hour instead of 3 in a row and no 50 ms sampling; the comparable reading there is the
  one after each round (Orin 10.9 to 13.4 MiB). The concurrent peak stays the final comparison's (40.0 to
  41.5 MiB on the Orin): this run does not replace that memory check.
- The test tool's own VmRSS (not the candidate's) grew from 46 to 55 MiB (Orin) and 36 to 44 MiB (Pi), the
  Hub-like client's bookkeeping; it ended without error.

What this shows: candidate 1 installed as a hardened, DynamicUser service beside the production one, swapped
in and out on the same INI and port, restarted, killed and stopped at awkward moments without a leftover
process or a lost record, and 24 h under a Hub-like load with a restart in the middle, within its memory
budget. What it does not: the package's `postinst` and an `apt` upgrade (the units were installed by a
script, not by the `.deb`), a reboot, the production port 9797 and a real Hub (the client was the tool),
nvidia-smi on a native Linux NVIDIA driver, more than 24 h.

Raw: `~/.local/state/platmon-measure/native-20261021-deploy/` (SHA-256, first 16 hex digits: `checks-pc.jsonl`
`de7d4b0b005396ba`, `checks-orin.jsonl` `05c266b57571e433`, `checks-pi.jsonl` `10e544e660d08fef`,
`soak-orin.jsonl` `c6a182c0cf493c18`, `soak-pi.jsonl` `598339ce2e0116f4`, the `.log` files beside them;
the `--quick` try `quick-pi.jsonl` `eda2b1b1567d7fc4`). The board copies were removed; the installed
candidate units stay until the owner runs `uninstall.sh`.

### The released Hub (P1-1)

2026-10-22, `native/tools/hub_check.py`, one run per server on the same events: platmon-hub 1.1.1 (the
PyPI release, from the PC's Hub venv) as a temporary Hub with raw capture on and a new database, against
the installed candidate units (127.0.0.1:19797) on the PC, the Orin and the Pi (boards through SSH
tunnels); candidate 1 (`platmon-native-candidate`) and, as the reference, v1.3.0
(`platmon-python-candidate`). Events: every agent restarted (180 s), killed with SIGKILL and restarted by
systemd (300 s), the Hub stopped for 60 s and started on the same database (420 to 480 s), every agent
stopped for 30 s (600 s), the end (780 s). Rules set before the runs, from the Hub's database and APIs (the
tool's docstring has them in full): the stored instances are the agents' 4; no replay id missing without a
loss; new instances stored from id 1; no `buffer_unavailable`; one `agent_restart_unknown_extent` per
captured stream per instance change; the Hub's down time back-filled; the active core cursor on the
agent's instance within 10 ids of its latest; the node current; no writer drop; `integrity_check` ok; no
traceback.

| per host (PC / Orin / Pi) | Python v1.3.0 | candidate 1 |
| --- | --- | --- |
| result | PASS on all 3 | PASS on all 3 |
| instances stored / the agents had | 4 / 4 | 4 / 4 |
| restart losses (core, storage, wifi, battery) | 3 each | 3 each |
| other losses | none | none |
| core publications stored | 741 / 738 / 741 | 744 / 738 / 746 |
| core back-filled from the Hub's 60 s down | 56 / 56 / 56 | 57 / 57 / 57 |
| end: core cursor behind the agent's latest | 4 / 2 / 2 ids | 2 / 5 / 0 ids |

The `probe` stream is not configured on these hosts, so it has no cursor. What this shows: the released
Hub follows the candidate the way it follows v1.3.0 through restarts, a crash, its own restart and an agent
outage, with the same cursor and loss records. What it does not: the production port and the PC's own Hub
(the agents were the candidate units, the Hub a temporary one), a reboot, more than 13 minutes, Hub
versions other than 1.1.1.

Raw: `~/.local/state/platmon-measure/native-20261022-hub/` (`python/report.json` `df3290b1e3297f60`,
`native/report.json` `00322928f89de3fa`, each run's `history.db` and Hub log beside it).

### nvidia-smi on an NVIDIA GPU (WSL2)

2026-10-21, candidate 1's x86_64 binary (`027e35b1c9251b9d`) against the v1.3.0 tree, as plain processes
(no service unit), on a Windows PC's WSL2: Ubuntu 24.04.5, kernel 6.6.87.2-microsoft-standard-WSL2+, Python
3.12.3, NVIDIA GeForce RTX 4070 Ti with the Windows driver 610.88. Reached over SSH (port 2222, key only),
set up by the owner.
- The path it takes: the only DRM card is `vgem` (a driver neither side reads), so both go to the
  nvidia-smi fallback; `nvidia-smi` is not on `PATH`, so both find it at the WSL path
  (`/usr/lib/wsl/lib/nvidia-smi`). The query answered in about 0.04 to 0.05 s (the timeout is 0.5 s).
- `compare.py` 10 times: no difference in any run. Both sides wrote the same entry each time, `driver`
  `nvidia`, `provider` `nvidia-smi`, `usage` 0 to 2 %, `freq` 210 MHz, `max_freq` 3105 MHz in Hz; the
  `gpu` collector `ok` with no issue; the same sensor source ids (the GPU's UUID in the descriptor).
- `live_http.py` once: no difference (the whole default service, the replay walk, 320 concurrent answers,
  the burst back to its threads, 409 after the restart).
- What it shows: the fallback on a real GPU answering normally, through the WSL path. What it does not:
  a native Linux NVIDIA driver on a PC or server (WSL2's `nvidia-smi` answers through the Windows driver),
  a GPU under load (the clocks were idle), several GPUs on real hardware (fixture only).

Raw: `~/.local/state/platmon-measure/native-20261021-deploy/wsl/` (`live.json` `c4013bc96a7d6b50`,
`compare-1.json` `c9c5e81d10b0f5d2`, ten `compare-*.json`). The copies on the WSL host were removed.

### Earlier run (2026-10-18)

The first prototype run, A B B A with the `cache-alloc` build only: Orin 1.273, 1.230 → 0.432, 0.431 %
(−66 %), Raspberry Pi 0.797, 0.807 → 0.255, 0.262 % (−68 %), x86 0.620, 0.623 → 0.371, 0.391 % (−39 %).
Peak RSS (`ru_maxrss`, a different binary) 15.8 / 20.0 / 21.1 MiB for Python, 1.5 / 1.7 / 1.5 MiB for Rust.

Before that, with std file I/O and musl's allocator, Rust had used Orin 0.55, Raspberry Pi 0.37 and x86
0.48 %: already about 55 % less than Python on the boards. `strace -c` over 23 x86 collections showed where
the rest went:
- 4233 `fcntl` calls: musl adds an `F_SETFD` after every `O_CLOEXEC` open.
- About 1,500 `mmap`/`munmap` pairs: musl's allocator returns freed groups to the kernel.

Raw `openat`/`read`/`readlinkat` with one reused buffer (`sysfs.rs`) removed the first. The allocator
cache removed the second; the 2026-10-19 run above shows what each part is worth. A product build would
compare vetted allocators instead of this cache (mimalloc: MIT; jemalloc: BSD-2-Clause; their Rust wrappers
and bundled sources to be checked separately).

Raw records are kept outside the repository (step 3A: the live check reports and the runtime mutants'
outputs). SHA-256, first 16 hex digits:
- 2026-10-19, with the passive groups: results Orin `bc7f4bfc203ca6a1`, Raspberry Pi `3840f075d3d1bc54`, x86
  `924f307d60ae6ac6`
- 2026-10-19, core groups only: results Orin `ecad202e537830f8`, Raspberry Pi `94a036d9804393e1`, x86 `d4128078aeb6597d`
- 2026-10-18: results Orin `ecbd98eda8b499d9`, Raspberry Pi `d2290973d157211a`, x86 `e044f35c961b0856`

The files used on the boards were removed afterwards.
