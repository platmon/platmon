import itertools
import json
import threading
import time
import uuid

import pytest

from collector.common import CpuCounters
from runtime.sampler import Sampler, pick_clock


def test_latest_follows_collect():
    n = itertools.count()
    s = Sampler(lambda: {"n": next(n)}, interval=0.01).start()
    first = s.latest()["n"]
    for _ in range(100):
        if s.latest()["n"] > first:
            break
        time.sleep(0.01)
    assert s.latest()["n"] > first
    s.stop()


def test_failed_collect_keeps_last_snapshot():
    calls = itertools.count()

    def collect():
        if next(calls) > 0:
            raise OSError("sensor gone")
        return {"ok": True}

    s = Sampler(collect, interval=0.01).start()
    assert s.latest() == {"ok": True}
    time.sleep(0.05)  # several failing rounds
    assert s.latest() == {"ok": True}
    s.stop()


def test_no_snapshot_yet():
    def collect():
        raise OSError("never works")

    s = Sampler(collect, interval=0.01).start()
    assert s.latest(timeout=0.05) is None
    s.stop()


def test_stale_snapshot_is_not_served():
    """collect keeps failing after one success: the old snapshot counts until stale_after, then None."""
    ok = [True]

    def collect():
        if not ok[0]:
            raise OSError("sensor gone")
        return {"ok": True}

    s = Sampler(collect, interval=0.01, stale_after=0.1).start()
    assert s.latest() == {"ok": True}
    ok[0] = False
    time.sleep(0.2)
    assert s.latest() is None
    ok[0] = True  # recovers as soon as collect works again
    time.sleep(0.05)
    assert s.latest() == {"ok": True}
    s.stop()


def test_default_stale_after():
    assert Sampler(dict, interval=1.0).stale_after == 5.0  # floor, so a slow collect alone does not trip it
    assert Sampler(dict, interval=10.0).stale_after == 30.0


# sample metadata and runtime status, driven by a fake clock: _attempt() is one collection round


class Clock:
    def __init__(self):
        self.ns, self.wall = 10**12, 1_700_000_000.0

    def advance(self, seconds, wall=None):
        """Moves elapsed time; the wall clock moves by `wall` if given (a clock step), else by the same."""
        self.ns += round(seconds * 1e9)
        self.wall += seconds if wall is None else wall


def fake(collect, clock, **kw):
    return Sampler(collect, clock=(lambda: clock.ns, {"source": "fake", "suspend_aware": False}),
                   wall=lambda: clock.wall, **kw)


def slow(clock, stats, seconds=0.25):
    """A collect that takes `seconds` of (fake) time."""
    def collect():
        clock.advance(seconds)
        return stats
    return collect


STATS = {"time": 1234.5, "cpu": [{"id": 0, "usage": 0.0, "freq": None}], "gpu": None}  # a measured 0 stays 0


def test_metadata_is_added_and_stats_are_unchanged():
    c = Clock()
    s = fake(slow(c, STATS), c)
    s._attempt()
    c.advance(0.1)
    stats, status = s.read()
    assert {k: v for k, v in stats.items() if k not in ("schema_version", "sample", "collectors")} == STATS
    assert stats["schema_version"] == 1 and stats["collectors"] == {"core": {"state": "ok", "reason": None}}
    assert stats["sample"] == {
        "instance_id": s.instance_id, "sequence": 1,
        "started_at": 1_700_000_000.0, "completed_at": 1_700_000_000.25,  # completed after collect returned
        "duration_ms": 250.0, "age_ms": 100.0, "cycle_age_ms": 350.0, "data_age_ms": 350.0,
        "data_age_basis": "cycle_start_upper_bound",  # a plain dict: no read records
        "interval_ms": 1000.0, "stale_after_ms": 5000.0}
    sample = stats["sample"]
    assert sample["data_age_ms"] == sample["age_ms"] + sample["duration_ms"]
    assert s.latest() == STATS  # the old interface: no metadata
    assert status["state"] == "ready" and status["ready"] is True


def test_polling_does_not_change_the_sample():
    c = Clock()
    s = fake(slow(c, STATS), c)
    s._attempt()
    first = s.read()[0]["sample"]
    c.advance(1)
    again = s.read()[0]["sample"]
    assert again["sequence"] == first["sequence"] == 1 and again["completed_at"] == first["completed_at"]
    assert again["age_ms"] == first["age_ms"] + 1000  # only the ages move


def test_sequence_counts_published_snapshots_only():
    c = Clock()
    mode = ["ok"]

    def collect():
        c.advance(6 if mode[0] == "slow" else 0.1)
        if mode[0] == "fail":
            raise OSError("sensor gone")
        return {"n": 1}

    s = fake(collect, c)
    s._attempt()
    assert s.read()[1]["sample"]["sequence"] == 1
    for m, reason in (("fail", "collection_failed"), ("slow", "collection_too_slow")):
        mode[0] = m
        s._attempt()
        assert s.read()[1]["last_attempt"]["reason"] == reason
        assert s.read()[1]["sample"]["sequence"] == 1
    mode[0] = "ok"
    s._attempt()
    assert s.read()[1]["sample"]["sequence"] == 2


def test_each_sampler_is_a_new_instance():
    c = Clock()
    a, b = fake(dict, c), fake(dict, c)
    a._attempt(), a._attempt(), b._attempt()
    assert a.instance_id != b.instance_id and str(uuid.UUID(a.instance_id)) == a.instance_id
    assert (a.read()[0]["sample"]["sequence"], b.read()[0]["sample"]["sequence"]) == (2, 1)


@pytest.mark.parametrize("step", [-3600, 3600])
def test_wall_clock_steps_do_not_change_durations(step):
    c = Clock()

    def collect():
        c.advance(0.25, wall=step)  # e.g. NTP corrects the clock mid-collect
        return {}

    s = fake(collect, c)
    s._attempt()
    c.advance(4, wall=-step)
    sample = s.read()[0]["sample"]
    assert sample["completed_at"] - sample["started_at"] == pytest.approx(step)
    assert (sample["duration_ms"], sample["age_ms"], sample["data_age_ms"]) == (250.0, 4000.0, 4250.0)


def test_stale_boundary():
    c = Clock()
    s = fake(slow(c, {}), c, stale_after=5)
    s._attempt()
    c.advance(4.75)  # data age exactly stale_after: still current
    assert s.read()[0]["sample"]["data_age_ms"] == 5000.0
    c.ns += 1
    stats, status = s.read()
    assert stats is None and status["state"] == "stale" and status["ready"] is False
    assert status["sample"]["sequence"] == 1  # still described, no longer served


def test_too_slow_result_is_not_published():
    c = Clock()
    s = fake(slow(c, {"x": 1}, seconds=5.001), c)
    s._attempt()
    stats, status = s.read()
    assert stats is None and status["sample"] is None and status["state"] == "stale"
    assert status["last_attempt"] == {"state": "error", "reason": "collection_too_slow",
                                      "completed_at": 1_700_000_005.001, "duration_ms": 5001.0,
                                      "consecutive_failures": 1}


def test_failure_keeps_last_good_then_recovers():
    c = Clock()
    ok = [True]

    def collect():
        c.advance(0.05)
        if not ok[0]:
            raise OSError("/secret/path gone")
        return {"v": c.ns}

    s = fake(collect, c)
    s._attempt()
    good = s.read()[0]
    c.advance(1)
    ok[0] = False
    s._attempt()
    s._attempt()
    stats, status = s.read()
    assert stats["v"] == good["v"] and stats["sample"]["sequence"] == 1  # same values, times, sequence
    assert stats["sample"]["completed_at"] == good["sample"]["completed_at"]
    assert stats["collectors"]["core"]["state"] == "ok"  # the snapshot's own state, not the latest attempt's
    assert status["state"] == "degraded" and status["ready"] is True
    assert status["last_attempt"]["reason"] == "collection_failed"
    assert status["last_attempt"]["consecutive_failures"] == 2
    assert "secret" not in json.dumps(status)  # no exception text in the API
    c.advance(5)
    stats, status = s.read()
    assert stats is None and status["state"] == "stale" and status["sample"]["sequence"] == 1
    ok[0] = True
    s._attempt()
    stats, status = s.read()
    assert stats["sample"]["sequence"] == 2 and stats["v"] != good["v"]
    assert status["state"] == "ready" and status["last_attempt"]["consecutive_failures"] == 0


def test_starting_status():
    c = Clock()
    status = fake(dict, c).read()[1]
    assert status["state"] == "starting" and status["ready"] is False
    assert status["sample"] is None and status["last_attempt"] is None and status["collecting_for_ms"] is None
    assert status["clock"] == {"source": "fake", "suspend_aware": False}


def test_blocked_first_collect_turns_stale():
    """status answers at once while the first collect hangs, and says how long it has been running."""
    c = Clock()
    entered, release = threading.Event(), threading.Event()

    def collect():
        entered.set()
        release.wait(10)
        return {}

    s = fake(collect, c).start()
    try:
        assert entered.wait(5)
        assert s.read()[1]["collecting_for_ms"] == 0.0
        c.advance(5)
        status = s.read()[1]
        assert (status["state"], status["collecting_for_ms"], status["last_attempt"]) == ("starting", 5000.0, None)
        c.ns += 1
        assert s.read()[1]["state"] == "stale"
        assert s.read(timeout=0.01) == (None, s.read()[1])  # a bounded wait, then no stats
    finally:
        s.stop()
        release.set()
        s._thread.join(5)


def test_returned_dicts_are_copies():
    c = Clock()
    shared = {"cpu": [{"id": 0, "usage": 1.0}]}
    s = fake(lambda: shared, c)
    s._attempt()
    before = s.read()
    stats, status = s.read()
    stats["cpu"][0]["usage"] = 99.0
    stats["sample"]["sequence"] = 99
    status["clock"]["source"] = "changed"
    status["sample"]["sequence"] = 99
    shared["cpu"][0]["usage"] = 50.0  # the collector reusing its own dict
    s.latest()["cpu"].append("x")
    assert s.read() == before


def test_reads_match_their_metadata_while_collecting():
    """Values and metadata come from one capture: collect returns n == the sequence it will be published as."""
    n = itertools.count(1)
    s = Sampler(lambda: {"n": next(n)}, interval=0.001).start()
    try:
        seen = set()
        for _ in range(2000):
            stats = s.read(timeout=5)[0]
            assert stats["n"] == stats["sample"]["sequence"]
            seen.add(stats["n"])
        assert len(seen) > 1
    finally:
        s.stop()


def test_start_twice_is_one_writer_and_restart_is_refused():
    s = Sampler(dict, interval=10).start()
    t = s._thread
    assert s.start()._thread is t
    s.stop()
    with pytest.raises(RuntimeError, match="create a new Sampler"):
        s.start()


def test_concurrent_start_makes_one_writer(monkeypatch):
    """Two callers race into start(): the first is held while it creates the writer thread, the second
    arrives meanwhile. Only one writer may be created and only one collect may run."""
    real = threading.Thread
    creating, go, second = threading.Event(), threading.Event(), threading.Event()
    created, collects, collected = [], [], threading.Event()

    class Gated(real):
        def __init__(self, *args, **kw):
            if kw.get("name") == "sampler":
                created.append(self)
                if len(created) == 1:
                    creating.set()
                    go.wait(5)
                else:
                    second.set()
            super().__init__(*args, **kw)

    s = Sampler(lambda: collects.append(1) or collected.set() or {}, interval=10)
    callers = [real(target=s.start) for _ in range(2)]
    monkeypatch.setattr(threading, "Thread", Gated)
    try:
        callers[0].start()
        assert creating.wait(5)
        callers[1].start()
        assert not second.wait(0.2)  # the second caller must not get to create a thread at all
        go.set()
        for t in callers:
            t.join(5)
        assert collected.wait(5)
    finally:
        go.set()
        s.stop()
        for t in created:
            t.join(5)  # stop() ends a writer after its round; bounded
    assert len(created) == 1 and s._thread is created[0]
    assert collects == [1]


def test_pick_clock(monkeypatch):
    if hasattr(time, "CLOCK_BOOTTIME"):
        assert pick_clock()[1] == {"source": "boottime", "suspend_aware": True}
    monkeypatch.delattr(time, "CLOCK_BOOTTIME", raising=False)
    assert pick_clock() == (time.monotonic_ns, {"source": "monotonic", "suspend_aware": False})


def test_pick_clock_when_boottime_fails(monkeypatch):
    def unsupported(clock):
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(time, "clock_gettime_ns", unsupported, raising=False)
    assert pick_clock()[1]["source"] == "monotonic"


def test_cpu_baseline_follows_readings_not_publishes(monkeypatch):
    """The service's CPU window runs from the last successful /proc/stat reading, even when that collection
    then failed or was dropped, so it is never stretched over a publish gap; a long one is a "gap"."""
    import functools

    from collector import core as collector
    from collector import common
    from tests.collector.test_common import Proc, idle_busy

    c = Clock()
    p = Proc(monkeypatch)
    cpu = CpuCounters(lambda: c.ns, max_gap=5.0)
    mode = ["ok"]

    def meminfo():  # runs after the CPU reading in common.collect()
        if mode[0] == "fail":
            raise OSError("memory unreadable")
        if mode[0] == "slow":
            c.advance(6)
        return {"MemTotal": 8, "MemAvailable": 4, "SwapTotal": 0, "SwapFree": 0}

    monkeypatch.setattr(common, "meminfo", meminfo)
    s = fake(functools.partial(collector.collect, cpu), c)

    def attempt(after_s, busy, m="ok"):
        c.advance(after_s)
        p.readings.append({0: idle_busy(busy, busy)})
        mode[0] = m
        s._attempt()
        return s.read()

    stats, _ = attempt(0, 0)
    assert stats["cpu"] == [] and stats["cpu_sampling"]["unavailable"] == [{"id": 0, "reason": "warmup"}]
    stats, status = attempt(1, 10, "fail")  # CPU read, then the collection failed: nothing published
    assert stats["sample"]["sequence"] == 1 and stats["cpu"] == [] and status["state"] == "degraded"
    reads = p.reads
    for _ in range(3):  # polling neither reads /proc/stat nor changes what was published
        assert s.read()[0]["cpu_sampling"] == stats["cpu_sampling"]
    assert p.reads == reads
    stats, _ = attempt(1, 20)  # compared with the failed attempt's reading, 1 s ago, not the published one
    assert stats["sample"]["sequence"] == 2 and stats["cpu_sampling"]["window_ms"] == 1000.0
    assert [c_["usage"] for c_ in stats["cpu"]] == [50.0]
    _, status = attempt(1, 30, "slow")  # dropped as too slow; its reading is 7 s old by the next one
    assert status["last_attempt"]["reason"] == "collection_too_slow"
    stats, _ = attempt(1, 40)
    assert stats["cpu"] == [] and stats["cpu_sampling"] == {
        "mode": "interval", "window_ms": 7000.0, "unavailable": [{"id": 0, "reason": "gap"}]}
    assert stats["sample"]["duration_ms"] == 0.0  # the snapshot itself is new; only the CPU window was long
    earlier = s.read()[0]
    stats, _ = attempt(1, 50)
    assert [c_["usage"] for c_ in stats["cpu"]] == [50.0] and stats["sample"]["sequence"] == 4
    assert earlier["cpu_sampling"]["unavailable"] == [{"id": 0, "reason": "gap"}]  # earlier copies unchanged


@pytest.mark.parametrize("step", [-3600, 3600])
def test_cpu_window_ignores_wall_clock_steps(monkeypatch, step):
    from tests.collector.test_common import Proc, idle_busy

    c = Clock()
    p = Proc(monkeypatch)
    cpu = CpuCounters(lambda: c.ns, max_gap=5.0)

    def collect():
        usage, sampling = cpu.sample()
        return {"cpu": [{"id": n, "usage": u, "freq": None} for n, u in usage.items()], "cpu_sampling": sampling}

    s = fake(collect, c)
    p.readings += [{0: idle_busy(0, 0)}, {0: idle_busy(30, 10)}]
    s._attempt()
    c.advance(1.25, wall=step)  # NTP steps the wall clock between the two readings
    s._attempt()
    stats = s.read()[0]
    assert stats["cpu_sampling"]["window_ms"] == 1250.0 and stats["cpu"][0]["usage"] == 25.0


def test_broken_cpu_reading_fails_the_collection(tmp_path, monkeypatch):
    """A broken /proc/stat reading is collection_failed (not published), first or later; the next valid
    reading is published with warmup, and the one after that is measured."""
    import functools

    from collector import core as collector
    from tests.collector.test_common import stat_files, without_host_sensors

    bad, good = "cpu0 1 0 0 1", ["cpu0 %d 0 0 %d 0 0 0 0" % (n, n) for n in range(10, 60, 10)]
    stat_files(tmp_path, monkeypatch, [bad, good[0], good[1], bad, good[2], good[3]])
    without_host_sensors(monkeypatch, tmp_path)
    c = Clock()
    s = fake(functools.partial(collector.collect, CpuCounters(lambda: c.ns, max_gap=5.0)), c)

    def attempt():
        c.advance(1)
        s._attempt()
        return s.read()

    stats, status = attempt()  # broken first reading: nothing published, sequence not started
    assert stats is None and status["sample"] is None
    assert status["last_attempt"]["reason"] == "collection_failed"
    stats, status = attempt()  # recovery: published, CPUs warming up
    assert stats["sample"]["sequence"] == 1 and stats["cpu"] == [] and status["state"] == "ready"
    assert stats["cpu_sampling"]["unavailable"] == [{"id": 0, "reason": "warmup"}]
    good_stats = attempt()[0]
    assert good_stats["sample"]["sequence"] == 2 and [x["usage"] for x in good_stats["cpu"]] == [50.0]
    stats, status = attempt()  # broken again: last good values, times and sequence stay
    assert status["state"] == "degraded" and stats == {**good_stats, "sample": {
        **good_stats["sample"], "age_ms": 1000.0, "cycle_age_ms": 1000.0, "data_age_ms": 1000.0}}
    stats = attempt()[0]
    assert stats["sample"]["sequence"] == 3 and stats["cpu"] == []
    assert stats["cpu_sampling"]["unavailable"] == [{"id": 0, "reason": "warmup"}]
    stats = attempt()[0]
    assert stats["sample"]["sequence"] == 4 and stats["cpu_sampling"]["window_ms"] == 1000.0


# optional collector groups: kept as collect() returned them; partial/error make a fresh snapshot "degraded"

def group(state, reason=None, issues=()):
    return {"state": state, "reason": reason, "issues": list(issues), "issues_truncated": 0}


def test_optional_collectors_are_kept_and_core_is_the_samplers():
    c = Clock()
    returned = [{"v": 1, "collectors": {"core": group("error", "made_up"), "temperature": group(
        "partial", "some_unreadable", [{"target": "hwmon0.temp2", "reason": "invalid_data"}]),
        "gpu": group("unavailable", "unsupported_platform")}}]
    s = fake(lambda: returned[0], c)
    s._attempt()
    stats, status = s.read()
    assert list(stats["collectors"]) == ["core", "temperature", "gpu"]
    assert stats["collectors"]["core"] == {"state": "ok", "reason": None}  # collect() cannot override core
    assert stats["collectors"]["temperature"]["issues"] == [{"target": "hwmon0.temp2", "reason": "invalid_data"}]
    assert (status["state"], status["ready"]) == ("degraded", True)
    assert status["last_attempt"]["state"] == "ok" and status["last_attempt"]["consecutive_failures"] == 0
    assert s.latest() == {"v": 1, "collectors": {k: v for k, v in returned[0]["collectors"].items() if k != "core"}}
    # the same sample polled again is the same, and changing what was returned changes nothing
    stats["collectors"]["temperature"]["state"] = "changed"
    returned[0]["collectors"]["temperature"]["state"] = "changed"
    c.advance(1)
    again = s.read()[0]
    assert again["collectors"]["temperature"]["state"] == "partial" and again["sample"]["sequence"] == 1
    # recovered in the next collection: a new sample, ready again
    returned[0] = {"v": 2, "collectors": {"temperature": group("ok"), "gpu": group("unavailable", "not_detected")}}
    s._attempt()
    stats, status = s.read()
    assert stats["sample"]["sequence"] == 2 and status["state"] == "ready"


@pytest.mark.parametrize("collectors, expected", [
    ({"fans": group("unavailable", "not_detected"), "gpu": group("unavailable", "unsupported_platform")}, "ready"),
    ({"gpu": group("error", "io_error")}, "degraded"),
    ("not a dict", "ready"),                       # anything else from a generic callable is ignored
    ({"odd": "not a dict", "x": {"state": 5}}, "ready"),
])
def test_status_from_optional_collectors(collectors, expected):
    c = Clock()
    s = fake(lambda: {"v": 1, "collectors": collectors}, c)
    s._attempt()
    stats, status = s.read()
    assert status["state"] == expected and stats["collectors"]["core"]["state"] == "ok"


def test_all_optional_failing_still_publishes_then_required_failure_keeps_last_good(tmp_path, monkeypatch):
    """Every optional sensor unreadable: a current snapshot (200), ready but degraded, last attempt ok.
    Then memory (required) fails: collection_failed, and the last good snapshot stays with its collectors."""
    import errno
    import functools

    from collector import core as collector
    from collector import common, sysfs
    from tests.collector.test_common import Proc, fail_reads, fake_hwmon, idle_busy, without_host_accelerators

    monkeypatch.setattr(collector, "BOARD", None)  # a plain Linux host: a board module fills groups of its own
    without_host_accelerators(monkeypatch, tmp_path)  # this host's own devices would answer otherwise
    root = fake_hwmon(tmp_path, [{"name": "chip", "temp1_input": 1, "power1_input": 1, "fan1_input": 1}])
    monkeypatch.setattr(common, "hwmon_chips", lambda r, *groups: [(f"{root}/hwmon0", "chip")])
    monkeypatch.setattr(common, "thermal_zones", lambda group=None: ({}, set(), set()))
    freq = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
    # the fan is denied, not EIO: an EIO fan speed is a board without a tachometer, not a failure
    fail_reads(monkeypatch, {**{f"{root}/hwmon0/{f}": errno.EIO for f in ("temp1_input", "power1_input")},
                            f"{root}/hwmon0/fan1_input": errno.EACCES, freq: errno.EACCES})
    c = Clock()
    p = Proc(monkeypatch)
    s = fake(functools.partial(collector.collect, CpuCounters(lambda: c.ns, max_gap=5.0)), c)
    for busy in (0, 10):
        c.advance(1)
        p.readings.append({0: idle_busy(busy, busy)})
        s._attempt()
    stats, status = s.read()
    assert stats is not None and stats["cpu"] == [{"id": 0, "usage": 50.0, "freq": None}]
    assert {k: v["state"] for k, v in stats["collectors"].items()} == {
        "core": "ok", "cpu_frequency": "error", "gpu": "unavailable", "npu": "unavailable",
        "temperature": "error", "power": "error",
        "fans": "error", "power_mode": "unavailable", "board_info": "unavailable"}
    assert stats["fans"] == [{"name": "chip fan1", "rpm": None, "percent": None}]
    assert (status["state"], status["ready"], status["last_attempt"]["state"]) == ("degraded", True, "ok")
    assert str(tmp_path) not in json.dumps(stats["collectors"])

    def no_memory():
        raise OSError("meminfo")
    monkeypatch.setattr(common, "meminfo", no_memory)
    c.advance(1)
    p.readings.append({0: idle_busy(20, 20)})
    s._attempt()
    again, status = s.read()
    assert status["last_attempt"]["reason"] == "collection_failed" and status["state"] == "degraded"
    assert again["sample"]["sequence"] == stats["sample"]["sequence"]
    assert again["collectors"] == stats["collectors"]  # the last good snapshot as it was
