# HTTP API

platmon answers on port 9797 (`[http]` in `platmon.ini`). There is no authentication: run it on trusted
networks only. Every answer of `/api/stats`, `/api/status`, `/api/observations`, `/api/history`, `/api/samples`, `/api/observation-samples` and `/text`
carries `Cache-Control: no-store`.

| Path | Answer |
| --- | --- |
| `/api/stats` | the current snapshot as JSON; 503 when there is none |
| `/api/status` | the collector's runtime status as JSON; always 200, also without the web page |
| `/api/observations` | low-frequency groups (storage capacity, Wi-Fi signal, TCP connect probe) with their own ages; always 200 ([observations.md](observations.md)) |
| `/api/history` | recent numbers of the published snapshots, when `[history]` is on; 404 otherwise ([history.md](history.md)) |
| `/api/samples` | full published core snapshots after a cursor; bounded memory replay ([replay.md](replay.md)) |
| `/api/observation-samples` | full publications of one slow group after its own cursor ([replay.md](replay.md)) |
| `/text` | the `platmon` terminal screen as plain text; 503 when there is no current snapshot |
| `/` | the web page (only with `web = yes`) |

One background thread collects a snapshot every `interval`; HTTP requests only read the latest one and
never collect on their own. `/api/stats` and `/text` wait up to 5 s for the first snapshot after the
service starts; `/api/status` never waits.

The HTTP server allows at most 32 active connections. Excess connections are closed before a request
thread is created; clients can retry after an active connection finishes. Request reads time out after
10 s without input. Response writes time out after 10 s without send progress; a steadily transferring
response may take longer overall. Disconnected or stalled clients release their connection slots.

## `/api/stats`

The metrics (`cpu`, `gpu`, `memory`, `disk`, `temperature`, `power`, `fans`, `time`, ...) keep their
keys, units and types. Schema version 1 adds `schema_version`, `sample`, `collectors` and, from the
service, `sensor_meta` next to them; answers without `schema_version` come from an older platmon (legacy)
and have no sample metadata. With `[network] enabled = yes` (the default) there is also `network`: the
interface counters and rates of platmon's own network namespace, described in [network.md](network.md), and
with `[disk_io] enabled = yes` (the default) `disk_io`: the I/O counters and rates of each physical disk, in
[disk-io.md](disk-io.md). With `[pressure] enabled = yes` (on by default) there is `pressure`: the kernel's
pressure stall information, in [pressure.md](pressure.md).

```json
{
  "schema_version": 1,
  "sample": {
    "instance_id": "8dfad9fa-9bf0-4d61-91e0-ef22aebd2b91",
    "sequence": 42,
    "started_at": 1791345600.0,
    "completed_at": 1791345600.25,
    "duration_ms": 250.0,
    "age_ms": 100.0,
    "cycle_age_ms": 350.0,
    "data_age_ms": 349.8,
    "data_age_basis": "oldest_current_read_start",
    "interval_ms": 1000.0,
    "stale_after_ms": 5000.0
  },
  "collectors": {"core": {"state": "ok", "reason": null}}
}
```

- `instance_id`, `sequence`: identify the sample. `instance_id` is a random UUID made when the service
  starts (not derived from the host or user); `sequence` counts the snapshots that instance published,
  from 1. Polling does not change them: the same `(instance_id, sequence)` is the same sample read again.
  A failed or dropped collection does not count. After a restart `instance_id` changes and `sequence`
  starts over.
- `started_at`, `completed_at`: wall-clock Unix seconds when the collection began and ended. Shown for
  people; if the system clock is stepped they can be off, even `completed_at < started_at`.
- `duration_ms`, `age_ms`, `cycle_age_ms`, `data_age_ms`: measured on an elapsed clock (see Clock), not
  the wall clock, as of this answer. `duration_ms` is how long the collection took, `age_ms` the time
  since it completed, `cycle_age_ms` the time since it started (`cycle_age_ms = age_ms + duration_ms` up
  to rounding), and `data_age_ms` how old the data is, counted as `data_age_basis` says:
  - `oldest_current_read_start` (the service): from the start of the earliest read this snapshot's data
    comes from: the CPU reading of this collection, memory, disk, uptime, every sensor value in it, and
    the network and disk counters' reads (`network.read`, `disk_io.read`).
    Not counted: the CPU baseline (the start of `cpu_sampling.window_ms`), failed reads, load candidates
    that were not used, and names, labels and other text. `data_age_ms <= cycle_age_ms`.
  - `cycle_start_upper_bound`: from the start of the collection (`data_age_ms = cycle_age_ms`). Used for
    a collect function that keeps no read records (a plain dict, as from a direct `collector.collect()`),
    and as a fallback when the service's records do not check out: a required read (CPU, memory, disk,
    uptime) missing, a sensor value without its read, a record for a value that is not in the snapshot or
    at a pointer other than the one platmon writes for it (never published), a read outside the collection or reversed, another clock, or records of the wrong
    shape. The values are published either way; the service log says why, once per change.

  Either way these are the times platmon read the files, not when a sensor measured: a driver may cache
  values, and hardware may convert earlier. CPU usage averages back to the previous reading (see CPU
  usage); that window is not part of the data age. Schema version 1 is unchanged, but `data_age_ms` of the
  service now counts from its oldest read instead of the collection start: it is a little lower, and a
  snapshot goes stale correspondingly later.
- `interval_ms`: the configured collection interval. It is not the window CPU usage is averaged over:
  that is `cpu_sampling.window_ms`.
- `stale_after_ms`: when the snapshot stops counting as current: 3 intervals, at least 5 s.
- `collectors.core`: the state when this snapshot was made. `ok` means the required data was collected
  and the snapshot built, not that every sensor could be read: the optional groups next to it say that
  (see Optional metrics), and some or all CPUs can still be missing from `cpu` (see `cpu_sampling`). A
  later failure does not change it: see `/api/status`.

A snapshot is current while `data_age_ms <= stale_after_ms`. A collection that already took longer than
`stale_after` (its whole `duration`, however recent its last read) is dropped (`collection_too_slow`)
instead of being served as new data.

### Sensor sources and read times

`sensor_meta` (from the service) describes the sensor values of this snapshot, keyed by a JSON Pointer
(RFC 6901: `~` is written `~0`, `/` is `~1`) to the value it describes. It does not repeat the values.

```json
"temperature": {"cpu": 51.2},
"sensor_meta": {
  "/temperature/cpu": {
    "id": "src1:9c1f…(64 hex digits)",
    "provider": "thermal",
    "identity_basis": "resolved_path",
    "unit": "celsius",
    "read": {"started_offset_ms": 4.2, "completed_offset_ms": 4.7}
  }
}
```

Covered: every value in `temperature` and `power`, `rpm` and `percent` of every fan, `freq` of every CPU
(CPU usage has `cpu_sampling`), and the GPU's and NPU's `usage`, `freq` and `max_freq`. A pointer names a position in
this snapshot only: labels and the order of `fans` and `cpu` can change (the index in `/cpu/1/freq` is the
row, not the CPU id). A value left out has no pointer; a `null` value can have one, with `read: null`.

- `read`: when platmon read the value, as offsets from the collection's start (`sample.started_at`): from
  the start of the first read it took to the end of the last. A power rail computed from voltage × current
  spans both reads (they are not one instant). Label and name reads are not part of it. `null` for a
  value that was not read, or whose record does not fit the collection.
- `unit`: `celsius`, `watt`, `rpm`, `percent` or `hertz`; the value's unit as reported (`freq` in Hz).
- `id`: `src1:` and the SHA-256 of a canonical JSON descriptor (sorted keys, no spaces: version,
  provider, unit, basis, where): the source, not the label, value, position, host or reading time. Two
  values with the same id came from the same source; renaming a label does not change it. It is not a
  hardware serial: a replaced device at the same place keeps the id, and readings across a restart or a
  gap are not promised to be one series (use it together with `sample.instance_id`).
- `identity_basis`, how far the id holds:
  - `device_channel`: the device and attribute, e.g. a hwmon chip by the device it sits on, so a
    renumbered `hwmonN` keeps its id; a CPU clock by the CPU id. Only when no other hwmon node of the
    same device is listed.
  - `resolved_path`: the path inside `/sys` once symlinks are resolved (thermal zones, the GPU, a device
    with several hwmon nodes, anything under `devices/virtual`): the same id only while that path stays
    the same.
  - `unresolved`: the path did not resolve; `id` is `null`, the value is kept and its group is not
    degraded for it.

Where `/sys` is reached through another path (a container, a test tree), only the part inside `/sys`
counts, so the id is the same. No hardware ids, host names or full paths are in the API.

### CPU usage

`cpu` is a list of `{"id", "usage", "freq"}` as before; `usage` is the busy share of the CPU's ticks
between two `/proc/stat` readings (idle and iowait count as idle; guest time is not counted twice), in
percent with one decimal. Next to it, `cpu_sampling` says what that window was:

```json
{
  "cpu": [{"id": 0, "usage": 50.0, "freq": 1200000000}],
  "cpu_sampling": {
    "mode": "interval",
    "window_ms": 1250.0,
    "unavailable": [{"id": 2, "reason": "warmup"}]
  }
}
```

- `mode`: `interval` for the service. It reads `/proc/stat` once per collection and compares with its
  previous successful reading, so a collection no longer waits 250 ms for a second one. `oneshot` for a
  direct `collector.collect()` call without the service's state: two readings 250 ms apart, nothing kept.
- `window_ms`: the elapsed time between the two readings, measured when each reading began. It follows
  the real timing, so it is not `interval_ms` (a collection takes time, and failed ones still read). `null`
  when there is no earlier reading or the clock did not move forward.
- `unavailable`: CPUs that were read but have no `usage` in this sample, with a reason. They are left out
  of `cpu` rather than shown as 0 %; a measured idle CPU is `0.0` and is not listed here.

| Reason | Meaning | Next reading |
| --- | --- | --- |
| `warmup` | no earlier reading of this CPU: the service's first sample, a CPU that came online, or one back after going offline | measured |
| `counter_regressed` | one of its counters went down (iowait can, on some kernels); not treated as a reboot and not clamped | measured, from this reading |
| `no_ticks` | its counters did not move: nothing to average, which is not 0 % | measured, from this reading |
| `invalid_interval` | the elapsed clock did not advance between the readings (all CPUs) | measured, from this reading |
| `gap` | the readings are more than `max(3 × interval, 5 s)` apart (all CPUs), e.g. after failing or slow collections | measured, from this reading |

What changed: until this version, every snapshot's CPU usage covered 250 ms inside its collection. From
the service it now covers the time since the previous reading, about one interval (`window_ms`). The JSON
types are the same, but the numbers average over a different window: a short burst weighs less.

The service's CPU baseline is the last successful reading, not the last published snapshot: when a
collection reads the CPUs and then fails on another sensor (or is dropped as too slow), nothing is
published, but the next window still starts at that reading, so it never spans readings that were not
taken. A window longer than the gap limit is not used. A `/proc/stat` read that fails, or has a `cpuN`
line with fewer than the 8 counters user..steal, fails the collection (`collection_failed`), is not used
as a baseline, and the next valid reading starts with `warmup`.

The service's first CPU reading is all `warmup`, so a snapshot made from it has `cpu: []` and is still a
normal, current snapshot (`ready`). Usually that is the first published snapshot; if the first collection
read the CPUs and then failed on another sensor, the first published one already has CPU usage.

Limits: CPUs that go offline and come back between two readings are not noticed (their counters continue
or are caught as `counter_regressed`); the lines of `/proc/stat` are not read at one instant.

### GPU

`gpu` is one object, or `null` where the host has no graphics device at all:

```json
{"gpu": {"usage": 12.5, "freq": 700000000, "max_freq": 2250000000, "driver": "i915", "provider": "rc6_residency"}}
```

`usage` is a percentage with one decimal, `freq` and `max_freq` are hertz; each is `null` where that
driver or that moment has no value, and a measured `0` is `0` (an idle Intel GPU has its clock gated
off). `driver` and `provider` are there when the entry came from the DRM class or NVIDIA query; a board's own source
sets neither.

| Source | `provider` | What `usage` is |
| --- | --- | --- |
| the board's own (Jetson GPU devfreq) | absent | the load the board reports |
| `i915` (Intel, `/sys/class/drm/cardN`) | `rc6_residency` | the share of the time between two collections the GPU was not in its RC6 idle state |
| `amdgpu` | `gpu_busy_percent` | the busy share the driver publishes, as it is |
| NVIDIA (`nvidia-smi`, including WSL2) | `nvidia-smi` | the utilization percentage reported by NVIDIA |

- The board module is asked first and the DRM class is read where it gave nothing, so a Jetson keeps its
  own numbers and every other host gets what its driver publishes.
- When no supported DRM reader is available, the optional driver command `nvidia-smi` is queried
  once per collection for GPU UUID, utilization, current graphics clock and maximum graphics clock.
  It is discovered from PATH or `/usr/lib/wsl/lib/nvidia-smi`; it is not installed by platmon.
  `driver` is `nvidia`. One GPU is chosen by sorted UUID, with UUID and query field identifying each
  sensor's `device_channel` source. Unsupported fields are null (`not_exposed`); invalid numbers
  report `invalid_data`. Queries time out after 0.5 seconds and command failures report `io_error`
  (execution permission errors report `permission_denied`). Failed queries do not reuse old readings.
  WSL support depends on the fields exposed by the installed NVIDIA driver. No native Linux or Docker
  NVIDIA hardware acceptance is implied by the WSL2 validation.
- An `i915` card has no `usage` in the first collection (`null`): RC6 is a counter, so a share needs two
  readings, like a CPU warming up. A break longer than `max(3 × interval, 5 s)` is not averaged over, and
  a counter that went backwards starts again instead of reporting the jump.
- RC6 residency is idle time, not engine occupancy: a GPU that is awake with nothing to do counts as
  busy. The per-engine numbers that would separate the two come from `perf` events or per-process
  `fdinfo`, neither of which an unprivileged service can read for the whole host.
- A card whose driver has no reader here is still reported, with its `driver` and no numbers, so a host
  with a GPU platmon cannot measure does not look like a host without one: the group is `unavailable` /
  `not_exposed`. With no card at all it is `unavailable` / `not_detected`.
- One DRM card is reported: the first, by card number, with a reader. Reading these DRM attributes does not wake
  the GPU (measured on Meteor Lake: the same RC6 share at 0, 1 and 100 reads per second).

### NPU

`npu` is one object, or `null` where the host has no compute accelerator:

```json
{"npu": {"usage": 0.0, "freq": 0, "max_freq": 1400000000, "driver": "intel_vpu", "provider": "npu_busy_time"}}
```

The fields are the GPU's, read the same way from the accel class the kernel gives compute accelerators
(`/sys/class/accel/accelN`, DRM accel). `intel_vpu` publishes a cumulative busy time, so `usage` is the
share of the time between two collections the NPU worked, with the same first-collection `null`, gap
limit and counter-reset rule as the GPU. An accelerator whose driver has no reader here is reported with
its `driver` and no numbers (`unavailable` / `not_exposed`); a host without one is `unavailable` /
`not_detected`. No board module fills this group.

An NPU that has never run reads `0`, which is an idle NPU, not a missing one: a device that is suspended
also reports its clock as `0`. Reading these attributes does not resume it (measured on Meteor Lake: it
stays in D3hot through repeated reads). `[thresholds]` sets no levels for an NPU, so viewers show it
without warning colors.

### Optional metrics

Required for a snapshot: the CPU counters (`/proc/stat`), memory, the root filesystem's disk usage and the
identity fields (model, system, uptime). If one of them cannot be read, the whole collection fails
(`collection_failed`): nothing new is published, the last good snapshot is served until it is stale, then
503. Optional: CPU clocks, GPU, temperatures, power, fans, the Jetson power mode and L4T release. What
cannot be read there is reported in its group and left out, and the snapshot is still published with
everything else it read in this collection; earlier values are never copied in.

How a value that could not be read looks: a temperature or power rail is missing from its table; a fan
stays listed with `rpm` or `percent` `null`; a CPU's `freq` and the GPU's `usage`/`freq`/`max_freq` are
`null`; `gpu` itself is `null` only without a graphics device; `power_mode` is `null`; `system.l4t` is
missing. A measured `0` is `0`.

`collectors` has one entry per optional group: `cpu_frequency`, `gpu`, `npu`, `temperature`, `power`, `fans`,
`power_mode`, `board_info` (the L4T release), and `network` / `disk_io` while enabled ([network.md](network.md),
[disk-io.md](disk-io.md)):

```json
"collectors": {
  "core": {"state": "ok", "reason": null},
  "temperature": {"state": "partial", "reason": "some_unreadable",
                  "issues": [{"target": "hwmon2.temp1", "reason": "invalid_data"}], "issues_truncated": 0},
  "gpu": {"state": "unavailable", "reason": "not_detected", "issues": [], "issues_truncated": 0}
}
```

| State | Meaning | `reason` |
| --- | --- | --- |
| `ok` | values read, no read failures (an attribute a device does not have is not a failure) | `null` |
| `partial` | some values read, some failed | `some_unreadable` |
| `error` | read failures and no value at all | the most serious failure |
| `unavailable` | no value, and nothing failed: not there, not supported, no data now | the most specific absence |

Read failures, most serious first: `internal_error` (an unexpected exception in that group's code),
`permission_denied` (EACCES/EPERM), `io_error` (any other read or listing error), `disappeared` (listed in
this collection, gone when read), `invalid_data` (empty, not text, or not a number). A directory that
cannot be listed (a sensor class or one chip) is a failure, not "nothing found"; the other chips are kept. Absences, most specific first:
`no_data` (there, but no current value, e.g. ENODATA from an inactive thermal zone, an EIO fan speed on a
board that declares a fan but reports no tachometer, or no CPU rows to read
clocks for during warmup), `not_exposed` (the device does not provide that attribute), `not_detected`
(nothing of the kind found), `unsupported_platform` (no provider on this board, e.g. the power mode off
Jetson).
A group with nothing to try is `unavailable`, never `ok`.

`issues` lists the failures only, at most 32 per group (`issues_truncated` counts the rest), as a target
(a channel such as `hwmon2.temp1`, a directory such as `hwmon0`, or a field such as `cur_freq`, up to 64
characters; not a stable sensor id) and a reason. Paths, file contents and exception text are not in the API; the service log has them,
written when a group's problems change rather than on every collection. Where one value has several
sources (the Jetson GPU load), the first that reads is used and the others' failures are not reported;
a devfreq directory that cannot be listed is still reported, since it also hides the GPU clocks. A name or
label that cannot be read (a chip's or thermal zone's name, a channel label) is reported and the default
name is used instead (the chip's or zone's directory name, e.g. `hwmon2`, or the channel name), so its
values are kept; ina3221 rails, which need their label, are left out instead.

A snapshot with a `partial` or `error` group is still current: `/api/stats` and `/text` answer 200,
`/api/status` says `degraded` with `ready: true`, and `last_attempt` is `ok` with 0 consecutive failures,
because the collection itself succeeded. `unavailable` groups alone do not make it `degraded`.

Limits: this handles reads that fail or return bad data. platmon does not cancel a read that blocks.
Only when the collection returns is its duration checked: if it took longer than `stale_after`, it is
dropped as `collection_too_slow`. While a read stays blocked, the collection is still running
(`collecting_for_ms` grows, `last_attempt` is unchanged) and the last snapshot just becomes stale.
`/api/status` keeps answering in that state, which does not mean the collection is making progress.
Stable sensor ids and per-sensor reading times are not part of this.

When there is no current snapshot (none yet, or collection keeps failing), the answer is 503:

```json
{
  "error": "no current data (not collected yet, or collection keeps failing)",
  "schema_version": 1,
  "code": "no_current_data",
  "instance_id": "8dfad9fa-9bf0-4d61-91e0-ef22aebd2b91",
  "sample": {"sequence": 42, "age_ms": 6000.0, "cycle_age_ms": 6250.0, "data_age_ms": 6249.8,
             "data_age_basis": "oldest_current_read_start"}
}
```

`sample` describes the last good snapshot (null if there was none); its values and `sensor_meta` are not
included.
`/text` answers 503 with the `error` text only.

## `/api/status`

What the collector is doing now. Always HTTP 200: check `ready`, not the status code (the Docker health
check uses `/api/stats` for that reason).

```json
{
  "schema_version": 1,
  "instance_id": "8dfad9fa-9bf0-4d61-91e0-ef22aebd2b91",
  "state": "degraded",
  "ready": true,
  "sample": {"sequence": 42, "age_ms": 1100.0, "cycle_age_ms": 1350.0, "data_age_ms": 1349.8,
             "data_age_basis": "oldest_current_read_start"},
  "last_attempt": {
    "state": "error",
    "reason": "collection_failed",
    "completed_at": 1791345601.3,
    "duration_ms": 50.0,
    "consecutive_failures": 1
  },
  "collecting_for_ms": null,
  "clock": {"source": "boottime", "suspend_aware": true},
  "levels": {"cpu": [80.0, 90.0], "gpu": [80.0, 90.0], "memory": [80.0, 90.0], "filesystem": [80.0, 90.0],
             "temperature": [85.0, 95.0]}
}
```

- `state`, `ready`:
  - `ready` (ready: true): the snapshot is current and the last collection succeeded.
  - `degraded` (ready: true): the snapshot is still current, but the last collection failed, or an
    optional group of that snapshot is `partial` or `error` (see Optional metrics in `/api/stats`).
  - `starting` (ready: false): no snapshot and no failure yet, and the first collection has not run
    longer than `stale_after`.
  - `stale` (ready: false): anything else: the last snapshot is too old, or there never was one and the
    first collection failed or runs too long.
- `sample`: the last good snapshot, also when stale; null if there never was one.
- `last_attempt`: the latest finished collection, null before the first one finishes. `reason` is one of
  `collection_failed` (collect raised an error; details are in the service log, not in the API) and
  `collection_too_slow` (it took longer than `stale_after`). `consecutive_failures` resets on success.
- `collecting_for_ms`: how long the running collection has taken so far, null between collections. A
  growing value does not mean the reads are making progress.
- `levels`: the warning and critical levels from `[thresholds]` in `platmon.ini`, as `[warning, critical]`
  in percent, and in °C for `temperature`. When `[thresholds]` leaves `temperature` empty, it is this
  board's default (Jetson Orin 85/95, Raspberry Pi 75/80, others 80/90). The web page and the `platmon`
  command color readings with them. Older servers send no `levels`, or a null `temperature`: the `platmon`
  command then uses those defaults.

## Clock

Durations and ages use `CLOCK_BOOTTIME` where available (`"source": "boottime"`), which keeps counting
while the device is suspended, so a snapshot from before a suspend shows its real age. Elsewhere platmon
falls back to the monotonic clock (`"source": "monotonic", "suspend_aware": false`), which may not count
suspended time, so ages can read too low right after a resume. Changes to the wall clock never affect them.

## Web page

The page shows `sample #N · data age S` (in the status tooltip): the server's `data_age_ms` plus browser time since that answer
arrived (transfer time is not included). Polling the same sample again does not reset it. On a failed
request it keeps the last good values and says they are not current. For an older server without
metadata it shows the time since its last answer instead, and says that it is not the sample's age.

CPUs listed in `cpu_sampling.unavailable` get a short note under the CPU rows ("CPU sampling: warming
up"), on the page and in the `platmon` command alike; they are not drawn as 0 %.

Optional groups that are `partial` or `error` are named in one line under the header ("Collection:
temperature partial, gpu error"), on the page and in the `platmon` command; `unavailable` groups (no GPU,
no fans) are not. The details are in `collectors` of `/api/stats`.

Network interfaces and disks (`network`, `disk_io`) are shown with the rates the server computed, never
recomputed from the counters, on the page ("Network", "Disk I/O") and in the `platmon` command and `/text`
(`NET`, `IO` lines):

- Network: per interface `rx` / `tx` in bytes per second (binary units: B/s, KiB/s, MiB/s, GiB/s;
  never bits), packets per second in the command, and the cumulative `errors` / `dropped` counters only
  where they are not 0. A note says the interfaces are those of platmon's own network namespace.
- Disk I/O: per disk read / write bytes per second, completed reads and writes per second, "I/O time"
  (`io_time_ratio` as a percentage: the share of the window with I/O in flight, not how busy or saturated
  the disk is) and requests in flight when there are any.
- A row without rates shows why ("warming up", "restarting after a gap", "counter went backwards", ...;
  an unknown reason code as it is), never 0. Names are shown as text, never as HTML.
- The `platmon` command lists at most 12 interfaces and 12 disks and then says how many more there are
  ("+4 more interfaces (all in /api/stats)"); the page lists all.
- Nothing is shown for a server without these fields (older, or the feature off) or with an empty list;
  a failed reading appears in the "Collection:" line like any optional group.

Storage observations can report the additive issue reason `mount_changed` when the directory opened
for capacity no longer belongs to mountinfo's device number. The filesystem entry remains present
with null capacity, and the group is partial/error under the existing C1 rules.
