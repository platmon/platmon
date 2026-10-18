"""Who owns the snapshot: read() hands out independent copies, while /api/stats serializes the published
snapshot without copying it (Sampler._stats_json) and only bytes leave. Both must give the same answer."""
import copy
import functools
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from collector import core as collector
from runtime import sampler as sampler_module
from collector.common import CpuCounters
from runtime.sampler import Sampler
from runtime.server import make_handler
from tests.collector.test_common import idle_busy
from tests.collector.test_provenance import Tick, recorded  # noqa: F401 - the fixture
from tests.runtime.test_sampler import Clock, fake, slow

STATS = {"time": 1.5, "cpu": [{"id": 0, "usage": 1.0, "freq": None}], "temperature": {"a": 40.0},
         "nested": {"list": [{"deep": [1, 2, {"x": None}]}], "text": "보드 온도"},
         "collectors": {"temperature": {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0},
                        "power": {"state": "partial", "reason": "some_unreadable",
                                  "issues": [{"target": "hwmon0.power1", "reason": "io_error"}],
                                  "issues_truncated": 0}}}


def body(s):
    data, status = s._stats_json()
    return (None if data is None else json.loads(data)), status


def test_stats_json_is_read_serialized():
    """Same clock reading: the bytes equal json.dumps of read()'s stats, key order included."""
    c = Clock()
    s = fake(slow(c, copy.deepcopy(STATS)), c)
    s._attempt()
    c.advance(0.3)
    stats, status = s.read()
    data, status2 = s._stats_json()
    assert data == json.dumps(stats).encode() and status2 == status
    assert list(json.loads(data))[-3:] == ["schema_version", "sample", "collectors"]


def test_callers_changing_nested_values_do_not_reach_others():
    c = Clock()
    s = fake(slow(c, copy.deepcopy(STATS)), c)
    s._attempt()
    a, _ = s.read()
    a["nested"]["list"][0]["deep"][2]["x"] = "changed"
    a["collectors"]["power"]["issues"].clear()
    a["sample"]["sequence"] = 99
    b, _ = s.read()
    assert b["nested"]["list"][0]["deep"][2]["x"] is None and b["collectors"]["power"]["issues"]
    assert body(s)[0] == b and b["sample"]["sequence"] == 1


def test_the_collectors_own_dict_changed_later_does_not_reach_the_snapshot():
    c = Clock()
    mine = copy.deepcopy(STATS)
    s = fake(lambda: mine, c)
    s._attempt()
    before = body(s)[0]
    mine["nested"]["list"][0]["deep"].append(3)
    mine["temperature"]["a"] = 99.0
    del mine["collectors"]
    assert body(s)[0] == before and s.read()[0] == before


def test_serializing_does_not_change_the_snapshot():
    c = Clock()
    s = fake(slow(c, copy.deepcopy(STATS)), c)
    s._attempt()
    record = s._record
    frozen = copy.deepcopy(record)
    for _ in range(3):
        s._stats_json()
        s.read()
    assert s._record is record and record == frozen


def test_a_publish_during_serialization_does_not_mix_snapshots(monkeypatch):
    """A new snapshot published while the old one is being serialized: the body is the old one, whole."""
    n = iter(range(1, 100))
    c = Clock()
    s = fake(lambda: {"n": next(n), "list": [1, 2, 3]}, c)
    s._attempt()
    real = json.dumps

    def dumps_with_a_publish(obj, *a, **k):
        s._attempt()  # publishes sequence 2 while sequence 1 is serialized
        return real(obj, *a, **k)
    monkeypatch.setattr(sampler_module.json, "dumps", dumps_with_a_publish)
    old, status = body(s)
    monkeypatch.setattr(sampler_module.json, "dumps", real)
    assert old["n"] == old["sample"]["sequence"] == status["sample"]["sequence"] == 1
    new, _ = body(s)
    assert new["n"] == new["sample"]["sequence"] == 2


def test_concurrent_requests_for_one_sequence():
    c = Clock()
    s = fake(slow(c, copy.deepcopy(STATS)), c)
    s._attempt()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, False))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/stats"
    out, errors = [], []

    def get():
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                out.append(json.loads(r.read()))
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
    try:
        threads = [threading.Thread(target=get) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        httpd.shutdown()
    assert not errors and len(out) == 16
    assert all(o == out[0] for o in out) and out[0] == s.read()[0]  # frozen clock: equal, ages included


@pytest.mark.parametrize("case", ["ready", "degraded_attempt", "degraded_optional", "last_good", "stale",
                                  "starting", "starting_stale"])
def test_status_and_absence_match_read(case):
    c = Clock()
    fail = {"on": False}
    stats = copy.deepcopy(STATS)
    if case != "degraded_optional":
        stats["collectors"]["power"]["state"] = "ok"

    def collect():
        c.advance(0.1)
        if fail["on"]:
            raise OSError("gone")
        return stats
    s = fake(collect, c)
    if case not in ("starting", "starting_stale"):
        s._attempt()
    if case in ("degraded_attempt", "last_good"):
        fail["on"] = True
        s._attempt()
    if case == "stale":
        fail["on"] = True
        s._attempt()
        c.advance(10)
    if case == "starting_stale":
        s._collecting_since = c.ns
        c.advance(10)
    stats_read, status_read = s.read()
    data, status = body(s)
    assert status == status_read and data == stats_read
    expected = {"ready": "ready", "degraded_attempt": "degraded", "degraded_optional": "degraded",
                "last_good": "degraded", "stale": "stale", "starting": "starting", "starting_stale": "stale"}[case]
    assert status["state"] == expected and (data is None) == (expected in ("stale", "starting"))


def test_same_snapshot_twice_ages_move():
    c = Clock()
    s = fake(slow(c, copy.deepcopy(STATS)), c)
    s._attempt()
    first = body(s)[0]["sample"]
    c.advance(1.0)
    again = body(s)[0]["sample"]
    assert again["sequence"] == first["sequence"] and again["age_ms"] == first["age_ms"] + 1000


def test_sensor_meta_ids_and_reads_match_and_stay_owned(recorded):  # noqa: F811
    result, tick, p, _ = recorded
    s = Sampler(None, clock=(tick, {"source": "fake"}), wall=lambda: 1_700_000_000.0)
    cpu = CpuCounters(tick)
    p.readings += [{n: idle_busy(30, 30) for n in (0, 2, 5)}, {n: idle_busy(40, 40) for n in (0, 2, 5)}]
    s.collect = functools.partial(collector.collect_recorded, cpu, None, tick)
    s._attempt()
    s._attempt()
    a, _ = s.read()
    served = json.loads(s._stats_json()[0])
    assert served["sensor_meta"] == a["sensor_meta"] and served["sample"]["data_age_basis"] == "oldest_current_read_start"
    assert {k: v for k, v in served.items() if k != "sample"} == {k: v for k, v in a.items() if k != "sample"}
    ptr = next(iter(a["sensor_meta"]))
    a["sensor_meta"][ptr]["read"]["started_offset_ms"] = -1.0
    a["sensor_meta"][ptr]["id"] = "changed"
    b, _ = s.read()
    assert b["sensor_meta"] == served["sensor_meta"] == json.loads(s._stats_json()[0])["sensor_meta"]


class Odd(dict):
    """A dict subclass a generic collector might return; deep-copied as itself, serialized as a dict."""


def test_generic_collector_values():
    c = Clock()
    shared = [1, 2]
    stats = {"tuple": (1, 2), "odd": Odd(a=1), "alias": {"x": shared, "y": shared}}
    s = fake(lambda: stats, c)
    s._attempt()
    read, _ = s.read()
    assert isinstance(read["odd"], Odd) and read["tuple"] == (1, 2)
    assert read["alias"]["x"] is read["alias"]["y"]  # one deepcopy keeps aliases, as before
    assert s._stats_json()[0] == json.dumps(read).encode()


def test_unserializable_values_fail_as_before():
    """A collect() result JSON cannot represent: the HTTP path fails as json.dumps(read()) did."""
    c = Clock()
    s = fake(lambda: {"bad": {1, 2}}, c)
    s._attempt()
    with pytest.raises(TypeError):
        json.dumps(s.read()[0])
    with pytest.raises(TypeError):
        s._stats_json()
    assert s.read()[0]["bad"] == {1, 2}  # read() itself still works for in-process callers
