# Recent history (`/api/history`)

With `[history] enabled = yes`, the service keeps the last `retention` seconds (600 by default, 60 to
3600) of selected numbers from its published snapshots, **in memory only** (no file, no database; a
restart starts empty), and serves them at `/api/history` for trend graphs. **On by default**;
`[history] enabled = no` disables recording and the endpoint.

```json
{
  "schema_version": 1,
  "instance_id": "8dfad9fa-9bf0-4d61-91e0-ef22aebd2b91",
  "retention_s": 600.0,
  "interval_ms": 1000.0,
  "dropped_series": 0,
  "series": {
    "memory/used_bytes": [[41, 2000.0, 2147483648], [42, 1000.0, 2147483648]],
    "network/netns1:3f0c…/2/eth0/rx_bytes_per_s": [[41, 2000.0, null], [42, 1000.0, 1024.0]]
  }
}
```

## What is kept

One append per **published** core snapshot or independent slow observation, installed before either
publisher starts. Failed or dropped core collections add no measured point; an elapsed publication gap
inserts a null break at the next successful publication. A core recovery also inserts a null after even
one failed attempt, using internal publication metadata rather than inferring failure from time alone.
The next uninterrupted success adds no extra break. HTTP requests never add points. Duplicate or
out-of-order publication IDs are ignored within each of the four domains (core/storage/wifi/probe). A point is
`[publication_id, age_ms, value]`: the core sequence, or the independent observation ID for an
`observation/` series, and how long ago that
collection started (elapsed clock, as of the answer; wall-clock steps do not change it).

| series id | value | identity |
| --- | --- | --- |
| `cpu/<id>/usage` | CPU usage % | the CPU id |
| `gpu/usage` | GPU usage % | - (one GPU is reported, [api.md](api.md#gpu)) |
| `memory/used_bytes` | used memory | - |
| `temperature/<source id>` (or `temperature/name:<key>` without an id) | °C | `sensor_meta` source id ([api.md](api.md#sensor-sources-and-read-times)) |
| `network/<namespace id>/<ifindex>/<name>/rx_bytes_per_s`, `…/tx_bytes_per_s` | bytes/s | namespace, index and name together ([network.md](network.md#scope-and-identity)); interfaces without a known index are not kept |
| `disk_io/<major>:<minor>/<name>/read_bytes_per_s`, `…/write_bytes_per_s` | bytes/s | device number and name ([disk-io.md](disk-io.md)) |

A replaced device, another namespace or index, or a new source id starts a **new** series instead of
continuing an old one. Rates are the server's own, never recomputed from the counters.

Gaps: a value without a rate (`warmup`, `gap`, `counter_regressed`, a CPU without usage) is `null`, never 0
and never interpolated. A previously seen series missing from its own publisher gets an explicit null. Missing markers do not
extend a disappeared identity's lifetime. A publisher gap greater than `max(3 × interval, 5 s)` inserts
a null immediately before the next point, with that publication's ID (a gap marker, not another measured
sample). Downsampling keeps a null when any input in the bucket is missing, and preserves the newest
point exactly. This can omit some valid detail, but never hides an outage by joining a line through it.

Low-frequency series use **their own** observation IDs and start times:

| series | value / identity |
| --- | --- |
| `observation/storage/<major>:<minor>/<token>/used_bytes` and `available_bytes` | bytes; token hashes device number/name, filesystem type and source (not a universal filesystem UUID) |
| `observation/wifi/<namespace>/<ifindex>/<name>/signal_dbm` | dBm when reported and connected; unknown namespace/index omitted, disconnect is null |
| `observation/probe/<address>:<port>/connect_ms` | TCP connect ms; timeout/failure is null; IPv6 address bracketed |

A completed observation already older than three group intervals contributes null, never a current
value. Partial groups retain their valid numbers; missing/error values are gaps. `series_intervals_ms`
is an additive endpoint field mapping series IDs to their publisher cadence. Existing core IDs and
point units stay unchanged; older consumers can ignore new prefixes and this metadata.

## Bounds

- At most `min(3602, retention / min(core interval, 1 s) + 2)` points per series and 64 series; series beyond that are not kept and
  counted in `dropped_series`, once per series ID, not once per publication (at most 256 IDs are
  remembered, after which the count stops growing). IDs are limited to 512 characters; only finite
  numeric values are retained.
  Each publisher removes points older than its current retention cutoff when it appends. Low-frequency
  series therefore release expired points without waiting to fill the core-cadence point cap; HTTP reads
  still only copy/filter and never mutate the stored history.
  At very fast cadences, the point cap can shorten the effective retained window; retention is an upper limit. A series not seen for longer than `retention` is removed, so devices that come
  and go do not pile up.
- Which series keep a slot when a host has more identities than the limit (a 22-core PC with 31
  temperature channels needs 69): `memory/`, then `cpu/`, `gpu/`, `network/`, `disk_io/`, `observation/`, and
  `temperature/` last, because boards expose dozens of nearly identical temperature channels while every
  core, interface and disk is a reading of its own. A series that is already kept wins a tie against a
  new one of the same rank, so sensors never take turns dropping each other. Each publisher's slots are
  its own: the core publisher drops its least important series instead of waiting for one to expire,
  and the low-frequency groups keep the slots they already hold.
- `/api/history?prefix=…&seconds=…&points=…` (all optional): series whose id starts with `prefix` (at most
  128 characters), from the last `seconds` (up to `retention`), at most `points` per series (1 to 600, default
  300; gap-preserving buckets, the newest kept). Anything else is 400. 404 when history is off.
- Reading copies point references under the history lock, then filters, downsamples and serializes outside it; appending never waits for a
  reader's serialization.

## Web page

With history on, the Resources, Network, Storage and Temperature & power tabs show a "Recent history" panel with that topic's CPU, memory,
GPU, temperature, network, disk or low-frequency series: one graph per series, bounded by the endpoint's own
series limit. A row of buttons above the graphs lists the kinds of series that view has with how many each
covers ("CPU cores 22", "RAM 1"); a button turns its kind off and on, and a view with only one kind has no
buttons. Each kind that is on gets its own labelled section, in the order of the button row, so a single
RAM graph is never mixed into a wall of cores. Turning every kind off leaves the buttons in place with a
notice instead of graphs. The choice lasts while the page is open. Individual series are not selected one
at a time. Each graph uses its own
scale and shows the latest value next to it, "no value" when the latest point is a gap; a gap breaks the
line. Horizontal position uses actual elapsed age, not equally spaced indexes. Each row says the latest
point age, advanced with browser elapsed time between responses; these are historical values, not a fresh live reading.
Only the age text changes each second; SVG graphs change on a response or view/series selection.
Detail tabs share one `/api/history?points=120` cache, polled at most every 10 s while the page is visible;
switching tabs does not reset that deadline. Overview and System do not request history. A 404 stops polling. The
`platmon` command and `/text` do not show history.

## Not included

Persistence across restarts, zooming, export, alerts.

Memory validation target: at most 32 MiB retained Python allocations, 64 MiB including one bounded
read/JSON encoding at 64 series × 3602 points. One synthetic `tracemalloc` run checks the maximum point
configuration; a separate 600 s board run is required for real-time retention/worker/RSS validation.
The older 3.7 MiB report is historical and is not a measurement of this implementation.

Synthetic maximum-capacity result on Python 3.12 (64 series × 3602 points, then a 600-point read):
22,505,990 bytes retained and 28,765,190 bytes peak including JSON, within the 32/64 MiB targets.
This is Python allocation accounting with virtual observation time, not daemon RSS or ARM cost.
