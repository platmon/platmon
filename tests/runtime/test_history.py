"""runtime/history.py and /api/history: which numbers are kept under which identity, gaps, bounds,
retention, and that only published snapshots append. Fake clocks; no host data."""
import json
import threading

import pytest

from runtime.history import History, observation_points, points_of
from runtime.sampler import Sampler
from runtime.server import make_handler
from tests.runtime.test_sampler import Clock, fake
from tests.runtime.test_server import get

S = 10**9


def stats(n, rx=100.0, ns="netns1:a", ifindex=2, disk_rates=True):
    return {"cpu": [{"id": 0, "usage": 10.0 + n}, {"id": 1, "usage": None}], "memory": {"used": 1000 + n},
            "temperature": {"cpu": 40.0, "a/b": 41.0},
            "power": {"VDD_IN": 5.25, "VDD_CPU_GPU_CV": 1.5},
            "network": {"scope": {"id": ns}, "interfaces": [
                {"name": "eth0", "ifindex": ifindex, "rates": {"rx_bytes_per_s": rx, "tx_bytes_per_s": 0.0}},
                {"name": "wlan0", "ifindex": 3, "rates": None, "reason": "warmup"},
                {"name": "odd", "ifindex": None, "rates": {"rx_bytes_per_s": 1.0, "tx_bytes_per_s": 1.0}}]},
            "disk_io": {"disks": [{"name": "sda", "major": 8, "minor": 0,
                                   "rates": {"read_bytes_per_s": 5.0, "write_bytes_per_s": 6.0} if disk_rates else None}]}}


def test_series_ids_follow_identities_and_gaps_are_none():
    meta = {"/temperature/cpu": {"id": "src1:abc"}, "/temperature/a~1b": {"id": None},
            "/power/VDD_IN": {"id": "src1:rail"}}
    p = points_of(stats(1), meta)
    assert p["cpu/0/usage"] == 11.0 and p["cpu/1/usage"] is None
    assert p["memory/used_bytes"] == 1001
    assert p["temperature/src1:abc"] == 40.0 and p["temperature/name:a/b"] == 41.0  # no id: by name, marked so
    # a power rail is a named sensor value like a temperature, and is identified the same way
    assert p["power/src1:rail"] == 5.25 and p["power/name:VDD_CPU_GPU_CV"] == 1.5
    assert p["network/netns1:a/2/eth0/rx_bytes_per_s"] == 100.0
    assert p["network/netns1:a/3/wlan0/rx_bytes_per_s"] is None  # warmup: a gap, not 0
    assert not any("/odd/" in k for k in p)  # no ifindex: no identity to join points on
    assert p["disk_io/8:0/sda/read_bytes_per_s"] == 5.0
    assert points_of(stats(1, disk_rates=False), {})["disk_io/8:0/sda/write_bytes_per_s"] is None


def test_the_gpu_is_one_series_and_a_card_without_a_load_is_a_gap():
    assert points_of({"gpu": {"usage": 12.5}}, {})["gpu/usage"] == 12.5
    assert points_of({"gpu": {"usage": None}}, {})["gpu/usage"] is None  # a card that publishes no load
    assert "gpu/usage" not in points_of({"gpu": None}, {})  # no graphics device: no series to keep


def test_a_new_namespace_or_index_is_a_new_series():
    a, b = points_of(stats(1), {}), points_of(stats(1, ns="netns1:b", ifindex=9), {})
    assert not {k for k in a if k.startswith("network/")} & {k for k in b if k.startswith("network/")}


def record(n, t):
    return {"stats": stats(n), "sensor_meta": {}, "sequence": n, "started": t}


def test_points_are_bounded_and_old_series_go_away():
    h = History(retention=60, interval=1.0, max_series=64)
    for n in range(1, 500):
        h.append(record(n, n * S))
    series, dropped = h.view(499 * S)
    assert len(h._series["memory/used_bytes"]) == 61  # includes the point exactly at the retention boundary
    assert len(series["memory/used_bytes"]) == 61 and series["memory/used_bytes"][-1] == [499, 0.0, 1499]  # last 60 s
    later = {"stats": {"memory": {"used": 1}}, "sensor_meta": {}, "sequence": 550, "started": 550 * S}
    h.append(later)  # eth0, sda and the CPUs are not in it: kept until they are older than the retention
    assert "cpu/0/usage" in h.view(550 * S)[0]
    h.append({**later, "sequence": 700, "started": 700 * S})
    assert list(h.view(700 * S)[0]) == ["memory/used_bytes"]


def test_series_limit_counts_what_it_drops():
    h = History(retention=60, interval=1.0, max_series=3)
    h.append(record(1, S))
    series, dropped = h.view(S)
    assert len(series) == 3 and dropped == len(points_of(stats(1), {})) - 3
    for n in range(2, 12):  # the same ids do not fit again: counted once each, never once per publication
        h.append(record(n, n * S))
    assert h.view(11 * S)[1] == dropped


def busy_host(warmup=False, cpus=22, temps=31, interfaces=4, rails=3):
    """As many identities as a 22-core PC with 31 temperature channels: 64 core series, 69 with the
    low-frequency groups below. warmup: the first snapshot, where no CPU has been measured yet."""
    return {"cpu": [] if warmup else [{"id": i, "usage": float(i)} for i in range(cpus)],
            "memory": {"used": 1000},
            "temperature": {f"sensor{i}": 40.0 + i for i in range(temps)},
            "power": {f"rail{i}": 1.0 + i for i in range(rails)},
            "network": {"scope": {"id": "netns1:a"}, "interfaces": [
                {"name": f"eth{i}", "ifindex": i + 1, "rates": {"rx_bytes_per_s": 1.0, "tx_bytes_per_s": 2.0}}
                for i in range(interfaces)]},
            "disk_io": {"disks": [{"name": "nvme0n1", "major": 259, "minor": 0,
                                   "rates": {"read_bytes_per_s": 3.0, "write_bytes_per_s": 4.0}}]}}


def fs(minor):
    return {"major": 259, "minor": minor, "device": f"nvme0n1p{minor}", "fstype": "ext4",
            "source": f"/dev/nvme0n1p{minor}", "used_bytes": 10, "available_bytes": 20}


def test_battery_points_carry_the_charge_and_the_draw():
    """What a consumption reading is made of on the battery's side: the charge the gauge counts, so a
    drain is a difference over the window, and the draw where the driver reports it. The bay's name is
    the identity, so a cell swapped into the same bay continues that bay's series."""
    data = {"batteries": [
        {"name": "BAT1", "present": True, "percent": 89, "charge_now_uah": 3026000,
         "energy_now_uwh": None, "power_uw": None},
        {"name": "BAT2", "present": False, "percent": None, "charge_now_uah": None,
         "energy_now_uwh": None, "power_uw": None}], "external": [{"name": "ADP1", "online": True}]}
    p = observation_points("battery", data)
    assert p["observation/battery/BAT1/percent"] == 89
    assert p["observation/battery/BAT1/charge_now_uah"] == 3026000
    # the other gauge family, an unreported draw and an empty bay get no series at all: one that can
    # never hold a value would take a slot from one that can. History gaps a series that goes missing.
    assert "observation/battery/BAT1/energy_now_uwh" not in p
    assert "observation/battery/BAT1/power_uw" not in p
    assert not any("BAT2" in sid for sid in p)
    assert not any("ADP1" in sid for sid in p)  # external power stores no charge, so it has no series


def test_a_battery_observation_reaches_the_history():
    h = History(retention=60, interval=1.0)
    for n in (1, 2):
        h.append_observation("battery", 60, {
            "id": n, "started": n * S, "completed": n * S, "collector": {"state": "ok"},
            "data": {"batteries": [{"name": "BAT1", "present": True, "percent": 90 - n,
                                    "charge_now_uah": 3000000 - n, "energy_now_uwh": None,
                                    "power_uw": None}], "external": []}})
    h.append_observation("battery", 60, {  # the gauge stops answering: the series stays, with a gap
        "id": 3, "started": 3 * S, "completed": 3 * S, "collector": {"state": "partial"},
        "data": {"batteries": [{"name": "BAT1", "present": True, "percent": 87,
                                "charge_now_uah": None, "energy_now_uwh": None, "power_uw": None}],
                 "external": []}})
    series, _ = h.view(3 * S)
    assert [v for _, _, v in series["observation/battery/BAT1/percent"]] == [89, 88, 87]
    assert [v for _, _, v in series["observation/battery/BAT1/charge_now_uah"]] == [2999999, 2999998, None]


def test_every_cpu_core_keeps_its_series_when_sensors_outnumber_the_limit():
    """A 22-core PC with 31 temperature channels needs 69 series. What does not fit is the least important
    temperature channels, not whatever happened to be published last: the CPU cores used to arrive one
    snapshot after the temperatures (they are warming up) and lost their slots to them."""
    h = History(retention=60, interval=1.0)
    h.append_observation("storage", 30, {"id": 1, "started": S, "completed": S, "collector": {"state": "ok"},
                                         "data": {"filesystems": [fs(1), fs(6)]}})
    h.append_observation("wifi", 5, {"id": 1, "started": S, "completed": S, "collector": {"state": "ok"},
                                     "data": {"scope": {"id": "ns:a"}, "interfaces": [
                                         {"name": "wlan0", "ifindex": 9, "connected": True, "signal_dbm": -60}]}})
    h.append({"sequence": 1, "started": S, "stats": busy_host(warmup=True), "sensor_meta": {}})
    h.append({"sequence": 2, "started": 2 * S, "stats": busy_host(), "sensor_meta": {}})
    series, dropped = h.view(2 * S)
    assert len(series) == 64 and dropped == 8
    assert all(f"cpu/{i}/usage" in series for i in range(22))
    assert len([sid for sid in series if sid.startswith("power/")]) == 3  # the rails keep theirs
    assert len([sid for sid in series if sid.startswith("temperature/")]) == 23  # 31 channels, 23 slots left
    assert len([sid for sid in series if sid.startswith("network/")]) == 8
    assert len([sid for sid in series if sid.startswith("disk_io/")]) == 2
    assert len([sid for sid in series if sid.startswith("observation/")]) == 5  # the slow groups keep theirs
    assert "memory/used_bytes" in series
    h.append({"sequence": 3, "started": 3 * S, "stats": busy_host(), "sensor_meta": {}})
    series, dropped = h.view(3 * S)  # a stable choice: no series takes turns with another
    assert dropped == 8 and len(series) == 64 and all(f"cpu/{i}/usage" in series for i in range(22))
    assert all(len(points) == 2 for sid, points in series.items() if sid.startswith("cpu/"))


def test_view_bounds_prefix_seconds_and_points():
    h = History(retention=600, interval=1.0)
    for n in range(1, 301):
        h.append(record(n, n * S))
    s, _ = h.view(300 * S, prefix="memory/", seconds=100, points=10)
    pts = s["memory/used_bytes"]
    assert list(s) == ["memory/used_bytes"] and len(pts) == 10 and pts[-1][0] == 300  # newest kept
    assert all(p[1] <= 100000.0 for p in pts)
    s, _ = h.view(300 * S, seconds=10**6)  # capped at the retention
    assert len(s["memory/used_bytes"]) == 300


def test_only_published_snapshots_append_and_reads_never_do():
    c = Clock()
    seq = [0]

    def collect():
        seq[0] += 1
        if seq[0] == 2:
            raise OSError("required read failed")
        if seq[0] == 3:
            c.advance(10)  # too slow: dropped
        return stats(seq[0])
    s = fake(collect, c)
    h = History(retention=600, interval=1.0)
    s.on_publish = h.append
    for _ in range(4):
        c.advance(1)
        s._attempt()
    for _ in range(20):
        s.read()
        s._stats_json()
    pts = h.view(c.ns)[0]["memory/used_bytes"]
    assert [p[0] for p in pts if p[2] is not None] == [1, 2]  # attempts 1 and 4 only
    assert [p[2] for p in pts] == [1001, None, 1004]  # the elapsed outage inserts a gap, not a sample


def test_one_failed_core_attempt_breaks_the_line_on_recovery():
    c = Clock()
    calls = [0]

    def collect():
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("one required read failed")
        return stats(calls[0])

    s = fake(collect, c)
    h = History(retention=600, interval=1.0)
    s.on_publish = h.append
    for _ in range(3):
        c.advance(1)
        s._attempt()
    pts = h.view(c.ns)[0]["memory/used_bytes"]
    assert [p[0] for p in pts if p[2] is not None] == [1, 2]
    assert [p[2] for p in pts] == [1001, None, 1003]
    c.advance(1)
    s._attempt()
    assert [p[2] for p in h.view(c.ns)[0]["memory/used_bytes"]] == [1001, None, 1003, 1004]


def test_a_failing_append_never_stops_the_core(capsys):
    c = Clock()
    s = fake(lambda: {"v": 1}, c)
    s.on_publish = lambda record: 1 / 0
    s._attempt()
    assert s.read()[0]["sample"]["sequence"] == 1 and "history append failed" in capsys.readouterr().err


def test_concurrent_append_and_view():
    h = History(retention=60, interval=1.0)
    done = threading.Event()
    errors = []

    def reader():
        while not done.is_set():
            try:
                json.dumps(h.view(10**12))
            except Exception as e:
                errors.append(e)
    t = threading.Thread(target=reader)
    t.start()
    for n in range(1, 2000):
        h.append(record(n, n * S))
    done.set()
    t.join()
    assert errors == []


@pytest.fixture
def server():
    from http.server import ThreadingHTTPServer
    c = Clock()
    s = Sampler(lambda: stats(1), clock=(lambda: c.ns, {"source": "fake"}), wall=lambda: c.wall)
    h = History(retention=600, interval=1.0)
    s.on_publish = h.append
    s._attempt()
    servers = []

    def start(history):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s, True, None, history))
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"
    yield start, h
    for httpd in servers:
        httpd.shutdown()


def test_history_endpoint(server):
    start, h = server
    url = start(h)
    code, headers, body = get(url + "/api/history?prefix=memory/&points=5")
    doc = json.loads(body)
    assert (code, headers["Content-Type"], headers["Cache-Control"]) == (200, "application/json", "no-store")
    assert list(doc["series"]) == ["memory/used_bytes"] and doc["retention_s"] == 600 and doc["interval_ms"] == 1000
    for q in ("points=0", "points=601", "seconds=0", "seconds=601", "prefix=" + "x" * 129, "points=x", "unknown=1", "unknown=", "points=2&points=3"):
        assert get(url + "/api/history?" + q)[0] == 400, q
    assert get(start(None) + "/api/history")[0] == 404  # history off


def test_missing_series_and_elapsed_gap_do_not_join_values():
    h = History(retention=60, interval=1)
    h.append(record(1, S))
    h.append({"sequence": 2, "started": 2*S, "stats": {}, "sensor_meta": {}})
    h.append(record(3, 3*S))
    assert [p[2] for p in h.view(3*S, prefix="memory/")[0]["memory/used_bytes"]] == [1001, None, 1003]
    h.append(record(4, 20*S))
    assert h.view(20*S, prefix="memory/")[0]["memory/used_bytes"][-2][2] is None
    h.append(record(4, 20*S))
    assert sum(p[0] == 4 and p[2] is not None for p in h.view(20*S)[0]["memory/used_bytes"]) == 1


def test_downsampling_preserves_missing_bucket_and_newest():
    h = History(retention=60, interval=1)
    for n in range(1, 21):
        r = record(n, n*S)
        if n == 9:
            r["stats"]["memory"]["used"] = None
        h.append(r)
    pts = h.view(20*S, prefix="memory/", points=4)[0]["memory/used_bytes"]
    assert len(pts) <= 4 and pts[-1][2] == 1020
    assert any(p[2] is None for p in pts)


def test_low_observation_ids_append_once_independent_of_core_reads():
    h = History(retention=60, interval=1)
    o = {"id": 1, "started": S, "completed": S, "collector": {"state": "ok"},
         "data": {"targets": [{"address": "127.0.0.1", "port": 80, "connect_ms": 0.0}]}}
    for _ in range(10):
        h.append_observation("probe", 10, o)
    series = h.view(S)[0]
    assert series["observation/probe/127.0.0.1:80/connect_ms"] == [[1, 0.0, 0.0]]
    h.append_observation("probe", 10, {**o, "id": 2, "started": 11*S, "completed": 11*S,
        "data": {"targets": [{"address": "127.0.0.1", "port": 80, "connect_ms": None}]}})
    assert h.view(11*S)[0]["observation/probe/127.0.0.1:80/connect_ms"][-1][2] is None


@pytest.mark.parametrize("name,interval,data", [
    ("storage", 30, {"filesystems": [{"major": 8, "minor": 1, "device": "sda1", "fstype": "ext4",
                                     "source": "/dev/sda1", "used_bytes": 100, "available_bytes": 200}]}),
    ("wifi", 5, {"scope": {"id": "ns:a"}, "interfaces": [{"name": "wlan0", "ifindex": 2,
                                                         "connected": True, "signal_dbm": -60}]}),
    ("probe", 10, {"targets": [{"address": "127.0.0.1", "port": 80, "connect_ms": 1.0}]}),
])
def test_slow_history_releases_expired_points_after_retention(name, interval, data):
    h = History(retention=600, interval=1)
    for second in range(interval, 1801, interval):
        h.append_observation(name, interval, {"id": second // interval, "started": second*S,
            "completed": second*S, "collector": {"state": "ok"}, "data": data})
    visible = h.view(1800*S, points=600)[0]
    for sid, retained in h._series.items():
        assert retained[0][1] >= 1200*S  # filtering the HTTP response alone does not release memory
        assert len(retained) == 600 // interval + 1
        assert len(visible[sid]) == len(retained)


def test_series_slot_is_freed_before_accepting_replacement():
    h = History(retention=60, interval=1, max_series=1)
    h.append({"sequence": 1, "started": S, "stats": {"memory": {"used": 1}}})
    h.append({"sequence": 2, "started": 100*S, "stats": {"cpu": [{"id": 0, "usage": 2}]}})
    assert list(h.view(100*S)[0]) == ["cpu/0/usage"]


def test_slow_callback_observes_publish_once_and_never_runs_under_lock(capsys):
    from runtime.slow import Slow
    c = Clock()
    h = History(retention=60, interval=1)
    g = Slow("probe", lambda group: {"targets": [group.got({"address": "127.0.0.1", "port": 80, "connect_ms": 1})]},
             10, lambda: c.ns)
    def published(name, interval, record):
        assert g.view()["observation"]["id"] == record["id"]  # would deadlock under group lock
        h.append_observation(name, interval, record)
    g.on_publish = published
    g.observe_once()
    for _ in range(5):
        g.view()
        h.view(c.ns)
    assert len(h.view(c.ns)[0]["observation/probe/127.0.0.1:80/connect_ms"]) == 1
    g.on_publish = lambda *args: 1/0
    g.observe_once()
    g.observe_once()
    assert capsys.readouterr().err.count("observation history append failed") == 1
    assert g.view()["observation"]["id"] == 3


def test_wifi_history_requires_namespace_and_index_and_breaks_disconnection():
    from runtime.history import observation_points
    data = {"scope": {"id": "ns:a"}, "interfaces": [{"name": "wlan0", "ifindex": 2,
                                                    "connected": True, "signal_dbm": -60}]}
    first = observation_points("wifi", data)
    assert first == {"observation/wifi/ns:a/2/wlan0/signal_dbm": -60}
    assert observation_points("wifi", {**data, "scope": {}}) == {}
    data["interfaces"][0]["ifindex"] = 3
    assert not set(first) & set(observation_points("wifi", data))
    data["interfaces"][0]["connected"] = False
    assert list(observation_points("wifi", data).values()) == [None]


def test_storage_history_identity_and_independent_cadence():
    h = History(retention=600, interval=1)
    fs = {"major": 8, "minor": 1, "device": "sda1", "source": "/dev/sda1", "fstype": "ext4",
          "used_bytes": 0, "available_bytes": 100}
    o = {"id": 1, "started": S, "completed": S, "collector": {"state": "ok"},
         "data": {"filesystems": [fs]}}
    h.append_observation("storage", 30, o)
    for n in range(1, 20):
        h.append(record(n, n*S))
    series = h.view(20*S, prefix="observation/storage/")[0]
    assert len(series) == 2 and all(len(p) == 1 for p in series.values())
    assert set(h.intervals()[sid] for sid in series) == {30000}
    assert any(pts[0][2] == 0 for pts in series.values())
    h.append_observation("storage", 30, {**o, "id": 2, "started": 31*S, "completed": 122*S})
    assert all(pts[-1][2] is None for pts in h.view(122*S, prefix="observation/storage/")[0].values())


def test_point_and_series_limits_even_at_fastest_cadence():
    from runtime.history import MAX_POINTS_PER_SERIES
    h = History(retention=3600, interval=0.1)
    for n in range(MAX_POINTS_PER_SERIES + 50):
        h.append({"sequence": n, "started": n*100000000,
                  "stats": {"cpu": [{"id": i, "usage": float(i)} for i in range(70)]}})
    assert len(h._series) == 64
    assert all(len(points) == MAX_POINTS_PER_SERIES for points in h._series.values())
    assert h.dropped_series == 6  # cpu/64 .. cpu/69: counted once each, never once per publication
