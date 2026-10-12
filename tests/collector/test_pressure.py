"""collector/pressure.py: the PSI parser, Pressure.sample and its place in the service. Fake files and
clocks; the last test reads the host's /proc/pressure where there is one (the boards platmon is checked on
have none, a CI host may)."""
import json
import os

import pytest

import platmon
from collector import metadata
from collector import sysfs
from collector.pressure import Pressure, parse
from runtime.sampler import Collected
from tests.collector.test_network_integration import ini_file, started
from tests.collector.test_provenance import at, good_required, recorded_with
from tests.runtime.test_sampler import Clock

SOME = "some avg10=1.25 avg60=0.50 avg300=0.10 total=123456"
FULL = "full avg10=0.00 avg60=0.00 avg300=0.00 total=0"


def test_parse_some_and_full():
    assert parse(f"{SOME}\n{FULL}\n") == {"some": {"avg10": 1.25, "avg60": 0.5, "avg300": 0.1, "total_us": 123456},
                                          "full": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total_us": 0}}
    assert parse(SOME) == {"some": {"avg10": 1.25, "avg60": 0.5, "avg300": 0.1, "total_us": 123456}}  # before full existed


@pytest.mark.parametrize("text", ["", FULL, f"{SOME}\n{SOME}", "some avg10=1 avg60=0.00 avg300=0.00 total=1",
                                  "some avg10=-1.00 avg60=0.00 avg300=0.00 total=1", "some avg10=1.00 total=1",
                                  f"{SOME} extra=1", "partial avg10=0.00 avg60=0.00 avg300=0.00 total=0"])
def test_parse_refuses_other_formats(text):
    with pytest.raises(ValueError):
        parse(text)


class Files:
    def __init__(self, **files):
        self.files, self.opened = files, []

    def read(self, path):
        self.opened.append(path)
        v = self.files[os.path.basename(path)]
        if isinstance(v, Exception):
            raise v
        return v


def sample(files, exists=True, clock=None):
    p = Pressure(clock or (lambda: 7), root="/p", read=files.read, exists=lambda path: exists)
    g = metadata.Group("pressure")
    out, span = p.sample(g)
    return out, span, g.status()


def test_all_three_with_cpu_full_left_out():
    f = Files(cpu=f"{SOME}\n{FULL}\n", memory=f"{SOME}\n{FULL}\n", io=f"{SOME}\n{FULL}\n")
    out, span, status = sample(f)
    assert list(out["resources"]) == ["cpu", "memory", "io"] and span == (7, 7) and status["state"] == "ok"
    assert out["resources"]["cpu"]["full"] is None  # undefined at the system level, not a measured 0
    assert out["resources"]["io"]["full"]["total_us"] == 0 and out["resources"]["io"]["some"]["avg10"] == 1.25
    assert out["scope"] == {"kind": "host"} and out["provider"] == "proc_pressure" and out["read"] is None


def test_no_pressure_directory_opens_nothing():
    f = Files()
    out, span, status = sample(f, exists=False)
    assert out["resources"] == {} and span is None and f.opened == []
    assert (status["state"], status["reason"]) == ("unavailable", "not_exposed")


@pytest.mark.parametrize("error, reason, state", [(FileNotFoundError(2, "x"), "not_exposed", "ok"),
                                                  (PermissionError(13, "x"), "permission_denied", "partial"),
                                                  ("garbage", "invalid_data", "partial")])
def test_one_unreadable_file_keeps_the_others(error, reason, state):
    f = Files(cpu=SOME, memory=error, io=SOME)
    out, _, status = sample(f)
    assert list(out["resources"]) == ["cpu", "io"] and status["state"] == state
    assert reason in {i["reason"] for i in status["issues"]} or reason == "not_exposed"


def test_all_unreadable_is_an_error_without_a_read():
    f = Files(cpu=OSError(5, "x"), memory=OSError(5, "x"), io=OSError(5, "x"))
    out, span, status = sample(f)
    assert out["resources"] == {} and span is None and status["state"] == "error"


def test_the_read_spans_the_three_files():
    t = iter(range(1, 100))
    out, span, _ = sample(Files(cpu=SOME, memory=SOME, io=SOME), clock=lambda: next(t))
    assert span == (1, 4)  # before the first read to after the last


def test_on_by_default_and_off_when_asked(monkeypatch, tmp_path):
    assert started(monkeypatch, ini_file(tmp_path, "[pressure]\nenabled = no\n")).args[5] is None
    collect = started(monkeypatch)
    assert isinstance(collect.args[5], Pressure) and collect.args[5].clock is collect.args[2]  # the service's clock


def test_disabled_reads_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(Pressure, "sample", lambda *a: calls.append(a))
    stats = started(monkeypatch, ini_file(tmp_path, "[pressure]\nenabled = no\n")).__call__().stats
    assert calls == [] and "pressure" not in stats and "pressure" not in stats["collectors"]


PSI = {"scope": {"kind": "host"}, "provider": "proc_pressure", "read": "forged",
       "resources": {"io": {"some": {"avg10": 1.0, "avg60": 0.0, "avg300": 0.0, "total_us": 1}, "full": None}}}


def answer(make):
    c = Clock()
    s = recorded_with(c, make)
    s._attempt()
    c.advance(0.2)
    return s.read()[0]


def test_pressure_read_counts_for_the_data_age_and_falls_back_alone():
    good = answer(lambda start, clock: Collected({"pressure": json.loads(json.dumps(PSI))}, {}, good_required(start), clock,
                                                 None, None, at(start, 20, 21)))
    assert (good["sample"]["data_age_ms"], good["sample"]["data_age_basis"]) == (480.0, "oldest_current_read_start")
    assert good["pressure"]["read"] == {"started_offset_ms": 20.0, "completed_offset_ms": 21.0}
    bad = answer(lambda start, clock: Collected({"pressure": json.loads(json.dumps(PSI))}, {}, good_required(start), clock,
                                                None, None, None))
    assert bad["sample"]["data_age_basis"] == "cycle_start_upper_bound" and bad["pressure"]["read"] is None
    empty = answer(lambda start, clock: Collected({"pressure": {**PSI, "resources": {}}}, {}, good_required(start), clock))
    assert empty["sample"]["data_age_basis"] == "oldest_current_read_start"  # nothing read: no fallback for others


def test_real_host_pressure():
    g = metadata.Group("pressure")
    out, span = Pressure().sample(g)
    if not os.path.isdir("/proc/pressure"):
        assert out["resources"] == {} and g.status()["reason"] == "not_exposed"
        pytest.skip("no /proc/pressure on this host (PSI not built in or disabled)")
    assert set(out["resources"]) <= {"cpu", "memory", "io"} and span is not None
    for r in out["resources"].values():
        assert 0 <= r["some"]["avg10"] <= 100 and r["some"]["total_us"] >= 0
