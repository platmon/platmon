"""Helpers for reading /proc and /sys, shared by the common and board-specific collectors."""
import contextlib
import errno
import hashlib
import json
import os
import re
import sys
import traceback

# Host paths a container cannot see at their usual place (Docker hides /sys/firmware and has its own /).
# Point them at bind mounts there; see compose.yaml.
DEVICE_TREE = os.environ.get("PLATMON_DEVICE_TREE", "/proc/device-tree")
HOST_ROOT = os.environ.get("PLATMON_HOST_ROOT", "/")  # the host's / : disk usage, /etc/os-release, ...


def host_path(path):
    """path on the host's root filesystem (unchanged outside a container)."""
    return os.path.join(HOST_ROOT, path.lstrip("/"))


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=None):
    v = read(path)
    return int(v) if v else default


def entries(path, *groups, listed=False):
    """Sorted names in directory path. A directory that is not there is empty; any other listing error is
    noted in groups (target: the directory's name) and gives [], so it is not taken for "nothing there".
    listed: path itself was found by listing in this collection, so its absence means it disappeared."""
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        if listed:
            for g in groups:
                g.note(os.path.basename(path), "disappeared")
        return []
    except OSError as e:
        for g in groups:
            g.note(os.path.basename(path), reason_of(e), f"{path}: {e.strerror}")
        return []


def driver_of(device, group, target="uevent"):
    """The driver bound to a device, from the DRIVER= line of its uevent (DRIVER=i915); None where there
    is none to read. device: the class entry (…/class/drm/card0), whose device/ holds the uevent."""
    text = group.read(f"{device}/device/uevent", target, parse=str, found=device)
    for line in (text or "").split("\n"):
        if line.startswith("DRIVER="):
            return line[len("DRIVER="):].strip()
    return None


def numbered(names, prefix, suffix):
    """Sorted channel numbers N of names like prefix{N}suffix (temp{N}_input) among a directory's names."""
    return sorted(int(m.group(1)) for n in names if (m := re.fullmatch(rf"{prefix}(\d+){suffix}", n)))


def hwmon_chips(root="/sys/class/hwmon", *groups):
    """(dir, chip name) for every hwmon device. A class directory that cannot be listed, or a name that cannot
    be read, is noted in groups; such a chip is named after its directory (hwmon2) and still read."""
    chips = []
    for n in entries(root, *groups):
        if re.fullmatch(r"hwmon\d+", n):
            d = f"{root}/{n}"
            name = Group("name")
            chips.append((d, name.read(f"{d}/name", f"{n}.name", parse=str, found=d) or n))
            for g in groups:
                g.merge(name)
    return chips


# Diagnostics of optional metrics (collectors.<group> in /api/stats). Reasons in PROBLEMS are read failures,
# most serious first; ABSENT ones say why there is no value without calling it an error, most specific first.
PROBLEMS = ("internal_error", "permission_denied", "io_error", "disappeared", "mount_changed", "invalid_data")
ABSENT = ("no_data", "not_exposed", "not_detected", "unsupported_platform")
OPTIONAL = ("cpu_frequency", "gpu", "npu", "temperature", "power", "fans", "power_mode", "board_info")
MAX_ISSUES, MAX_TARGET = 32, 64


def reason_of(e):
    """The reason code of an OSError from reading or listing a sysfs/procfs entry."""
    if e.errno in (errno.EACCES, errno.EPERM):
        return "permission_denied"
    return "no_data" if e.errno == errno.ENODATA else "io_error"  # ENODATA: e.g. an inactive thermal zone


class Group:
    """One optional metric group in one collection: how many values it got, and why others are missing.
    Only the reason codes and short targets (e.g. "hwmon2.temp1") reach the API; paths and exception text
    go to the service log."""

    def __init__(self, name, trace=None):
        self.name, self.values, self.reasons = name, 0, set()
        self.issues, self.truncated, self.details = [], 0, []
        self.trace = trace  # the service's per-collection Trace; None for a one-off collect()
        self.span = None    # (start, end) elapsed ns of the last successful read(), with a trace
        self.sensors = {}   # output slot -> sensor metadata (source and read span), with a trace

    def note(self, target, reason, detail=None):
        """Records why target has no value; returns None so a reader can return it."""
        self.reasons.add(reason)
        if reason in PROBLEMS:
            if len(self.issues) < MAX_ISSUES:
                self.issues.append({"target": target[:MAX_TARGET], "reason": reason})
                self.details.append(f"{target}: {reason}" + (f" ({detail})" if detail else ""))
            else:
                self.truncated += 1
        return None

    def merge(self, other):
        """Takes over what another Group noted (e.g. one shared read, or candidates that all failed)."""
        self.reasons |= other.reasons
        for issue, detail in zip(other.issues, other.details):
            if len(self.issues) < MAX_ISSUES:
                self.issues.append(issue)
                self.details.append(detail)
            else:
                self.truncated += 1
        self.truncated += other.truncated

    def got(self, value):
        """Counts a value the group reports."""
        self.values += 1
        return value

    def read(self, path, target, parse=int, found=None, io_absent=False):
        """parse(content of path), or None with the reason noted. found: what this collection discovered by
        listing (the file itself, or its device directory); if that is gone too the file disappeared,
        otherwise the attribute is just not exposed.
        io_absent: on this attribute an I/O error means the hardware has no value to give, like ENODATA
        from an inactive thermal zone, so it counts as no_data instead of io_error (see collector/common.py).
        With a trace, self.span is the read's (start, end) on success and None otherwise."""
        clock = self.trace.clock if self.trace else None
        self.span = None
        start = clock() if clock else None
        try:
            with open(path) as f:
                text = f.read().strip()
        except UnicodeDecodeError:  # bytes that are not text cannot be a number either
            return self.note(target, "invalid_data", path)
        except FileNotFoundError:
            gone = found is not None and not os.path.exists(found)
            return self.note(target, "disappeared" if gone else "not_exposed")
        except OSError as e:
            reason = reason_of(e)
            if io_absent and reason == "io_error":
                return self.note(target, "no_data")
            return self.note(target, reason, f"{path}: {e.strerror}")
        try:
            value = parse(text)
        except ValueError:  # int("") included: an empty number is not 0
            return self.note(target, "invalid_data", path)
        if clock:
            self.span = (start, clock())
        return value

    def sensor(self, slot, source, span):
        """Records, with a trace, where the value in output slot came from and when it was read."""
        if self.trace:
            self.sensors[slot] = {**source, "span": span}

    def status(self):
        problems = [r for r in PROBLEMS if r in self.reasons]
        if self.values:
            state, reason = ("partial", "some_unreadable") if problems else ("ok", None)
        elif problems:
            state, reason = "error", problems[0]
        else:  # nothing tried counts as nothing found, never as ok
            state, reason = "unavailable", next((r for r in ABSENT if r in self.reasons), "not_detected")
        return {"state": state, "reason": reason, "issues": list(self.issues), "issues_truncated": self.truncated}


@contextlib.contextmanager
def guard(*groups):
    """An unexpected exception inside one optional part becomes internal_error of its groups instead of
    failing the collection; the traceback goes to the log only."""
    try:
        yield
    except Exception:
        for g in groups:
            g.note(g.name, "internal_error", traceback.format_exc(limit=3).strip().replace("\n", " | "))


def groups(*names, trace=None):
    return {n: Group(n, trace) for n in names or OPTIONAL}


class Trace:
    """What the service records in one collection besides the values (sensor_meta, data age): the elapsed
    clock every read is timed with, and the (start, end) of the required reads. Made new for each
    collection, never reused."""

    def __init__(self, clock):
        self.clock, self.required = clock, {}
        self.network = None  # (start, end) of the network counters' read, when there is one
        self.disk_io = None  # the same for the disk counters
        self.pressure = None  # and for the pressure files (one span for the three)

    def timed(self, name, fn, *args):
        start = self.clock()
        value = fn(*args)
        self.required[name] = (start, self.clock())
        return value


# Sensor sources. The id is the SHA-256 of a canonical descriptor: provider, unit, how the source was
# identified and where (device + attribute, or a resolved path), never a label, index, value or host name.
# It names a source for as long as that device or path stays the same; it is not a hardware serial.

def source(provider, unit, basis, **where):
    """Sensor metadata without the read. basis: device_channel, resolved_path or unresolved (id None)."""
    if basis == "unresolved":
        return {"id": None, "provider": provider, "identity_basis": basis, "unit": unit}
    descriptor = {"v": 1, "provider": provider, "unit": unit, "basis": basis, **where}
    text = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"id": "src1:" + hashlib.sha256(text.encode()).hexdigest(), "provider": provider,
            "identity_basis": basis, "unit": unit}


def canonical(path, sysroot):
    """path relative to the sysfs root once symlinks are resolved, so /sys/class aliases and access prefixes
    (a test tree) drop out; None if it does not resolve to something inside the root.
    Resolved anew on every call (a path can point elsewhere later under the same name). On Linux the
    kernel resolves it (_kernel_path), which costs a few system calls instead of Python's step-by-step
    realpath; wherever that cannot answer, the portable way gives the result. The path is resolved
    last, so a node removed or a link retargeted before that is seen, as the portable way's final
    existence check sees it; neither way is a snapshot of a tree that changes during the call."""
    if _O_PATH is not None:
        try:
            base = _kernel_path(sysroot)
            real = _kernel_path(path)
        except OSError:  # missing, not permitted, no /proc, ...: the portable way decides
            real = base = None
        if real is not None and base is not None:
            if real == base:
                return os.curdir
            prefix = base.rstrip(os.sep) + os.sep
            return real[len(prefix):] if real.startswith(prefix) else None
    return _canonical_portable(path, sysroot)


_O_PATH = getattr(os, "O_PATH", None)  # Linux only


def _kernel_path(path):
    """The absolute path the kernel resolved an existing path to (open without reading, then the
    /proc/self/fd link); None if it went away in between."""
    fd = os.open(path, _O_PATH | os.O_CLOEXEC)
    try:
        real = os.readlink(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)
    return None if real.endswith(" (deleted)") or not real.startswith(os.sep) else real


def _canonical_portable(path, sysroot):
    try:
        real, base = os.path.realpath(path), os.path.realpath(sysroot)
        rel = os.path.relpath(real, base)
    except (OSError, ValueError):
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or not os.path.exists(real):
        return None
    return rel


def chip_identities(chips, sysroot):
    """{chip dir: (basis, where)} for hwmon chips. A chip under .../<device>/hwmon/hwmonN is identified by
    its device when no other chip of this listing shares that device (then a renumbered hwmonN keeps the
    id); otherwise by its resolved path; unresolved if the path does not resolve."""
    rels = {d: canonical(d, sysroot) for d, _ in chips}
    anchors = {}
    for d, rel in rels.items():
        m = rel and re.fullmatch(r"(.+)/hwmon/hwmon\d+", rel)
        if m and rel.split(os.sep)[:2] != ["devices", "virtual"]:  # nothing under devices/virtual is hardware
            anchors[d] = m.group(1)
    shared = {a for a in anchors.values() if list(anchors.values()).count(a) > 1}
    out = {}
    for d, rel in rels.items():
        if d in anchors and anchors[d] not in shared:
            out[d] = ("device_channel", {"device": anchors[d]})
        elif rel:
            out[d] = ("resolved_path", {"path": rel})
        else:
            out[d] = ("unresolved", {})
    return out


def pointer(*parts):
    """JSON Pointer (RFC 6901) to a value of the response: ~ becomes ~0 and / becomes ~1."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def summarize(groups, logged=None):
    """collectors entries of the groups. Their problems go to stderr when they change: logged is the
    service's {group: last details} kept between collections, so a lasting failure is logged once."""
    out = {}
    for g in groups.values():
        out[g.name] = g.status()
        details = tuple(g.details) + ((f"+{g.truncated} more",) if g.truncated else ())
        before = None if logged is None else logged.get(g.name, ())
        if details != before:
            if details:
                print(f"platmon: {g.name}: " + "; ".join(details), file=sys.stderr)
            elif before:
                print(f"platmon: {g.name}: readable again", file=sys.stderr)
            if logged is not None:
                logged[g.name] = details
    return out
