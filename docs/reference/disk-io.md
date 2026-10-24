# Disk I/O counters

`/api/stats` has a `disk_io` object while `[disk_io] enabled = yes` (the default). It holds the read and
write counters of each selected **whole disk** visible to the process, read from `/proc/diskstats` once per collection by
the collector thread, and rates over the time between two such readings. Nothing is written, probed or
timed with a test load. It is separate from `disk` (the root filesystem's size and usage), which keeps
its keys. General API rules (freshness, `collectors`, 503): [api.md](api.md).

```json
"disk_io": {
  "scope": {"kind": "host_block_devices"},
  "provider": "proc_diskstats",
  "disks": [
    {
      "name": "nvme0n1",
      "major": 259,
      "minor": 0,
      "read": {"ios": 1000, "merges": 10, "bytes": 40960000, "time_ms": 500},
      "write": {"ios": 2000, "merges": 20, "bytes": 81920000, "time_ms": 900},
      "in_flight": 0,
      "io_time_ms": 1200,
      "rates": {"read_bytes_per_s": 4096.0, "write_bytes_per_s": 8192.0, "reads_per_s": 1.0, "writes_per_s": 2.0,
                "io_time_ratio": 0.01},
      "window_ms": 1000.0,
      "reason": null
    }
  ],
  "read": {"started_offset_ms": 12.0, "completed_offset_ms": 12.1}
}
```

(The numbers show the format; they are not a measurement.)

## Which disks

Included: whole disks whose `/sys/block/<name>` link resolves outside `devices/virtual/`, found by this
rule for each newly observed device identity, never by a list of names. On the boards platmon is checked on that is the
Jetson Orin Nano's `nvme0n1` and the Raspberry Pi 4's `mmcblk0`. This is a selection rule for the supported
environments, not a general definition of "physical": sysfs placement is the kernel's, and an unusual
driver can be placed otherwise.

Left out by default:

- Partitions: the unit is the whole disk. The kernel counts partitions too, but they are not added to
  their disk; a partition view could be collected separately later.
- Virtual block devices (under `devices/virtual/`): loop, ram, zram (compressed memory, swap),
  device-mapper (LVM, LUKS) and md (software RAID). Their I/O is another layer: it reaches a physical
  disk below (shown) or memory (zram, not a disk). Logical volumes and zram may become options later.

A confirmed whole-disk or virtual-device classification is reused only while the same name **and
major/minor** remain listed across successful diskstats reads. A changed number is looked up again.
Missing `/sys/block` entries are ambiguous (partition, temporary disappearance or restricted sysfs), so
absence is retried, never cached permanently. Other lookup errors are reported and retried. A failed
whole-file read clears the classification cache and rate baselines. A vanished identity is forgotten.
Replacement with the same name and number between consecutive reads cannot always be detected; a
counter regression still resets rates. This is not a persistent hardware identity.

## Fields

- `name`, `major`, `minor`: the kernel's device name and number. `disks` is sorted by name.
- `read`, `write`: the kernel's cumulative counters since the device appeared: completed `ios`, `merges`
  (adjacent requests merged), `bytes` (sectors × 512: `/proc/diskstats` counts 512-byte sectors whatever
  the device's own sector size, as Documentation/block/stat.rst defines) and `time_ms` spent on those
  requests (milliseconds, measured with nanosecond precision and truncated since 4.19). `in_flight`: requests in progress
  now (a gauge, not a counter). `io_time_ms`: time with at least one request in flight.
- `rates`, over `window_ms`: `read_bytes_per_s`, `write_bytes_per_s` (bytes, not bits),
  `reads_per_s`, `writes_per_s` (completed requests), and `io_time_ratio`: the `io_time_ms` difference
  divided by the window, the share of the window the kernel counted as "doing I/O". It is not utilisation,
  saturation or remaining capacity: a device that serves many requests in parallel (NVMe) can be at 1.0
  with capacity to spare. The kernel documents the counter's limits: since Linux 5.0 it counts jiffies in
  which at least one request was started or completed, and with concurrent requests that run longer than
  two jiffies some I/O time may not be counted (Documentation/admin-guide/iostats.rst). The kernel's
  jiffies and platmon's elapsed clock differ, so the ratio can be slightly above 1; it is reported as
  computed, not clamped. Counters that did not move give `0.0`.
- `window_ms`, `reason`: as for network ([network.md](network.md#fields)). Reasons: `warmup` (no earlier
  reading of this major:minor and name), `invalid_interval`, `gap` (more than `max(3 × interval, 5 s)`),
  `counter_regressed` (one counter went down: the device was reset or replaced under the same number and
  name; not taken for a wrap). `in_flight` may go down and is not checked.

Each valid reading is the next baseline, also when it gives no rate; a collection dropped as
`collection_too_slow` still read the counters. Only the disks of the latest reading are kept.

## Scope

`scope.kind` is `host_block_devices`: `/proc/diskstats` is not namespaced, so platmon in a container
(as in `deploy/docker/compose.yaml`, which keeps the container's own `/sys`) lists the host's block devices like a
native one. What the selection sees depends on the environment and was checked only natively on the two
boards: in a container the `/sys/block` links must be visible (a restricted or missing `/sys` makes every
lookup fail, reported per disk); in a virtual machine the "physical" disks are the VM's virtual disks
(virtio, emulated SCSI), which sit outside `devices/virtual/` and are therefore collected; WSL 2 shows its
virtual SCSI disks the same way. A container deployment of this feature has not been benchmarked. On 32-bit kernels some counters are 32-bit and
wrap; a wrap shows as `counter_regressed` for one reading.

## Windows

The Windows build of the native server (not released; native/README.md, Windows) reads each
`\\.\PhysicalDriveN` that Windows lists (`QueryDosDeviceW`) with `IOCTL_DISK_PERFORMANCE` instead of
`/proc/diskstats`, with the same rates, reasons and baselines. Not every field has a Windows counter:

- `provider` is `ioctl_disk_performance`; `scope` stays `host_block_devices`. `name` is `PhysicalDrive0`,
  `PhysicalDrive1`, …; every disk Windows lists is kept (Windows has no `devices/virtual/` to tell virtual
  ones apart; Storage Spaces' member disks are not listed, their virtual disk is).
- `major` and `minor` are `null`: Windows has no device numbers. A disk is matched by its name, whose
  number Windows can give to another disk after a removal; a counter regression still resets rates. Such
  disks have no history series ([history.md](history.md)).
- `merges` is `null` (Windows counts split requests, not merged ones) and so is `io_time_ms` (no
  cumulative busy time). `bytes` are Windows's own byte counts; `time_ms` is its read and write time,
  truncated from 100 ns units; `in_flight` its queue depth.
- `io_time_ratio` is the window less the time the disk was idle (no request outstanding, Windows's
  cumulative idle time), divided by the window: the complement of Linux's "at least one request in
  flight". The idle time is the driver's clock and the window platmon's, so it can be slightly below 0
  or above 1; it is reported as computed.
- `ios` are 32-bit on Windows: at 100,000 requests a second they wrap in about 12 hours, which shows as
  `counter_regressed` for one reading, as on 32-bit Linux kernels.
- A disk that cannot be opened or queried is left out and reported under its name: `permission_denied`,
  `disappeared` (gone between the listing and the query), `not_exposed` (a disk without performance
  counters) or `io_error`. A listing that fails gives `disks: []` and state `error`; no disk is
  `unavailable` (`not_detected`). The query needs no access rights; this was checked only on CI's runner,
  which runs as an administrator.

## Diagnostics and freshness

`collectors.disk_io` follows the optional-group rules ([api.md](api.md#optional-metrics)), as network does:
a row that does not parse (fewer than 11 counters, a value that is not an unsigned 64-bit integer, an
invalid name, a later field that is not a counter) is left out and reported as `invalid_data`, and a name
on several rows leaves out all of them. Only rows of collected disks are converted and checked; rows of
left-out devices (virtual ones, partitions) are matched by name only and never reported. A file that cannot be read, is not text or has no rows gives
`disks: []` and state `error` (`unavailable` with `not_exposed` if the file is not there). A host without
a physical disk is `unavailable` with `not_detected`. A `/sys/block` lookup that fails is reported under
the disk's name; that disk is left out and looked up again next time. An unexpected exception is
`internal_error` and never fails the collection.

`disk_io.read` is the one read every disk comes from and counts for `data_age_ms`, exactly as
`network.read` (see [network.md](network.md#diagnostics-and-freshness)). Network and disk records are
checked apart.

## Configuration and direct calls

On by default (also without a config file or `[disk_io]` section). To turn it off:

```ini
[disk_io]
enabled = no
```

Then platmon makes no `DiskCounters`, reads neither `/proc/diskstats` nor `/sys/block`, and there is no
`disk_io` and no `collectors.disk_io`. `collector.collect()` and `collect_recorded()` take an optional
`disk_io` (a `collector.disk_io.DiskCounters`); without it they return what they did before.

## Not included

Shown on the web page, in the `platmon` command and `/text` (see [api.md](api.md#web-page)). Not collected: partitions, logical volumes, zram, discard and flush
counters, the weighted time field, per-process I/O, SMART data, filesystem usage beyond `disk`,
pressure stall information (PSI). Measured cost: [performance/disk-io-product.md](../performance/disk-io-product.md).

Official references checked during the resumed audit:
[block statistics](https://docs.kernel.org/block/stat.html) (512-byte sectors and in-flight gauge),
[I/O statistics](https://docs.kernel.org/admin-guide/iostats.html) (counter resets and Linux 5.0+ I/O time limits).


The CLI, `/text` and web page show the actual rate window in seconds when supplied. `in_flight` is
shown even when zero or while rates are warming up: it is a current gauge, not a cumulative counter.
