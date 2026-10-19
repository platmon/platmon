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


def setup(interval=1.0, slow=(), clock="boottime"):
    return ("setup", {"op": "setup", "interval": interval, "instance_id": "8dfad9fa-9bf0-4d61-91e0-ef22aebd2b91",
                      "clock": clock, "slow": [list(g) for g in slow]})


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


SCENARIOS = {"core": core_steps, "provenance": provenance_steps, "observations": observation_steps}


# --- comparing -----------------------------------------------------------------------------------------

def show(name, a, b, out):
    """The first differences of two JSON texts, by pointer when both parse."""
    try:
        x, y = json.loads(a), json.loads(b)
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
                    if a[k] != b[k]:
                        show(k, a[k], b[k], out)
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
