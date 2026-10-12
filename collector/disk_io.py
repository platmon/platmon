"""Passive disk I/O counters: per physical disk totals from /proc/diskstats and rates over the elapsed time
between two consecutive readings. Read once per collection by the service's sampler thread; nothing is
written, probed or timed with a test load. Contract: docs/reference/disk-io.md.
"""
import os
import time

from .counters import counter, read_file
from .sysfs import reason_of

PROVIDER = "proc_diskstats"
SCOPE = "host_block_devices"  # block devices are not namespaced: a container sees the host's disks
DISKSTATS = "/proc/diskstats"
SYS_BLOCK = "/sys/block"
SECTOR = 512  # /proc/diskstats counts sectors of 512 bytes whatever the device's own sector size
# Fields after major, minor, name (Documentation/block/stat.rst, admin-guide/iostats.rst): 11 on older
# kernels, then discard and flush fields. Only the first 11 are used; more are accepted.
MIN_FIELDS = 11
IO = ("ios", "merges", "bytes", "time_ms")


class InvalidFile(ValueError):
    """/proc/diskstats as a whole is not the expected format."""


def parse(text, wanted=lambda name: True, identify=None):
    """({(major, minor, name): (r ios, merges, sectors, ms, w ios, merges, sectors, ms, in_flight, io_ms)},
    [(target, why)]). identify, when supplied, selects by (major, minor, name).
    Only rows whose name wanted() accepts are converted and checked: the others (virtual
    devices, partitions) are not reported on, as they are not collected. A row that does not parse is left
    out and reported (target: its name if that is a plain device name, else "line<N>"); a name on several
    rows is ambiguous, so none of them is used. Raises InvalidFile for text without any row: every Linux host
    lists at least one block device."""
    rows, bad, seen = {}, [], {}
    for n, line in enumerate(text.split("\n"), 1):
        fields = line.split()
        if not fields:
            continue
        name = fields[2] if len(fields) > 2 else ""
        if not valid_name(name):
            bad.append((f"line{n}", "invalid_data"))
            continue
        if name in seen:
            rows.pop(seen[name], None)
            bad.append((name, "invalid_data"))
            continue
        seen[name] = None
        if not wanted(name):
            continue
        try:
            major, minor = counter(fields[0]), counter(fields[1])
            if identify is not None and not identify((major, minor, name)):
                continue
            if len(fields) < 3 + MIN_FIELDS:
                raise ValueError(line)
            values = [counter(f) for f in fields[3:3 + MIN_FIELDS]]
            for f in fields[3 + MIN_FIELDS:]:
                counter(f)  # later fields are not used, but must be counters too
        except ValueError:
            bad.append((name, "invalid_data"))
            continue
        seen[name] = (major, minor, name)
        rows[seen[name]] = (*values[0:8], values[8], values[9])
    if not seen and not bad:
        raise InvalidFile("no rows")
    return rows, bad


def valid_name(name):
    """A kernel block device name: printable, no / or whitespace, at most 31 characters (DISK_NAME_LEN)."""
    return 0 < len(name) < 32 and name.isprintable() and "/" not in name and not any(c.isspace() for c in name)


def is_physical(name, sys_block=SYS_BLOCK):
    """True for a whole disk on a real device, False for a virtual one (loop, ram, zram, device-mapper, md:
    under devices/virtual) and None for a name that is not a whole disk (a partition). OSError otherwise."""
    try:
        target = os.readlink(f"{sys_block}/{name}")
    except FileNotFoundError:
        return None
    return "/devices/virtual/" not in f"/{target}"


class DiskCounters:
    """Kept by the service between collections, like NetworkCounters: each sample() reads /proc/diskstats
    once and compares every physical disk with its previous successful reading of the same major:minor and
    name. Confirmed classification is reused only while that identity stays listed;
    absent sysfs links and failed lookups are retried."""

    def __init__(self, clock=time.monotonic_ns, max_gap=None, path=DISKSTATS,
                 read=read_file, physical=is_physical):
        self.clock, self._path, self._read, self._physical = clock, path, read, physical
        self._max_gap = None if max_gap is None else round(max_gap * 1e9)
        self._last = None  # (elapsed ns the reading began, {(major, minor, name): counters})
        self._kind = {}    # (major, minor, name) -> confirmed bool; missing sysfs entries are retried

    def sample(self, group):
        """(disk_io output, read span or None). Problems are noted in group; a broken file gives no disks and
        clears every baseline; an unexpected exception clears them too and is raised, for the caller's guard."""
        try:
            return self._sample(group)
        except Exception:
            self._last = None
            self._kind = {}
            raise

    def _sample(self, group):
        out = {"scope": {"kind": SCOPE}, "provider": PROVIDER, "disks": [], "read": None}
        start = self.clock()
        try:
            text = self._read(self._path).decode()
            span = (start, self.clock())
            kind = {}
            rows, bad = parse(text, identify=lambda key: self._is_physical(key, kind, group))
        except FileNotFoundError:
            self._last = None
            self._kind = {}
            group.note("diskstats", "not_exposed")
            return out, None
        except OSError as e:
            self._last = None
            self._kind = {}
            group.note("diskstats", reason_of(e), f"{self._path}: {e.strerror}")
            return out, None
        except (UnicodeDecodeError, InvalidFile):
            self._last = None
            self._kind = {}
            group.note("diskstats", "invalid_data", self._path)
            return out, None
        for target, why in bad:
            group.note(target, why, self._path)

        self._kind = kind  # only confirmed identities listed now; no unbounded cache

        last, now = self._last, start
        window = None if last is None else now - last[0]
        kept = {}
        for key in sorted(rows, key=lambda k: k[2]):
            if not kind.get(key):  # virtual, a partition, or not known
                continue
            row, rates = rows[key], None
            before = None if last is None else last[1].get(key)
            if before is None:
                reason = "warmup"
            elif window <= 0:
                reason = "invalid_interval"
            elif self._max_gap is not None and window > self._max_gap:
                reason = "gap"
            elif any(a < b for i, (a, b) in enumerate(zip(row, before)) if i != 8):  # 8: in flight, a gauge
                reason = "counter_regressed"  # not taken for a wrap or a reset to 0: no rate, a new baseline
            else:
                reason = None
                d = [a - b for a, b in zip(row, before)]
                rates = {"read_bytes_per_s": _rate(d[2] * SECTOR, window), "write_bytes_per_s": _rate(d[6] * SECTOR, window),
                         "reads_per_s": _rate(d[0], window), "writes_per_s": _rate(d[4], window),
                         "io_time_ratio": round(d[9] * 1e6 / window, 4)}  # ms with I/O in flight per ms
            kept[key] = row
            out["disks"].append(group.got({
                "name": key[2], "major": key[0], "minor": key[1],
                "read": dict(zip(IO, (row[0], row[1], row[2] * SECTOR, row[3]))),
                "write": dict(zip(IO, (row[4], row[5], row[6] * SECTOR, row[7]))),
                "in_flight": row[8], "io_time_ms": row[9],
                "rates": rates, "window_ms": round(window / 1e6, 3) if reason in (None, "gap", "counter_regressed") else None,
                "reason": reason}))
        self._last = (now, kept)
        if not out["disks"] and not group.reasons:
            group.note("disks", "not_detected")
        return out, span


    def _is_physical(self, key, kind, group):
        """Reuse only a confirmed classification of the same device number and name."""
        if key in self._kind:
            kind[key] = self._kind[key]
        else:
            name = key[2]
            try:
                result = self._physical(name)
            except OSError as e:
                group.note(name, reason_of(e), f"{SYS_BLOCK}/{name}: {e.strerror}")
                return False
            if result is None:
                # No /sys/block link can mean a partition OR a temporarily absent/restricted entry.
                # Do not turn that ambiguity into a permanent negative cache.
                return False
            kind[key] = result
        return bool(kind[key])


def _rate(delta, window_ns):
    return round(delta * 1e9 / window_ns, 3)


def unavailable():
    """The output when sample() raised."""
    return {"scope": {"kind": SCOPE}, "provider": PROVIDER, "disks": [], "read": None}
