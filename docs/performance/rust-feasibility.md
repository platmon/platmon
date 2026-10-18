# Rust feasibility: what a native collector could save

Exploratory study, 2026-10-18. It asks how much CPU and memory the service could save if it were written in
Rust, and whether one native binary per CPU architecture can serve the supported boards. Nothing here
changes the product, and no decision to rewrite has been made. Every number is a measurement unless it is
marked as estimated or as a hypothesis.

## Summary

- **Collection only (measured):** replaying the file operations of one Python collection in Rust uses
  53–76 % less CPU than the Python collector itself (2.1–4.1 times less): Orin 1.37 → 0.43 %, Raspberry Pi 4
  1.16 → 0.28 %, x86 PC 0.66 → 0.31 % of one core. This is a micro-benchmark of reading and parsing. It is
  not a Rust implementation of the collector (see [Limits](#limits)).
- **Where the saving comes from:** user time falls by about 90–95 %. Kernel time stays: on the Orin it is
  about 27 % lower in Rust (0.52 → 0.38 %); on the Raspberry Pi and the x86 PC it is about the same. Part of
  the Orin difference may be fewer system calls per file rather than the language.
- **The whole service is a different number.** The installed services, measured in a separate 60 s window,
  used Orin 2.15 %, Raspberry Pi 2.75 %, x86 1.13 % with Hub polling and replay on. Collection is 1.37 / 1.16 /
  0.66 % of that; the rest (requests, replay, history, slow groups, threads) is about 0.8 / 1.6 / 0.5 pp.
  Replacing only the collector would give about 1.21 / 1.87 / 0.78 % (estimated, below). A whole Rust service
  at 0.5–0.7 % is a **hypothesis**: HTTP, replay and history were not measured in Rust.
- **Memory (measured, collection only):** the Python collector process peaked at 16–21 MiB RSS, the Rust
  probe at about 1 MiB. Neither holds replay or history buffers. A whole-service figure is not measured.
- **One binary per architecture (measured):** one `aarch64-unknown-linux-musl` static binary ran unchanged on
  the Orin (L4T, Ubuntu 22.04, kernel 5.15) and the Raspberry Pi 4 (Debian 13, kernel 6.18).
- **Packaging (checked with Lintian, Debian 13):** native binaries in an `Architecture: all` package are a
  Lintian error (`arch-independent-package-contains-binary-or-object`), so a native service means one `.deb`
  per architecture. A musl static binary is also a Lintian error (`statically-linked-binary`) and would need
  an override with its reason in `packaging/debian/lintian-overrides`.
- **Not a release judgment.** This was one exploratory round (60 s windows, no warmup beyond three
  collections), not the [budget](budget.md) procedure (30 s warmup, 120 s windows, A B B A). The Raspberry Pi
  service's 2.75 % was measured under Hub polling with replay on. It is not a failure of the 2.5 % target for
  the default configuration.

## Method

Two programs ran next to the installed platmon 1.3.0-1 services. Those services, and the Hub polling them,
kept running and were not touched. Each program reports its own `getrusage(RUSAGE_SELF)` over the window.

- **Python (P):** the installed collector (`PYTHONPATH=/usr/share/platmon`), wired as `platmon.py` wires it
  from `/etc/platmon/platmon.ini`: `collect_recorded` with CPU, Network, Disk I/O, Pressure (as configured),
  GPU and NPU. No Sampler thread, HTTP, history, replay or slow groups. One collection per second on absolute
  deadlines, after three warm-up collections.
- **Trace:** after the warm-up, the same Python program records every filesystem call of one steady-state
  collection (`open`, `os.open`, `listdir`/`scandir`, `readlink`, `stat`/`lstat`, `statvfs`,
  `os.path.realpath`/`exists`/`isdir`/`isfile`) with its path.
- **Rust (R):** a std-only program (rustc 1.88, release, `x86_64`/`aarch64-unknown-linux-musl`, static)
  replays that trace once per second, three warm-ups first. For every read it parses each
  whitespace-separated word as an integer, so the reads are not optimized away.
  - **Rr:** only the opens and reads.
  - **Rf:** every recorded operation (listings, link reads, stats, path resolution) every collection.
- **Order per host:** trace, then P, Rr, Rf, Rf, Rr, P, 60 s each. One round, no repeats, no window discarded.
- **Whole service:** utime and stime of the running service from `/proc/<pid>/stat` over a separate 60 s window
  the same day.

Hosts:

| host | CPU | OS, kernel | Python |
| --- | --- | --- | --- |
| Jetson Orin Nano Super | 6 cores, aarch64 | Ubuntu 22.04.5 (L4T R36.5), 5.15.199-tegra | 3.10.12 |
| Raspberry Pi 4 | 4 cores, aarch64 | Debian 13, 6.18.50+rpt-rpi-v8 | 3.13.5 |
| x86 PC | 22 threads, x86_64 | Ubuntu 26.04.1, 7.0.0-38-generic | 3.14.4 |

Operations in one steady-state collection (from the trace):

| host | reads | listings | link reads | stats | statvfs | total |
| --- | --- | --- | --- | --- | --- | --- |
| Orin | 105 | 9 | 63 | 12 | 1 | 190 |
| Raspberry Pi 4 | 28 | 5 | 10 | 2 | 1 | 46 |
| x86 PC | 185 | 11 | 79 | 12 | 1 | 288 |

## Results

CPU % of one core, user + system, per window in run order (P, Rr, Rf, Rf, Rr, P):

| host | P | Rr | Rf | Rf | Rr | P |
| --- | --- | --- | --- | --- | --- | --- |
| Orin | 1.320 | 0.337 | 0.431 | 0.425 | 0.335 | 1.421 |
| Raspberry Pi 4 | 1.172 | 0.215 | 0.269 | 0.285 | 0.207 | 1.156 |
| x86 PC | 0.592 | 0.284 | 0.323 | 0.303 | 0.277 | 0.719 |

Means and the split between user and system time:

| host | P total (user / sys) | Rf total (user / sys) | Rf vs P | Rr total |
| --- | --- | --- | --- | --- |
| Orin | 1.37 (0.85 / 0.52) | 0.43 (0.05 / 0.38) | −69 % (3.2×) | 0.34 |
| Raspberry Pi 4 | 1.16 (0.97 / 0.20) | 0.28 (0.05 / 0.23) | −76 % (4.1×) | 0.21 |
| x86 PC | 0.66 (0.41 / 0.25) | 0.31 (0.04 / 0.27) | −53 % (2.1×) | 0.28 |

- The Raspberry Pi's user/system split looked tick-quantized: one Rr window reported exactly equal user
  and system time. Its totals are consistent between windows, so use the totals.
- **Rf is the comparison, not Rr.** [identity-resolution.md](identity-resolution.md) chose to resolve every
  source path on every collection, with the kernel doing the resolution, and rejected caching, because a path
  can point elsewhere later (hotplug, relinked `hwmon` numbers). Rr's lower number would need an
  invalidation design the current contract does not have.
- Peak RSS: P 16.5 MiB (Orin), 21.0 MiB (Raspberry Pi), 23.0 MiB (x86). Rust 0.98 / 1.07 / 0.77 MiB.

### Whole-service estimate

Estimated, not measured. The current service's CPU, with the Python collector's cost swapped for Rf's:

| host | service now (measured) | collector only in Rust (estimated) | change |
| --- | --- | --- | --- |
| Orin | 2.15 | ≈ 1.21 | −44 % |
| Raspberry Pi 4 | 2.75 | ≈ 1.87 | −32 % |
| x86 PC | 1.13 | ≈ 0.78 | −31 % |

This assumes that the in-service collection costs what the standalone loop costs. It also leaves out the
cost of joining Rust to the service, which is either a Python extension in the service process or a separate
process with IPC. Neither variant removes the Python dependency. The service and the loops were measured in
different windows.

## Packaging and distribution

- **Static musl binaries** do not depend on the host's glibc or Python. That removes the glibc and Python
  version axes, but nothing else. A native service still depends on the kernel and its system calls, the
  `/proc`, `/sys` and device-tree layout, external programs such as `tegrastats`, file permissions and the
  service manager. The oldest kernel to support (for example L4T R32's 4.9) needs its own test against the
  Rust target's minimum kernel.
- **Architectures:** `x86_64-unknown-linux-musl` (amd64) and `aarch64-unknown-linux-musl` (arm64) cover the
  current boards. `arm-unknown-linux-musleabihf` (ARMv6, armhf) would cover 32-bit Raspberry Pi OS and the
  older boards. `riscv64gc-unknown-linux-musl` would be optional.
- **One package per architecture.** Checked on a test package (two musl binaries, `Architecture: all`) with
  Lintian in a Debian 13 container:
  - `E: arch-independent-package-contains-binary-or-object` for each binary
  - `E: statically-linked-binary`

  Through the signed APT repository, `apt install platmon` would pick the right package, as it does now.
  A manual download would need the right file.
- **Only a standalone native service removes the Python dependency** and with it the Python version floor
  (`python3 (>= 3.9)` today). A Rust extension or helper next to the Python service keeps that floor.

## Limits

- **Not an implementation.** The Rust probe repeats file operations and parses integers. It builds no stats
  model, checks no provenance or read spans, serializes no JSON and handles no requests.
- **A defect in the trace.** The recorder logged the kernel path resolution's `os.open(path, O_PATH)` as a
  read, and its `readlink("/proc/self/fd/N")` was replayed literally in the Rust process, where that
  descriptor is not the same file.
  - Rust therefore opened those paths (all directories: `/sys`, `hwmon*`, a thermal zone) for reading, and
    the read failed with `EISDIR`. That happened 48 times per collection on the Orin, 8 on the Raspberry Pi
    and 72 on the x86 PC.
  - Rust also read a link that did not resolve the same way.
  - Both are small next to the totals, but Rf is not exactly the Python collection's system calls. A rerun
    must record O_PATH resolution as one operation and replay it as `open(O_PATH)` + `readlink` of its own
    descriptor.
- **One round, exploratory windows,** with the production service and Hub polling on the same host. Not
  the budget procedure.
- **Elapsed time is not measured.** Lower CPU does not imply a lower read latency: device wake-ups (for
  example the 1 s battery firmware cache) stay.

## Next steps

Proposed, not decided:

1. **Rust collector prototype** on an experimental branch, not connected to the service. It covers the core
   groups and their metadata. It is compared with the Python collector on the same fixture trees and on the
   same host at the same time: values, `collectors` states and reasons, sensor source identities and read
   spans. Then CPU, RSS and elapsed time.
2. **Only if 1 passes:** a Rust service with HTTP, history and replay, A/B against Python 1.3.0 under the
   same load with the budget procedure, then 24 h.

Proposed gates for deciding a rewrite. They are suggestions for justifying the cost, not product
requirements:

| measure | proposed gate |
| --- | --- |
| CPU | default configuration ≤ 2.5 % on both ARM boards (the existing target) |
| improvement | whole-service CPU at least 30 % lower than Python |
| memory | whole-service RSS/PSS at least 50 % lower |
| cadence | 1 s kept, no more overruns |
| contract | the HTTP API, observations, history and replay contracts unchanged, checked against Python output |
| stability | 24 h without failed collections or lasting memory growth |
| hosts | Orin, Raspberry Pi 4 and x86 Linux on real hardware |

## Records

Raw results, traces, scripts and the Rust source are kept outside the repository. SHA-256, first 16 hex
digits:

- results: Orin `aa7bbe2d6ea927eb`, Raspberry Pi `7d07c0993dafee5e`, x86 `b4983b8c0cddf3aa`
- traces: Orin `e1b260a71e708b77`, Raspberry Pi `f348d4d0386b734c`, x86 `504f9775567a70d5`
- probe binaries: aarch64 `dd05ebb50c4fec52`, x86_64 `f79e9aa380734049`

The bench files were removed from the boards afterwards.
