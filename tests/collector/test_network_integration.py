"""Network in the service: config, collect()/collect_recorded(), the Sampler's checks and the HTTP answers.
Fake clocks and counters; core values come from the host as in test_sampler.py."""
import functools
import json
import threading

import pytest

from collector import core as collector
import platmon
from collector import common
from collector.common import CpuCounters
from collector.network import NetworkCounters
from runtime.sampler import REQUIRED_READS, Collected, Sampler
from tests.collector.test_common import Proc, idle_busy
from tests.collector.test_network import Host
from tests.collector.test_provenance import BASE, MS, at, good_required, recorded_with
from tests.runtime.test_sampler import Clock, fake
from tests.frontends.test_server import get, serve

S = 10**9


# ---------- config and wiring ----------

def started(monkeypatch, ini=None):
    """platmon.main() up to the Sampler: the collect function it would run, nothing started."""
    made = {}

    class Stop(Exception):
        pass

    def sampler(collect, interval, clock):
        made["collect"] = collect
        raise Stop
    monkeypatch.setattr(platmon, "Sampler", sampler)
    monkeypatch.setattr(platmon.signal, "signal", lambda *a: None)
    with pytest.raises(Stop):
        platmon.main([ini] if ini else [])
    return made["collect"]


def ini_file(tmp_path, text):
    ini = tmp_path / "p.ini"
    ini.write_text(text)
    return str(ini)


@pytest.mark.parametrize("text", [None, "[core]\ninterval = 1\n", "[network]\n", "[network]\nenabled = yes\n"])
def test_on_unless_disabled_and_kept_by_the_service(monkeypatch, tmp_path, text):
    """No config file, an INI without [network] (from before it existed), an empty [network], or yes."""
    collect = started(monkeypatch, None if text is None else ini_file(tmp_path, text))
    assert collect.func is collector.collect_recorded
    cpu, logged, clock, network, *_ = collect.args
    assert isinstance(network, NetworkCounters) and network.clock is clock is cpu._clock
    assert network._max_gap == cpu._max_gap == 5 * S


@pytest.mark.parametrize("text", ["[network]\nenabled = no\n", "[core]\ninterval = 2\n[network]\nenabled = false\n"])
def test_disabled_never_reads(monkeypatch, tmp_path, text):
    """An explicit no is kept: no counters made, nothing read."""
    made, calls = [], []
    monkeypatch.setattr(platmon, "NetworkCounters", lambda *a, **k: made.append(a))
    collect = started(monkeypatch, ini_file(tmp_path, text))
    assert collect.args[3] is None and made == []
    monkeypatch.setattr(NetworkCounters, "sample", lambda *a: calls.append(a))
    stats = collect().stats
    assert calls == [] and "network" not in stats and "network" not in stats["collectors"]


@pytest.mark.parametrize("text, error", [("[network]\nenabled = maybe\n", "is not a valid bool"),
                                         ("[network]\ninterval = 5\n", "unknown key")])
def test_bad_network_config_is_refused(tmp_path, text, error):
    ini = tmp_path / "p.ini"
    ini.write_text(text)
    with pytest.raises(ValueError, match=error):
        platmon.load_config(str(ini))


def test_network_default_is_on():
    assert platmon.load_config()["network"].getboolean("enabled")


def test_direct_collect_without_network_is_unchanged():
    stats = collector.collect()
    assert "network" not in stats and "network" not in stats["collectors"]


def test_direct_collect_with_network_has_no_read_time():
    stats = collector.collect(network=NetworkCounters())
    assert stats["network"]["read"] is None and stats["collectors"]["network"]["state"] in ("ok", "partial", "unavailable")


# ---------- collect_recorded with a fake network ----------

@pytest.fixture
def service(monkeypatch, tmp_path):
    """collect_recorded with fake /proc/stat and fake counters, all on one fake clock, in a fake Sampler."""
    from tests.collector.test_common import without_host_sensors
    without_host_sensors(monkeypatch, tmp_path)
    c = Clock()
    clock = lambda: c.ns  # noqa: E731 - the one clock everything uses
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    h.clock = clock
    p = Proc(monkeypatch)
    cpu = CpuCounters(clock, max_gap=5.0)
    net = h.counters()
    s = Sampler(functools.partial(collector.collect_recorded, cpu, {}, clock, net),
                clock=(clock, {"source": "fake"}), wall=lambda: c.wall)

    def attempt(rx=0):
        c.advance(1)
        h.ifaces["eth0"] = (2, (rx, 0, 0, 0), (0, 0, 0, 0))
        p.readings.append({0: idle_busy(c.ns // S, c.ns // S)})
        s._attempt()
    return s, attempt, h, c, net


def test_service_publishes_rates_with_their_read(service):
    s, attempt, h, c, _ = service
    attempt()
    attempt(rx=1000)
    stats, status = s.read()
    eth = stats["network"]["interfaces"][0]
    assert eth["rates"]["rx_bytes_per_s"] == 1000.0 and eth["window_ms"] == 1000.0
    assert stats["network"]["read"]["started_offset_ms"] <= stats["network"]["read"]["completed_offset_ms"]
    assert stats["collectors"]["network"]["state"] == "ok" and status["state"] == "ready"
    assert stats["sample"]["data_age_basis"] == "oldest_current_read_start"


def test_network_failure_keeps_the_core_going(service, capsys):
    s, attempt, h, c, _ = service
    attempt()
    h.error = PermissionError(13, "Permission denied")
    attempt()
    stats, status = s.read()
    assert stats["sample"]["sequence"] == 2 and stats["network"]["interfaces"] == [] and stats["network"]["read"] is None
    assert stats["collectors"]["network"]["state"] == "error"
    assert (status["state"], status["ready"], status["last_attempt"]["state"]) == ("degraded", True, "ok")
    assert stats["sample"]["data_age_basis"] == "oldest_current_read_start"  # no values, nothing to fall back for
    attempt()
    assert capsys.readouterr().err.count("network:") == 1  # a lasting failure is logged once
    h.error = None
    attempt()
    assert s.read()[0]["network"]["interfaces"][0]["reason"] == "warmup"  # never values from before the failure


def test_unexpected_network_error_is_contained(service, monkeypatch):
    s, attempt, h, c, net = service
    attempt()
    monkeypatch.setattr(net, "_indexes", lambda: 1 / 0)
    attempt()
    stats, status = s.read()
    assert stats["sample"]["sequence"] == 2 and stats["network"]["interfaces"] == []
    assert stats["collectors"]["network"] == {"state": "error", "reason": "internal_error",
                                              "issues": [{"target": "network", "reason": "internal_error"}],
                                              "issues_truncated": 0}
    assert net._last is None


def test_core_failure_keeps_the_last_good_network_and_reads_nothing(service, monkeypatch):
    s, attempt, h, c, _ = service
    attempt()
    attempt(rx=500)
    good = s.read()[0]
    reads = h.reads
    monkeypatch.setattr(common, "meminfo", lambda: 1 / 0)
    attempt(rx=900)
    stats, status = s.read()
    assert h.reads == reads  # the collection failed before the network part: no observation made up
    assert status["last_attempt"]["reason"] == "collection_failed"
    assert stats["network"] == good["network"] and stats["sample"]["sequence"] == good["sample"]["sequence"]
    c.advance(10)
    httpd, url = serve(None, sampler=s)
    try:
        code, _, body = get(url + "/api/stats")
    finally:
        httpd.shutdown()
    assert code == 503 and b"interfaces" not in body and b"eth0" not in body


def test_a_dropped_slow_collection_still_is_the_next_baseline(service):
    s, attempt, h, c, net = service
    attempt()
    slow = s.collect

    def too_slow():
        out = slow()
        c.advance(1)
        return out
    s._stale_ns = S // 2  # a 1 s collection is dropped, the next window stays under the gap limit
    s.collect = too_slow
    attempt(rx=100)
    assert s._attempt_result["reason"] == "collection_too_slow" and s.read()[1]["sample"]["sequence"] == 1
    s.collect = slow
    attempt(rx=1200)
    eth = s.read()[0]["network"]["interfaces"][0]
    assert eth["reason"] is None and eth["window_ms"] == 2000.0  # from the dropped reading, not the published
    assert eth["rates"]["rx_bytes_per_s"] == 550.0


# ---------- the Sampler's checks of the network read ----------

NET = {"scope": {"kind": "process_network_namespace", "id": "netns1:x"}, "provider": "proc_net_dev",
       "interfaces": [{"name": "eth0", "ifindex": 2, "rx": {}, "tx": {}, "rates": None, "window_ms": None,
                       "reason": "warmup"}], "read": "forged"}


def answer(make):
    """Cycle 0-300 ms, required reads from 100 ms, answer at 500 ms."""
    c = Clock()
    s = recorded_with(c, make)
    s._attempt()
    c.advance(0.2)
    return s.read()[0]


def test_network_read_counts_for_the_data_age():
    stats = answer(lambda start, clock: Collected({"network": json.loads(json.dumps(NET))}, {}, good_required(start),
                                                  clock, at(start, 20, 21)))
    assert (stats["sample"]["data_age_ms"], stats["sample"]["data_age_basis"]) == (480.0, "oldest_current_read_start")
    assert stats["network"]["read"] == {"started_offset_ms": 20.0, "completed_offset_ms": 21.0}


@pytest.mark.parametrize("span", ["missing", "reversed", "future", "before", "float", "clock"])
def test_a_bad_network_read_falls_back(span, capsys):
    def make(start, clock):
        spans = {"missing": None, "reversed": at(start, 21, 20), "future": at(start, 20, 400),
                 "before": at(start, -1, 21), "float": (start + 20.0 * MS, start + 21 * MS), "clock": at(start, 20, 21)}
        return Collected({"network": json.loads(json.dumps(NET))}, {}, good_required(start),
                         (lambda: 0) if span == "clock" else clock, spans[span])
    stats = answer(make)
    assert (stats["sample"]["data_age_ms"], stats["sample"]["data_age_basis"]) == (500.0, "cycle_start_upper_bound")
    assert stats["network"]["read"] is None  # never the raw or forged time
    assert stats["network"]["interfaces"][0]["name"] == "eth0"  # the values are kept
    assert "data age counts from the collection start" in capsys.readouterr().err


def test_a_failed_network_reading_does_not_move_the_others_to_the_fallback():
    empty = dict(NET, interfaces=[])
    stats = answer(lambda start, clock: Collected({"network": dict(empty)}, {}, good_required(start), clock, None))
    assert (stats["sample"]["data_age_ms"], stats["sample"]["data_age_basis"]) == (400.0, "oldest_current_read_start")
    assert stats["network"]["read"] is None


def test_a_network_record_without_network_falls_back():
    stats = answer(lambda start, clock: Collected({"temperature": {}}, {}, good_required(start), clock, at(start, 20, 21)))
    assert stats["sample"]["data_age_basis"] == "cycle_start_upper_bound" and "network" not in stats


def test_unusable_records_never_publish_the_collected_read():
    stats = answer(lambda start, clock: Collected({"network": json.loads(json.dumps(NET))}, "x", "y", clock, 5))
    assert stats["network"]["read"] is None and stats["sample"]["data_age_basis"] == "cycle_start_upper_bound"


def test_constructor_without_network_is_compatible():
    c = Collected({}, {}, dict.fromkeys(REQUIRED_READS), None)
    assert c.network is None


# ---------- ownership and answers ----------

def test_same_sample_polled_again_and_copies_are_independent(service):
    s, attempt, h, c, _ = service
    attempt()
    attempt(rx=100)
    first = s.read()[0]
    first["network"]["interfaces"][0]["rx"]["bytes"] = -1
    first["network"]["interfaces"].clear()
    c.advance(0.5)
    again, body = s.read()[0], s._stats_json()[0]
    assert again["sample"]["sequence"] == first["sample"]["sequence"]
    assert again["sample"]["data_age_ms"] > first["sample"]["data_age_ms"]  # the time moves, the values not
    assert again["network"]["interfaces"][0]["rx"]["bytes"] == 100
    assert json.loads(body) == again
    assert s.latest()["network"] == again["network"]


def test_serializing_while_collecting(service):
    s, attempt, h, c, _ = service
    attempt()
    done, bodies = threading.Event(), []

    def poll():
        while not done.is_set():
            bodies.append(s._stats_json()[0])
    t = threading.Thread(target=poll)
    t.start()
    for rx in range(100, 2100, 100):
        attempt(rx=rx)
    done.set()
    t.join()
    for b in bodies:  # every body is one whole sample
        stats = json.loads(b)
        eth = stats["network"]["interfaces"][0]
        assert stats["sample"]["sequence"] == 1 or eth["rx"]["bytes"] == 100 * (stats["sample"]["sequence"] - 1)


def test_http_answers_with_network(service):
    s, attempt, *_ = service
    attempt()
    attempt(rx=100)
    httpd, url = serve(None, sampler=s)
    try:
        answers = {p: get(url + p) for p in ("/api/stats", "/api/status", "/text", "/nope")}
    finally:
        httpd.shutdown()
    for path, ctype in (("/api/stats", "application/json"), ("/api/status", "application/json"),
                        ("/text", "text/plain; charset=utf-8")):
        code, headers, _ = answers[path]
        assert (code, headers["Content-Type"], headers["Cache-Control"]) == (200, ctype, "no-store")
    assert answers["/nope"][0] == 404
    stats = json.loads(answers["/api/stats"][2])
    assert stats["schema_version"] == 1 and stats["network"]["interfaces"][0]["name"] == "eth0"
    text = answers["/text"][2].decode()  # the same renderer as the platmon command
    assert "NET   bytes/s, this process's network namespace" in text and "  eth0  " in text


def test_generic_callable_with_a_network_key_is_passed_through():
    c = Clock()
    s = fake(lambda: {"network": {"anything": 1, "read": "theirs"}}, c)
    s._attempt()
    assert s.read()[0]["network"] == {"anything": 1, "read": "theirs"}


def test_real_service_reading():
    """The real collect_recorded with the product's NetworkCounters on this host's clock."""
    from runtime.sampler import pick_clock
    clock = pick_clock()
    cpu = CpuCounters(clock[0], max_gap=5.0)
    s = Sampler(functools.partial(collector.collect_recorded, cpu, {}, clock[0], NetworkCounters(clock[0], 5.0)),
                clock=clock)
    s._attempt()
    stats = s.read()[0]
    assert stats["network"]["read"] is not None and stats["sample"]["data_age_basis"] == "oldest_current_read_start"
    assert "lo" in [i["name"] for i in stats["network"]["interfaces"]]
