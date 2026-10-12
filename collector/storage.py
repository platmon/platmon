"""Storage capacity, a low-frequency observation (runtime/slow.py): the partitions of the physical disks
and the size, use and free space of local block-backed filesystems. Reads /proc/partitions and
/proc/self/mountinfo once and calls statvfs once per filesystem; never reads a block device, mounts or
changes anything. Contract: docs/reference/storage.md.
"""
import os
import errno
import re

from .disk_io import is_physical
from .sysfs import reason_of

PARTITIONS = "/proc/partitions"
MOUNTINFO = "/proc/self/mountinfo"
SYS_CLASS_BLOCK = "/sys/class/block"
# Filesystems on local block devices whose capacity is read. Everything else (tmpfs, overlay, squashfs
# images, network and FUSE filesystems, autofs, pseudo filesystems) is never statvfs'ed: some can block on
# a remote server or trigger an automount, others have no meaningful capacity.
LOCAL_FS = frozenset(("ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat", "ntfs3", "jfs", "reiserfs"))


ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape(field):
    """mountinfo escapes space, tab, newline and backslash as \\040, \\011, \\012, \\134."""
    return ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field) if "\\" in field else field


def parse_mountinfo(text, fstypes=None):
    """[(major, minor, mount point, read only, fstype, source)] of the mounts (only those of fstypes, if
    given: the other lines are not converted), and [(target, why)] for lines that do not parse (left out).
    Format (proc(5)): id parent major:minor root mount-point options [optional fields...] - fstype source
    super-options.
    Read only comes from the super-options (the filesystem itself), not from the per-mount options: a
    sandboxed service (systemd ProtectSystem, a read-only bind mount) sees the host's writable
    filesystems as read-only mounts in its own namespace, and that is a property of this process, not of
    the filesystem it reports on."""
    mounts, bad = [], []
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        f = line.split(" ")
        try:
            sep = f.index("-", 6)
            fstype = f[sep + 1]
            if fstypes is not None and fstype not in fstypes:
                continue
            major, minor = (int(x) for x in f[2].split(":"))
            source = unescape(f[sep + 2])
            read_only = "ro" in f[sep + 3].split(",")
        except (ValueError, IndexError):
            bad.append((f"line{n}", "invalid_data"))
            continue
        mounts.append((major, minor, unescape(f[4]), read_only, fstype, source))
    return mounts, bad


def parse_partitions(text):
    """{(major, minor): (name, size in bytes)} from /proc/partitions (sizes in 1 KiB blocks)."""
    out = {}
    for line in text.split("\n")[2:]:
        f = line.split()
        if len(f) == 4 and all(x.isdigit() for x in f[:3]):
            out[int(f[0]), int(f[1])] = (f[3], int(f[2]) * 1024)
    return out


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def parent_of(name, root=SYS_CLASS_BLOCK):
    """The whole disk a partition belongs to, or None for anything that is not a partition."""
    if not os.path.exists(f"{root}/{name}/partition"):
        return None
    return os.path.basename(os.path.dirname(os.readlink(f"{root}/{name}")))


def filesystem_capacity(path, device):
    """Pin a directory, verify its filesystem, then read capacity through the same descriptor.
    O_PATH avoids requiring directory read permission for filesystem metadata.
    A mount replaced after mountinfo must not lend its capacity to the previous device."""
    fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if (os.major(st.st_dev), os.minor(st.st_dev)) != device:
            raise OSError(errno.ESTALE, "mount changed")
        return os.fstatvfs(fd)
    finally:
        os.close(fd)


class Storage:
    """observe(group) for runtime/slow.py. Paths and lookups are injectable for tests."""

    def __init__(self, read=None, statvfs=filesystem_capacity, parent=parent_of, physical=is_physical):
        self._read, self._statvfs, self._parent, self._physical = read or read_text, statvfs, parent, physical

    def __call__(self, group):
        try:
            names = parse_partitions(self._read(PARTITIONS))
        except (OSError, UnicodeDecodeError) as e:
            group.note("partitions", reason_of(e) if isinstance(e, OSError) else "invalid_data", PARTITIONS)
            names = {}
        try:
            mounts, bad = parse_mountinfo(self._read(MOUNTINFO), LOCAL_FS)
        except (OSError, UnicodeDecodeError) as e:
            group.note("mountinfo", reason_of(e) if isinstance(e, OSError) else "invalid_data", MOUNTINFO)
            mounts, bad = [], []
        for target, why in bad:
            group.note(target, why, MOUNTINFO)

        filesystems = {}  # (major, minor) -> entry: one filesystem, however many mount points show it
        for major, minor, path, ro, fstype, source in mounts:
            if fstype not in LOCAL_FS or not source.startswith("/dev/"):
                continue
            fs = filesystems.setdefault((major, minor), {
                "device": names.get((major, minor), (None,))[0], "major": major, "minor": minor, "fstype": fstype,
                "source": source, "mount_points": [], "total_bytes": None, "used_bytes": None, "available_bytes": None})
            fs["mount_points"].append({"path": path, "read_only": ro})
        for fs in filesystems.values():
            for index, mount in enumerate(fs["mount_points"]):
                path = mount["path"]
                try:
                    st = self._statvfs(path, (fs["major"], fs["minor"]))
                except OSError as e:
                    # Docker may list a file bind before a usable directory on the same filesystem.
                    # Skip only ENOTDIR; other failures, especially identity changes, stay visible.
                    if e.errno == errno.ENOTDIR and index + 1 < len(fs["mount_points"]):
                        continue
                    why = ("mount_changed" if e.errno == errno.ESTALE else
                           "disappeared" if isinstance(e, FileNotFoundError) else reason_of(e))
                    group.note(fs["device"] or f"{fs['major']}:{fs['minor']}", why, f"statvfs {path}: {e.strerror}")
                    break  # no valid capacity: listed without numbers, never as 0
                fs.update(total_bytes=group.got(st.f_blocks * st.f_frsize), used_bytes=(st.f_blocks - st.f_bfree) * st.f_frsize,
                          available_bytes=st.f_bavail * st.f_frsize)
                break

        partitions = []
        for (major, minor), (name, size) in sorted(names.items(), key=lambda kv: kv[1][0]):
            try:
                parent = self._parent(name)
                if parent is None or not self._physical(parent):
                    continue
            except OSError as e:
                group.note(name, reason_of(e), f"{SYS_CLASS_BLOCK}/{name}: {e.strerror}")
                continue
            fs = filesystems.get((major, minor))
            partitions.append(group.got({"name": name, "disk": parent, "major": major, "minor": minor, "size_bytes": size,
                                         "mount_points": [m["path"] for m in fs["mount_points"]] if fs else []}))
        out = sorted(filesystems.values(), key=lambda f: f["mount_points"][0]["path"])
        if not out and not partitions and not group.reasons:
            group.note("storage", "not_detected")
        return {"partitions": partitions, "filesystems": out}
