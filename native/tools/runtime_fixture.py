"""Deterministic equivalence of the runtime (step 3A): the service's Sampler and slow groups (runtime_py.py)
and platmon-native's (`runtime-script`) get the same operations with the same times and collections, and
every answer must be the same, byte for byte: the /api/stats body (or none), the /api/status body without
levels, and the /api/observations body, each as json.dumps writes it. No tolerance. Covers publication,
the states (starting, ready, degraded, stale), failures and recovery, collections dropped as too slow, the
data age and its basis from the read records (and every reason to fall back to the collection start),
sensor_meta, the passive groups' read, and the slow groups' entries. Runs on the host: no tree is read.

Before the scenarios, the harness is run against fake sides (CONTROLS): equal ones must pass, and a
difference, an early exit, a nonzero exit or a hang must fail.

  PYTHONPATH=<platmon tree> runtime_fixture.py BINARY [SCENARIO ...]
  runtime_fixture.py --self-test"""
import json
import os
import subprocess
import sys
import tempfile

from fixture import Broken, ask, diff

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TIMEOUT = 30
S, MS = 1_000_000_000, 1_000_000
T0, W0 = 10**12, 1_700_000_000.0  # elapsed ns and wall seconds of a scenario's start


def start(cmd, log):
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": REPO, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, env=env, cwd="/")
    p.pending = b""
    return p


# --- building blocks -----------------------------------------------------------------------------------

REQUIRED = ("cpu", "memory", "disk", "uptime")


def required(start, end):
    return {n: [start + i * 10_007, end] for i, n in enumerate(REQUIRED)}


def result(stats, sensors=None, req=None, passive=None):
    return {"stats": stats, "sensors": sensors or {}, "required": req, "passive": passive or {}}


INSTANCE = "8dfad9fa-9bf0-4d61-91e0-ef22aebd2b91"
LEVELS = {"cpu": [80.0, 90.0], "gpu": [80.0, 90.0], "memory": [80.0, 90.0], "filesystem": [80.0, 90.0],
          "temperature": [80.0, 90.0], "battery": [20.0, 10.0], "wifi": [-70.0, -80.0]}


def setup(interval=1.0, slow=(), clock="boottime", history=None, replay=None):
    """history: its retention or None (off); replay: (retention, max_bytes) or None."""
    return ("setup", {"op": "setup", "interval": interval, "instance_id": INSTANCE, "clock": clock,
                      "slow": [list(g) for g in slow], "levels": LEVELS,
                      "history": history and {"retention": history},
                      "replay": replay and {"retention": replay[0], "max_bytes": replay[1]}})


def http(label, t, path, raw=None):
    """One request to each side's HTTP server at elapsed time t: GET path, or a raw request."""
    return (label, {"op": "http", "now": t, "request": raw or f"GET {path} HTTP/1.0\r\n\r\n"})


def collection(label, t, duration, res, clock="same", wall=None):
    """begin at t, finish duration later with res; wall seconds follow the elapsed time."""
    w = W0 + (t - T0) / S if wall is None else wall
    return [(f"{label}: begin", {"op": "begin", "now": t, "wall": w}),
            (f"{label}: finish", {"op": "finish", "now": t + duration, "wall": w + duration / S + 0.000123,
                                  "result": res, "clock": clock})]


def view(label, t):
    return (label, {"op": "view", "now": t})


def observations(label, t):
    return (label, {"op": "observations", "now": t})


def observation(label, group, t, duration, data=None, notes=(), values=1, raise_=None):
    w = W0 + (t - T0) / S
    return [(f"{label}: begin", {"op": "slow_begin", "group": group, "now": t, "wall": w}),
            (f"{label}: finish", {"op": "slow_finish", "group": group, "now": t + duration, "wall": w + duration / S,
                                  "data": data, "notes": [list(n) for n in notes], "values": values, "raise": raise_})]


def stats_pc(n=1):
    """Stats as the collector makes them, with sensor values, passive groups and collectors."""
    return {"platform": "PC", "cpu": [{"id": 0, "usage": 12.5, "freq": 1200000000}, {"id": 1, "usage": 0.0}],
            "cpu_sampling": {"mode": "interval", "window_ms": 1000.123, "unavailable": []},
            "memory": {"total": 16 << 30, "used": 5 << 30, "percent": 31.3}, "disk": {"percent": 40.0},
            "temperature": {"cpu": 51.2 + n, "a/b~c": 40.0, "gone": None, "label": "x"},
            "power": {"vdd_in": 4.75}, "fans": [{"name": "cpu", "rpm": 1500, "percent": None}, "not a fan"],
            "gpu": {"name": "i915", "usage": 3.0, "freq": 300000000, "max_freq": None},
            "npu": None, "time": "12:00:00", "uptime": 1234.5, "système": "Linux é ☃ \U0001F600",
            "tiny": 1e-05, "huge": 1e16, "negzero": -0.0, "big": 2**64 + 1,
            "network": {"read": "old", "namespace": 4026531840, "interfaces": [{"name": "lo", "rates": None}]},
            "disk_io": {"disks": []}, "pressure": {"resources": {"cpu": {"some": {"avg10": 0.5}}}},
            "sensor_meta": {"stale": True},
            "collectors": {"core": {"state": "error"}, "gpu": {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0},
                           "temperature": {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0}}}


def sensors_pc(t, src="src1:" + "a" * 64):
    span = [t + 100_000, t + 2_345_678]
    info = {"id": src, "provider": "hwmon", "identity_basis": "device_channel", "unit": "celsius", "span": span}
    return {"/temperature/cpu": info, "/temperature/a~1b~0c": {**info, "span": [t + 1_500, t + 1_500]},
            "/temperature/gone": {**info, "span": None}, "/temperature/label": {**info, "span": span},
            "/power/vdd_in": {"id": None, "provider": "ina3221", "identity_basis": "unresolved", "unit": "watt", "span": span},
            "/fans/0/rpm": {**info, "unit": "rpm"}, "/fans/0/percent": {**info, "unit": "percent", "span": None},
            "/cpu/0/freq": {**info, "provider": "cpufreq", "unit": "hertz"},
            "/gpu/usage": {**info, "provider": "drm"}, "/gpu/freq": {**info, "provider": "drm"},
            "/gpu/max_freq": {**info, "provider": "drm", "span": None}}


def good(t, n=1, **change):
    """A collection whose records all check out: the data age counts from its oldest read (t + 1500)."""
    stats = stats_pc(n)
    stats.update(change.pop("stats", {}))
    res = result(stats, sensors_pc(t), required(t + 50_000, t + 9_000_500),
                 {"network": [t + 3_000_500, t + 3_100_000], "disk_io": [t + 3_200_000, t + 3_300_001],
                  "pressure": [t + 3_400_000, t + 3_500_000]})
    res.update(change)
    return res


# --- scenarios -----------------------------------------------------------------------------------------

def core_steps():
    t1, t2, t3 = T0 + 11 * S, T0 + 14 * S, T0 + 35 * S
    return [
        setup(),
        view("no collection yet: starting", T0),
        ("first collection: begin", {"op": "begin", "now": T0, "wall": W0}),
        view("while it runs: starting, collecting_for_ms", T0 + 500 * MS + 1234),
        view("at stale_after: still starting", T0 + 5 * S),
        view("past stale_after: stale", T0 + 5 * S + 1),
        ("first collection: finish after 5.5 s: dropped as too slow",
         {"op": "finish", "now": T0 + 5500 * MS, "wall": W0 + 5.5, "result": good(T0)}),
        view("dropped: stale, no sample", T0 + 5600 * MS),
        *collection("taking exactly stale_after: kept", T0 + 5600 * MS, 5 * S, good(T0 + 5600 * MS, 3)),
        view("published", T0 + 10600 * MS),
        *collection("published", t1, 250 * MS + 7, good(t1)),
        view("ready", t1 + 300 * MS),
        view("data age at stale_after: still current", t1 + 1500 + 5 * S),
        view("one ns later: stale, stats withheld", t1 + 1501 + 5 * S),
        *collection("failed", t2, 40 * MS, {"error": "sensor gone"}),
        view("current snapshot, failed attempt: degraded", t2 + 50 * MS),
        *collection("failed again", t2 + S, 40 * MS, {"error": "sensor gone"}),
        view("two failures in a row", t2 + S + 50 * MS),
        view("the last good one is stale", t2 + 3 * S),
        *collection("recovered", t2 + 4 * S, 30 * MS, good(t2 + 4 * S, 2)),
        view("ready again, sequence 2, failures reset", t2 + 4 * S + 31 * MS),
        *collection("an optional group partial", t3, 10 * MS,
                    good(t3, stats={"collectors": {"fans": {"state": "partial", "reason": "some_unreadable",
                                                            "issues": [{"target": "hwmon1.fan1", "reason": "io_error"}],
                                                            "issues_truncated": 0}}})),
        view("degraded by the snapshot's own group", t3 + 20 * MS),
        *collection("an optional group error, core listed", t3 + S, 10 * MS,
                    good(t3 + S, stats={"collectors": {"core": {"state": "x"}, "npu": {"state": "error"}}})),
        view("degraded; core is the Sampler's", t3 + S + 20 * MS),
        *collection("groups unavailable, collectors not a dict", t3 + 2 * S, 10 * MS,
                    good(t3 + 2 * S, stats={"collectors": ["x"]})),
        view("ready: unavailable is not degraded", t3 + 2 * S + 20 * MS),
        *collection("no collectors at all, odd wall times", t3 + 3 * S, 1_234_567,
                    result({"a": 1}, {}, required(t3 + 3 * S, t3 + 3 * S + 5)), wall=1700000123.987654321),
        view("ready", t3 + 3 * S + 2_000_000),
        *collection("stats without members", t3 + 4 * S, 5, result({}, {}, required(t3 + 4 * S, t3 + 4 * S + 5))),
        view("an empty head", t3 + 4 * S + 7),
    ]


def provenance_steps():
    """Each collection with one reason for the data age to fall back to the collection start (or none)."""
    out, t = [setup()], T0

    def case(label, res, clock="same"):
        nonlocal t
        out.extend(collection(label, t, S - 1000, res, clock))
        out.append(view(f"{label}: view", t + S))
        t += 2 * S

    case("all records check out", good(t))
    case("read records on another clock", good(t), clock="other")
    r = good(t); del r["required"]["uptime"]
    case("a required read missing", r)
    r = good(t); r["required"]["extra"] = [t + 1, t + 2]
    case("an extra required read", r)
    r = good(t); r["required"]["cpu"] = [t - 1, t + 5]
    case("a required read before the collection", r)
    r = good(t); r["required"]["cpu"] = [t + 10, t + S]
    case("a required read after it", r)
    r = good(t); r["required"]["cpu"] = [t, t]; r["required"]["disk"] = [t + S - 1000, t + S - 1000]
    case("reads at the collection's very start and end", r)
    r = good(t); r["required"]["cpu"] = [t + 10, t + 5]
    case("a reversed span", r)
    r = good(t); r["required"]["cpu"] = [float(t + 1), float(t + 2)]
    case("a span of floats", r)
    r = good(t); r["required"]["cpu"] = [True, True]
    case("a span of booleans", r)
    r = good(t); r["required"]["cpu"] = [t + 1, t + 2, t + 3]
    case("a span of three", r)
    r = good(t); r["required"] = None
    case("required not a dict", r)
    r = good(t); r["sensors"]["/temperature/none"] = r["sensors"]["/temperature/cpu"]
    case("a record for a value that is not there", r)
    r = good(t); r["sensors"]["/temperature/cpu"] = ["not", "a", "dict"]
    case("a record that is not a dict", r)
    r = good(t); del r["sensors"]["/power/vdd_in"]
    case("a number without its record", r)
    r = good(t); r["sensors"]["/temperature/cpu"]["span"] = [t + 1, t + 2 * S]
    case("a number whose read is outside", r)
    r = good(t); del r["sensors"]["/temperature/cpu"]["span"]
    case("a record without a span", r)
    r = good(t); r["sensors"] = ["x"]
    case("sensor records of the wrong type", r)
    r = good(t); r["passive"]["network"] = None
    case("network values without their read", r)
    r = good(t); r["passive"]["disk_io"] = None
    case("disk_io without a read, but no disks", r)
    r = good(t); r["passive"]["pressure"] = [t + 1, t + 2 * S]
    case("pressure's read outside", r)
    r = good(t); r["stats"]["network"] = None
    case("a network read without network values", r)
    r = good(t, stats={"network": None, "disk_io": "x"}); r["passive"] = {"pressure": [t + 1, t + 2]}
    case("passive groups that are not dicts, no reads", r)
    r = good(t, stats={"temperature": {}, "power": {}, "fans": [], "cpu": [], "gpu": None})
    r["sensors"], r["passive"] = {}, {}
    r["stats"].pop("network"); r["stats"].pop("disk_io"); r["stats"].pop("pressure")
    case("only the required reads", r)
    return out


def observation_steps():
    t = T0
    return [
        setup(slow=[("storage", 30.0), ("wifi", 5.0), ("battery", 0.1)]),
        observations("no observation yet: starting", t),
        ("storage starts", {"op": "slow_begin", "group": "storage", "now": t, "wall": W0}),
        observations("while it runs", t + 1234567),
        observations("at stale_after: still starting", t + 90 * S),
        observations("past it: stale", t + 90 * S + 1),
        ("storage done", {"op": "slow_finish", "group": "storage", "now": t + 90 * S + 2, "wall": W0 + 90.0,
                          "data": {"partitions": [], "filesystems": [{"mount": "/", "percent": 40.0, "nom": "é"}]},
                          "notes": [], "values": 2, "raise": None}),
        observations("published, already old", t + 90 * S + 3),
        *observation("wifi partial", "wifi", t + 100 * S, 3 * MS + 17, {"connected": None},
                     notes=[("wlan0.signal", "io_error"), ("x", "no_data")], values=1),
        observations("partial", t + 100 * S + 5 * MS),
        observations("wifi at stale_after", t + 115 * S),
        observations("wifi stale, data kept", t + 115 * S + 1),
        *observation("wifi error", "wifi", t + 116 * S, MS, None, notes=[("wlan0", "permission_denied")], values=0),
        *observation("battery unavailable", "battery", t + 116 * S, MS, None, notes=[("BAT0", "not_exposed")], values=0),
        observations("error and unavailable", t + 116 * S + 2 * MS),
        *observation("battery raised", "battery", t + 117 * S, MS, {"ignored": 1}, values=1, raise_="boom"),
        *observation("wifi with many issues", "wifi", t + 117 * S, MS, {"n": 1},
                     notes=[(f"wlan{i}" + "x" * 70, "io_error") for i in range(40)], values=0),
        observations("internal_error, issues truncated", t + 117 * S + 2 * MS),
        *collection("a collection", t + 118 * S, 100 * MS, good(t + 118 * S)),
        ("storage starts during a collection", {"op": "begin", "now": t + 119 * S, "wall": W0 + 119}),
        ("storage begins inside it", {"op": "slow_begin", "group": "storage", "now": t + 119 * S + MS, "wall": W0 + 119.001}),
        view("core view while both run", t + 119 * S + 2 * MS),
        observations("observations while both run", t + 119 * S + 2 * MS),
        ("storage finishes", {"op": "slow_finish", "group": "storage", "now": t + 119 * S + 3 * MS, "wall": W0 + 119.003,
                              "data": {"partitions": [1]}, "notes": [], "values": 1, "raise": None}),
        ("the collection finishes", {"op": "finish", "now": t + 119 * S + 4 * MS, "wall": W0 + 119.004,
                                     "result": good(t + 119 * S)}),
        view("core published", t + 119 * S + 5 * MS),
        observations("storage id 2", t + 119 * S + 5 * MS),
    ]


def stats_hist(t, n, temps=3, cpus=2, ns="netns:4026531840", ifs=(("eth0", 2), ("wlan0", 3)), rate=None, first=()):
    """Collector-shaped stats for history: CPU usage, memory, GPU, temperature and power with source ids,
    interfaces with a namespace and ifindex, disks; rate None gives rates, "none" gives gaps."""
    rates = (lambda k: None) if rate == "none" else (lambda k: float(n * 100 + len(k)))
    stats = {"cpu": [{"id": i, "usage": float(n + i) if i != 1 or n % 3 else None} for i in range(cpus)],
             "memory": {"total": 8 << 30, "used": (1 << 30) + n * 4096},
             "gpu": {"usage": 12.5, "freq": 1, "max_freq": None}, "npu": None,
             "temperature": {**{k: 30.0 for k in first}, **{f"t{i}": 40.0 + i + n / 10 for i in range(temps)}},
             "power": {"VDD_IN": 4.5, "x": "y"},
             "network": {"scope": {"kind": "process_network_namespace", "id": ns},
                         "interfaces": [{"name": name, "ifindex": idx, "rates": None if rate == "none" else
                                         {"rx_bytes_per_s": rates("rx"), "tx_bytes_per_s": 2**70 if n == 4 else rates("tx")}}
                                        for name, idx in ifs] + [{"name": "ghost0", "ifindex": None, "rates": {}}]},
             "disk_io": {"disks": [{"name": "sda", "major": 8, "minor": 0,
                                    "rates": {"read_bytes_per_s": float("1e300") if n == 5 else 1.5, "write_bytes_per_s": True}}]},
             "collectors": {}}
    sensors = {f"/temperature/t{i}": {"id": f"src1:{i:064x}" if i % 2 == 0 else None, "provider": "thermal",
                                      "identity_basis": "resolved_path", "unit": "celsius", "span": [t + 10, t + 20]}
               for i in range(temps)}
    sensors["/power/VDD_IN"] = {"id": "src1:" + "b" * 64, "provider": "ina3221", "identity_basis": "device_channel",
                                "unit": "watt", "span": [t + 10, t + 20]}
    sensors["/power/x"] = {"id": "src1:" + "c" * 64, "provider": "ina3221", "identity_basis": "device_channel",
                           "unit": "watt", "span": None}
    sensors["/gpu/usage"] = {"id": None, "provider": "drm", "identity_basis": "unresolved", "unit": "percent", "span": [t + 10, t + 20]}
    sensors["/gpu/freq"] = {**sensors["/gpu/usage"], "unit": "hertz"}
    sensors["/gpu/max_freq"] = {**sensors["/gpu/usage"], "unit": "hertz", "span": None}
    return result(stats, sensors, required(t + 5, t + 50_000), {"network": [t + 40, t + 50], "disk_io": [t + 60, t + 70]})


def history_steps():
    """Core and slow publications into History, read through each side's real /api/history."""
    out, t = [setup(slow=[("storage", 30.0), ("wifi", 5.0), ("battery", 60.0), ("probe", 10.0)], history=60.0)], T0
    for n in range(6):
        out += collection(f"core {n + 1}", t, 20 * MS, stats_hist(t, n))
        t += S
    out += [http("all series", t, "/api/history"),
            http("a prefix, percent-encoded", t, "/api/history?prefix=cpu%2F"),
            http("the last 2.5 s", t, "/api/history?seconds=2.5"),
            http("3 points: buckets with a null stay null", t, "/api/history?points=3"),
            http("1 point", t, "/api/history?points=1&prefix=memory")]
    out += collection("failed", t, 10 * MS, {"error": "x"})
    t += S
    out += collection("after a failure: a break", t, 10 * MS, stats_hist(t, 6))
    t += 9 * S
    out += collection("after 9 s without a publication: a break", t, 10 * MS, stats_hist(t, 7, rate="none"))
    t += S
    out += collection("another namespace, wlan0 gone, two more CPUs", t, 10 * MS,
                      stats_hist(t, 8, cpus=4, ns="netns:999", ifs=(("eth0", 2),)))
    out.append(http("new series, missing ones null", t + 1, "/api/history?prefix=network"))
    out += observation("storage", "storage", t, MS, {"filesystems": [
        {"major": 8, "minor": 1, "device": "sda1", "fstype": "ext4", "source": "/dev/sda1", "used_bytes": 10, "available_bytes": 20},
        {"major": 0, "minor": 30, "device": None, "fstype": "vfat", "source": "é", "used_bytes": 1.5, "available_bytes": None}]})
    out += observation("wifi", "wifi", t, MS, {"scope": {"id": "netns:1"}, "interfaces": [
        {"name": "wlan0", "ifindex": 3, "connected": True, "signal_dbm": -60},
        {"name": "wlan1", "ifindex": 4, "connected": False, "signal_dbm": -70},
        {"name": "wlan2", "ifindex": None, "connected": True, "signal_dbm": -50}]})
    out += observation("battery", "battery", t, MS, {"batteries": [
        {"name": "BAT0", "percent": 80, "charge_now_uah": None, "energy_now_uwh": 1000, "power_uw": None}]})
    out += observation("probe", "probe", t, MS, {"targets": [
        {"address": "192.0.2.1", "port": 22, "connect_ms": 1.25}, {"address": "2001:db8::1", "port": 443, "connect_ms": None}]})
    out += observation("storage started before its last one: ignored", "storage", t - S, MS, {"filesystems": [
        {"major": 8, "minor": 1, "device": "sda1", "fstype": "ext4", "source": "/dev/sda1", "used_bytes": 99, "available_bytes": 1}]})
    out.append(http("observation series", t + 2 * S, "/api/history?prefix=observation"))
    out += observation("wifi taking longer than 3 intervals: nulls", "wifi", t + 5 * S, 16 * S, {"scope": {"id": "netns:1"},
                       "interfaces": [{"name": "wlan0", "ifindex": 3, "connected": True, "signal_dbm": -61}]})
    out += observation("battery in error: nulls", "battery", t + 61 * S, MS, None, notes=[("BAT0", "io_error")], values=0)
    out.append(http("observation series and their intervals", t + 62 * S, "/api/history?prefix=observation"))
    t += 70 * S
    out += collection("70 s later: expired series dropped", t, 10 * MS, stats_hist(t, 9, temps=70, cpus=4))
    out.append(http("70 temperatures: the limit, by priority", t + 1, "/api/history?points=2"))
    out += collection("8 CPUs: core drops its own least important", t + S, 10 * MS, stats_hist(t + S, 10, temps=70, cpus=8))
    out.append(http("dropped counted once each", t + S + 1, "/api/history?points=1&prefix=cpu"))
    out += collection("two new temperatures listed first: the kept ones keep their slots", t + 2 * S, 10 * MS,
                      stats_hist(t + 2 * S, 11, temps=70, cpus=8, first=("a0", "a1")))
    out.append(http("no slot taken by the newcomers", t + 2 * S + 1, "/api/history?points=1&prefix=temperature"))
    for label, q in [("unknown field", "x=1"), ("repeated", "points=1&points=2"), ("four fields", "a&b&c&d"),
                     ("seconds 0", "seconds=0"), ("seconds past retention", "seconds=60.5"), ("seconds nan", "seconds=nan"),
                     ("seconds with underscores", "seconds=1_0"), ("seconds _1", "seconds=_1"), ("points 601", "points=601"),
                     ("points +5", "points=+5"), ("points empty", "points="), ("points not a number", "points=x"),
                     ("prefix of 129", "prefix=" + "a" * 129), ("prefix of 128", "prefix=" + "a" * 128),
                     ("seconds with spaces", "seconds=%201%20"), ("blank fields", "&&points=2")]:
        out.append(http(f"query: {label}", t + 2 * S, f"/api/history?{q}"))
    return out


def replay_steps():
    """Core and slow publications into Replay, read through each side's real /api/samples."""
    out, t = [setup(slow=[("storage", 30.0), ("wifi", 5.0)], replay=(30.0, 15000))], T0
    for n in range(5):
        out += collection(f"core {n + 1}", t, 20 * MS + n, good(t, n))
        t += S
    out += [http("from the start", t, "/api/samples"),
            http("a page of 2", t, "/api/samples?limit=2"),
            http("after 2", t, f"/api/samples?after=2&instance_id={INSTANCE}&limit=2"),
            http("at the latest", t, f"/api/samples?after=5&instance_id={INSTANCE}"),
            http("ahead of it", t, f"/api/samples?after=6&instance_id={INSTANCE}"),
            http("another instance", t, "/api/samples?instance_id=x"),
            http("after without an instance", t, "/api/samples?after=1")]
    for label, q in [("limit 0", "limit=0"), ("limit 101", "limit=101"), ("limit x", "limit=x"), ("limit 0100", "limit=0100"),
                     ("after -1", "after=-1"), ("after 01", f"after=01&instance_id={INSTANCE}"), ("after 2^63", f"after=9223372036854775808&instance_id={INSTANCE}"),
                     ("after empty", "after="), ("unknown", "x=1"), ("repeated", "limit=1&limit=2"), ("five fields", "a&b&c&d&e"),
                     ("group on the core stream", "group=storage"), ("instance of 129", "instance_id=" + "i" * 129),
                     ("instance empty", "instance_id=")]:
        out.append(http(f"query: {label}", t, f"/api/samples?{q}"))
    out += collection("failed", t, 10 * MS, {"error": "x"})
    out += collection("too slow", t + S, 6 * S, good(t + S, 9))
    t += 8 * S
    out += collection("recovered: gap_before", t, 10 * MS, good(t, 6))
    big = good(t + S, 7)
    big["stats"]["blob"] = "x" * 50000
    out += collection("larger than max_bytes: not kept", t + S, 10 * MS, big)
    out.append(http("its id is known: latest and a missing range", t + S + 20 * MS, f"/api/samples?after=6&instance_id={INSTANCE}"))
    t += 2 * S
    out += collection("after it", t, 10 * MS, good(t, 8))
    out += observation("storage", "storage", t, MS, {"filesystems": [{"mount": "/", "nom": "é"}]})
    out += observation("wifi partial", "wifi", t, MS, {"connected": None}, notes=[("wlan0", "io_error")])
    out += [http("capacity evicted the oldest; the big one is missing", t + S, "/api/samples"),
            http("storage", t + S, "/api/observation-samples?group=storage"),
            http("wifi, 20 s later: stale", t + 20 * S, f"/api/observation-samples?group=wifi&instance_id={INSTANCE}"),
            http("no such group stream", t + S, "/api/observation-samples?group=probe"),
            http("not a group", t + S, "/api/observation-samples?group=disk"),
            http("no group", t + S, "/api/observation-samples"),
            http("core 25 s later: stale", t + 25 * S, f"/api/samples?after=7&instance_id={INSTANCE}"),
            http("all expired: the cursor still moves on", t + 60 * S, f"/api/samples?after=3&instance_id={INSTANCE}")]
    return out


def stats_screen(n):
    """Stats with every key the /text screen reads, in a few variants."""
    st = {"model": "NVIDIA Jetson Orin Nano Super", "platform": "Jetson Orin", "power_mode": "MAXN_SUPER" if n % 2 else None,
          "uptime": 93784.9 + n, "system": {"os": "Ubuntu 22.04", "kernel": "5.15", "arch": "aarch64", "hostname": "h",
                                           "l4t": "36.5", "extra": 3, "empty": ""},
          "cpu": [{"id": i, "usage": [0.0, 12.25, 99.95, 100.0, 12.5][i], "freq": [None, 0, 729600000, 1510400000, 2500][i]}
                  for i in range(5)] if n != 2 else [],
          "cpu_sampling": {"mode": "interval", "window_ms": 1000.0,
                           "unavailable": [{"id": 5, "reason": "warmup"}, {"id": 6, "reason": "gap"}, {"id": 7, "reason": "odd"},
                                           {"id": 8, "reason": "warmup"}] if n else []},
          "gpu": {"usage": 37.5, "freq": 306000000, "max_freq": 1020000000} if n != 1 else {"usage": None, "freq": None},
          "npu": {"usage": 0.0, "freq": None} if n == 1 else None,
          "memory": {"total": 8 << 30, "used": 3 * (1 << 30) + 123456789, "swap_total": (4 << 30) if n % 2 else 0, "swap_used": 1 << 28},
          "disk": {"total": 100 << 30, "used": 0 if n == 3 else 55 << 30},
          "temperature": {"cpu": 51.25, "gpu": 49.95, "tj": 52.0} if n != 3 else {},
          "power": {"VDD_IN": 4.755, "VDD_CPU_GPU_CV": 0.005} if n != 3 else {},
          "fans": [{"name": "pwmfan", "rpm": 2100, "percent": 35}, {"name": "x", "rpm": None, "percent": None}],
          "network": {"scope": {"id": "netns1:x"}, "interfaces": [
              {"name": "eth0", "rates": {"rx_bytes_per_s": 1023.5, "tx_bytes_per_s": 1048576.0, "rx_packets_per_s": 10.25,
                                         "tx_packets_per_s": 0.0}, "window_ms": 1000.5, "rx": {"errors": 0, "dropped": 2},
               "tx": {"errors": 1, "dropped": 0}},
              {"name": "a-very-long-interface-name", "rates": None, "reason": "warmup"},
              {"name": "wlan0", "rates": {}, "reason": "something_new"}] + [
              {"name": f"veth{i}", "rates": None, "reason": None} for i in range(12 if n == 1 else 0)]},
          "disk_io": {"disks": [{"name": "nvme0n1", "rates": {"read_bytes_per_s": 5e9, "reads_per_s": 1.0, "write_bytes_per_s": 0.0,
                                                               "writes_per_s": 0.0, "io_time_ratio": 0.0125}, "window_ms": 999,
                                 "in_flight": 3},
                                {"name": "sda", "rates": None, "reason": "gap", "in_flight": None}]},
          "pressure": {"resources": {"cpu": {"some": {"avg10": 1.25}}, "io": {"some": {"avg10": 0.0}, "full": {"avg10": 0.05}}}},
          "collectors": {"fans": {"state": "partial"}, "gpu": {"state": "error"}, "npu": {"state": "unavailable"}}}
    return st


def text_steps():
    """The /text screen from each side's real server: snapshots and slow observations of every kind."""
    out, t = [setup(slow=[("storage", 30.0), ("wifi", 5.0), ("battery", 60.0), ("probe", 10.0)])], T0
    out.append(http("no snapshot yet: 503", t, "/text"))
    obs = [("storage", {"partitions": [{"disk": "nvme0n1", "mount_points": ["/"]}, {"disk": "nvme0n1", "mount_points": []},
                                       {"disk": "sda", "mount_points": []}],
                        "filesystems": [{"device": "nvme0n1p1", "major": 259, "minor": 1, "fstype": "ext4",
                                         "mount_points": [{"path": "/", "read_only": False}, {"path": "/b", "read_only": True}],
                                         "total_bytes": 982806532096, "used_bytes": 113806532096, "available_bytes": 819000000000},
                                        {"device": None, "major": 0, "minor": 30, "fstype": "btrfs",
                                         "mount_points": [{"path": "/data", "read_only": True}], "total_bytes": 0,
                                         "used_bytes": 0, "available_bytes": 0},
                                        {"device": "sda1", "major": 8, "minor": 1, "fstype": "vfat",
                                         "mount_points": [{"path": "/boot", "read_only": False}], "total_bytes": None,
                                         "used_bytes": None, "available_bytes": None}]}),
           ("wifi", {"interfaces": [{"name": "wlan0", "connected": True, "signal_dbm": -61, "signal_raw": None, "link_quality": 49,
                                     "noise_dbm": -90},
                                    {"name": "wlan1", "connected": True, "signal_dbm": None, "signal_raw": 45, "link_quality": None,
                                     "noise_dbm": None},
                                    {"name": "wlan2", "connected": True, "signal_dbm": None, "signal_raw": None,
                                     "link_quality": None, "noise_dbm": None},
                                    {"name": "wlan3", "connected": False}, {"name": "wlan4", "connected": None,
                                                                            "connection_state": "permission_denied"},
                                    {"name": "wlan5", "connected": None}]}),
           ("battery", {"batteries": [{"name": "BAT0", "present": True, "percent": 80, "status": "Discharging", "voltage_uv": 12000000,
                                       "current_ua": -1500000, "power_uw": None, "cycle_count": 42, "charge_full_uah": 4000000,
                                       "charge_full_design_uah": 4400000},
                                      {"name": "BAT1", "present": False},
                                      {"name": "BAT2", "present": None, "percent": None, "capacity_level": "Low", "current_ua": 0,
                                       "energy_full_uwh": 5, "energy_full_design_uwh": 0, "power_uw": 7000000},
                                      {"name": "BAT3", "percent": None, "status": None}],
                         "external": [{"name": "AC", "online": True}, {"name": "USB", "online": False},
                                      {"name": "W", "online": None}]}),
           ("probe", {"targets": [{"address": "192.0.2.1", "port": 22, "connect_ms": 1.25, "reason": None, "failures": 0, "attempts": 3},
                                  {"address": "2001:db8::1", "port": 443, "connect_ms": None, "reason": "timeout", "failures": 3,
                                   "attempts": 10}]})]
    for g, data in obs:
        out += observation(g, g, t, MS, data)
    for n in range(4):
        stats = stats_screen(n)
        out += collection(f"variant {n}", t + (n + 1) * S, MS, result(stats, {}, required(t + (n + 1) * S, t + (n + 1) * S + 5)))
        out.append(http(f"variant {n}: the screen", t + (n + 1) * S + 2 * MS, "/text"))
    out.append(http("200 s later: the snapshot stale", t + 200 * S, "/text"))
    out += collection("a fresh snapshot", t + 200 * S, MS, result(stats_screen(0), {}, required(t + 200 * S, t + 200 * S + 5)))
    out.append(http("observations not current", t + 200 * S + 2 * MS, "/text"))
    bad = stats_screen(0)
    bad["temperature"]["cpu"] = None  # Python cannot format it: no answer
    out += collection("a temperature without a value", t + 201 * S, MS, result(bad, {}, required(t + 201 * S, t + 201 * S + 5)))
    out.append(http("no answer on both", t + 201 * S + 2 * MS, "/text"))
    nomodel = stats_screen(0)
    del nomodel["model"]
    out += collection("no model", t + 202 * S, MS, result(nomodel, {}, required(t + 202 * S, t + 202 * S + 5)))
    out.append(http("no answer on both", t + 202 * S + 2 * MS, "/text"))
    out += observation("storage of another shape", "storage", t + 203 * S, MS, {"filesystems": [{"mount_points": []}]})
    out += collection("a good snapshot", t + 203 * S, MS, result(stats_screen(1), {}, required(t + 203 * S, t + 203 * S + 5)))
    out.append(http("storage that cannot be shown: no answer", t + 203 * S + 2 * MS, "/text"))
    return out


def replay_off_steps():
    return [setup(), *collection("one", T0, MS, good(T0)),
            http("replay off", T0 + S, "/api/samples"), http("history off", T0 + S, "/api/history"),
            http("status with levels", T0 + S, "/api/status"), http("stats", T0 + S, "/api/stats"),
            http("observations", T0 + S, "/api/observations"), http("unknown", T0 + S, "/x"),
            http("HEAD", T0 + S, None, "HEAD /api/status HTTP/1.0\r\n\r\n")]


SCENARIOS = {"core": core_steps, "provenance": provenance_steps, "observations": observation_steps,
             "history": history_steps, "replay": replay_steps, "features_off": replay_off_steps, "text": text_steps}


# --- comparing -----------------------------------------------------------------------------------------

def unstamped(response):
    """An HTTP answer without its Date and Server headers, which differ by design, and without the markup of
    an error page and its length, which differ between Python versions (the status line, with the
    error's message, stays)."""
    head, sep, body = response.partition("\r\n\r\n")
    if not head.startswith("HTTP/"):
        return response
    lines = [h for h in head.split("\r\n") if not h.startswith(("Date: ", "Server: "))]
    if "Content-Type: text/html;charset=utf-8" in lines:
        lines = [h for h in lines if not h.startswith("Content-Length: ")]
        body = "<error page>" if body else ""
    return "\r\n".join(lines) + sep + body


def show(name, a, b, out):
    """The first differences of two JSON texts, by pointer when both parse."""
    try:
        x, y = json.loads(a.partition("\r\n\r\n")[2] or a), json.loads(b.partition("\r\n\r\n")[2] or b)
    except (TypeError, ValueError):
        out.append(f"/{name}: {a!r:.200} (python) != {b!r:.200} (rust)")
        return
    before = len(out)
    diff(x, y, f"/{name}", out)
    if len(out) == before:
        out.append(f"/{name}: the same JSON, different text: {a!r:.200} / {b!r:.200}")


def compare(steps, py_cmd, rs_cmd, timeout=TIMEOUT):
    """Runs the operations on both sides: (differences, report lines). Every answer must be the same; both
    sides must exit with 0. A side that breaks ends the run."""
    logs = {n: tempfile.TemporaryFile("w+") for n in ("python", "rust")}
    procs = {"python": start(py_cmd, logs["python"]), "rust": start(rs_cmd, logs["rust"])}
    report, problems, equal = [], 0, 0
    for i, (label, op) in enumerate(steps):
        out, res = [], {}
        for n, p in procs.items():
            try:
                res[n] = ask(p, json.dumps(op), timeout)
            except Broken as e:
                out.append(f"{n}: {e}")
        if len(res) == 2:
            a, b = res["python"], res["rust"]
            if not isinstance(a, dict) or not isinstance(b, dict) or list(a) != list(b):
                out.append(f"answers {json.dumps(a)[:120]} (python) != {json.dumps(b)[:120]} (rust)")
            else:
                for k in a:
                    x, y = (unstamped(a[k]), unstamped(b[k])) if k == "response" else (a[k], b[k])
                    if x != y:
                        show(k, x, y, out)
        if not out:
            equal += 1
        report.append(f"  {i + 1}. {label}: " + ("OK" if not out else f"{len(out)} DIFFERENCES"))
        report += [f"       {d}" for d in out[:20]]
        problems += len(out)
        if len(res) < 2:
            report.append("       stopped: a side did not answer")
            break
    for n, p in procs.items():
        p.stdin.close()
        try:
            code = p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            code = f"{p.wait()} (killed: no exit 10 s after its input closed)"
        if code != 0:
            report.append(f"  {n} exited with {code}")
            problems += 1
        if code != 0 or problems:
            logs[n].seek(0)
            report += [f"       {n} stderr: {t}" for t in logs[n].read().splitlines()[-5:]]
        logs[n].close()
    report.append(f"  {equal}/{len(steps)} answers equal")
    return problems, report


def run(name, binary):
    problems, report = compare(SCENARIOS[name](), [sys.executable, f"{HERE}/runtime_py.py"], [binary, "runtime-script"])
    print(f"{name}: {'OK' if not problems else 'FAILED'}")
    print("\n".join(report))
    return problems


# --- the harness against itself ------------------------------------------------------------------------

FAKE = """import json, sys, time
kind, n = sys.argv[1], 0
for line in sys.stdin:
    n += 1
    if kind == "hang" and n == 2:
        time.sleep(60)
    if kind == "exit_early" and n == 2:
        sys.exit(0)
    text = json.dumps({"n": n, "x": 2.5 if kind == "other" and n == 2 else 1.5})
    if kind == "spaced" and n == 2:
        text = text.replace(": ", ":")
    print(json.dumps({"stats": text}), flush=True)
sys.exit(3 if kind == "exit3" else 0)
"""

CONTROLS = [  # name, python side, rust side, whether the harness must pass it
    ("equal answers", "ok", "ok", True),
    ("rust's answer differs", "ok", "other", False),
    ("the same JSON, other text", "ok", "spaced", False),
    ("rust exits early", "ok", "exit_early", False),
    ("rust exits with 3", "ok", "exit3", False),
    ("rust hangs", "ok", "hang", False),
]


def self_test():
    wrong, lines = 0, []
    steps = [(f"op {i}", {"op": "x"}) for i in range(3)]
    for name, a, b, passes in CONTROLS:
        problems, report = compare(steps, [sys.executable, "-c", FAKE, a], [sys.executable, "-c", FAKE, b], timeout=2)
        if (problems == 0) != passes:
            wrong += 1
            lines += [f"  {name}: {'failed' if passes else 'passed'}, must {'pass' if passes else 'fail'}"] + report
    print(f"harness self-test: {len(CONTROLS) - wrong}/{len(CONTROLS)} controls as expected")
    if wrong:
        print("\n".join(lines))
    return not wrong


def main():
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(0 if self_test() else 1)
    if not self_test():
        sys.exit(1)
    binary, names = os.path.abspath(sys.argv[1]), sys.argv[2:] or list(SCENARIOS)
    failed = sum(run(n, binary) > 0 for n in names)
    print(f"{len(names) - failed}/{len(names)} scenarios equal")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
