# Storage capacity

> **TL;DR**
> - The `storage` observation: partitions of the physical disks and the size, use and free space of local
>   disk filesystems, every 30 s by default; on by default.
> - Only `/dev/` mounts of local disk filesystems are read; network, FUSE, tmpfs, overlay, squashfs and
>   pseudo filesystems never are. `disk` in `/api/stats` and `disk_io` are separate.
> - On Windows: volumes by GUID and each drive's partitions.

A low-frequency group ([observations.md](observations.md)): the partitions of the physical disks and the
size, use and free space of the local disk filesystems, every `[storage] interval` seconds (30 by
default). **On by default** (`[storage] enabled = no` turns it off). It is separate from:

- `disk` in `/api/stats`, the root filesystem's size and use, which keeps its keys and its 1 s cadence;
- `disk_io` ([disk-io.md](disk-io.md)), how busy the disks are.

```json
"storage": {
  "data": {
    "partitions": [
      {"name": "nvme0n1p1", "disk": "nvme0n1", "major": 259, "minor": 1, "size_bytes": 998643240960,
       "mount_points": ["/"]},
      {"name": "nvme0n1p2", "disk": "nvme0n1", "major": 259, "minor": 2, "size_bytes": 134217728,
       "mount_points": []}
    ],
    "filesystems": [
      {"device": "nvme0n1p1", "major": 259, "minor": 1, "fstype": "ext4", "source": "/dev/nvme0n1p1",
       "mount_points": [{"path": "/", "read_only": false}],
       "total_bytes": 982806532096, "used_bytes": 113806532096, "available_bytes": 819000000000}
    ]
  }
}
```

(The numbers show the format; they are not a measurement.)

## What is read

One read each of `/proc/partitions` and `/proc/self/mountinfo`, one `os.fstatvfs` per filesystem, and for
each partition one check of `/sys/class/block/<name>` (is it a partition, of which disk). Never a block
device itself: an unmounted partition's filesystem is not identified, nothing is mounted, and no
partition table is read or changed.

## Fields

- `partitions`: partitions of physical disks (the disks [disk-io.md](disk-io.md) selects): `name`, `disk`,
  `major`, `minor`, `size_bytes` (from `/proc/partitions`), and the `mount_points` of a listed local
  filesystem on it in this mount namespace (empty: not mounted here; no capacity is given for it). Partitions of virtual
  devices (loop, zram, device-mapper, md) are left out.
- `filesystems`: one entry per mounted filesystem, however many mount points show it (bind mounts are
  listed in `mount_points`, never counted twice):
  - `device`: the kernel name of its device number (`major`:`minor` from mountinfo), e.g. `nvme0n1p1` or
    `dm-0`; `null` when the number has no block device name (btrfs and some others use an anonymous
    number). `source`: the mount source as mountinfo gives it.
  - `fstype`, `mount_points` (`path`, and `read_only`: whether the filesystem itself is mounted read
    only, from the mountinfo super-options, so every mount point of one filesystem says the same).
    A read-only mount in platmon's own namespace is not reported: a service hardened with
    `ProtectSystem=strict` (the shipped unit) or a read-only bind mount sees the host's writable
    filesystems as read-only mounts, which says something about platmon's process, not about the
    filesystem it reports on.
  - `total_bytes`, `used_bytes`, `available_bytes` from `statvfs`, as `df` computes them: total =
    blocks × fragment size, used = total − free, available = what unprivileged users can still write
    (`f_bavail`; less than total − used when blocks are reserved for root). `null` when `statvfs` failed
    (reported in `collector`), never 0; a measured 0 is 0.
- `disk`, `device` and the link between them follow the device numbers; when a filesystem sits on a
  logical volume (`dm-0`), its link to the physical partition is not resolved.

## Which filesystems

Only mounts whose source is a `/dev/` device and whose type is a local disk filesystem: `ext2`, `ext3`,
`ext4`, `xfs`, `btrfs`, `f2fs`, `vfat`, `exfat`, `ntfs3`, `jfs`, `reiserfs`. Never `statvfs`'ed:

- network and FUSE filesystems (NFS, CIFS, sshfs, ...) and autofs: a call could wait for a remote server
  or trigger an automount;
- `tmpfs`, `overlay` (a container's root), `squashfs` images (snap packages on loop devices), and pseudo
  filesystems (`proc`, `sysfs`, `cgroup`, ...): they are memory, layers or read-only images, not disk
  space.

The mount namespace is platmon's own (which is why `read_only` comes from the filesystem, not the
mount): in a container (as in `deploy/docker/compose.yaml`) that is the container's
mounts (its overlay root is left out; the host's root bind-mounted at `/host/root` is listed with that
path), while `/proc/partitions` lists the host's partitions. Other host filesystems need their own
explicit directory bind mounts; the root bind is deliberately non-recursive. Mount points can contain names chosen by
users (`/media/<user>/...`); like the rest of the API, run platmon on trusted networks only.

## Diagnostics

`collector` ([observations.md](observations.md)) uses the optional-group rules: an unreadable
`/proc/partitions` or `/proc/self/mountinfo` (target `partitions` / `mountinfo`) leaves out what it would
have given and keeps the rest; a mountinfo line that does not parse is left out (`line<N>`, `invalid_data`);
only lines of the listed filesystem types are converted and checked (others, such as a broken tmpfs line,
are never reported); a `statvfs` that fails is reported under the filesystem's device (`disappeared` when it was unmounted in
between) and that filesystem stays listed without numbers. Nothing found at all is `unavailable`
(`not_detected`).

## Configuration

```ini
[storage]
enabled = yes     ; no: do not observe it
interval = 30     ; seconds between observations, 5 to 3600
```

30 s is a starting point: capacity changes slowly and the cost per observation is small (see the
performance report); it was not derived as an optimum. Shown on the web page ("Storage") and in the
`platmon` command and `/text` (`STORAGE` lines: filesystems with used / total and available, partitions
counted per disk), with the observation's age. A filesystem is shown by its first mount point and how many
more there are ("/ +60 more": bind mounts can be dozens, e.g. under Docker Desktop on WSL); the full list is
in the API.

## Windows

The Windows build of the native server (not released; native/README.md, Windows) reads the same two lists
from Windows's own calls, with no access rights on any handle (nothing is read from or written to a disk):

- `filesystems`: each volume Windows lists (`FindFirstVolumeW`) on a fixed or removable disk with at least one
  mount point (a drive letter or a folder, `GetVolumePathNamesForVolumeNameW`); optical, network and RAM
  disks are left out. `device` is the volume GUID name (`Volume{…}`), the volume's identity, which a drive
  letter is not; `source` its path (`\\?\Volume{…}\`); `major` and `minor` are `null`. `fstype` and
  `read_only` come from `GetVolumeInformationW` (the volume's own read-only flag), the capacity from
  `GetDiskFreeSpaceExW`: total, used = total − free, available = what platmon's account may still write (a
  quota can make it less). A volume that cannot answer (an empty card reader is `no_data`, a locked BitLocker
  volume `permission_denied` or `io_error`) stays listed without `fstype` or numbers.
- `partitions`: each partition of each `PhysicalDriveN` (`IOCTL_DISK_GET_DRIVE_LAYOUT_EX`), named
  `Harddisk<N>Partition<M>` as Windows's own device names are, `disk` the drive, `major` and `minor` `null`,
  `size_bytes` the partition's length; `mount_points` those of the volume whose disk extent starts there
  (`IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS`). Unused MBR slots are left out; a partition with no volume, or
  whose volume is not mounted, has none.
- History keeps a volume as `observation/storage/Volume{…}/used_bytes` and `…/available_bytes`
  ([history.md](history.md)).

Measured on a laptop (one NVMe drive with six partitions, a USB card, Windows 11): one observation 2.3 ms,
without administrator rights.

## Not included

Unmounted filesystems' contents or types, partition tables, LVM / md topology, inode counts, quotas,
per-directory usage, network filesystems. Numeric capacity history is available when history is enabled.

## Mount replacement during observation

Capacity is read through one read-only directory descriptor. The descriptor's `st_dev` is checked
against mountinfo's major/minor before `fstatvfs`; a mismatch is `mount_changed` and all capacity
numbers stay null. The descriptor is closed on success and failure. A mount renamed or unmounted
after opening cannot redirect that descriptor to another filesystem. The final path component must
be a directory, not a symlink. Opening requires directory read permission: a mount visible in mountinfo
but not readable by the service is reported unavailable with `permission_denied` instead of guessing.

This pins the filesystem being measured, not a mount-table transaction. A same-device remount or
replacement reusing the same device number cannot always be distinguished; mount options and mount
list remain the earlier mountinfo observation. Blocking remains isolated to the existing single storage
worker. No new thread or raw device access is introduced. The [resumed integration report](../performance/archive/resumed-integration-results.md) measures this descriptor
path; its call p95 still exceeds 2 ms. It is now enabled by default by user policy;
this does not change the measured result or the 2 ms target.

Directory bind mounts are supported under the same identity check. If a file bind appears first,
the reader skips `ENOTDIR` and tries the next mount of that same filesystem until a directory succeeds.
Only one successful capacity read is made per filesystem. File-only groups still report unknown
capacity; permission, I/O and device-identity failures stay explicit rather than being retried away.
Overlay and pseudo filesystems remain excluded. Core snapshots stay fresh even when these optional
capacity observations fail. This is a documented provider limit, not successful file-mount capacity
validation or evidence that container mounts describe the whole host.
