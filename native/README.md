# platmon-native (experimental)

A Rust port of the core of `collector.collect_recorded`, to measure what a native collector would cost
and to check whether it can produce the same output. It is next step 1 of
[rust-feasibility.md](../docs/performance/rust-feasibility.md). **Not part of the product:** the service,
the packages and the source archive do not include it, and nothing here changes the release.

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

Not ported: Network, Disk I/O, Pressure, the slow groups (storage, Wi-Fi, battery, probe), the
nvidia-smi fallback, and everything in `runtime/` (Sampler, history, replay, HTTP).

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

Dependencies: `libc` (MIT or Apache-2.0, no dependencies of its own) for system call numbers, flags and
struct layouts per target. JSON output and SHA-256 are implemented here, because the output has to match
Python's float repr and key order exactly, and both are tested against Python. Builds target `x86_64` and
`aarch64-unknown-linux-musl` as static binaries.

The allocator cache measured on 2026-10-18 (`src/alloc.rs`) is behind the `cache-alloc` feature. The default
build uses the system allocator; see [Results](#results).

## Use

```sh
native/build.sh                     # in a rust:1.88 container; unit tests, then both builds for both targets
native/tools/fixture.sh             # deterministic equivalence on fake trees, in a container
PYTHONPATH=. python3 native/tools/compare.py native/target/x86_64-unknown-linux-musl/release/platmon-native
PYTHONPATH=. native/tools/bench.sh native/target/x86_64-unknown-linux-musl/release/platmon-native out.jsonl \
    native/target/cache-alloc/x86_64-unknown-linux-musl/release/platmon-native
```

On a board, use the installed collector as the reference: `PYTHONPATH=/usr/share/platmon`.

- `fixture.sh` / `fixture.py`: **the equivalence test.** Fake `/sys` and `/proc` trees are mounted over the
  real ones in a throwaway container (a local Debian 13 image with python3, built on first use and never
  pushed). Both collectors run as `nobody`, so file modes apply, on the same clocks (`platmon-native script`
  and `fixture_py.py`; the elapsed clock advances 1 µs per call, wall time is fixed). Every output value is
  compared exactly: JSON types, key order, numbers, read spans, required reads, `collectors` and source ids.
  No tolerance.
- `compare.py`: **a live smoke test** on a real host, both collectors at the same moment. Structure, types,
  identities, `collectors`, source ids, which values have a span, the clock source, and spans within 5 s of
  each other are exact. Readings that move between two reads are only checked to be plausible (a GPU clock
  is not negative, a usage is within 0–100 %): this does not show numeric equivalence.
- `bench.sh`: the Python collector (A) against platmon-native (B), and optionally a second build (C), over
  the same scope: A B B A, or A B C C B A, with 30 s warmup and 120 s windows. Each window reports its own
  CPU (`getrusage`), RSS at the start, every 30 collections and the end, collection time p50/p95/max,
  missed 1 s deadlines, how often each group was in each state, and the source ids of the last collection.
  stderr goes to `OUT.jsonl.log`.

## Results

### Equivalence on fake trees

`fixture.sh`, 2026-10-19, x86_64 build: 5 of 5 scenarios, 20 collections, no difference.

| scenario | what it covers |
| --- | --- |
| `pc` | i915 GPU (0 MHz in RC6, RC6 share, a reset, a share above 100 % clamped to 0), intel_vpu NPU, ACPI zones with a hwmon twin, duplicate chip names, two chips on one device, a fan with and without a tachometer, INA3221 rails and a sum channel; CPU warmup, usage, a regressed counter, no ticks, the same clock twice (invalid interval), a gap over 5 s, a core coming online, a fan chip removed |
| `failures` | unreadable files and directories (`permission_denied`), a chip that disappeared (dangling link), invalid numbers and text that is not UTF-8 (`invalid_data`), empty files, the os-release fallback, an unknown DRM driver next to amdgpu, no accel class; `/proc/stat` missing (the collection fails) and back (warmup again), a model that is not UTF-8 (fails), class directories that cannot be listed |
| `jetson` | Tegra device tree, devfreq GPU, nvpmodel mode by name, L4T release, INA3221 rails, pwmfan and pwm_tach; then an invalid nvpmodel status, an unreadable rpm and a GPU without load |
| `bare` | a host with nothing optional: every group unavailable with its reason |
| `pi` | Raspberry Pi device tree, a zone whose hwmon twin is named with `_`, a DRM card (`v3d`) named but not read |

The harness was checked against itself: six deliberate defects in a copy of the Rust code were each
reported (rail rounding, the span of a computed value, the RC6 clamp, the gap limit, `disappeared` vs
`not_exposed`, a 0 MHz clock dropped). The RC6 clamp was first missed, which added the clamped case to
`pc`.

Not covered by the fixtures: `ENODATA` and `EIO` (a tmpfs cannot return them; the reason mapping has a
unit test), an error in the middle of a listing, and a file that vanishes while it is read. `EINTR` has a
unit test (a FIFO opened while a signal without `SA_RESTART` arrives); it fails when the retry is removed.

### Live smoke test

`compare.py`, 2026-10-19: no difference, three runs per host. Both sides used `boottime`.

| host | sensors with a source id | with a read span | temperatures | power | fans | CPUs | covered |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Jetson Orin Nano Super (L4T R36.5.2) | 20 | 20 | 6 | 3 (INA3221 rails) | 2 (pwmfan, pwm_tach) | 6 | Tegra GPU, nvpmodel `MAXN_SUPER`, L4T |
| Raspberry Pi 4 (Debian 13) | 5 | 5 | 1 | 0 | 0 | 4 | DRM card `v3d` named, not read |
| x86 PC (Ubuntu 26.04) | 58 | 56 | 28 | 0 | 1 | 22 | i915 GPU, intel_vpu NPU |

### CPU, same scope

2026-10-19, A B C C B A, 30 s warmup and 120 s windows; A the Python collector, B platmon-native with the
system allocator (the default build), C the `cache-alloc` build. User + system % of one core from each
program's own `getrusage`; use the totals, the user/system split is tick-sampled on these kernels. The board
services (platmon 1.3.0-1) and the Hub polling them kept running. 120 collections per window, none failed,
no 1 s deadline missed, no group changed state in any window, no problem logged, the same source ids on
every side (20 / 5 / 58).

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
  −61–62 % on the boards. The allocator cache adds 0.04–0.08 pp of one core there (0.10 pp on x86, whose two B
  windows differ by 0.12 pp: a desktop host, the noisiest of the three). It is not needed for the saving.
- **Collection time** halves on the Orin and falls to about a third on the Raspberry Pi. On the x86 PC it is
  about the same: there it is spent waiting for device reads (GPU, NPU, ACPI), not in the collector.
- **RSS over the window** stays flat on every side (sampled every 30 collections). RSS of the cache-alloc
  build is higher, as expected from a cache that keeps what it holds.
- These figures cover collection without network, disk_io or pressure, and not the service: no HTTP, no
  JSON for responses, no history or replay.

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

Raw records are kept outside the repository. SHA-256, first 16 hex digits:
- 2026-10-19: results Orin `ecad202e537830f8`, Raspberry Pi `94a036d9804393e1`, x86 `d4128078aeb6597d`
- 2026-10-18: results Orin `ecbd98eda8b499d9`, Raspberry Pi `d2290973d157211a`, x86 `e044f35c961b0856`

The files used on the boards were removed afterwards.
