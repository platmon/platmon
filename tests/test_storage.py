"""collector/slow.py (low-frequency groups) and collector/storage.py (partitions, filesystem capacity), and
/api/observations. Fake files, statvfs, sysfs lookups and clocks; nothing on the host is statvfs'ed except
in the last test."""
import json
import os
import threading
import time

import pytest

import platmon
from collector import sysfs
from collector.slow import Observations, Slow
from collector.storage import Storage, parse_mountinfo, parse_partitions, unescape
from test_network_integration import ini_file
from test_server import get

S = 10**9
PARTS = """major minor  #blocks  name

 259        0  976762584 nvme0n1
 259        1  975237540 nvme0n1p1
 259        2     131072 nvme0n1p2
   7        0     100000 loop0
 252        0    1298320 zram0
"""


def mi(major_minor, path, fstype, source, opts="rw,relatime", optional="shared:1", super_opts="rw"):
    return f"25 1 {major_minor} / {path} {opts} {optional} - {fstype} {source} {super_opts}"


MOUNTS = "\n".join([
    mi("259:1", "/", "ext4", "/dev/nvme0n1p1"),
    mi("259:1", "/srv/bind\\040dir", "ext4", "/dev/nvme0n1p1", opts="ro,relatime", optional="shared:2 master:1"),  # a bind mount
    mi("0:25", "/dev/shm", "tmpfs", "tmpfs"),
    mi("7:0", "/snap/core/1", "squashfs", "/dev/loop0", optional=""),
    mi("0:50", "/mnt/nfs", "nfs4", "server:/export"),
    mi("0:51", "/mnt/fuse", "fuse.sshfs", "user@host:"),
    mi("0:30", "/", "overlay", "overlay"),
]) + "\n"


class Stat:
    def __init__(self, blocks, bfree, bavail, frsize=4096):
        self.f_blocks, self.f_bfree, self.f_bavail, self.f_frsize = blocks, bfree, bavail, frsize


class Host:
    def __init__(self):
        self.files = {"/proc/partitions": PARTS, "/proc/self/mountinfo": MOUNTS}
        self.statted, self.stat_error = [], {}
        self.parents = {"nvme0n1p1": "nvme0n1", "nvme0n1p2": "nvme0n1"}

    def read(self, path):
        v = self.files[path]
        if isinstance(v, Exception):
            raise v
        return v

    def statvfs(self, path, device):
        self.statted.append(path)
        if path in self.stat_error:
            raise self.stat_error[path]
        return Stat(1000, 250, 200)

    def storage(self):
        return Storage(read=self.read, statvfs=self.statvfs, parent=self.parents.get,
                       physical=lambda name: name == "nvme0n1")


def observe(h):
    g = sysfs.Group("storage")
    return h.storage()(g), g.status()


def test_parse_mountinfo_escapes_optional_fields_and_bad_lines():
    mounts, bad = parse_mountinfo(MOUNTS + "garbage line\n26 1 x:y / /a rw - ext4 /dev/a rw\n")
    assert mounts[1] == (259, 1, "/srv/bind dir", False, "ext4", "/dev/nvme0n1p1")  # ro bind of an rw filesystem
    assert mounts[0][3] is False and len(mounts) == 7
    assert [t for t, _ in bad] == ["line8", "line9"]
    assert unescape("a\\040b\\011c\\012d\\134e") == "a b\tc\nd\\e"


def test_read_only_is_the_filesystem_not_this_process_mount():
    """A sandboxed service (systemd ProtectSystem=strict, a read-only bind mount) sees the host's writable
    filesystems as read-only mounts in its own namespace; only the super-options say the filesystem is
    read-only."""
    sandboxed = mi("259:1", "/", "ext4", "/dev/nvme0n1p1", opts="ro,relatime")
    assert parse_mountinfo(sandboxed)[0][0][3] is False
    really = mi("259:1", "/", "ext4", "/dev/nvme0n1p1", opts="ro,relatime", super_opts="ro")
    assert parse_mountinfo(really)[0][0][3] is True
    assert parse_mountinfo(mi("259:1", "/", "ext4", "/dev/nvme0n1p1", super_opts="ro,noatime"))[0][0][3] is True
    missing = "25 1 259:1 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p1"  # no super-options: not a mountinfo line
    assert parse_mountinfo(missing) == ([], [("line1", "invalid_data")])


def test_parse_partitions():
    assert parse_partitions(PARTS)[259, 2] == ("nvme0n1p2", 131072 * 1024)


def test_local_block_filesystems_once_each_with_all_mount_points():
    h = Host()
    out, status = observe(h)
    assert h.statted == ["/"]  # tmpfs, squashfs, nfs, fuse and overlay are never statvfs'ed; the bind once
    assert out["filesystems"] == [{
        "device": "nvme0n1p1", "major": 259, "minor": 1, "fstype": "ext4", "source": "/dev/nvme0n1p1",
        "mount_points": [{"path": "/", "read_only": False}, {"path": "/srv/bind dir", "read_only": False}],
        "total_bytes": 1000 * 4096, "used_bytes": 750 * 4096, "available_bytes": 200 * 4096}]
    assert out["partitions"] == [
        {"name": "nvme0n1p1", "disk": "nvme0n1", "major": 259, "minor": 1, "size_bytes": 975237540 * 1024,
         "mount_points": ["/", "/srv/bind dir"]},
        {"name": "nvme0n1p2", "disk": "nvme0n1", "major": 259, "minor": 2, "size_bytes": 131072 * 1024,
         "mount_points": []}]  # not mounted: a size, no capacity made up
    assert status["state"] == "ok"


def test_a_measured_zero_is_zero():
    h = Host()
    h.statvfs = lambda path, device: Stat(0, 0, 0)
    out, _ = observe(h)
    assert (out["filesystems"][0]["total_bytes"], out["filesystems"][0]["available_bytes"]) == (0, 0)


@pytest.mark.parametrize("error, reason", [(FileNotFoundError(2, "x"), "disappeared"), (PermissionError(13, "x"), "permission_denied"),
                                           (OSError(5, "x"), "io_error")])
def test_statvfs_failure_keeps_the_entry_without_numbers(error, reason):
    h = Host()
    h.stat_error["/"] = error
    out, status = observe(h)
    fs = out["filesystems"][0]
    assert (fs["total_bytes"], fs["used_bytes"], fs["available_bytes"]) == (None, None, None)
    assert status["state"] == "partial" and status["issues"] == [{"target": "nvme0n1p1", "reason": reason}]


def test_unreadable_mountinfo_or_partitions():
    h = Host()
    h.files["/proc/self/mountinfo"] = PermissionError(13, "x")
    out, status = observe(h)
    assert out["filesystems"] == [] and len(out["partitions"]) == 2 and status["state"] == "partial"
    h.files["/proc/partitions"] = FileNotFoundError(2, "x")
    out, status = observe(h)
    assert out == {"partitions": [], "filesystems": []} and status["state"] == "error"


def test_nothing_found_is_unavailable():
    h = Host()
    h.files = {"/proc/partitions": "major minor  #blocks  name\n\n", "/proc/self/mountinfo": ""}
    out, status = observe(h)
    assert out == {"partitions": [], "filesystems": []} and (status["state"], status["reason"]) == ("unavailable", "not_detected")


def test_a_device_number_without_a_name_is_kept():
    h = Host()
    h.files["/proc/self/mountinfo"] = mi("0:40", "/data", "btrfs", "/dev/sdb") + "\n"  # btrfs: an anonymous device number
    out, _ = observe(h)
    assert out["filesystems"][0]["device"] is None and out["filesystems"][0]["total_bytes"] == 1000 * 4096


# ---------- the low-frequency group ----------

class Clock:
    def __init__(self):
        self.ns = 10**12

    def __call__(self):
        return self.ns


def group(observe, interval=30.0):
    c = Clock()
    return Slow("storage", observe, interval, c, wall=lambda: 1.7e9), c


def test_starting_then_observed_then_stale():
    calls = []
    g, c = group(lambda grp: calls.append(1) or {"n": grp.got(len(calls))})
    v = g.view()
    assert v["state"] == "starting" and v["observation"] is None and v["data"] is None
    c.ns += 2 * S  # the observation itself takes 2 s
    g.observe_once()  # (started at the current clock: a fake, so the duration is 0)
    c.ns += 10 * S
    v = g.view()
    assert v["state"] == "ok" and v["data"] == {"n": 1} and v["observation"]["id"] == 1
    assert v["observation"]["age_ms"] == v["observation"]["data_age_ms"] == 10000.0 and not v["observation"]["stale"]
    c.ns += 80 * S  # exactly 3 intervals after its start: still current
    assert g.view()["observation"]["stale"] is False
    c.ns += 1
    v = g.view()
    assert v["state"] == "stale" and v["observation"]["stale"] and v["data"] == {"n": 1}  # kept, said to be old
    assert v["observation"]["id"] == 1 and len(calls) == 1  # reading the view never observes


def test_an_unexpected_error_is_the_groups_own():
    g, _ = group(lambda grp: 1 / 0)
    g.observe_once()
    v = g.view()
    assert v["state"] == "error" and v["collector"]["reason"] == "internal_error" and v["data"] is None


def test_a_blocked_observation_stalls_only_its_group():
    c = Clock()
    release, entered = threading.Event(), threading.Event()
    n = [0]

    def observe(grp):
        n[0] += 1
        if n[0] == 2:
            entered.set()
            release.wait(10)
        return {"n": n[0]}
    g = Slow("storage", observe, 30.0, c, wall=time.time)
    g.observe_once()
    t = threading.Thread(target=g.observe_once)
    t.start()
    assert entered.wait(5)
    c.ns += 120 * S
    v = g.view()  # does not wait for the blocked read
    assert v["data"] == {"n": 1} and v["collecting_for_ms"] == 120000.0 and v["state"] == "stale"
    assert threading.active_count() <= 3  # this test's thread and the blocked one; nothing started per call
    release.set()
    t.join(5)
    assert g.view()["data"] == {"n": 2} and g.view()["collecting_for_ms"] is None


def test_the_view_is_new_and_the_observation_unchanged():
    g, _ = group(lambda grp: {"list": [grp.got(1), 2]})
    g.observe_once()
    v = g.view()
    v["state"] = "changed"
    assert g.view()["state"] == "ok" and g.view()["data"] is v["data"]  # the published data, never changed


def test_start_twice_is_one_thread():
    g, _ = group(lambda grp: {}, interval=3600)
    before = threading.active_count()
    g.start().start()
    assert threading.active_count() == before + 1
    g.stop()


# ---------- config, wiring, HTTP ----------

def test_storage_is_on_by_default_and_its_interval_is_checked(monkeypatch, tmp_path):
    cfg = platmon.load_config()
    assert cfg["storage"].getboolean("enabled") and cfg["storage"].getfloat("interval") == 30.0
    for text, error in (("[storage]\ninterval = 1\n", "out of range"), ("[storage]\nenabled = sometimes\n", "valid bool")):
        with pytest.raises(ValueError, match=error):
            platmon.load_config(ini_file(tmp_path, text))


BAT = ("battery", 60.0)  # on by default, like storage and wifi


@pytest.mark.parametrize("text, groups, history_on", [
    (None, [("storage", 30.0), ("wifi", 5.0), BAT], True),
    ("[storage]\ninterval = 60\n", [("storage", 60.0), ("wifi", 5.0), BAT], True),
    ("[storage]\nenabled = no\n", [("wifi", 5.0), BAT], True),
    ("[wifi]\nenabled = no\n", [("storage", 30.0), BAT], True),
    ("[battery]\nenabled = no\n", [("storage", 30.0), ("wifi", 5.0)], True),
    ("[battery]\ninterval = 120\n", [("storage", 30.0), ("wifi", 5.0), ("battery", 120.0)], True),
    ("[storage]\nenabled = no\n[wifi]\nenabled = no\n[battery]\nenabled = no\n[history]\nenabled = no\n", [], False),
    ("[probe]\ntargets = 127.0.0.1:9797\n", [("storage", 30.0), ("wifi", 5.0), BAT, ("probe", 10.0)], True),
    ("[probe]\nenabled = no\ntargets = 127.0.0.1:9797\n", [("storage", 30.0), ("wifi", 5.0), BAT], True),
    ("[probe]\ntargets = , ,\n", [("storage", 30.0), ("wifi", 5.0), BAT], True),
])
def test_slow_groups_and_history_follow_configuration(monkeypatch, tmp_path, text, groups, history_on):
    class Stop(Exception):
        pass

    class FakeSampler:
        instance_id, clock = "x", {"source": "fake"}
        on_publish = None

        def start(self):
            return self
    seen = {}

    def frontend(sampler, cfg, observations, history=None, levels=None):
        seen.update(observations=observations, sampler=sampler, history=history)
        raise Stop
    monkeypatch.setattr(platmon, "Sampler", lambda *a, **k: FakeSampler())
    monkeypatch.setitem(platmon.FRONTENDS, "http", frontend)
    monkeypatch.setattr(platmon.signal, "signal", lambda *a: None)
    monkeypatch.setattr(Slow, "start", lambda self: self)  # no thread in this test
    with pytest.raises(Stop):
        platmon.main([ini_file(tmp_path, text)] if text else [])
    assert [(g.name, g.interval) for g in seen["observations"].groups] == groups
    if history_on:
        assert seen["sampler"].on_publish.__self__ is seen["history"]
        assert all(g.on_publish.__self__ is seen["history"] for g in seen["observations"].groups)
    else:
        assert seen["history"] is None and seen["sampler"].on_publish is None


def test_observations_endpoint():
    from test_server import NoWait
    s = NoWait(lambda: {"v": 1})
    s._attempt()
    g, c = group(lambda grp: {"filesystems": [], "partitions": []})
    g.observe_once()
    from frontends.server import make_handler
    from http.server import ThreadingHTTPServer
    for observations, groups in ((Observations([g], s.instance_id, s.clock), ["storage"]), (None, [])):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, True, observations))
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        try:
            code, headers, body = get(f"http://127.0.0.1:{httpd.server_address[1]}/api/observations")
        finally:
            httpd.shutdown()
        doc = json.loads(body)
        assert (code, headers["Content-Type"], headers["Cache-Control"]) == (200, "application/json", "no-store")
        assert list(doc["groups"]) == groups and doc["instance_id"] == s.instance_id and doc["schema_version"] == 2


def test_real_host_observation():
    g = sysfs.Group("storage")
    out = Storage()(g)
    assert g.status()["state"] in ("ok", "partial", "unavailable")
    if os.path.exists("/proc/self/mountinfo"):
        assert all(f["fstype"] in ("ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat", "ntfs3", "jfs", "reiserfs")
                   for f in out["filesystems"])


def test_http_answers_while_a_group_is_blocked_and_stale():
    """A stale, blocked storage group next to a fresh core: /api/stats, /api/status and /api/observations
    all answer at once; only the storage entry says it is old."""
    from http.server import ThreadingHTTPServer

    from frontends.server import make_handler
    from test_server import NoWait
    s = NoWait(lambda: {"v": 1, "uptime": 1})
    s._attempt()
    release, entered = threading.Event(), threading.Event()
    c = Clock()
    n = [0]

    def observe(grp):
        n[0] += 1
        if n[0] == 2:
            entered.set()
            release.wait(10)
        return {"filesystems": [], "partitions": [grp.got({})]}
    g = Slow("storage", observe, 30.0, c)
    g.observe_once()
    t = threading.Thread(target=g.observe_once)
    t.start()
    assert entered.wait(5)
    c.ns += 100 * S
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, True, Observations([g], s.instance_id, s.clock)))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        t0 = time.monotonic()
        answers = {p: get(url + p) for p in ("/api/stats", "/api/status", "/api/observations")}
        assert time.monotonic() - t0 < 3  # nothing waited for the blocked read
    finally:
        release.set()
        t.join(5)
        httpd.shutdown()
    assert answers["/api/stats"][0] == 200 and json.loads(answers["/api/status"][2])["ready"] is True
    st = json.loads(answers["/api/observations"][2])["groups"]["storage"]
    assert st["state"] == "stale" and st["observation"]["stale"] and st["collecting_for_ms"] == 100000.0


def test_cli_storage_lines_and_ages():
    from frontends.cli import aged, storage_lines
    body = {"groups": {"storage": {"observation": {"id": 2, "data_age_ms": 4000, "stale": False}, "data": {
        "filesystems": [{"device": "nvme0n1p1", "major": 259, "minor": 1, "fstype": "ext4", "total_bytes": 4 * 2**30,
                         "used_bytes": 2**30, "available_bytes": 3 * 2**30,
                         "mount_points": [{"path": "/", "read_only": False}, {"path": "/srv", "read_only": True}]},
                        {"device": None, "major": 0, "minor": 40, "fstype": "btrfs", "total_bytes": None,
                         "used_bytes": None, "available_bytes": None, "mount_points": [{"path": "/data", "read_only": False}]}],
        "partitions": [{"name": "p1", "disk": "nvme0n1", "mount_points": ["/"]}, {"name": "p2", "disk": "nvme0n1", "mount_points": []}]}}}}
    assert storage_lines(aged(body, 6)) == [
        "STORAGE  observed 10 s ago",
        "  / +1 more  ext4 nvme0n1p1  used 1.0G/4.0G (25%)  available 3.0G",
        "  /data  btrfs 0:40  capacity unknown",
        "  nvme0n1: 2 partitions, 1 mounted"]
    assert body["groups"]["storage"]["observation"]["data_age_ms"] == 4000  # the answer itself is kept
    stale = {"groups": {"storage": {**body["groups"]["storage"], "observation": {"data_age_ms": 95000, "stale": True}}}}
    assert storage_lines(stale)[0] == "STORAGE  observed 95 s ago (not current)"
    for odd in (None, {}, {"groups": {}}, {"groups": {"storage": {"state": "starting", "observation": None, "data": None}}},
                {"groups": "x"}):
        assert storage_lines(odd) == []


def test_only_local_filesystem_lines_are_converted():
    text = MOUNTS + "26 1 x:y / /odd rw - tmpfs tmpfs rw\n"  # a broken line of a type that is never read
    mounts, bad = parse_mountinfo(text, {"ext4"})
    assert [m[2] for m in mounts] == ["/", "/srv/bind dir"] and bad == []
    assert parse_mountinfo(text)[1] == [("line8", "invalid_data")]  # without the filter it is reported


def test_capacity_is_bound_to_verified_mount_descriptor(monkeypatch):
    from collector import storage
    from types import SimpleNamespace
    opened, queried, closed = [], [], []
    monkeypatch.setattr(storage.os, "open", lambda path, flags: opened.append(path) or 42)
    monkeypatch.setattr(storage.os, "fstat", lambda fd: SimpleNamespace(st_dev=os.makedev(8, 1)))
    monkeypatch.setattr(storage.os, "fstatvfs", lambda fd: queried.append(fd) or Stat(10, 5, 5))
    monkeypatch.setattr(storage.os, "close", closed.append)
    result = storage.filesystem_capacity("/mount", (8, 1))
    assert result.f_blocks == 10 and opened == ["/mount"] and queried == closed == [42]
    with pytest.raises(OSError, match="mount changed"):
        storage.filesystem_capacity("/mount", (8, 2))
    assert queried == [42] and closed == [42, 42]  # replacement is never queried as the old filesystem


def test_replaced_mount_has_no_capacity_and_an_explicit_reason():
    import errno
    h = Host()
    h.stat_error["/"] = OSError(errno.ESTALE, "mount changed")
    out, status = observe(h)
    assert out["filesystems"][0]["total_bytes"] is None
    assert status["state"] == "partial"
    assert status["issues"] == [{"target": "nvme0n1p1", "reason": "mount_changed"}]


def test_capacity_descriptor_closes_on_statvfs_failure(monkeypatch):
    from collector import storage
    from types import SimpleNamespace
    closed = []
    monkeypatch.setattr(storage.os, "open", lambda *args: 42)
    monkeypatch.setattr(storage.os, "fstat", lambda fd: SimpleNamespace(st_dev=os.makedev(8, 1)))
    def failed(fd):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(storage.os, "fstatvfs", failed)
    monkeypatch.setattr(storage.os, "close", closed.append)
    with pytest.raises(PermissionError):
        storage.filesystem_capacity("/mount", (8, 1))
    assert closed == [42]


@pytest.mark.parametrize("file_mounts", [["/usr/sbin/docker-init"], ["/etc/hostname", "/etc/hosts"]])
def test_storage_skips_file_binds_before_a_directory(file_mounts):
    import errno
    h = Host()
    paths = file_mounts + ["/host/root", "/another-dir"]
    h.files["/proc/self/mountinfo"] = "\n".join(mi("259:1", p, "ext4", "/dev/nvme0n1p1") for p in paths)
    h.stat_error = {p: NotADirectoryError(errno.ENOTDIR, "not a directory") for p in file_mounts}
    out, status = observe(h)
    assert out["filesystems"][0]["total_bytes"] == 1000 * 4096
    assert h.statted == file_mounts + ["/host/root"]
    assert status["state"] == "ok" and status["issues"] == []
    assert [m["path"] for m in out["filesystems"][0]["mount_points"]] == paths


def test_only_file_binds_keep_unknown_capacity():
    import errno
    h = Host()
    h.files["/proc/self/mountinfo"] = mi("259:1", "/file", "ext4", "/dev/nvme0n1p1")
    h.stat_error["/file"] = NotADirectoryError(errno.ENOTDIR, "not a directory")
    out, status = observe(h)
    assert out["filesystems"][0]["total_bytes"] is None
    assert status["state"] == "partial" and status["issues"] == [{"target": "nvme0n1p1", "reason": "io_error"}]


def test_file_bind_fallback_keeps_descriptor_identity_check(tmp_path):
    from collector.storage import filesystem_capacity
    file = tmp_path / "file"
    file.write_text("x")
    device = os.stat(tmp_path).st_dev
    major, minor = os.major(device), os.minor(device)
    h = Host()
    h.files["/proc/self/mountinfo"] = "\n".join(
        mi(f"{major}:{minor}", str(p), "ext4", "/dev/test") for p in (file, tmp_path))
    h.statvfs = filesystem_capacity
    out, status = observe(h)
    expected = os.statvfs(tmp_path)
    assert out["filesystems"][0]["total_bytes"] == expected.f_blocks * expected.f_frsize
    assert status["state"] == "ok"


@pytest.mark.skipif(os.geteuid() == 0, reason="requires unprivileged permission enforcement")
def test_capacity_does_not_require_directory_read_permission(tmp_path):
    from collector.storage import filesystem_capacity
    mount = tmp_path / "mount"
    mount.mkdir()
    device = mount.stat().st_dev
    mount.chmod(0)
    try:
        expected = os.statvfs(mount)
        result = filesystem_capacity(mount, (os.major(device), os.minor(device)))
        assert result.f_blocks == expected.f_blocks and result.f_frsize == expected.f_frsize
    finally:
        mount.chmod(0o700)


def test_capacity_rejects_symlinks_and_regular_files(tmp_path):
    from collector.storage import filesystem_capacity
    device = tmp_path.stat().st_dev
    file = tmp_path / "file"
    file.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    for path in (file, link):
        with pytest.raises(OSError):
            filesystem_capacity(path, (os.major(device), os.minor(device)))
