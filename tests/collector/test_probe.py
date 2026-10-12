"""collector/probe.py: target parsing, the TCP connect timing (against this machine's loopback only), the
observation and its config. No packet leaves the host: every real connection is to 127.0.0.1."""
import socket
import threading

import pytest

import platmon
from collector import metadata
from collector import sysfs
from collector.probe import WINDOW, Probe, connect_ms, parse_targets
from frontends.cli import probe_lines
from tests.collector.test_network_integration import ini_file


def test_parse_targets():
    assert parse_targets("192.0.2.1:443, [2001:db8::1]:22 ,") == [("192.0.2.1", 443), ("2001:db8::1", 22)]
    assert parse_targets("") == []


@pytest.mark.parametrize("text", ["example.com:80", "192.0.2.1", "192.0.2.1:0", "192.0.2.1:65536", "192.0.2.1:http",
                                  "2001:db8::1:22", "192.0.2.1:1,192.0.2.1:1", ",".join(f"192.0.2.{n}:1" for n in range(5))])
def test_parse_targets_refuses(text):
    with pytest.raises(ValueError):
        parse_targets(text)  # names are never resolved; at most 4; no duplicates


@pytest.fixture
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    stop = threading.Event()

    def accept():
        s.settimeout(0.1)
        while not stop.is_set():
            try:
                c, _ = s.accept()
                c.close()
            except OSError:
                pass
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield s.getsockname()[1]
    stop.set()
    t.join(2)
    s.close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    return port


def test_connect_to_loopback_listener(listener):
    ms, reason = connect_ms("127.0.0.1", listener, 2.0)
    assert reason is None and 0 <= ms < 2000


def test_refused_is_a_failure_not_a_time():
    assert connect_ms("127.0.0.1", closed_port(), 2.0) == (None, "refused")


def test_timeout_is_reported_without_a_time(monkeypatch):
    import collector.probe as probe
    monkeypatch.setattr(probe.select, "select", lambda r, w, x, t: ([], [], []))  # no answer within the timeout
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, addr: probe.errno.EINPROGRESS)
    assert connect_ms("127.0.0.1", 9, 0.01) == (None, "timeout")


def test_observation_keeps_a_bounded_failure_window():
    results = iter([(1.5, None), (None, "timeout")] * 20)
    p = Probe([("192.0.2.1", 443)], timeout=1.0, connect=lambda a, port, t: next(results))
    for n in range(1, 25):
        g = metadata.Group("probe")
        t = p(g)["targets"][0]
        assert t["attempts"] == min(n, WINDOW) and g.status()["state"] == "ok"  # a failed attempt is a result
    assert (t["connect_ms"], t["reason"], t["failures"], t["failure_ratio"]) == (None, "timeout", 5, 0.5)
    first = Probe([("192.0.2.1", 443)], connect=lambda a, port, t: (None, "refused"))(metadata.Group("probe"))["targets"][0]
    assert first["connect_ms"] is None and first["failures"] == 1  # a first failure is never 0 ms


def test_targets_are_probed_one_after_another():
    active, peak = [0], [0]

    def connect(address, port, timeout):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        active[0] -= 1
        return 1.0, None
    p = Probe([("192.0.2.1", 1), ("192.0.2.2", 2), ("192.0.2.3", 3)], connect=connect)
    assert [t["port"] for t in p(metadata.Group("probe"))["targets"]] == [1, 2, 3] and peak[0] == 1


@pytest.mark.parametrize("text, error", [
    ("[probe]\ntargets = host.example:80\n", "targets"),
    ("[probe]\nenabled = yes\ntargets = 192.0.2.1:1, 192.0.2.2:1\ntimeout = 5\ninterval = 10\n", "interval must be longer"),
    ("[probe]\ntimeout = 20\n", "out of range")])
def test_config_refuses(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        platmon.load_config(ini_file(tmp_path, text))


def test_enabled_by_default_without_targets():
    cfg = platmon.load_config()
    assert cfg["probe"].getboolean("enabled") and cfg["probe"]["targets"] == ""


def test_cli_lines():
    obs = {"groups": {"probe": {"observation": {"data_age_ms": 4000, "stale": False}, "data": {"targets": [
        {"address": "192.0.2.1", "port": 443, "connect_ms": 12.34, "reason": None, "attempts": 10, "failures": 1},
        {"address": "2001:db8::1", "port": 22, "connect_ms": None, "reason": "timeout", "attempts": 3, "failures": 3}]}}}}
    assert probe_lines(obs) == ["TCP   connect time, observed 4 s ago", "  192.0.2.1:443  12.3 ms  failed 1/10 recent",
                                "  [2001:db8::1]:22  timeout  failed 3/3 recent"]
    assert probe_lines(None) == [] and probe_lines({"groups": {"probe": {"data": None}}}) == []
