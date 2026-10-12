"""collector/network.py: the /proc/net/dev parser and NetworkCounters' baselines and rates.
Fake file contents, namespace, index list and elapsed clock; nothing on the host is read."""
import os

import pytest

from collector import metadata
from collector import sysfs
from collector.network import NetworkCounters, parse

HEAD = ("Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n")
S = 10**9


def row(name, rx=(0, 0, 0, 0), tx=(0, 0, 0, 0)):
    """One /proc/net/dev line: rx/tx = (bytes, packets, errors, dropped); the other counters 7."""
    return f"{name:>6}: " + " ".join(map(str, (*rx, 7, 7, 7, 7, *tx, 7, 7, 7, 7)))


class Host:
    """Everything NetworkCounters reads, settable between samples."""

    def __init__(self, **ifaces):
        self.ns, self.text, self.error, self.ns_error, self.index_error = (1, 100), None, None, None, None
        self.reads = 0
        self.t = 10**12
        self.ifaces = {n: (i, rx, tx) for n, (i, rx, tx) in ifaces.items()}
        self.wall = 0

    def clock(self):
        return self.t

    def netns(self):
        if self.ns_error:
            raise self.ns_error
        return os.stat_result((0, self.ns[1], self.ns[0], 0, 0, 0, 0, 0, 0, 0))

    def indexes(self):
        if self.index_error:
            raise self.index_error
        return [(i, n) for n, (i, _, _) in self.ifaces.items()]

    def read(self, path):
        self.reads += 1
        if self.error:
            raise self.error
        if self.text is not None:
            return self.text
        return (HEAD + "".join(row(n, rx, tx) + "\n" for n, (_, rx, tx) in self.ifaces.items())).encode()

    def counters(self, max_gap=5.0):
        return NetworkCounters(self.clock, max_gap, netns=self.netns, indexes=self.indexes, read=self.read)


def sample(net):
    g = metadata.Group("network")
    out, span = net.sample(g)
    return out, span, g.status()


def by_name(out):
    return {i["name"]: i for i in out["interfaces"]}


def test_warmup_then_rates_over_the_observed_window():
    h = Host(eth0=(2, (1000, 10, 0, 0), (2000, 20, 0, 0)), lo=(1, (5, 1, 0, 0), (5, 1, 0, 0)))
    net = h.counters()
    out, span, status = sample(net)
    assert [i["name"] for i in out["interfaces"]] == ["eth0", "lo"]  # deterministic: by name
    assert all(i["rates"] is None and i["reason"] == "warmup" and i["window_ms"] is None for i in out["interfaces"])
    assert out["scope"]["kind"] == "process_network_namespace" and out["scope"]["id"].startswith("netns1:")
    assert out["provider"] == "proc_net_dev" and out["read"] is None and span == (h.t, h.t)
    assert status == {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0}  # warmup counts as values
    h.t += 2 * S  # 2 s actually passed, whatever the configured interval
    h.ifaces["eth0"] = (2, (1200, 14, 1, 2), (2600, 26, 0, 3))
    out, _, _ = sample(net)
    eth = by_name(out)["eth0"]
    assert eth == {"name": "eth0", "ifindex": 2,
                   "rx": {"bytes": 1200, "packets": 14, "errors": 1, "dropped": 2},
                   "tx": {"bytes": 2600, "packets": 26, "errors": 0, "dropped": 3},
                   "rates": {"rx_bytes_per_s": 100.0, "tx_bytes_per_s": 300.0, "rx_packets_per_s": 2.0,
                             "tx_packets_per_s": 3.0},
                   "window_ms": 2000.0, "reason": None}
    lo = by_name(out)["lo"]  # counters did not move: measured 0, not missing
    assert lo["rates"] == dict.fromkeys(eth["rates"], 0.0) and lo["reason"] is None


def test_rate_uses_the_read_start_and_integer_ns():
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    sample(net)
    h.t += 3  # 3 ns: no float rounding of the window
    h.ifaces["eth0"] = (2, (3, 0, 0, 0), (0, 0, 0, 0))
    eth = by_name(sample(net)[0])["eth0"]
    assert eth["rates"]["rx_bytes_per_s"] == 1e9 and eth["window_ms"] == 0.0


@pytest.mark.parametrize("step", [0, -5, 3600])
def test_wall_clock_is_not_used(monkeypatch, step):
    import time
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    sample(net)
    monkeypatch.setattr(time, "time", lambda: 1e9 + step)
    h.t += S
    h.ifaces["eth0"] = (2, (100, 0, 0, 0), (0, 0, 0, 0))
    assert by_name(sample(net)[0])["eth0"]["rates"]["rx_bytes_per_s"] == 100.0


@pytest.mark.parametrize("field", range(8))
def test_any_counter_going_down_gives_no_rate_and_a_new_baseline(field):
    start = [100] * 8
    h = Host(eth0=(2, tuple(start[:4]), tuple(start[4:])))
    net = h.counters()
    sample(net)
    lower = list(start)
    lower[field] = 99
    h.t += S
    h.ifaces["eth0"] = (2, tuple(lower[:4]), tuple(lower[4:]))
    eth = by_name(sample(net)[0])["eth0"]
    assert eth["rates"] is None and eth["reason"] == "counter_regressed" and eth["window_ms"] == 1000.0
    assert eth["rx"]["bytes"] == lower[0]  # counters as read, not clamped
    h.t += S
    up = [v + 10 for v in lower]
    h.ifaces["eth0"] = (2, tuple(up[:4]), tuple(up[4:]))
    assert by_name(sample(net)[0])["eth0"]["rates"]["rx_bytes_per_s"] == 10.0  # from the regressed reading


@pytest.mark.parametrize("window, reason", [(5 * S, None), (5 * S + 1, "gap"), (0, "invalid_interval"),
                                            (-S, "invalid_interval")])
def test_gap_boundary_and_bad_intervals(window, reason):
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters(max_gap=5.0)
    sample(net)
    h.t += window
    eth = by_name(sample(net)[0])["eth0"]
    assert eth["reason"] == reason and (eth["rates"] is None) == (reason is not None)
    h.t += S
    assert by_name(sample(net)[0])["eth0"]["reason"] is None  # every valid reading is the next baseline


def test_interfaces_come_go_and_are_renamed_or_reindexed():
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)), wlan0=(3, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    sample(net)
    del h.ifaces["wlan0"]
    h.ifaces["usb0"] = (4, (0, 0, 0, 0), (0, 0, 0, 0))
    h.t += S
    out = by_name(sample(net)[0])
    assert set(out) == {"eth0", "usb0"} and out["usb0"]["reason"] == "warmup" and out["eth0"]["reason"] is None
    h.ifaces["wlan0"] = (3, (5, 0, 0, 0), (0, 0, 0, 0))  # back after one reading without it: no old baseline
    h.ifaces["eth0"] = (9, (0, 0, 0, 0), (0, 0, 0, 0))   # same name, another device
    h.ifaces["usb1"] = h.ifaces.pop("usb0")              # same index, renamed
    h.t += S
    out = by_name(sample(net)[0])
    assert {n: i["reason"] for n, i in out.items()} == {"eth0": "warmup", "usb1": "warmup", "wlan0": "warmup"}
    assert len(net._last[2]) == 3  # only what the last reading saw


def test_namespace_change_restarts_every_baseline():
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    first = sample(net)[0]["scope"]["id"]
    h.ns = (1, 200)
    h.t += S
    out = sample(net)[0]
    assert out["scope"]["id"] != first and by_name(out)["eth0"]["reason"] == "namespace_changed"
    h.t += S
    assert by_name(sample(net)[0])["eth0"]["reason"] is None


def test_identity_lookup_failures_keep_counters_without_rates():
    h = Host(eth0=(2, (1, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    sample(net)
    h.ns_error = PermissionError(13, "Permission denied")
    h.t += S
    out, _, status = sample(net)
    eth = by_name(out)["eth0"]
    assert out["scope"]["id"] is None and eth["rx"]["bytes"] == 1 and eth["rates"] is None
    assert eth["reason"] == "identity_unavailable"
    assert status["state"] == "partial" and status["issues"] == [{"target": "netns", "reason": "permission_denied"}]
    h.ns_error, h.index_error = None, OSError(5, "EIO")
    h.t += S
    out, _, status = sample(net)
    assert by_name(out)["eth0"]["ifindex"] is None and by_name(out)["eth0"]["reason"] == "identity_unavailable"
    assert status["issues"] == [{"target": "ifindex", "reason": "io_error"}]
    h.index_error = None
    h.t += S
    assert by_name(sample(net)[0])["eth0"]["reason"] == "warmup"  # unknown identity was no baseline
    del h.ifaces["eth0"]  # in the file but gone from the index list: unknown, not an error
    h.text = (HEAD + row("eth0") + "\n").encode()
    h.t += S
    out, _, status = sample(net)
    assert by_name(out)["eth0"]["reason"] == "identity_unavailable" and status["state"] == "ok"


def test_bad_rows_are_left_out_and_others_kept():
    lines = [row("eth0", (10, 1, 0, 0)), row("lo"), "  bad: 1 2 3",  # short row
             row("neg").replace(" 0 ", " -1 ", 1), row("dec").replace(" 0 ", " 1.5 ", 1),
             row("big").replace(" 0 ", f" {2**64} ", 1), row("max").replace(" 0 ", f" {2**64 - 1} ", 1),
             row("uni").replace(" 0 ", " ٣ ", 1),  # an Arabic-Indic digit is not a counter
             row("dup"), row("dup"), row("x/y"), "eth1 1 2 3", "\x1b[2J: " + " ".join("0" * 16), row("long" * 4),
             row("toolong") + " 0", ""]
    rows, bad = parse(HEAD + "\n".join(lines))
    assert sorted(rows) == ["eth0", "lo", "max"] and rows["eth0"][:2] == (10, 1)
    assert rows["max"][0] == 2**64 - 1
    targets = [t for t, _ in bad]
    assert all(r == "invalid_data" for _, r in bad)
    assert targets == ["bad", "neg", "dec", "big", "uni", "dup", "line13", "line14", "line15", "line16", "toolong"]
    assert not any("/" in t or "\x1b" in t for t in targets)  # never a path or control character


def test_a_malformed_duplicate_still_makes_the_name_ambiguous():
    rows, bad = parse(HEAD + "  dup: 1 2\n" + row("dup") + "\n")
    assert rows == {} and [t for t, _ in bad] == ["dup", "dup"]


def test_bad_rows_in_the_counters_report_and_keep_the_rest():
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    h.text = (HEAD + row("eth0") + "\n" + row("dup") + "\n" + row("dup") + "\n").encode()
    out, span, status = sample(net)
    assert [i["name"] for i in out["interfaces"]] == ["eth0"] and span is not None
    assert status["state"] == "partial" and status["issues"] == [{"target": "dup", "reason": "invalid_data"}]


@pytest.mark.parametrize("text, reason", [
    (b"\xff\xfe" + HEAD.encode(), "invalid_data"),  # not text
    (b"Inter-|\n face |bytes packets\n", "invalid_data"),  # unknown header
    (b"", "invalid_data"),
])
def test_a_broken_file_gives_nothing_and_clears_baselines(text, reason):
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    sample(net)
    h.text = text
    h.t += S
    out, span, status = sample(net)
    assert out["interfaces"] == [] and span is None
    assert status["state"] == "error" and status["issues"] == [{"target": "net_dev", "reason": reason}]
    h.text = None
    h.t += S
    assert by_name(sample(net)[0])["eth0"]["reason"] == "warmup"


@pytest.mark.parametrize("error, state, reason", [
    (FileNotFoundError(2, "No such file"), "unavailable", "not_exposed"),
    (PermissionError(13, "Permission denied"), "error", "permission_denied"),
    (OSError(5, "EIO"), "error", "io_error"),
])
def test_unreadable_file(error, state, reason):
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    sample(net)
    h.error = error
    out, span, status = sample(net)
    assert out["interfaces"] == [] and span is None and (status["state"], status["reason"]) == (state, reason)
    assert net._last is None


def test_an_empty_list_is_not_a_broken_file():
    h = Host()
    out, span, status = sample(h.counters())
    assert out["interfaces"] == [] and span is not None
    assert (status["state"], status["reason"], status["issues"]) == ("unavailable", "not_detected", [])


def test_many_interfaces_one_read_and_capped_issues():
    h = Host(**{f"veth{n}": (n + 10, (n, n, 0, 0), (n, n, 0, 0)) for n in range(300)})
    h.text = h.read(None) + "".join(f"  bad{n}: 1\n" for n in range(100)).encode()
    h.reads = 0
    net = h.counters()
    out, _, status = sample(net)
    assert h.reads == 1 and len(out["interfaces"]) == 300  # nothing left out for being virtual or many
    assert len(status["issues"]) == metadata.MAX_ISSUES and status["issues_truncated"] == 100 - metadata.MAX_ISSUES


def test_changing_the_output_does_not_touch_the_baseline():
    h = Host(eth0=(2, (100, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    out, _, _ = sample(net)
    out["interfaces"][0]["rx"]["bytes"] = 0
    out["interfaces"].clear()
    h.t += S
    assert by_name(sample(net)[0])["eth0"]["rates"]["rx_bytes_per_s"] == 0.0


def test_an_unexpected_error_clears_the_baseline_and_is_raised():
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    sample(net)
    net._indexes = lambda: [("not", "pairs", "x")]
    with pytest.raises(ValueError):
        sample(net)
    assert net._last is None


def test_real_host_reading():
    """The product's defaults on this host: one reading of /proc/self/net/dev, loopback included."""
    out, span, status = sample(NetworkCounters())
    names = [i["name"] for i in out["interfaces"]]
    assert names == sorted(names) and status["state"] in ("ok", "partial", "unavailable")
    if os.path.exists("/proc/self/net/dev"):
        assert "lo" in names and span is not None


def test_namespace_is_looked_up_every_time_and_hashed_once(monkeypatch):
    from collector import network
    hashed = []
    real = network.namespace_id
    monkeypatch.setattr(network, "namespace_id", lambda st: hashed.append((st.st_dev, st.st_ino)) or real(st))
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    stats = []
    netns = h.netns
    h.netns = lambda: stats.append(1) or netns()
    net = h.counters()
    ids = []
    for _ in range(3):
        ids.append(sample(net)[0]["scope"]["id"])
        h.t += S
    assert len(stats) == 3 and hashed == [(1, 100)] and len(set(ids)) == 1 and ids[0] == real(h.netns())


def test_namespace_a_b_a_and_a_failure_in_between():
    from collector.network import namespace_id
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    a = sample(net)[0]["scope"]["id"]
    h.ns, h.t = (1, 200), h.t + S
    out = sample(net)[0]
    b = out["scope"]["id"]
    assert b != a and b == namespace_id(h.netns()) and by_name(out)["eth0"]["reason"] == "namespace_changed"
    h.ns, h.t = (1, 100), h.t + S
    out = sample(net)[0]
    assert out["scope"]["id"] == a and by_name(out)["eth0"]["reason"] == "namespace_changed"
    h.ns_error, h.t = OSError(5, "EIO"), h.t + S
    out, _, status = sample(net)
    assert out["scope"]["id"] is None and by_name(out)["eth0"]["reason"] == "identity_unavailable"  # never the old id
    assert status["issues"] == [{"target": "netns", "reason": "io_error"}]
    h.ns_error, h.t = None, h.t + S
    out, _, status = sample(net)
    assert out["scope"]["id"] == a and by_name(out)["eth0"]["reason"] == "warmup" and status["state"] == "ok"
    h.t += S
    assert by_name(sample(net)[0])["eth0"]["reason"] is None


def test_same_inode_on_another_device_is_another_namespace():
    h = Host(eth0=(2, (0, 0, 0, 0), (0, 0, 0, 0)))
    net = h.counters()
    a = sample(net)[0]["scope"]["id"]
    h.ns, h.t = (2, 100), h.t + S
    out = sample(net)[0]
    assert out["scope"]["id"] != a and by_name(out)["eth0"]["reason"] == "namespace_changed"


def test_the_namespace_id_is_kept_per_instance():
    h1, h2 = Host(), Host()
    h2.ns = (1, 300)
    one, two = h1.counters(), h2.counters()
    a = sample(one)[0]["scope"]["id"]
    b = sample(two)[0]["scope"]["id"]
    assert a != b and sample(one)[0]["scope"]["id"] == a and one._ns != two._ns


def _counter_before(text):  # the previous implementation, for comparison
    from collector.counters import MAX_COUNTER
    if not (text.isascii() and text.isdigit()) or int(text) > MAX_COUNTER:
        raise ValueError(text)
    return int(text)


@pytest.mark.parametrize("text", ["0", "00", "7", str(2**64 - 1), str(2**64), "1" + "0" * 30, "-1", "+1", "1.5", "1e3",
                                  "", " ", "abc", "٣", "²", "0x10", "1_000", "１"])
def test_counter_accepts_exactly_what_it_did(text):
    from collector.network import counter
    try:
        expected = _counter_before(text)
    except ValueError:
        with pytest.raises(ValueError):
            counter(text)
    else:
        assert counter(text) == expected and type(counter(text)) is int
