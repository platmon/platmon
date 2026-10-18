"""The stage recorder of benchmarks/breakdown.py: its arithmetic, its threads, and that wrapping the product
in place neither changes what it does nor stays behind. No performance thresholds."""
import copy
import json
import threading
import time

import pytest

from collector import metadata
from collector import core as collector
from benchmarks import breakdown, cadence
from collector import common, sysfs
from runtime import sampler
from runtime.sampler import Sampler, pick_clock
from runtime import server


class FakeClock:
    """Both clocks of the recorder; the timed functions advance it."""

    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(breakdown, "mono", c)
    monkeypatch.setattr(breakdown, "tcpu", c)
    return c


def recorder(capacity=100):
    rec = breakdown.Recorder(cadence.Table)
    rec.allocate(capacity, capacity)
    return rec


def rows(rec):
    d = rec.dump()
    out = {}
    for r in cadence.rows(d["rows"]):
        out[d["names"][r["name"]]] = r
    return out


def test_nested_inclusive_and_self_do_not_overlap(clock):
    rec = recorder()

    def inner():
        clock.t += 5

    inner = rec.wrap("inner", inner)

    def outer():
        clock.t += 3
        inner()
        inner()

    outer = rec.wrap("outer", outer)
    begun = rec.begin()
    outer()
    rec.end("collection", begun)
    r = rows(rec)
    assert (r["outer"]["calls"], r["outer"]["incl_cpu_ns"], r["outer"]["self_cpu_ns"]) == (1, 13, 3)
    assert (r["inner"]["calls"], r["inner"]["incl_cpu_ns"], r["inner"]["self_cpu_ns"]) == (2, 10, 10)
    unit = cadence.rows(rec.dump()["units"])[0]
    assert unit["cpu_ns"] == 13 and unit["wrapped_calls"] == 3 and unit["kind"] == breakdown.KINDS["collection"]


def test_recursion_counts_the_outermost_call_only(clock):
    rec = recorder()

    def walk(n):
        clock.t += 1
        return n if n == 0 else walk(n - 1)

    walk = rec.wrap("walk", walk)
    begun = rec.begin()
    assert walk(4) == 0
    rec.end("collection", begun)
    r = rows(rec)["walk"]
    assert r["calls"] == 1 and r["incl_cpu_ns"] == 5 and r["self_cpu_ns"] == 5


def test_wrapper_calls_once_and_passes_results_and_exceptions():
    rec = recorder()
    calls = []

    def fn(x, y=0):
        calls.append(x)
        if x < 0:
            raise KeyError(x)
        return x + y

    w = rec.wrap("fn", fn)
    begun = rec.begin()
    assert w(1, y=2) == 3
    with pytest.raises(KeyError):
        w(-1)
    rec.end("collection", begun)
    assert calls == [1, -1] and rows(rec)["fn"]["calls"] == 2


def test_threads_keep_their_own_units():
    rec = recorder()
    gate = threading.Barrier(2)
    w = rec.wrap("work", lambda: None)

    def unit(n):
        begun = rec.begin()
        gate.wait()  # both units open at once
        for _ in range(n):
            w()
        gate.wait()
        rec.end("request", begun)

    threads = [threading.Thread(target=unit, args=(n,)) for n in (3, 7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    d = rec.dump()
    assert sorted(r["calls"] for r in cadence.rows(d["rows"])) == [3, 7]
    assert len({u["unit"] for u in cadence.rows(d["units"])}) == 2


def test_buffers_are_bounded():
    rec = recorder(capacity=2)
    w = rec.wrap("w", lambda: None)
    for _ in range(3):
        begun = rec.begin()
        w()
        rec.end("listener", begun)
    d = rec.dump()
    assert len(d["units"]["rows"]) == 2 and d["units"]["dropped"] == 1 and d["rows"]["dropped"] == 1


def test_install_reaches_from_imported_call_sites_and_restores(capsys):
    originals = {"collect": common.collect, "entries": common.entries, "read": common.read,
                 "group_read": metadata.Group.__dict__["read"], "copy": sampler.copy, "json": server.json,
                 "shutil": common.shutil, "sample": common.CpuCounters.__dict__["sample"]}
    rec = recorder(1000)
    with breakdown.install(rec):
        begun = rec.begin()
        collector.collect()
        sampler.copy.deepcopy({"a": [{"b": 1}, {"c": [2, 3]}]})  # recursive inside: one call
        assert server.json.dumps({"x": 1}) == json.dumps({"x": 1})
        rec.end("collection", begun)
    r = rows(rec)
    for name in ("common.collect", "common.thermal_zones", "common.hwmon_sensors", "common.meminfo",
                 "common.disk_usage", "common.read (uptime, model, os-release)", "common.cpu_sample",
                 "work.entries", "collect._collect", "collect.summarize"):
        assert r[name]["calls"] >= 1, name
    assert r["deepcopy"]["calls"] == 1 and r["json.dumps"]["calls"] == 1
    assert (common.collect, common.entries, common.read, metadata.Group.__dict__["read"], sampler.copy, server.json,
            common.shutil, common.CpuCounters.__dict__["sample"]) == tuple(originals.values())
    assert sampler.copy is copy and server.json is json
    assert capsys.readouterr().out == ""  # nothing of the bench on stdout


def test_install_restores_after_an_exception():
    with pytest.raises(RuntimeError):
        with breakdown.install(recorder()):
            raise RuntimeError
    assert sampler.copy is copy and not hasattr(common.collect, "__wrapped_stage__")


def test_product_output_and_reads_are_the_same_with_stages(monkeypatch):
    """Same reads (paths, in order), same keys, source ids and collector states; provenance still checks out."""
    paths = []
    plain_read = metadata.Group.read

    def counting(self, path, *args, **kwargs):
        paths.append(path)
        return plain_read(self, path, *args, **kwargs)
    monkeypatch.setattr(metadata.Group, "read", counting)

    def once(staged):
        paths.clear()
        clock = pick_clock()
        cpu = common.CpuCounters(clock[0])
        collect = lambda: collector.collect_recorded(cpu, {}, clock[0])  # noqa: E731
        s = Sampler(collect, 1.0, clock=clock)
        rec = recorder(1000)
        ctx = breakdown.install(rec) if staged else breakdown.contextlib.nullcontext()
        with ctx:
            s._attempt()
            time.sleep(0.2)  # ticks between the readings, so the second collection has CPU rows and clocks
            s._attempt()
            stats = s.read()[0]
        return stats, list(paths)

    plain, plain_paths = once(False)
    staged, staged_paths = once(True)

    def split(ps):  # which cores have a usage (and so a clock read) depends on the ticks between readings
        return [p for p in ps if "/cpufreq/" not in p], sum("/cpufreq/" in p for p in ps)
    assert split(staged_paths)[0] == split(plain_paths)[0]
    assert split(staged_paths)[1] == len(staged["cpu"]) and split(plain_paths)[1] == len(plain["cpu"])
    assert set(staged) == set(plain)

    def ids(stats):  # CPU clock records by core id: the row position depends on which cores had ticks
        meta = stats.get("sensor_meta", {})
        out = {k: v["id"] for k, v in meta.items() if not k.startswith("/cpu/")}
        for i, row in enumerate(stats["cpu"]):
            if f"/cpu/{i}/freq" in meta:
                out[f"cpu {row['id']}"] = meta[f"/cpu/{i}/freq"]["id"]
        return out
    common_cores = {f"cpu {r['id']}" for r in staged["cpu"]} & {f"cpu {r['id']}" for r in plain["cpu"]}
    a, b = ids(staged), ids(plain)
    assert {k: v for k, v in a.items() if not k.startswith("cpu ") or k in common_cores} == \
        {k: v for k, v in b.items() if not k.startswith("cpu ") or k in common_cores}
    def states(stats):  # cpu_frequency is "unavailable" when no core had ticks between the two readings
        return {k: v["state"] for k, v in stats["collectors"].items()
                if k != "cpu_frequency" or (stats["cpu"] and plain["cpu"] and staged["cpu"])}
    assert states(staged) == states(plain)
    assert staged["sample"]["data_age_basis"] == plain["sample"]["data_age_basis"]


def serve_result(units, rows_, names, t0=100, t1=200, elapsed=1.0, cpu=(0.0, 0.5)):
    return {"measured_elapsed_s": elapsed, "start": {"mono": t0, "self": {"ru_utime": cpu[0], "ru_stime": 0}},
            "end": {"mono": t1, "self": {"ru_utime": cpu[1], "ru_stime": 0}},
            "breakdown": {"names": names,
                          "rows": {"fields": ["unit", "name", "calls", "incl_ns", "incl_cpu_ns", "self_cpu_ns"], "rows": rows_},
                          "units": {"fields": ["t", "end", "kind", "unit", "cpu_ns", "thread_cpu_ns", "wrapped_calls"],
                                    "rows": units}}}


def test_stage_table_uses_window_units_only():
    names = ["a"]
    units = [[50, 60, 0, 1, 10**8, 0, 1],      # warmup: left out
             [120, 130, 0, 2, 10**8, 0, 1],
             [190, 250, 0, 3, 10**8, 0, 1],     # starts inside, ends after the window
             [150, 160, 1, 4, 10**7, 3 * 10**7, 1]]
    rows_ = [[1, 0, 1, 9 * 10**6, 10**8, 10**8], [2, 0, 2, 4 * 10**6, 4 * 10**6, 3 * 10**6],
             [3, 0, 2, 2 * 10**6, 2 * 10**6, 10**6]]
    s = serve_result(units, rows_, names)
    table, n = breakdown.stage_table([s], "collection")
    assert n == 2
    a = table[0]
    assert a["calls_per_unit"] == 2 and a["cpu_ms_per_unit"] == 3 and a["self_cpu_ms_per_unit"] == 2
    assert a["cpu_pct_one_core"] == pytest.approx(0.6) and a["units_with_stage"] == 2
    acc = breakdown.accounting(s)
    assert acc["parts"]["collection"] == {"units": 2, "straddling": 1, "cpu_s": 0.2}
    assert acc["parts"]["request"]["cpu_s"] == 0.03  # the worker thread's whole CPU, not cpu_ns
    assert acc["unattributed_cpu_s"] == pytest.approx(0.5 - 0.23)


def test_negative_unattributed_is_kept():
    s = serve_result([[120, 130, 0, 1, 9 * 10**8, 0, 0]], [], [], cpu=(0.0, 0.5))
    assert breakdown.accounting(s)["unattributed_cpu_s"] == pytest.approx(-0.4)


def test_wrapper_cost_is_measured():
    c = breakdown.wrapper_cost(n=2000)
    assert c["calls"] == 2000 and c["clock_calls_per_wrapped_call"] == 4


SUPPORTED = breakdown.profile_support() is None


def test_profile_support_by_version():
    assert breakdown.profile_support((3, 11)) is None
    assert "sys.monitoring" in breakdown.profile_support((3, 12)) and breakdown.profile_support((3, 13, 5))


@pytest.mark.skipif(SUPPORTED, reason="per-thread cProfile works on this Python")
def test_profiles_refused_where_threads_mix():
    with pytest.raises(RuntimeError, match="unsupported"):
        breakdown.Profiles()


def only_on_main_thread_xyz():
    return sum(i * i for i in range(20000))


def only_on_worker_thread_abc(n):
    return sorted(range(n), key=lambda x: -x)


@pytest.mark.skipif(not SUPPORTED, reason="profile modes are refused on this Python")
def test_profile_records_only_its_thread_while_others_run():
    """A: the worker's profile is enabled while the main thread runs a function of its own."""
    p = breakdown.Profiles()
    p.on.set()
    enabled, release = threading.Event(), threading.Event()

    def worker():
        with p.profiled():
            only_on_worker_thread_abc(100)
            enabled.set()
            release.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert enabled.wait(5)
        only_on_main_thread_xyz()
    finally:
        release.set()
        t.join()
    r = p.report(top=50)
    assert r["units"] == 1 and r["total_s"] >= 0
    assert "only_on_worker_thread_abc" in r["text"] and "only_on_main_thread_xyz" not in r["text"]


@pytest.mark.skipif(not SUPPORTED, reason="profile modes are refused on this Python")
def test_overlapping_profiles_keep_their_own_threads():
    """B: two profiles active at the same time, each thread's functions only in its own."""
    p = breakdown.Profiles()
    p.on.set()
    both, release, errors = threading.Barrier(3), threading.Event(), []

    def worker(fn):
        try:
            with p.profiled():
                fn()
                both.wait(5)
                release.wait(5)
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(f,))
               for f in (only_on_main_thread_xyz, lambda: only_on_worker_thread_abc(100))]
    for t in threads:
        t.start()
    try:
        both.wait(5)  # both profiles are enabled here
    finally:
        release.set()
        for t in threads:
            t.join()
    assert not errors and len(p.done) == 2
    texts = []
    for prof in p.done:
        out = breakdown.io.StringIO()
        breakdown.pstats.Stats(prof, stream=out).print_stats()
        texts.append(out.getvalue())
    assert sum("only_on_main_thread_xyz" in t for t in texts) == 1
    assert sum("only_on_worker_thread_abc" in t for t in texts) == 1


def test_invalid_profiles_are_marked_not_ranked():
    """C: a profile from an unsupported Python or with a negative total is kept but not usable."""
    good = {"python": "3.10.12", "profile": {"total_s": 1.5, "text": "x"}}
    assert breakdown.profile_validity(good) is None
    assert "sys.monitoring" in breakdown.profile_validity({**good, "python": "3.13.5"})
    old = {"python": "3.10.12", "profile": {"text": "  123 function calls in -1.616 seconds"}}
    assert "not a valid" in breakdown.profile_validity(old)


def test_lite_install_leaves_frequent_calls_unwrapped():
    rec = recorder(1000)
    with breakdown.install(rec, breakdown.LITE_SKIP):
        assert not hasattr(metadata.Group.__dict__["read"], "__wrapped_stage__")
        assert hasattr(sysfs.canonical, "__wrapped_stage__") and hasattr(common.collect, "__wrapped_stage__")
    assert not hasattr(sysfs.canonical, "__wrapped_stage__")


@pytest.fixture(scope="module")
def serve_pair(tmp_path_factory):
    """One short real reference run and one stages run (no clients), as the report reads them."""
    d = tmp_path_factory.mktemp("serve")
    out = {}
    for mode in ("reference", "stages"):
        path = d / f"{mode}.json"
        cadence.main(["serve", "--variant", "B0", "--instrumentation", mode, "--interval", "0.1",
                      "--warmup", "0.3", "--measure", "0.6", "--port", "0", "--out", str(path)])
        out[mode] = json.loads(path.read_text())
    # two real runs on a shared host may see a different scope (a sensor coming or going); these tests
    # are about which runs the report compares, so the pair starts out equal in every condition
    out["stages"]["scope"] = out["reference"]["scope"]
    return out


def record(serve, run_id, phase="breakdown", poll=1.0, **change):
    serve = copy.deepcopy(serve)
    serve.update(change)
    return {"kind": "run", "run_id": run_id, "alias": "t", "valid": True, "order": 0, "serve": serve,
            "cond": {"phase": phase, "clients": 0, "poll": poll}, "before": {"temps_mC": {}, "cpufreq_khz": {}},
            "after": {"temps_mC": {}, "cpufreq_khz": {}}}


def impact(tmp_path, recs):
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return json.loads(breakdown.report([str(path)]))["impact"]


def test_report_compares_matching_runs(tmp_path, serve_pair):
    rows_ = impact(tmp_path, [record(serve_pair["reference"], "r1"), record(serve_pair["stages"], "s1")])
    assert len(rows_) == 1 and rows_[0]["problems"] == [] and rows_[0]["delta_pp"] is not None
    assert rows_[0]["reference"] == [serve_pair["reference"]["cpu_pct_one_core"]]


@pytest.mark.parametrize("field, ref_change, staged_change", [
    ("source_hash", {"source_hash": "A"}, {"source_hash": "B"}),
    ("bench_hash", {"bench_hash": "A"}, {"bench_hash": "B"}),
    ("python", {"python": "3.10.12"}, {"python": "3.13.5"}),
    ("interval_s", {"interval_s": 1.0}, {"interval_s": 2.0}),
    ("scope", {}, {"scope": {"cpu_rows": 999}}),
])
def test_report_refuses_runs_that_differ(tmp_path, serve_pair, field, ref_change, staged_change):
    rows_ = impact(tmp_path, [record(serve_pair["reference"], "r1", **ref_change),
                              record(serve_pair["stages"], "s1", **staged_change)])
    staged = [r for r in rows_ if r["mode"] == "stages"]
    assert len(staged) == 1 and staged[0]["delta_pp"] is None
    assert any(field in p for p in staged[0]["problems"])
    md = breakdown.markdown({"impact": staged, "collection": [], "request": [], "accounting": [],
                             "profiles": [], "invalid": []})
    assert "not comparable" in md and "| ok |" not in md


def test_report_refuses_different_poll(tmp_path, serve_pair):
    rows_ = impact(tmp_path, [record(serve_pair["reference"], "r1", poll=1.0),
                              record(serve_pair["stages"], "s1", poll=2.0)])
    assert [r["delta_pp"] for r in rows_] == [None] and "poll_s" in rows_[0]["problems"][0]


def test_report_keeps_stages_and_lite_apart(tmp_path, serve_pair):
    lite = record(serve_pair["stages"], "l1", instrumentation="stages-lite")
    rows_ = impact(tmp_path, [record(serve_pair["reference"], "r1"), record(serve_pair["stages"], "s1"), lite])
    assert sorted(r["mode"] for r in rows_) == ["stages", "stages-lite"]
    assert all(len(r["stages"]) == 1 and r["problems"] == [] for r in rows_)


def test_report_skips_invalid_runs_and_profiles(tmp_path, serve_pair):
    bad = record(serve_pair["stages"], "s2")
    bad["valid"] = False
    prof = record(serve_pair["reference"], "p1", instrumentation="profile-collect",
                  profile={"units": 1, "timer": "t", "total_s": -1.0, "text": "x"})
    rows_ = impact(tmp_path, [record(serve_pair["reference"], "r1"), record(serve_pair["stages"], "s1"), bad, prof])
    assert len(rows_) == 1 and len(rows_[0]["stages"]) == 1 and len(rows_[0]["reference"]) == 1
    out = json.loads(breakdown.report([str(tmp_path / "results.jsonl")]))
    assert out["profiles"][0]["valid"] is False
