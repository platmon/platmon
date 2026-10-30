# platmon-server

> **TL;DR**
> - The Rust server the Debian package runs since 1.4.0: static musl binaries for x86_64, aarch64 and
>   armhf, a port of the Python service with the same HTTP API, `platmon.ini` and `/text`. Every
>   installation runs it; the Python service was removed after 1.6.0.
> - Checked against the Python service on fake trees, configurations, the runtime and live HTTP, then as
>   a service on the boards and with the released Hub. CI's `native` job compares with its recorded
>   answers (`tools/equivalence/golden/`).
> - The Windows build collects every group except pressure, NPU, power, fans, power mode and board info,
>   but is not released ([Windows](#windows-not-released)).

The platmon server the Debian package runs: a port of the Python service (`platmon.py`, `collector/`,
`runtime/`) that keeps its contracts, so that the two give the same answers. It began on 2026-10-18 as
next step 1 of [rust-feasibility.md](../docs/performance/rust-feasibility.md); candidate 1
([CANDIDATE.md](CANDIDATE.md)) passed the [final comparison](#final-comparison-candidate-1) and became the
packaged server in 1.4.0. The Python service, deprecated in 1.5.0, was removed after 1.6.0; its answers,
recorded before (`tools/equivalence/golden/`), stay the reference the server is compared with, and the
results below that name it were measured against it while it was there. The development record of the steps before candidate 1 (the step plans, the CPU runs of steps 1 and
2, the whole-service A/B of step 3C, the replay cost split and the allocator comparison) is in this file
as of `b96aba8`.

## Scope

The whole default service, with the same output as the Python one:

- the core collection: CPU usage per core and `cpu_sampling`; CPU clocks, thermal zones, hwmon
  temperatures, power (including INA3221 rails) and fans; memory, disk, `system`, `model`; the Jetson
  extension (GPU load and clocks, nvpmodel power mode, pwm_tach, L4T release); the DRM GPU (i915, amdgpu,
  other drivers by name), then the nvidia-smi fallback, and the accel NPU (intel_vpu)
- `collectors` states, reasons and issues; sensor source ids (SHA-256 descriptors), the JSON Pointers of
  `sensor_meta`, every value's read span and the required reads' spans
- the passive groups: Network (`/proc/self/net/dev`, the namespace id, ifindex from `if_nameindex`), Disk
  I/O (`/proc/diskstats`, physical disks by their `/sys/block` link) and Pressure (`/proc/pressure`)
- the slow groups: storage, Wi-Fi, battery and the TCP connect probe
- the runtime: the Sampler, history, replay, the HTTP API, the web page's files, `/text`, `platmon.ini`
  and `--frontends` ([Runtime](#runtime))

The `platmon` command (`frontends/cli.py`) stays the Python client; the package installs it, so it needs
the host's Python 3.9+.

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
struct layouts per target. On Windows only, `windows-sys` (MIT or Apache-2.0, with `windows-link`, the
same, its only dependency) for the Win32 declarations of `src/platform/windows.rs` and `src/collect/windows/`;
the Linux builds do not compile it, so the packaged binaries contain neither. JSON output and SHA-256 are implemented here, because the output has to match
Python's float repr and key order exactly, and both are tested against Python. Builds target `x86_64`,
`aarch64` and `arm` (`musleabihf`) Linux musl as static binaries, and `x86_64-pc-windows-msvc`.

The released build uses the system allocator. Two others were measured and kept as records, built
only by their own scripts: the size-class cache of 2026-10-18 (`src/alloc.rs`, the `cache-alloc`
feature, `build-cache-alloc.sh`) and mimalloc, compared on 2026-10-20 (the `mimalloc` feature,
`build-mimalloc.sh`), which did not beat the system allocator by the rules set for it.

## Runtime

**Contracts kept** (from the Python service and docs/reference): requests never collect; a slow group has
its own id and times, and a blocked read neither blocks the core nor queues more work for its group.
History keeps failures, gaps and counter resets as null and broken segments, never 0; a changed device
identity starts a new series; series that went away are dropped after the retention. Replay keeps
`instance_id`, a cursor per stream, `gap_before`, `missing_ranges` and `next_after`, answers 409 after a
restart, and moves the cursor on also when everything asked for has expired. Published data can be
reused, but never a finished answer: ages and `stale` are computed per answer, also for replayed samples.

`platmon-server serve [platmon.ini] [--frontends LIST] --web DIR [--app-version V]` (the version defaults to
the repository's `VERSION`, read at build time; without `--web` the start log says the page is off;
`platmon-server --help` lists the commands):

Publication and the live API (`src/runtime.rs`, `src/slow.rs`, `src/http.rs`):
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
  lines as `http.server` gives them; HEAD gets the 501's headers without its page; a request without a
  version (HTTP/0.9) gets 400 and a version other than 1.x 505, with a status line like every answer but an
  empty request line's ([ADR 0002](../docs/development/adr/0002-api-contract-not-python-answers.md)).
- Slow groups: one thread each, the next observation an interval after the last one started; their entries
  as `runtime/slow.py` writes them.

Service features:
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
- The slow readers (`src/collect/linux/observe/`, one file per group): storage (partitions of physical disks, local filesystems by device
  number, statvfs through a pinned descriptor), Wi-Fi (`/proc/net/wireless`, the namespace and ifindex,
  the carrier), battery (`/sys/class/power_supply`), the TCP connect probe.

## Support scope

Written down 2026-10-20 for candidate 1 and kept for the released server: `platmon-server serve` built by
`build.sh`, the system allocator. Four kinds: **implemented** (in the source, compared with the
Python service on fixtures), **verified on hardware** (also run live on a host below), **unverified**
(implemented, not shown on hardware or not by a fixture either), **unsupported** (not implemented).

| area | implemented and compared | verified on hardware | unverified / unsupported |
| --- | --- | --- | --- |
| targets | `x86_64` and `aarch64-unknown-linux-musl`, static, one binary per architecture | x86_64 PC (Ubuntu 26.04, kernel 7.0, i915 GPU, intel_vpu NPU, a battery); Jetson Orin Nano (Ubuntu 22.04 L4T, kernel 5.15); Raspberry Pi 4 (Debian 13, kernel 6.18) | other boards and distributions unverified; 32-bit ARM (`arm-unknown-linux-musleabihf`, released as armhf) built by `build-armhf.sh`, checked on the Raspberry Pi 4 in 32-bit mode and its package under QEMU in CI ([below](#32-bit-arm)), no 32-bit kernel or ARMv6/ARMv7 board verified; `x86_64-pc-windows-msvc` builds, passes its unit tests and serves the required data in CI but is not released ([below](#windows-not-released)), so unsupported; other architectures unsupported |
| core collection | every group of `collect_recorded` with the passive groups (fixtures, 8 scenarios) | the three hosts (`compare.py`, `live_http.py`, both servers at once, before the removal) | the fixtures' "not covered" lists below |
| GPU | DRM: i915, amdgpu, other drivers by name; Jetson's own reader first; then the nvidia-smi fallback (3 scenarios, 32 collections, the Orin's real answer replayed) | i915 on the PC, the Jetson GPU on the Orin; the Orin's nvidia-smi (`nvidia-l4t-nvml`) answered the query once, read by hand: no `GPU-` UUID, every field `[N/A]` (both sides: `invalid_data`); it is not reached there, the Jetson reader answers first; **the fallback on a real NVIDIA GPU under WSL2** (GeForce RTX 4070 Ti, Windows driver 610.88, `/usr/lib/wsl/lib/nvidia-smi`; [results](#nvidia-smi-on-an-nvidia-gpu-wsl2)) | the nvidia-smi fallback with a native Linux NVIDIA driver on a PC or server: **not verified on such hardware** (WSL2 and the fixtures only; NVIDIA does not promise a stable nvidia-smi output); amdgpu fixture only; NVML unsupported; clocks of 2^128 Hz and more refused (below) |
| slow groups | storage, Wi-Fi, battery, probe (fixture `slow`) | what the three hosts have | |
| configuration | `platmon.ini` as `load_config` reads and checks it, `--frontends` after it (67 cases, also the two servers' refusals) | on the three hosts (Python 3.14, 3.10, 3.13) | Unicode digits and whitespace in numbers, non-UTF-8 file names and files; argparse's abbreviations (`--front`) and a value that starts with `-` are unsupported |
| HTTP API, web page, `/text`, history, replay | as `live_http.py` and `runtime_fixture.py` check them | the three hosts, no difference | which connections of a burst are refused (timing); Python versions' error page markup |
| clients | the HTTP contracts the Hub and the web page use | the Hub-like poller of the final comparison; the released Hub ([below](#the-released-hub-p1-1)) | the `platmon` command (`frontends/cli.py`) stays the Python client and is not rewritten: the server runs without Python; the package installs the command, which needs the host's Python 3.9+ |

**Load and memory.** Verified: the default configuration with the final comparison's load (1 s
collections, a Hub-like poller, a 480 s reconnect backlog fetched five times) and four pollers fetching
the same backlog at once, three rounds. Not verified: more concurrent replays. The HTTP server's limit of
32 handler threads bounds threads, not the memory 32 replay pages at once would hold; it is not a
verified load. The candidate's PSS there (system allocator, 2026-10-20, mean of two runs), and the budget
for the integrated run, set now, before it (measured + 25 %, to the MiB above; the owner may change it
before that run):

| PSS MiB | PC measured / budget | Orin | Pi |
| --- | --- | --- | --- |
| buffers full | 17.3 / 22 | 9.7 / 13 | 5.4 / 7 |
| four pollers' peak | 69.6 / 87 | 36.7 / 46 | 21.4 / 27 |
| 120 s after | 18.7 / 24 | 10.7 / 14 | 5.9 / 8 |

Python's PSS with full buffers was 32.8 / 25.4 / 21.8 MiB; its four-poller peak was not measured.

**Python versions.** The comparisons ran against the Python service on 3.14 (PC), 3.10 (Orin) and 3.13
(Pi). Where Python itself differs, the server follows 3.11 and later: a NUL inside nvidia-smi's CSV is a
character (3.10's `csv` refuses the whole answer, 3.11 and later refuse only that field's number).

**A deliberate difference.** A clock nvidia-smi gives as 2^128 Hz or more (over 3.4 × 10^32 MHz) is null
with `invalid_data`; Python publishes the integer. No GPU reports such a clock; the server refuses it
rather than publish a number it cannot hold (a float to integer cast saturates).

**Build.** `build.sh` is the release build: the system allocator only (`--no-default-features`; `--all-features`
is never a build of it, and the two allocator features cannot be built together), `Cargo.lock` as
committed (`--locked`), both musl targets, each binary checked to carry the system allocator's mark
(`platmon-server build-info` prints it), then the SHA-256. `build-cache-alloc.sh` and
`build-mimalloc.sh` build the two records, never a release. Candidate 1, with its source, build,
binaries, running conditions and comparison, is fixed in [CANDIDATE.md](CANDIDATE.md).

## Use

```sh
B=native/target/x86_64-unknown-linux-musl/release/platmon-server
native/build.sh                     # in a rust:1.88 container; unit tests, then the system allocator build for both targets
native/build-cache-alloc.sh         # the cache-alloc build (a record, for bench.sh), into target/cache-alloc
native/build-armhf.sh               # the 32-bit ARM build (armhf) and its unit tests, not run here
native/tools/equivalence/fixture.sh                 # deterministic equivalence on fake trees, in a container
python3 native/tools/equivalence/runtime_fixture.py $B
python3 native/tools/equivalence/config_fixture.py $B
python3 native/tools/equivalence/live_http.py $B
native/tools/equivalence/fixture.sh --bless; python3 native/tools/equivalence/runtime_fixture.py $B --bless; python3 native/tools/equivalence/config_fixture.py $B --bless   # after an intended change: rewrite golden/ from the server, review the diff
native/tools/perf/bench.sh $B out.jsonl native/target/cache-alloc/x86_64-unknown-linux-musl/release/platmon-server
python3 native/tools/perf/service_ab.py --tree . --binary $B --variant M=OTHER --order "B M M B" --out ab.jsonl   # about 2.5 h; --quick to try it
python3 native/tools/perf/ab_report.py ab.jsonl
$B replay-cost core.jsonl 20
sudo bash native/tools/deploy/install.sh   # on a host, once, from a staged copy with the binary: the candidate units
python3 native/tools/deploy/deploy_check.py checks --out checks.jsonl   # about 15 min; soak --hours N, report
python3 native/tools/deploy/hub_check.py --hub HUB_EXECUTABLE --hosts pc,orin=USER@HOST --out DIR   # 13 min
```

`tools/` by purpose: `equivalence/` compares the server with the recorded answers (first the Python
service's; `--bless` rewrites them, [ADR 0002](../docs/development/adr/0002-api-contract-not-python-answers.md)),
`perf/` measures builds against each other, `deploy/` runs a build as a candidate service; `walk.py` and
`worker.py`, which the live, A/B and deployment checks share, and the Windows smoke test stay in `tools/`.

The candidate units (`native/tools/deploy/`) run a build beside `platmon.service` on 127.0.0.1:19797:
`platmon-server-candidate` and `platmon-server-candidate-fakesmi` (the same with a slow fake nvidia-smi).
Since 1.4.0 the checks are the native server's alone: `start` on the INI, then restart, SIGKILL, the held
port and the stops. The Python unit and its swap and rollback checks, used for candidate 1 below, are gone
with the package's Python server; `uninstall.sh` still removes that unit where an earlier install left it.

- `equivalence/fixture.sh` / `fixture.py`: **the equivalence test.** Fake `/sys` and `/proc` trees are
  mounted over the real ones in a throwaway container (a local Debian 13 image with python3, built on first
  use and never pushed). The server runs as `nobody`, so file modes apply, on set clocks (`platmon-server
  script`; the elapsed clock advances 1 µs per call, wall time is fixed), and its every output value is
  compared exactly with the Python collector's recorded answer for the same tree and clocks: JSON types, key
  order, numbers, read spans, required reads, `collectors` and source ids. No tolerance. Each step states
  whether it expects a collection or a failed collection; both sides must give that and exit with 0 at the
  end, and a side that exits, hangs or writes something else fails the run. The timeout (30 s) is one
  deadline for the whole answer line, not for its first byte or each piece, and a line over 4 MiB is not
  read further. Before the scenarios, the harness checks itself against fake sides (`fixture.py
  --self-test`, no container needed): both failing where a collection is expected, a nonzero exit, a hang
  (also after part of an answer, or an answer completed in pieces past the deadline), output that is not
  JSON and an oversized line must each be reported. Interfaces are real ones in the container's own network
  namespace (empty bridges, created and removed by ioctl, so the kernel gives them their ifindex); the
  container has `NET_ADMIN` there and no network.
- `equivalence/golden.py` and `golden/`: **the Python service's answers, recorded** (1.6.0, before its
  removal) for `fixture.py`, `runtime_fixture.py` and `config_fixture.py`; `golden.py` replays them as the
  Python side. A recording holds what depends on the tree and the requests alone: the host name and tmpfs
  sizes are fixed (`fixture.sh`), the probe's ports are fixed, an HTTP response is kept unstamped, and the
  kernel's release and machine, an interface's index, the network namespace's id and the tmpfs devices are
  tokens resolved when replayed. They are not recorded again: a change of the server's answers is a change
  of its contract.
- `equivalence/runtime_fixture.py`: **the runtime equivalence test.** `platmon-server runtime-script` gets
  the operations the Python service's Sampler, slow groups, History, Replay and HTTP server got when its
  answers were recorded: a collection begins, a view is taken while it runs, it ends with a given
  collection or error; the same for observations; raw HTTP requests to the real server. Every answer must
  be the same text: the `/api/stats`, `/api/status` and `/api/observations` bodies, and whole HTTP answers
  without their `Date` and `Server` headers and without the markup of error pages (it differed between
  Python versions; the status line with the message stays). Runs on the host, reads no tree; the same
  self-test and deadlines as `fixture.py`.
- `equivalence/config_fixture.py`: platmon.ini files read by `platmon-server config-check` against
  `platmon.load_config`'s recorded answers: every setting with its type, the levels and the probe targets,
  or the same refusal message, also with a `--frontends` value; then `platmon-server serve`'s refusals as
  a process; last, the shipped `platmon.ini` must read as no file does (every value in it is the default).
- `equivalence/live_http.py`: **a live check of the service.** Starts `platmon-server serve` with the
  defaults on a local port and checks it, against the Python service's answers where they do not depend on
  the host (see its docstring). It stops only the process it started, by its handle.
- `perf/bench.sh`: two builds (B, C) over the same scope, B C C B, with 30 s warmup and 120 s windows. Each
  window reports its own CPU (`getrusage`), RSS at the start, every 30 collections and the end, collection
  time p50/p95/max, missed 1 s deadlines, how often each group was in each state, the number of source ids
  of the last collection, and its read scope: the SHA-256 of the sorted source ids and the interfaces,
  disks and pressure resources it read, with how many different scopes came up in the window. stderr goes
  to `OUT.jsonl.log`. Until the removal its A was the Python collector.
- `perf/service_ab.py` / `ab_report.py`: **the whole-service A/B** of the final comparison (see their
  docstrings), now between builds. Each run is judged complete first (its threads' end through
  `worker.py`, the counts its phases imply), then by the rules; an incomplete run is invalid. Both,
  `walk.py` and `worker.py` run their self-tests first.
- `replay-cost`: what a replay page costs per record, by stage, on fixed core publications; it can write
  the page. The Python side it was compared with went with the removal.
- Removed with the Python service: `compare.py` (the live smoke test of both collectors at once, results
  below) and the Python sides `fixture_py.py`, `observe_py.py`, `runtime_py.py`, `pyloop.py` and
  `replay_cost_py.py`.

## Results

The dated results below compared the server with the Python service while it was there; since its
removal CI compares with its recorded answers (`fixture.sh`: 11 of 11 scenarios, `runtime_fixture.py`: 7 of
7, `config_fixture.py`: 68 of 68 with the shipped-INI check, on 2026-10-29).

### Equivalence on fake trees

`fixture.sh`: 11 of 11 scenarios, 72 collections and observations equal and the 2 expected failures
(below) on both sides, every side exiting with 0 (2026-10-20; CI runs it on every change, and each
release gate records it). Harness self-test: 15 of 15 controls reported as they must be.

| scenario | what it covers |
| --- | --- |
| `pc` | i915 GPU (0 MHz in RC6, RC6 share, a reset, a share above 100 % clamped to 0), intel_vpu NPU, ACPI zones with a hwmon twin, duplicate chip names, two chips on one device, a fan with and without a tachometer, INA3221 rails and a sum channel; CPU warmup, usage, a regressed counter, no ticks, the same clock twice (invalid interval), a gap over 5 s, a core coming online, a fan chip removed |
| `failures` | unreadable files and directories (`permission_denied`), a chip that disappeared (dangling link), invalid numbers and text that is not UTF-8 (`invalid_data`), empty files, the os-release fallback, an unknown DRM driver next to amdgpu, no accel class; `/proc/stat` missing (an expected failure) and back (warmup again), a model that is not UTF-8 (an expected failure), class directories that cannot be listed |
| `jetson` | Tegra device tree, devfreq GPU, nvpmodel mode by name, L4T release, INA3221 rails, pwmfan and pwm_tach; then an invalid nvpmodel status, an unreadable rpm and a GPU without load |
| `bare` | a host with nothing optional: every group unavailable with its reason |
| `pi` | Raspberry Pi device tree, a zone whose hwmon twin is named with `_`, a DRM card (`v3d`) named but not read |
| `passive` | interfaces with and without an ifindex (one named beyond ASCII), physical, virtual and partition disks, a disk with the 11 fields of older kernels; rates, counters reset (no rate, a new baseline), in flight falling (a gauge, not a reset), an interface and a disk removed and others added, an interface back under its name with a new ifindex, a counter at 2^64−1 and one beyond (not a counter), bytes beyond 64 bits, a delta whose rate depends on the float conversion, windows of 1.7 s and 0.3 s, a gap, the same clock twice, the network namespace changed and the rates after it, a disk whose `/sys/block` link went away (its classification is kept) |
| `slow` | the slow groups' readers (`observe-script` against the recorded answers of the Python readers), 5 steps: partitions of physical, virtual and missing disks, a row listed twice; mountinfo with the real device numbers of the container's mounts, a bind mount, an escaped path, read only from the super-options, a file bind listed before a directory (`ENOTDIR`, skipped), a mount point gone, another device behind a path (`mount_changed`), one nobody cannot reach, non-local and non-`/dev` mounts, lines that do not parse; Wi-Fi rows connected, down, with a moved ifindex, an unreadable carrier, no ifindex, a level that is not dBm and one of 0, out of range, not a row; batteries present, an empty bay, invalid numbers and text, an unreadable attribute, a UPS, external supplies, one without a type; TCP connects accepted, refused and unreachable (their times only checked to be ≥ 0); then mountinfo unreadable, files gone, files broken, nothing there |
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

### Runtime equivalence

`runtime_fixture.py`: 7 of 7 scenarios, 317 answers the same text, on the x86_64 build and on the aarch64
build on the Orin and the Raspberry Pi (2026-10-19; CI runs it on every change). Harness self-test: 6 of
6 controls. `config_fixture.py`: 67 of 67 cases (configurations, `--frontends` and the two servers'
refusals as processes) the same on all three hosts.

| Scenario | What it covers |
| --- | --- |
| `core` | starting, a running first collection at and past `stale_after`, one dropped as too slow and one taking exactly `stale_after` (kept), ready, the data age at `stale_after` and 1 ns past it (stats withheld), failures (degraded, counted, stale) and recovery, a snapshot degraded by its own optional group, `core` in the collection's `collectors` replaced, `collectors` not a dict or missing, odd wall times, empty stats; non-ASCII keys and text, `1e-05`, `1e+16`, `-0.0`, integers beyond 64 bits |
| `provenance` | records that check out, and each reason to count the data age from the collection start: another clock, a required read missing or extra, spans before or after the collection, reversed, of floats, booleans or three values, required reads not a dict, a record for a value that is not there or not a dict, a number without its record or with a read outside, a record without a span, records not a dict, passive values without a read (and none without values), a passive read outside, a passive read without values; reads exactly at the collection's start and end (valid) |
| `observations` | groups starting, running at and past `stale_after`, ok, partial, error, unavailable, an exception (`internal_error`, data null), 40 issues (32 kept, targets cut to 64 characters), stale with its data kept, an observation inside a core collection, a second observation's id |
| `history` | `/api/history` after core and slow publications: CPU, memory, GPU, temperatures and rails by source id or name, interfaces by namespace and ifindex, disks; gaps from values without a rate, a break after a failure and after 9 s without a publication, missing series as null, another namespace, storage tokens, Wi-Fi only while connected, battery fields that are there, probe targets (IPv6 bracketed), an observation taking over 3 intervals and one in error as null, one started before its last one ignored, expiry, 70 temperatures and 8 CPUs against the 64-series limit (dropped counted once, kept series keeping their slots against newcomers); prefix, seconds, points (gap-keeping buckets, 1 point) and 16 invalid or edge queries |
| `replay` | `/api/samples` and `/api/observation-samples`: from the start, pages, a cursor at and ahead of the latest, another instance (409), 14 invalid or edge queries, a failed and a too slow collection (`gap_before`), a record over `max_bytes` (a missing range, the latest id known at once), capacity evicting the oldest, observation records stale later, a group without a stream (404), everything expired (the cursor still moves on) |
| `features_off` | history and replay off (404s), status with levels, stats, observations, an unknown path, HEAD |
| `text` | `/text` before a snapshot (503), four snapshots (CPU rows and the sampling note, GPU and NPU with and without load, swap, a full disk, temperatures, rails, fans, interfaces with errors and over 12 rows, disks, PSI) with storage, Wi-Fi, battery and probe observations of every kind, a stale snapshot, observations no longer current, and three snapshots or observations the screen cannot show (500); every `/text` is also compared with `frontends/cli.py`'s `render()` of the `/api/stats` and `/api/observations` answers at the same moment |

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

`live_http.py`, 2026-10-19, both services with the same platmon.ini (the defaults: every group, history and replay
on), x86 PC once, Orin and Raspberry Pi twice each: no difference. Slow groups storage, Wi-Fi and battery
in the same states on both (battery `unavailable` on the boards); the same `/api/history` series ids
(64, 42, 20: the x86 PC at the limit) and cadences; replay walked from the start with every id returned or
missing; the `/text` sections the same; 320 concurrent requests verified on each; 200 connections at once
raised the handler threads by 32 at most (6 → 38 Python, 5 → 37 Rust), the servers answered again about
1 s after they closed, and the threads went back; after a restart the old cursor got 409
`instance_changed` on both.

Not covered: which connections of a burst are refused (timing: with 40 opened at once the Python server
answered a request while the client held 40; the limit check opens them 20 ms apart, the burst check
looks at bounds and recovery only), Python versions' error page markup (the shape of the answer is
compared, not the page), a reader that stops in the live check (a unit test covers it with a 64 MiB
answer: no live answer is larger than the socket buffers), the nvidia-smi fallback with a native Linux
NVIDIA driver (checked under WSL2 since, [below](#nvidia-smi-on-an-nvidia-gpu-wsl2)),
configparser and
`float()`/`int()` beyond the forms listed (Unicode digits, other whitespace), non-UTF-8 file names and
config files, IPv6 scope ids and Python 3.10's text for IPv4-mapped addresses in probe targets.

### Final comparison (candidate 1)

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
- A response p95 more than 1 ms above Python's, a broken rule in the earlier step 3C, is reported as a
  performance exception, following the review's classification (contracts,
  completeness and budgets are pass or fail; relative performance is reported). A short `--quick` run on
  the PC (A B, to try the budget judgement) had already shown the candidate's concurrent page p95 above
  Python's (108 against 65 ms, smaller backlog).
- Budget: one round per host. A run that is invalid because of the tools: fix the tool, record it, run
  that host again once. Then the deployed form (a service unit, 24 h, install, upgrade, recovery), only
  for this same candidate.

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
  CPU seconds a round against 0.7 to 1.8 for Python, and on the PC the reconnect +20 % CPU. Not optimized.

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
the `--quick` try `quick-pi.jsonl` `eda2b1b1567d7fc4`). The board copies were removed.

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

### 32-bit ARM

Released as armhf since 1.6.0 (decided 2026-10-24, with no 32-bit test device: CI's QEMU check below
and the 2026-10-23 run on the Raspberry Pi 4 are the evidence). 2026-10-23: `build-armhf.sh` builds
`arm-unknown-linux-musleabihf` (ARMv6 with VFP, which runs on both Raspberry Pi OS's and Debian's armhf)
with build.sh's image, flags and system allocator. GitHub's arm64 runners cannot run 32-bit code, so CI's
`armhf-userspace` runs the package in Debian 13 and Ubuntu 24.04 armhf userspaces under QEMU: the extracted
package's smoke check (its server answers) and the dpkg lifecycle. The service check that `ubuntu-userspace`
runs on amd64 and arm64 (the upgrade from 1.3.0 under a booted systemd) does not work under QEMU user
emulation: the unit's sandbox does not start, even 1.3.0's Python unit on Debian, and the service's
executable reads as `qemu-arm` (first CI run, 2026-10-24). So the armhf service under systemd is not
verified. It ran on the Raspberry Pi 4 (Debian 13, arm64 kernel 6.18 running 32-bit programs,
Python 3.13.5) against this tree's Python service:
- First build: 27 of 28 unit tests passed. The failure was real. `libc::syscall` is variadic and
  `sysfs.rs` passed its arguments as `i64`. On 32-bit ARM a 64-bit vararg takes an aligned register
  pair and shifts the arguments after it, so every raw `openat`, `read` and `readlinkat` got wrong
  arguments. They now pass `c_long`, which is the same type as `i64` on x86_64 and aarch64. Built
  with the sysfs.rs line numbers kept, those two binaries are byte for byte candidate 1's and 1.4.0's
  (`027e35b1c9251b9d`, `ff97aa9ad4e9dd28`).
- After the fix: unit tests 28/28. `compare.py` 10 times with no difference. `config_fixture.py`
  67/67. `runtime_fixture.py` 7/7 scenarios. `live_http.py` with no difference. `package.py --binary
  armhf=...` builds `platmon_<version>-1_armhf.deb`. Lintian (Debian 13) is clean with the armhf
  override. The package smoke check passed on the Pi with its host check off, because the Pi's dpkg
  architecture is arm64.
- What it does not show: `fixture.sh` (needs Docker; the Pi has none), a 32-bit kernel, an ARMv6 or
  ARMv7 board, install and upgrade through apt on a device, the service under systemd, CPU or memory. The
  package's smoke check and dpkg lifecycle run only under QEMU in CI (`armhf-userspace`).

Raw: `~/.local/state/platmon-measure/native-20261023-armhf/` (binary `ce3307442d085a30`, unit tests
`bce4eb0a3e8886e8`; `sha256.txt`, `unit.txt`, ten `compare-*.json`, `config.txt`, `runtime.txt`, `live.json`,
`smoke.txt`).

### Windows (not released)

Not released and not in the support scope. It stays unreleased until the real-PC checks pass, then
becomes an experimental zip (the owner's decision, 2026-10-25; the checks and their status:
[remaining work](../docs/development/remaining-work.md#windows-build-2026-10-26)). `src/platform/`
holds what the service needs from the OS outside the collectors: the elapsed clocks, random bytes for the
instance id, the listening socket and the exit on a stop request, one module per OS with the same
functions. `platform/linux.rs` is the code `main.rs`, `metadata.rs`, `runtime.rs` and `http.rs` had,
unchanged. `src/collect/mod.rs` keeps what both collectors use (the CPU counters' windows and reasons, the
required reads' spans, `Collected`); `src/collect/linux/` (`sysfs.rs`, `common.rs`, `passive.rs`,
`observe/`, the device readers) and the commands only it serves (`script`, `loop`, `observe-script`) build
on Linux only.

On `x86_64-pc-windows-msvc`:
- CI (`native-windows`, `windows-latest`) builds it with Rust 1.88 and MSVC, runs the unit tests and
  `tools/windows_smoke.py`, which runs `collect` and `serve` on the runner.
- `serve` and `collect` run; `config-check`, `runtime-script`, `replay-cost` and `build-info` too. `script`,
  `loop` and `observe-script` print the usage (they drive Linux's fake trees and `rusage`).
- `src/collect/windows/`, Rust only (the Python service has no Windows collector to compare with): the
  required data in the fields, types and key order of a Linux collection, `platform` `"Windows"`.
  - `cpu`: per-CPU usage from `NtQuerySystemInformation` (`SystemProcessorPerformanceInformation`, 100 ns
    units) through the same `CpuCounters` as Linux: user, kernel time without idle as system (it includes
    DPCs and interrupts), idle; the counters Windows does not have are 0. Every processor group's CPUs
    (`NtQuerySystemInformationEx` with the group's number), numbered as Task Manager numbers them: group 0's
    first, then group 1's, and so on.
  - `cpu[].freq` (`cpu_frequency`): each CPU's `% Processor Performance` (PDH, by the counter's English
    name, so any display language) times its nominal clock (PDH's `Processor Frequency`, the base the share
    is of; the registry's `~MHz` is not, it can be one value for a hybrid CPU's P- and E-cores), in Hz
    rounded to the MHz: the speed Task Manager shows, an average since the last collection that moves with
    turbo and power saving. Every processor group's CPUs, numbered as their usage; sensor provider `pdh`.
  - `memory`: `GlobalMemoryStatusEx` (used = total − available); swap is the paging files' size and use
    (`SystemPageFileInformation`, which `winternl.h` leaves out; its layout has not changed since Windows
    NT). If that query fails, `swap_total` and `swap_used` are `null` and the collection goes on: only the
    physical memory is required. The web page, `platmon` and `/text` then show no swap row.
  - `disk`: the volume Windows is on (`GetDiskFreeSpaceExW` on the Windows directory), as Linux's root
    filesystem.
  - `uptime`: the interrupt time, the same clock as the data age, two decimals as `/proc/uptime`.
  - `model`: the firmware's `SystemProductName` (registry), else the host name; `system`: `os` from the
    registry's `ProductName` (corrected to "Windows 11" from build 22000, which it still calls Windows 10)
    and `DisplayVersion`, `kernel` major.minor.build.UBR, `arch` the machine's (not the binary's) named as
    uname names it, `hostname` the DNS host name.
  - `network`: `GetIfTable2`, with Linux's rates and reasons, scope `host` and provider `if_table2`
    ([network.md](../docs/reference/network.md#windows)).
  - `disk_io`: `IOCTL_DISK_PERFORMANCE` on each `PhysicalDriveN`, with Linux's rates and reasons, provider
    `ioctl_disk_performance`; `major`, `minor`, `merges` and `io_time_ms` are `null` and `io_time_ratio`
    comes from the idle time ([disk-io.md](../docs/reference/disk-io.md#windows)).
  - `gpu` (`gpu`): the first hardware display adapter (`D3DKMTEnumAdapters2`'s order; the Basic Render
    Driver is skipped, and with no other adapter `gpu` is `null`, `not_detected`). `usage` is Task
    Manager's: the busiest engine's `Utilization Percentage` (PDH `GPU Engine`, by the counter's English
    name), the processes' shares of one engine added up and capped at 100, an average since the last
    collection; an adapter with no engine at work is `0.0`. `freq` and `max_freq` are its 3D engine's
    clocks (`D3DKMT_NODE_PERFDATA`); a driver that reports none is `not_exposed`, and Intel's reports none
    while the GPU sleeps (measured on Meteor Lake: most idle collections), which is `no_data` once the
    adapter has reported its clocks. `driver` is the adapter's name, `provider` `pdh_gpu_engine`.
  - `temperature`: each hardware GPU's temperature from its adapter performance data
    (`D3DKMT_ADAPTER_PERFDATA`, what Task Manager shows; a driver that reports none, often an integrated
    GPU's, is `not_exposed`), keyed by the adapter's name, provider `d3dkmt`; and each drive's own
    temperature (`StorageDeviceTemperatureProperty`: NVMe and some SATA drives; virtual disks and most USB
    bridges are `not_exposed`, also when the query fails as a device error, `ERROR_IO_DEVICE`, as some
    virtual disks answer it), keyed by its model, provider `storage_temperature`. A repeated name gets
    ` (gpuN)` or ` (PhysicalDriveN)`. Windows has no API for the CPU's temperature, so there is none.
  - `sensor_meta` covers the CPU clocks, the GPU and temperatures, as on Linux (`device_channel`: the CPU
    number, the adapter's LUID, node and position and name, the `PhysicalDriveN` number).
  - The other optional groups (NPU, power, fans, power mode, board info) are `unavailable` with
    `unsupported_platform`. Pressure is left out, as when it is off, whatever `platmon.ini` says: Windows
    has no stall-time accounting ([pressure.md](../docs/reference/pressure.md#windows)).
  - Slow groups, all of Linux's: storage, the volumes by their GUID and each drive's partitions
    ([storage.md](../docs/reference/storage.md#windows)); Wi-Fi, the WLAN service's interface state and
    RSSI ([wifi.md](../docs/reference/wifi.md#windows)); battery, the battery class's IOCTLs and the AC
    line ([battery.md](../docs/reference/battery.md#windows)); and the probe, with `collect/probe.rs`
    shared by both platforms and std's `connect_timeout` for the connection
    ([probe.md](../docs/reference/probe.md#windows)).
- `platform/windows.rs`: `BCryptGenRandom` for the instance id; a WinSock listener with a backlog of 5,
  not inherited by child processes, and `SO_EXCLUSIVEADDRUSE` instead of Linux's `SO_REUSEADDR`, which on
  Windows lets another socket bind the same port.
- The elapsed clock: the interrupt time (`QueryInterruptTimePrecise`, 100 ns units), which counts through
  sleep and hibernation as `CLOCK_BOOTTIME` does, so the age of data after a resume is the time that passed,
  and `/api/status` reports `"clock": {"source": "boottime", "suspend_aware": true}` as on Linux (the Hub's
  contract unchanged); the unbiased interrupt time, which stops while asleep, stands for `CLOCK_MONOTONIC`.
  Chosen 2026-10-24.
- The stop request: Ctrl+C, Ctrl+Break, the console closed, logoff and shutdown exit with status 0, as
  SIGTERM does on Linux (`SetConsoleCtrlHandler`).
- `service [CONFIG] [serve's options]`: `serve` as a Windows service in its own process, for the service
  control manager (`sc.exe create platmon binPath= "C:\path\platmon-server.exe service C:\path\platmon.ini"`).
  It reports running once connected, and on a stop or shutdown request reports stopped and exits with
  status 0; a user's logoff does not stop it. Outside the service control manager it exits with status 2.
  Not provided: an installer, a default configuration path, recovery actions and a log: the server's
  messages go to standard error, which a service does not have, so a configuration error shows only as
  the service stopping (`config-check` shows it). CI's smoke test creates, starts and stops it.
- Exposure: as on Linux, the API has no authentication and `[http] bind` defaults to `0.0.0.0`, so a
  service started with the defaults answers every host that can reach port 9797, with the host name, model
  and OS version among its data. Windows Defender Firewall asks only when an interactive program first
  listens; a service is not asked about, so its port is open or closed by the firewall's existing rules.
  Set `bind = 127.0.0.1`, or allow the port only from trusted addresses
  ([deployment](../docs/guides/deployment.md)).
- Not shown in CI (a VM): a GPU or drive temperature, the CPU clock against Task Manager. Checked on a real
  PC since, and still open (more than 64 CPUs, a sleep and resume, the service as administrator):
  [remaining work](../docs/development/remaining-work.md#windows-build-2026-10-26). Windows on ARM is not
  built.
