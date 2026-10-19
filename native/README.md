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
- the DRM GPU (i915, amdgpu, other drivers by name) and the accel NPU (intel_vpu)
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
adds the slow groups and `runtime/`; the nvidia-smi fallback is still not ported.

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

The allocator cache measured on 2026-10-18 (`src/alloc.rs`) is behind the `cache-alloc` feature. The default
build uses the system allocator; see [Results](#results).

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

Status (2026-10-19, #93): 3A and 3B done; 3C not started. With 3B the candidate has every feature of
the default configuration on the three hosts compared (x86 PC, Orin, Raspberry Pi), checked against the
Python service below. Still missing: the nvidia-smi fallback of the GPU (none of the three hosts has
nvidia-smi; a PC with an NVIDIA card would differ), the `--frontends` option.

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

**What the candidate has** (`platmon-native serve [platmon.ini] --web DIR --app-version V`):

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

## Use

```sh
native/build.sh                     # in a rust:1.88 container; unit tests, then both builds for both targets
native/tools/fixture.sh             # deterministic equivalence on fake trees, in a container
PYTHONPATH=. python3 native/tools/compare.py native/target/x86_64-unknown-linux-musl/release/platmon-native
PYTHONPATH=. native/tools/bench.sh native/target/x86_64-unknown-linux-musl/release/platmon-native out.jsonl \
    native/target/cache-alloc/x86_64-unknown-linux-musl/release/platmon-native
python3 native/tools/runtime_fixture.py native/target/x86_64-unknown-linux-musl/release/platmon-native
PYTHONPATH=. python3 native/tools/config_fixture.py native/target/x86_64-unknown-linux-musl/release/platmon-native
PYTHONPATH=. python3 native/tools/live_http.py native/target/x86_64-unknown-linux-musl/release/platmon-native
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
  every setting with its type, the levels and the probe targets, or the same refusal message.
- `live_http.py`: **a live check of the service.** Starts the Python service and `platmon-native serve`
  with the same platmon.ini (the defaults, each on its own local port), checks both the same way and
  compares them (see its docstring). It stops only the two processes it started, by their handles.

## Results

### Equivalence on fake trees

`fixture.sh`, 2026-10-19, x86_64 build: 8 of 8 scenarios, no difference: 40 collections and observations
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
| `passive_failures` | a header that is not `/proc/net/dev`'s, diskstats that is not UTF-8, a pressure file that cannot be read, one that is not ASCII, one without `some`; then bad rows (an interface listed twice, 15 fields, invalid names, a line without `:`, a negative counter; a disk listed twice, too few fields, a later field that is not a counter, a major that is not a number, a line without a name, a `/sys/block` entry that is not a link), a namespace that cannot be identified, `some` twice, CRLF line ends; then every file gone, back (warmup), empty files, recovered |

The harness was checked against itself: deliberate defects in a copy of the Rust code were each reported.
Core groups, six: rail rounding, the span of a computed value, the RC6 clamp, the gap limit, `disappeared`
vs `not_exposed`, a 0 MHz clock dropped. Passive groups, eleven: `window_ms` dropped on a reset, in flight
taken for a counter, a namespace change ignored, no reuse of a disk's classification, CPU `full` kept,
counters written as 64-bit signed, the I/O ratio rounded to 3 digits, the pressure span ending at its
first read, duplicate interfaces kept, line numbers off by one, the rate computed without Python's float
conversion. The RC6 clamp, the I/O ratio and the float conversion were first missed (every window was
exactly 1 s), which added the clamped case to `pc` and the 1.7 s / 0.3 s windows and the large delta to
`passive`.

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
answer: no live answer is larger than the socket buffers), the nvidia-smi GPU fallback, configparser and
`float()`/`int()` beyond the forms listed (Unicode digits, other whitespace), non-UTF-8 file names and
config files, IPv6 scope ids and Python 3.10's text for IPv4-mapped addresses in probe targets.

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
