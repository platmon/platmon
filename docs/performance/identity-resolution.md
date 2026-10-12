# Sysfs path identity: reuse or resolve

Follow-up to [collection-response-breakdown.md](collection-response-breakdown.md), which found
`sysfs.canonical` (resolving a sysfs path for a sensor's source identity) at 3.7 ms of a 14.9 ms
collection on a Jetson Orin Nano and 1.3 ms of 8.3 ms on a Raspberry Pi 4 (instrumented stage costs).
This note decides how to make that cheaper without changing what the identities mean, and records the
change and its measured effect.

## Result (2026-10-08)

**Chosen: keep resolving every path on every collection, but let the kernel do it** (option D below).
No cache, so nothing can go stale. Measured A B B A A B B A at 1 s, one client polling every 1 s, 120 s
windows, reference instrumentation only (A: main `0d1a67d`, B: this change; same bench code,
`benchmarks/cadence.py run --phase ab` from two trees into one output directory):

| board | process CPU % (one core), A runs | B runs | B - A | collection thread CPU ms, A mean | B mean | B - A |
| --- | --- | --- | --- | --- | --- | --- |
| Orin Nano | 1.73, 1.79, 1.73, 1.74 (mean 1.75, spread 0.06) | 1.47, 1.50, 1.48, 1.49 (1.48, 0.03) | **-0.27 pp (-15 %)**, every B run below every A run | 14.44 | 11.79 | **-2.65 ms (-18 %)** |
| Raspberry Pi 4 | 1.39, 1.27, 1.28, 1.39 (1.33, 0.13) | 1.24, 1.19, 1.31, 1.29 (1.26, 0.12) | -0.08 pp: **not distinguishable** (smaller than the spread between repeats) | 7.16 | 6.37 | -0.80 ms (-11 %); per A B B A block -1.08 / -0.51 |

- On the Orin the change is clear at the process level. On the Raspberry Pi the collection got cheaper
  in both blocks, but its process CPU drifted between blocks by more than the change, so the effect on the
  whole process is not shown by these 8 windows.
- The instrumented stage cost of `canonical` was 3.7 ms (Orin) / 1.3 ms (Raspberry Pi, diagnostic); the
  collection's CPU fell by 2.65 / 0.80 ms. These are separate measurements, not a share of each other.
- Unchanged: request handling (do_GET thread CPU p50 1.16-1.19 ms Orin, 2.02-2.27 ms Raspberry Pi in both
  trees), actual interval (max 1000.5 ms), no failed collection. The observed scope (sensors, source
  records, collector states) was the same in every run; the runs differ only in the product hash.
- Environment: the boards' own platmon services kept running and were not touched (state compared before
  and after); temperatures between runs 50.5-53.5 °C (Orin), 51.6-53.6 °C (Raspberry Pi), `get_throttled`
  0x0 throughout; 8 windows / 20 min per board, no invalid run, no dropped record. Raw records kept
  outside the repository: SHA-256 (first 16 hex digits) orin `c917b2c68fd38ee3`, rpi4 `b0573828d64f97a8`.
- Not measured: whether the Raspberry Pi's process-level effect shows with more repeats; the change on a
  board where `/proc/self/fd` is unavailable (it falls back to the old way, so the cost is the old one).

## What has to stay true

`canonical(path, sysroot)` resolves symlinks, makes the result relative to the sysfs root and checks
that it exists; `chip_identities` then names a hwmon chip by its device when no other node of the
current listing shares that device, else by its resolved path, else `unresolved`. Source ids are the
SHA-256 of a descriptor built from these. A faster version must keep, collection by collection:

| change on the host | behaviour to keep |
| --- | --- |
| a value changes (temperature, fan speed) | read again, new read span (resolution does not touch values) |
| a label changes | displayed name follows, source id unchanged (labels are not in the descriptor) |
| a hwmon node appears or goes away | the shared-device decision is made again from the current listing |
| same names, a symlink now points elsewhere | the new target's identity, never the old one |
| a path that did not resolve resolves again | it gets its identity back; `unresolved` is not kept |

The first two do not depend on resolution at all. The last three do: whatever is reused across
collections has to notice them.

## Options

| | option | what it saves | what it risks | verdict |
| --- | --- | --- | --- | --- |
| A | share within one collection: one `hwmon_chips` + `chip_identities` result for `hwmon_sensors` and Jetson `tach_fans` | the duplicate: 8 of the Orin's 17 calls are the same 4 chips twice; nothing on the Raspberry Pi (no duplicate) | the two parts note listing and name errors in their own groups; a shared result must keep both reports | not needed after D (see below) |
| B | reuse across collections after a check | resolution, but each path still needs a check that sees a retargeted link: at least `stat` + `readlink` | links further up the path, inode reuse after hotplug, a test tree; the check alone costs about half of D | not adopted |
| C | resolve once at start and keep | everything | breaks the last three rows above | rejected |
| D | resolve on every call, but let the kernel do it | most of the cost (measured below) | same answers where verified (see "What is and is not the same"); falls back to the portable way where the kernel cannot answer | **chosen** |

The Orin profile in the breakdown pointed at Python's path handling inside `os.path.realpath` (one
`join`/`normpath`/`lstat` per component, plus `relpath`), not at the system calls. D removes that work
instead of avoiding it: open the path with `O_PATH` (no read access needed) and read the
`/proc/self/fd/N` link, which the kernel fills with the path it resolved; the relative part is then a
prefix check against the root, resolved the same way.

### Probe (read-only, before choosing)

Every `canonical` call of one collection, then each variant over those paths in a loop (200 rounds,
thread CPU per call). A loop is warm and only compares variants with each other; it does not predict
the cost in the service (the stage timing measured 217 µs per call on the Orin there).

| board | calls per collection | current | D: kernel path | B's minimum check (`stat` + `readlink`) | same results |
| --- | --- | --- | --- | --- | --- |
| Orin Nano (Python 3.10) | 17: 4 hwmon chips × 2, 6 thermal zones, 3 GPU files | 91 µs | 22 µs | 9 µs | yes |
| Raspberry Pi 4 (Python 3.13) | 3: 2 hwmon chips, 1 thermal zone | 137 µs | 38 µs | 20 µs | yes |

B would still pay about half of D per path for its check, plus whatever it takes to see a link further up
a path change, and it adds a cache with invalidation rules. A would remove at most the Orin's 8 duplicate
calls, at D's lower price per call. D resolves every time, as today, and needs no invalidation rules,
so it is the change.

## The change

`collector/sysfs.py`:

- `canonical` resolves `sysroot`, then `path`, with `_kernel_path` (`os.open(path, O_PATH | O_CLOEXEC)`,
  `os.readlink(/proc/self/fd/N)`, close). Equal paths give `.`, a path under the root gives the part after
  `root/`, anything else `None`.
- The path is resolved last, so it is the latest observation, as the portable way's final
  `os.path.exists(real)` is. A node removed or a link retargeted before it gives `None` or the new
  target, never the path seen a moment earlier (a first version resolved the path first and got this
  wrong; found in review, covered by tests now).
- Wherever the kernel cannot answer, the old algorithm (now `_canonical_portable`) gives the result: no
  `O_PATH` (not Linux), any `OSError` (missing path, dangling link, loop, a file in place of a directory,
  no permission, `/proc` not mounted), a path deleted between open and readlink (`" (deleted)"`) or a
  non-absolute answer.
- Nothing is cached; callers are unchanged.

### What is and is not the same

| case | status |
| --- | --- |
| ordinary sysfs paths (class aliases, device paths, links in the path, `.`/`..`, a symlinked root) and test trees | same answers: tested, and checked on every path of a real collection on both boards |
| the kernel cannot answer (not Linux, no `/proc`, the errors above) | the old algorithm answers, so the old result |
| the tree changes during the call | neither way is a snapshot. The order keeps the path as the last observation in both; a change after it is seen by the next collection |
| paths across mount changes, procfs links and other special files | not verified: the `/proc/self/fd` text is the kernel's view of an open file and can differ from a lexical `realpath` there. platmon resolves only sysfs paths, but no test covers these cases |

Tests (`tests/collector/test_provenance.py`): the new and the portable way agree for aliases, a link in the middle
of a path, a root given through a symlink, `.`/`..` in the path, missing, dangling, looping,
file-as-directory, outside the root; the same answers without `O_PATH` or `/proc`; a retargeted link
resolves to its new device; an unresolved path recovers; a second node on a device switches both to
path identity and back when it goes; a node removed, or a link retargeted, between the two resolutions
gives `None` / the new target. The existing identity tests (label change keeps the id, a
renumbered `hwmonN` keeps its device id, virtual devices are path-based) pass unchanged. On the boards
themselves, every path of a real collection resolved the same both ways (Orin 34 calls over two
collections, Raspberry Pi 6), and the Orin ran `tests/collector/test_provenance.py` (73 passed; the Raspberry Pi has
no pytest and none was installed).
