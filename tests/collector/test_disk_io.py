"""collector/disk_io.py: the /proc/diskstats parser, physical-disk selection and DiskCounters' baselines and
rates, plus its place in collect_recorded, the Sampler and the config. Fake file contents, /sys/block links
and clocks; nothing on the host is read except in the last tests."""
import functools
import json
import os

import pytest

from collector import metadata
from collector import core as collector
import platmon
from collector import sysfs
from collector.common import CpuCounters
from collector.disk_io import DiskCounters, is_physical, parse
from runtime.sampler import Collected
from tests.collector.test_common import Proc, idle_busy
from tests.collector.test_network_integration import ini_file, started
from tests.collector.test_provenance import at, good_required, recorded_with
from tests.runtime.test_sampler import Clock

S = 10**9


def row(major, minor, name, r=(0, 0, 0, 0), w=(0, 0, 0, 0), in_flight=0, io_ms=0, extra=(0, 0, 0, 0, 0, 0)):
    """One /proc/diskstats line: r/w = (ios, merges, sectors, ms); then in flight, io ms, weighted ms, extra."""
    return f"{major:4d} {minor:7d} {name} " + " ".join(map(str, (*r, *w, in_flight, io_ms, 0, *extra)))


class Host:
    def __init__(self, **disks):
        self.t, self.reads, self.lookups, self.text, self.error = 10**12, 0, [], None, None
        self.disks = disks  # name -> (major, minor, kind, r, w, in_flight, io_ms); kind True/False/None
        self.lookup_error = {}

    def clock(self):
        return self.t

    def read(self, path):
        self.reads += 1
        if self.error:
            raise self.error
        if self.text is not None:
            return self.text
        return "".join(row(*v[:2], n, *v[3:]) + "\n" for n, v in self.disks.items()).encode()

    def physical(self, name):
        self.lookups.append(name)
        if name in self.lookup_error:
            raise self.lookup_error[name]
        return self.disks[name][2] if name in self.disks else True  # a name only in h.text: a disk

    def counters(self, max_gap=5.0):
        return DiskCounters(self.clock, max_gap, read=self.read, physical=self.physical)


def disk(major, minor, kind=True, r=(0, 0, 0, 0), w=(0, 0, 0, 0), in_flight=0, io_ms=0):
    return (major, minor, kind, r, w, in_flight, io_ms)


def sample(c):
    g = metadata.Group("disk_io")
    out, span = c.sample(g)
    return out, span, g.status()


def by_name(out):
    return {d["name"]: d for d in out["disks"]}


def test_only_physical_disks_with_rates_over_the_observed_window():
    h = Host(nvme0n1=disk(259, 0), nvme0n1p1=disk(259, 1, None), loop0=disk(7, 0, False), zram0=disk(252, 0, False))
    c = h.counters()
    out, span, status = sample(c)
    assert [d["name"] for d in out["disks"]] == ["nvme0n1"] and out["disks"][0]["reason"] == "warmup"
    assert out["scope"] == {"kind": "host_block_devices"} and out["provider"] == "proc_diskstats" and out["read"] is None
    assert span == (h.t, h.t) and status["state"] == "ok"
    h.t += 2 * S
    h.disks["nvme0n1"] = disk(259, 0, True, (10, 1, 80, 4), (20, 2, 160, 6), in_flight=3, io_ms=500)
    d = by_name(sample(c)[0])["nvme0n1"]
    assert d == {"name": "nvme0n1", "major": 259, "minor": 0,
                 "read": {"ios": 10, "merges": 1, "bytes": 80 * 512, "time_ms": 4},
                 "write": {"ios": 20, "merges": 2, "bytes": 160 * 512, "time_ms": 6}, "in_flight": 3, "io_time_ms": 500,
                 "rates": {"read_bytes_per_s": 80 * 512 / 2, "write_bytes_per_s": 160 * 512 / 2, "reads_per_s": 5.0,
                           "writes_per_s": 10.0, "io_time_ratio": 0.25},
                 "window_ms": 2000.0, "reason": None}
    h.t += S  # idle: measured 0, not missing
    assert by_name(sample(c)[0])["nvme0n1"]["rates"]["write_bytes_per_s"] == 0.0


def test_confirmed_kinds_are_reused_and_missing_links_retried():
    h = Host(sda=disk(8, 0), sda1=disk(8, 1, None), loop0=disk(7, 0, False))
    c = h.counters()
    for _ in range(3):
        sample(c)
        h.t += S
    assert h.lookups.count("loop0") == h.lookups.count("sda") == 1
    assert h.lookups.count("sda1") == 3
    del h.disks["sda"], h.disks["sda1"]
    sample(c)
    assert set(c._kind) == {(7, 0, "loop0")}  # names that are gone are not kept
    h.disks["sda"] = disk(8, 0)  # back: looked up again, and a new baseline
    h.t += S
    assert by_name(sample(c)[0])["sda"]["reason"] == "warmup" and h.lookups.count("sda") == 2


def test_a_failed_lookup_is_reported_and_retried():
    h = Host(sda=disk(8, 0), sdb=disk(8, 16))
    h.lookup_error["sdb"] = PermissionError(13, "Permission denied")
    c = h.counters()
    out, _, status = sample(c)
    assert [d["name"] for d in out["disks"]] == ["sda"] and status["state"] == "partial"
    assert status["issues"] == [{"target": "sdb", "reason": "permission_denied"}]
    del h.lookup_error["sdb"]
    h.t += S
    out, _, status = sample(c)
    assert by_name(out)["sdb"]["reason"] == "warmup" and status["state"] == "ok"


@pytest.mark.parametrize("field", [0, 1, 2, 3, 4, 5, 6, 7, 9])
def test_any_counter_going_down_gives_no_rate_but_in_flight_may(field):
    values = [100] * 10
    h = Host(sda=disk(8, 0, True, tuple(values[:4]), tuple(values[4:8]), values[8], values[9]))
    c = h.counters()
    sample(c)
    lower = list(values)
    lower[field] -= 1
    h.t += S
    h.disks["sda"] = disk(8, 0, True, tuple(lower[:4]), tuple(lower[4:8]), lower[8], lower[9])
    d = by_name(sample(c)[0])["sda"]
    assert d["reason"] == "counter_regressed" and d["rates"] is None and d["window_ms"] == 1000.0
    h.t += S
    h.disks["sda"] = disk(8, 0, True, tuple(lower[:4]), tuple(lower[4:8]), 0, lower[9])  # in flight drops: fine
    assert by_name(sample(c)[0])["sda"]["reason"] is None


@pytest.mark.parametrize("window, reason", [(5 * S, None), (5 * S + 1, "gap"), (0, "invalid_interval"),
                                            (-S, "invalid_interval")])
def test_gap_boundary_and_bad_intervals(window, reason):
    h = Host(sda=disk(8, 0))
    c = h.counters(max_gap=5.0)
    sample(c)
    h.t += window
    d = by_name(sample(c)[0])["sda"]
    assert d["reason"] == reason and (d["rates"] is None) == (reason is not None)
    h.t += S
    assert by_name(sample(c)[0])["sda"]["reason"] is None


def test_a_new_device_number_under_the_same_name_starts_over():
    h = Host(sda=disk(8, 0, True, (100, 0, 0, 0)))
    c = h.counters()
    sample(c)
    h.disks["sda"] = disk(8, 32, True, (200, 0, 0, 0))
    h.t += S
    assert by_name(sample(c)[0])["sda"]["reason"] == "warmup"


def test_bad_rows_are_left_out_and_others_kept():
    text = "\n".join([row(8, 0, "sda"), "   8  16 sdb 1 2 3",                     # short
                      row(8, 32, "sdc").replace(" 0 ", " -1 ", 1),                  # signed
                      row(8, 48, "sdd").replace(" 0 ", f" {2**64} ", 1),           # too large
                      row(8, 64, "sde", extra=("x",)),                              # a later field not a counter
                      row(8, 80, "dup"), row(8, 96, "dup"),
                      row(8, 112, "a/b"), "8 128", row(8, 144, "sdf", extra=()), ""])  # 11 fields: old kernel, fine
    rows, bad = parse(text)
    assert sorted(k[2] for k in rows) == ["sda", "sdf"]
    assert [t for t, _ in bad] == ["sdb", "sdc", "sdd", "sde", "dup", "line8", "line9"]
    assert all(r == "invalid_data" for _, r in bad)


def test_rows_of_left_out_devices_are_not_converted_or_reported():
    h = Host(sda=disk(8, 0), loop0=disk(7, 0, False), sda1=disk(8, 1, None))
    h.text = (row(8, 0, "sda") + "\n7 0 loop0 x y\n8 1 sda1 -1\n").encode()
    out, _, status = sample(h.counters())
    assert [d["name"] for d in out["disks"]] == ["sda"] and status == {"state": "ok", "reason": None, "issues": [],
                                                                     "issues_truncated": 0}
    rows, bad = parse("7 0 loop0 x y\n", lambda name: False)
    assert rows == {} and bad == []  # listed, so the file is not empty, but nothing to collect


@pytest.mark.parametrize("text", [b"\xff\xfe", b"", b"\n\n"])
def test_a_broken_file_gives_nothing_and_clears_baselines(text):
    h = Host(sda=disk(8, 0))
    c = h.counters()
    sample(c)
    h.text = text
    h.t += S
    out, span, status = sample(c)
    assert out["disks"] == [] and span is None
    assert status["state"] == "error" and status["issues"] == [{"target": "diskstats", "reason": "invalid_data"}]
    h.text = None
    h.t += S
    assert by_name(sample(c)[0])["sda"]["reason"] == "warmup"


@pytest.mark.parametrize("error, state, reason", [(FileNotFoundError(2, "x"), "unavailable", "not_exposed"),
                                                  (PermissionError(13, "x"), "error", "permission_denied"),
                                                  (OSError(5, "x"), "error", "io_error")])
def test_unreadable_file(error, state, reason):
    h = Host(sda=disk(8, 0))
    c = h.counters()
    sample(c)
    h.error = error
    out, span, status = sample(c)
    assert out["disks"] == [] and span is None and (status["state"], status["reason"]) == (state, reason)
    assert c._last is None


def test_no_physical_disk_is_not_detected_not_an_error():
    h = Host(loop0=disk(7, 0, False), ram0=disk(1, 0, False))
    out, span, status = sample(h.counters())
    assert out["disks"] == [] and span is not None
    assert (status["state"], status["reason"], status["issues"]) == ("unavailable", "not_detected", [])


def test_many_devices_one_read_and_capped_issues():
    h = Host(**{f"loop{n}": disk(7, n, False) for n in range(200)}, sda=disk(8, 0))
    h.text = h.read(None) + "".join(f"8 {n} bad{n} 1\n" for n in range(100)).encode()
    h.reads = 0
    c = h.counters()
    out, _, status = sample(c)
    assert h.reads == 1 and [d["name"] for d in out["disks"]] == ["sda"]
    assert len(status["issues"]) == metadata.MAX_ISSUES and status["issues_truncated"] == 100 - metadata.MAX_ISSUES


def test_changing_the_output_does_not_touch_the_baseline():
    h = Host(sda=disk(8, 0, True, (10, 0, 0, 0)))
    c = h.counters()
    out, _, _ = sample(c)
    out["disks"][0]["read"]["ios"] = 0
    out["disks"].clear()
    h.t += S
    assert by_name(sample(c)[0])["sda"]["rates"]["reads_per_s"] == 0.0


def test_an_unexpected_error_clears_the_baseline_and_is_raised():
    h = Host(sda=disk(8, 0))
    c = h.counters()
    sample(c)
    c._physical = lambda name: 1 / 0
    c._kind = {}
    with pytest.raises(ZeroDivisionError):
        sample(c)
    assert c._last is None


def test_is_physical_follows_the_sysfs_link(tmp_path):
    (tmp_path / "devices/virtual/block/loop0").mkdir(parents=True)
    (tmp_path / "devices/pci0000:00/nvme/nvme0n1").mkdir(parents=True)
    (tmp_path / "block").mkdir()
    os.symlink("../devices/virtual/block/loop0", tmp_path / "block/loop0")
    os.symlink("../devices/pci0000:00/nvme/nvme0n1", tmp_path / "block/nvme0n1")
    root = str(tmp_path / "block")
    assert is_physical("nvme0n1", root) is True and is_physical("loop0", root) is False
    assert is_physical("nvme0n1p1", root) is None  # a partition has no /sys/block entry


# ---------- in the service ----------

@pytest.mark.parametrize("text", [None, "[core]\ninterval = 1\n", "[disk_io]\n", "[disk_io]\nenabled = yes\n"])
def test_on_unless_disabled(monkeypatch, tmp_path, text):
    collect = started(monkeypatch, None if text is None else ini_file(tmp_path, text))
    cpu, _, clock, _, disk_io, *_ = collect.args
    assert isinstance(disk_io, DiskCounters) and disk_io.clock is clock is cpu._clock and disk_io._max_gap == 5 * S


def test_disabled_never_reads(monkeypatch, tmp_path):
    made, calls = [], []
    monkeypatch.setattr(platmon, "DiskCounters", lambda *a, **k: made.append(a))
    collect = started(monkeypatch, ini_file(tmp_path, "[disk_io]\nenabled = no\n"))
    assert collect.args[4] is None and made == []
    monkeypatch.setattr(DiskCounters, "sample", lambda *a: calls.append(a))
    stats = collect().stats
    assert calls == [] and "disk_io" not in stats and "disk_io" not in stats["collectors"]


def test_shipped_ini_matches_the_default():
    from tests.runtime.test_platmon import SHIPPED_INI
    assert dict(platmon.load_config(SHIPPED_INI)["disk_io"]) == dict(platmon.load_config()["disk_io"]) == {"enabled": "yes"}


def test_direct_collect_without_disk_io_is_unchanged():
    stats = collector.collect()
    assert "disk_io" not in stats and "disk_io" not in stats["collectors"]


@pytest.fixture
def service(monkeypatch, tmp_path):
    from tests.collector.test_common import without_host_sensors
    without_host_sensors(monkeypatch, tmp_path)
    c = Clock()
    clock = lambda: c.ns  # noqa: E731
    h = Host(sda=disk(8, 0))
    h.clock = clock
    p = Proc(monkeypatch)
    d = h.counters()
    from runtime.sampler import Sampler
    s = Sampler(functools.partial(collector.collect_recorded, CpuCounters(clock, max_gap=5.0), {}, clock, None, d),
                clock=(clock, {"source": "fake"}), wall=lambda: c.wall)

    def attempt(ios=0):
        c.advance(1)
        h.disks["sda"] = disk(8, 0, True, (ios, 0, ios * 8, 0))
        p.readings.append({0: idle_busy(c.ns // S, c.ns // S)})
        s._attempt()
    return s, attempt, h, c, d


def test_service_publishes_rates_with_their_read(service):
    s, attempt, h, c, _ = service
    attempt()
    attempt(ios=50)
    stats, status = s.read()
    d = stats["disk_io"]["disks"][0]
    assert d["rates"]["reads_per_s"] == 50.0 and d["rates"]["read_bytes_per_s"] == 50 * 8 * 512
    assert stats["disk_io"]["read"] is not None and stats["sample"]["data_age_basis"] == "oldest_current_read_start"
    assert stats["collectors"]["disk_io"]["state"] == "ok" and status["state"] == "ready"
    assert stats["disk"].keys() == {"total", "used"}  # the filesystem usage field is unchanged


def test_disk_failure_keeps_the_core_going(service):
    s, attempt, h, c, _ = service
    attempt()
    h.error = OSError(5, "EIO")
    attempt()
    stats, status = s.read()
    assert stats["sample"]["sequence"] == 2 and stats["disk_io"]["disks"] == [] and stats["disk_io"]["read"] is None
    assert (status["state"], status["ready"], status["last_attempt"]["state"]) == ("degraded", True, "ok")
    assert stats["sample"]["data_age_basis"] == "oldest_current_read_start"


DISK = {"scope": {"kind": "host_block_devices"}, "provider": "proc_diskstats",
        "disks": [{"name": "sda", "reason": "warmup"}], "read": "forged"}


def answer(make):
    c = Clock()
    s = recorded_with(c, make)
    s._attempt()
    c.advance(0.2)
    return s.read()[0]


def test_disk_read_counts_for_the_data_age():
    stats = answer(lambda start, clock: Collected({"disk_io": json.loads(json.dumps(DISK))}, {}, good_required(start),
                                                  clock, None, at(start, 20, 21)))
    assert (stats["sample"]["data_age_ms"], stats["sample"]["data_age_basis"]) == (480.0, "oldest_current_read_start")
    assert stats["disk_io"]["read"] == {"started_offset_ms": 20.0, "completed_offset_ms": 21.0}


@pytest.mark.parametrize("span", ["missing", "reversed", "future"])
def test_a_bad_disk_read_falls_back(span):
    def make(start, clock):
        spans = {"missing": None, "reversed": at(start, 21, 20), "future": at(start, 20, 400)}
        return Collected({"disk_io": json.loads(json.dumps(DISK))}, {}, good_required(start), clock, None, spans[span])
    stats = answer(make)
    assert (stats["sample"]["data_age_ms"], stats["sample"]["data_age_basis"]) == (500.0, "cycle_start_upper_bound")
    assert stats["disk_io"]["read"] is None and stats["disk_io"]["disks"][0]["name"] == "sda"


def test_network_and_disk_reads_are_checked_apart():
    from tests.collector.test_network_integration import NET
    stats = answer(lambda start, clock: Collected(
        {"network": json.loads(json.dumps(NET)), "disk_io": json.loads(json.dumps(DISK))}, {}, good_required(start),
        clock, at(start, 30, 31), at(start, 20, 21)))
    assert stats["network"]["read"]["started_offset_ms"] == 30.0 and stats["disk_io"]["read"]["started_offset_ms"] == 20.0
    assert stats["sample"]["data_age_ms"] == 480.0


def test_real_host_reading():
    out, span, status = sample(DiskCounters())
    assert status["state"] in ("ok", "partial", "unavailable")
    if os.path.exists("/proc/diskstats"):
        assert span is not None
        for d in out["disks"]:
            assert is_physical(d["name"]) is True


def test_changed_device_number_rechecks_classification():
    h = Host(sda=disk(8, 0, True))
    c = h.counters()
    assert by_name(sample(c)[0])["sda"]["reason"] == "warmup"
    h.disks["sda"] = disk(8, 32, False)
    h.t += S
    assert sample(c)[0]["disks"] == []
    assert h.lookups == ["sda", "sda"]


def test_missing_sysfs_entry_is_retried_while_diskstats_name_remains():
    h = Host(sda=disk(8, 0, None))
    c = h.counters()
    assert sample(c)[0]["disks"] == []
    h.disks["sda"] = disk(8, 0, True)
    h.t += S
    assert by_name(sample(c)[0])["sda"]["reason"] == "warmup"


def test_file_failure_discards_classification_cache():
    h = Host(sda=disk(8, 0, True))
    c = h.counters()
    sample(c)
    h.error = OSError(5, "unreadable")
    sample(c)
    h.error = None
    h.disks["sda"] = disk(8, 0, False)
    assert sample(c)[0]["disks"] == []
