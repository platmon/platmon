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
- sensor source ids (SHA-256 descriptors) and the JSON Pointers of `sensor_meta`

Not ported: Network, Disk I/O, Pressure, the slow groups (storage, Wi-Fi, battery, probe), the
nvidia-smi fallback, and everything in `runtime/` (Sampler, history, replay, HTTP).

The crate uses std only (no crates.io dependencies). SHA-256, JSON output, `statvfs`, `uname` and
`getrusage` are implemented here. Builds target `x86_64` and `aarch64-unknown-linux-musl` as static
binaries.

## Use

```sh
native/build.sh                                   # in a rust:1.88 container; runs the unit tests
PYTHONPATH=. python3 native/tools/compare.py native/target/x86_64-unknown-linux-musl/release/platmon-native
PYTHONPATH=. native/tools/bench.sh native/target/x86_64-unknown-linux-musl/release/platmon-native out.jsonl
```

On a board, use the installed collector as the reference: `PYTHONPATH=/usr/share/platmon`.

- `compare.py` runs both collectors at the same moment and exits 1 on any difference. Structure, JSON types,
  identity fields, names and order, `collectors` and source ids must be equal. Readings that move between
  two reads moments apart are compared within tolerances.
- `bench.sh` measures the Python collector (A) against platmon-native (B) over the same scope, in A B B A
  order with 30 s warmup and 120 s windows. Each program reports its own `getrusage`.

## Results (2026-10-18)

The board services (platmon 1.3.0-1) and the Hub polling them kept running; both programs ran next to
them. The Python reference was the installed collector (`/usr/share/platmon`) on the boards and this tree
(same collector code) on the x86 PC.

**Equivalence.** `compare.py` reported no difference, three runs per board and several on x86 after the
fixes below.

| host | sensors with a source id | temperatures | power | fans | CPUs | covered |
| --- | --- | --- | --- | --- | --- | --- |
| Jetson Orin Nano Super (L4T R36.5.2) | 20 | 6 | 3 (INA3221 rails) | 2 (pwmfan, pwm_tach) | 6 | Tegra GPU, nvpmodel `MAXN_SUPER`, L4T |
| Raspberry Pi 4 (Debian 13) | 5 | 1 | 0 | 0 | 4 | DRM card `v3d` named, not read |
| x86 PC (Ubuntu 26.04) | 58 | 28 | 0 | 1 | 22 | i915 GPU, intel_vpu NPU |

The comparison needed two tolerance changes, both for real readings:
- A core moved up to 8 °C between two reads moments apart, so the temperature tolerance is 10 °C.
- i915 reports 0 MHz while the GPU is in RC6, so a GPU clock of 0 is accepted.

Failure paths were not exercised: no host had an unreadable, missing or vanishing attribute during the
runs. The Python tests cover those on fake trees; the Rust port has no fake-tree support yet.

**CPU, same scope, A B B A** (30 s warmup, 120 s windows, user + system % of one core, own `getrusage`):

| host | Python (A, A) | platmon-native (B, B) | change | peak RSS, Python / Rust |
| --- | --- | --- | --- | --- |
| Orin | 1.273, 1.230 | 0.432, 0.431 | −66 % (2.9×) | 16.1 / 1.5 MiB |
| Raspberry Pi 4 | 0.797, 0.807 | 0.255, 0.262 | −68 % (3.1×) | 20.0 / 1.7 MiB |
| x86 PC | 0.620, 0.623 | 0.371, 0.391 | −39 % (1.6×) | 21.1 / 1.5 MiB |

All 480 collections per host succeeded. Use the totals: the user/system split is tick-sampled on these
kernels. These figures cover collection only, without network, disk_io or pressure, so they are lower
than the floor-bench numbers in rust-feasibility.md, which included those.

**What it took to get there.** The first A B B A run, with std file I/O and musl's allocator, gave
Orin 0.55, Raspberry Pi 0.37 and x86 0.48 % for Rust. `strace -c` over 23 x86 collections showed two causes:
- 4233 `fcntl` calls: musl adds an `F_SETFD` after every `O_CLOEXEC` open.
- About 1,500 `mmap`/`munmap` pairs: musl's allocator returns freed groups to the kernel.

Raw `openat`/`read`/`readlinkat` with one reused buffer (`sysfs.rs`) and a size-class cache in front of the
allocator (`alloc.rs`) removed both. A product build would use a vetted allocator crate here
(mimalloc and jemalloc are MIT-licensed).

Raw records are kept outside the repository. SHA-256, first 16 hex digits:
- results: Orin `ecbd98eda8b499d9`, Raspberry Pi `d2290973d157211a`, x86 `e044f35c961b0856`
- binaries: aarch64 `dff5005a4efd29af`, x86_64 `9b568bfb7d2ff6a3`

The files used on the boards were removed afterwards.
