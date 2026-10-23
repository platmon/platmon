"""The bench tools' own arithmetic and bookkeeping (benchmarks/). No performance thresholds: those are judged
from recorded device runs, not on CI."""
import json
import subprocess
import sys
import time

import pytest

from benchmarks import cadence
from benchmarks.passive import Passive, parse_diskstats, parse_net_dev, parse_psi

NET = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: {lo} 10 0 0 0 0 0 0 {lo} 10 0 0 0 0 0 0
  eth0: {eth} 20 0 0 0 0 0 0 500 5 0 0 0 0 0 0
"""
DISK = """ 179 0 mmcblk0 {r} 0 {s} 0 10 0 80 0 0 {io} 0 0 0 0 0 0 0
 179 1 mmcblk0p1 5 0 40 0 0 0 0 0 0 0 0 0 0 0 0 0 0
   7 0 loop0 1 0 2 0 0 0 0 0 0 0 0 0 0 0 0 0 0
"""
PSI = "some avg10=1.50 avg60=0.00 avg300=0.00 total={some}\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


@pytest.fixture
def fake(tmp_path):
    (tmp_path / "proc/net").mkdir(parents=True)
    (tmp_path / "proc/pressure").mkdir()
    (tmp_path / "sys/class/net/eth0/device").mkdir(parents=True)
    (tmp_path / "sys/class/net/lo").mkdir()
    (tmp_path / "sys/block/mmcblk0").mkdir(parents=True)
    (tmp_path / "sys/block/loop0").mkdir()

    def write(lo=0, eth=1000, r=10, s=100, io=0, some=None):
        (tmp_path / "proc/net/dev").write_text(NET.format(lo=lo, eth=eth))
        (tmp_path / "proc/diskstats").write_text(DISK.format(r=r, s=s, io=io))
        if some is not None:
            (tmp_path / "proc/pressure/cpu").write_text(PSI.format(some=some))
    clock = Clock()
    return Passive(str(tmp_path / "proc"), str(tmp_path / "sys"), clock), write, clock


def test_parsers():
    assert parse_net_dev(NET.format(lo=1, eth=2))["eth0"] == (2, 20, 500, 5)
    d = parse_diskstats(DISK.format(r=1, s=2, io=3))
    assert d["mmcblk0"] == (1, 2, 10, 80, 3)
    assert parse_psi(PSI.format(some=7))["some"] == {"avg10": 1.5, "total_us": 7}


def test_rates_use_observed_time_and_warmup_is_not_zero(fake):
    p, write, clock = fake
    write(eth=1000, r=10, s=100, io=0)
    first = p()
    eth = first["network"]["interfaces"]["eth0"]
    assert eth["reason"] == "warmup" and "rx_bytes_per_s" not in eth
    write(eth=3000, r=20, s=300, io=500)
    clock.t = 2_000_000_000  # 2 s observed, whatever the configured interval was
    second = p()
    eth = second["network"]["interfaces"]["eth0"]
    assert eth["rx_bytes_per_s"] == 1000 and eth["rx_bits_per_s"] == 8000 and eth["physical"]
    assert second["network"]["interfaces"]["lo"]["physical"] is False
    disk = second["disk"]["devices"]
    assert set(disk) == {"mmcblk0"}  # the partition and loop0 are not added to the disk
    assert disk["mmcblk0"]["read_bytes_per_s"] == 512 * 200 / 2 and disk["mmcblk0"]["io_time_ratio"] == 0.25


def test_counter_reset_and_removed_device(fake, tmp_path):
    p, write, clock = fake
    write(eth=5000)
    p()
    write(eth=100)
    clock.t = 1_000_000_000
    eth = p()["network"]["interfaces"]["eth0"]
    assert eth["reason"] == "counter_reset" and "rx_bytes_per_s" not in eth
    (tmp_path / "proc/net/dev").write_text("\n".join(NET.format(lo=0, eth=0).splitlines()[:3]) + "\n")
    clock.t = 2_000_000_000
    assert p()["network"]["removed"] == ["eth0"]


def test_psi_unsupported_is_not_zero(fake):
    p, write, clock = fake
    write()
    out = p()["pressure"]
    assert out["memory"] == {"state": "unsupported", "reason": "ENOENT"}
    write(some=1000)
    clock.t = 1
    p()
    write(some=501000)
    clock.t = 1_000_000_001
    cpu = p()["pressure"]["cpu"]
    assert cpu["state"] == "ok" and cpu["some"]["stall_ratio"] == 0.5 and cpu["full"]["stall_ratio"] == 0


def test_cpu_pct_is_one_core_and_keeps_sign():
    a = {"ru_utime": 1.0, "ru_stime": 0.5}
    b = {"ru_utime": 1.6, "ru_stime": 0.6}
    assert cadence.cpu_pct_one_core(a, b, 70.0) == pytest.approx(1.0)
    runs = [dict(base, variant="B0", cpu_pct_one_core=0.9), dict(base, variant="B0", cpu_pct_one_core=0.8),
            dict(base, variant="B1", cpu_pct_one_core=0.7), dict(base, variant="B1", cpu_pct_one_core=0.8)]
    c = cadence.compare(runs)
    assert c["delta_pp"] == pytest.approx(-0.1) and c["distinguishable"] is False and c["problems"] == []
    assert c["rel_pct"] == pytest.approx(-100 * 0.1 / 0.85)


base = {"valid": True, "alias": "x", "interval_s": 1.0, "clients": 1, "poll_s": 1.0, "python": "3.13",
        "source_hash": "s", "bench_hash": "b", "scope": {"interfaces": 2}}


def test_compare_flags_mismatch_and_missing_variant():
    runs = [dict(base, variant="B0", cpu_pct_one_core=1.0), dict(base, variant="B1", cpu_pct_one_core=1.2,
                                                                scope={"interfaces": 3})]
    c = cadence.compare(runs)
    assert c["problems"] and "scope" in c["problems"][0] and c["spread_pp"] is None and not c["distinguishable"]
    assert cadence.compare([dict(base, variant="B0", cpu_pct_one_core=1.0)])["verdict"] == "insufficient"


def test_scope_counts_cores_not_the_rows_of_one_window():
    """A core without usage for one window (no_ticks, counter_regressed) is the same host, a core fewer is not."""
    def stats(ids, unavailable=()):
        return {"cpu": [{"id": n, "usage": 1.0} for n in ids],
                "cpu_sampling": {"unavailable": [{"id": n, "reason": "no_ticks"} for n in unavailable]},
                "sensor_meta": {**{f"/cpu/{n}/freq": {} for n in ids}, "/temperature/x": {}}}
    four = cadence.scope_of(stats([0, 1, 2, 3]), {})
    assert cadence.scope_of(stats([0, 1, 2], unavailable=[3]), {}) == four
    assert four["cpu_rows"] == 4 and four["sensor_meta"] == 1
    assert cadence.scope_of(stats([0, 1, 2]), {})["cpu_rows"] == 3


def test_percentile_and_missing_values():
    assert cadence.percentile([], 95) is None
    assert cadence.percentile(list(range(1, 101)), 95) == 95
    assert cadence.percentile([5, None, 1], 50) == 1
    assert cadence.dist([None])["n"] == 0 and cadence.dist([None])["mean"] is None


def test_table_is_fixed_size():
    t = cadence.Table(["t", "v"], 2)
    size = t.nbytes()
    for i in range(5):
        t.add(i, i * 10)
    assert t.n == 2 and t.dropped == 3 and t.nbytes() == size
    assert cadence.rows(t.dump(), 1, 2) == [{"t": 1, "v": 10}]


def test_budget_and_plan():
    assert [c["variant"] for c in cadence.plan("matrix", [1.0])] == ["B0", "B1", "B1", "B0"]
    assert cadence.plan("final", [], 5.0)[0]["measure"] == 600 and cadence.plan("final", [], 1.0)[0]["measure"] == 300
    cond = {"kind": "serve", "measure": 120}
    assert cadence.budget_left([], cond, 30, 5400, 32) is None
    assert "window" in cadence.budget_left([{"wall_s": 1}] * 32, cond, 30, 5400, 32)
    assert "time" in cadence.budget_left([{"wall_s": 5300}], cond, 30, 5400, 32)
    assert cadence.budget_left([{"wall_s": 5300, "counted": False}], cond, 30, 6000, 1) is None


def test_guard_and_throttle_bits(tmp_path):
    ok = {"mem_available": 2**30, "throttled": "throttled=0x50000"}  # sticky history bits only
    assert cadence.throttled_now(ok) == 0 and cadence.guard(ok, str(tmp_path), None) is None
    assert "throttle" in cadence.guard({**ok, "throttled": "throttled=0x50005"}, str(tmp_path), None)
    assert cadence.throttled_now({"mem_available": 1}) is None
    assert "memory" in cadence.guard({"mem_available": 1}, str(tmp_path), None)


def test_wireless_and_mounts():
    text = "Inter-| sta-|\n face | tus |\n wlan0: 0000   44.  -66.  -256  0 0 0 0 0 0\n"
    assert cadence.parse_wireless(text) == {"wlan0": -66.0}
    info = ("22 1 179:2 / / rw - ext4 /dev/mmcblk0p2 rw\n"
            "23 1 0:5 / /proc rw - proc proc rw\n"
            "24 1 0:40 / /mnt/nas rw - nfs4 nas:/x rw\n"
            "25 1 0:41 / /mnt/my\\040disk rw - fuseblk /dev/sdb1 rw\n"
            "26 1 8:1 / /boot/firmware rw - vfat /dev/mmcblk0p1 rw\n")
    assert cadence.local_mounts(info) == ["/", "/boot/firmware"]


def test_report_roundtrip(tmp_path, capsys):
    """A short real serve run: the report reads it back with warmup excluded and the same numbers again."""
    out = tmp_path / "s.json"
    cadence.main(["serve", "--variant", "B1", "--interval", "0.1", "--warmup", "0.3", "--measure", "0.6",
                  "--port", "0", "--out", str(out)])
    serve = json.loads(out.read_text())
    rec = {"kind": "run", "run_id": "r", "alias": "t", "valid": True, "order": 0, "serve": serve,
           "cond": {"phase": "matrix", "clients": 0, "poll": 1.0}, "before": {"temps_mC": {}, "cpufreq_khz": {}},
           "after": {"temps_mC": {}, "cpufreq_khz": {}}}
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps(rec) + "\n")
    capsys.readouterr()
    cadence.main(["report", str(results)])
    first = capsys.readouterr().out
    cadence.main(["report", str(results)])
    assert capsys.readouterr().out == first
    run = json.loads(first)["runs"][0]
    assert 0.5 < run["measured_elapsed_s"] < 1.5 and run["measured_elapsed_s"] == serve["measured_elapsed_s"]
    all_attempts = len(serve["tables"]["attempt"]["rows"])
    assert 0 < run["sample_count"] < all_attempts  # warmup attempts are not counted
    assert run["passive_ms"]["n"] > 0 and run["scope"]["interfaces"] > 0
    cadence.main(["report", "--md", str(results)])
    md = capsys.readouterr().out
    assert "| t | matrix | 0.1 |" in md and "| B1 |" in md


FAKE = r'''
import json, os, sys, time
args = sys.argv[1:]
out = args[args.index("--out") + 1]
mode = os.environ["FAKE_MODE"]
if args[0] == "client":
    json.dump({"rusage": {}, "table": {"fields": ["t"], "rows": []}}, open(out, "w"))
elif args[0] == "probe":
    json.dump({"pending_at_end": mode == "pending", "skipped_ticks": 3 if mode == "pending" else 0}, open(out, "w"))
elif mode == "silent":
    time.sleep(60)  # alive, stdout open, never ready
elif mode == "unsupported":
    print("unsupported: profile modes unsupported here", flush=True)
    sys.exit(3)
elif mode == "partial":
    sys.stdout.write("rea")  # part of the ready line, no newline, then stays alive
    sys.stdout.flush()
    time.sleep(60)
else:
    print("ready", flush=True)
    if mode == "hang":
        time.sleep(60)
    elif mode == "ok":
        json.dump({"clock_jump": False}, open(out, "w"))
    # mode "noresult": exits 0 without writing a result
'''


@pytest.fixture
def fake_child(tmp_path, monkeypatch):
    script = tmp_path / "fake_child.py"
    script.write_text(FAKE)
    started = []
    real = subprocess.Popen

    def popen(*args, **kw):
        started.append(real(*args, **kw))
        return started[-1]
    monkeypatch.setattr(cadence, "child_cmd", lambda me, *args: [me, str(script), *args])
    monkeypatch.setattr(cadence.subprocess, "Popen", popen)
    args = type("A", (), {"out": str(tmp_path), "warmup": 0.0, "port": 1, "ready_timeout": 0.5})()

    def execute(mode, **cond):
        monkeypatch.setenv("FAKE_MODE", mode)
        c = {"kind": "serve", "variant": "B0", "interval": 1.0, "clients": 1, "poll": 1.0, "measure": 0.1, **cond}
        return cadence.execute(sys.executable, "r", c, args)
    return execute, started, real


def test_execute_normal_path(fake_child):
    execute, started, real = fake_child
    out = execute("ok")
    assert out["valid"] and out["serve"] == {"clock_jump": False} and len(out["clients"]) == 1


def test_execute_server_never_ready_is_bounded_and_reaped(fake_child):
    execute, started, real = fake_child
    t0 = time.monotonic()
    out = execute("silent")
    assert time.monotonic() - t0 < 15 and not out["valid"] and "not ready" in out["invalid"]
    assert "timeout" not in out and all(p.poll() is not None for p in started)


def test_execute_timeout_kills_and_reaps(fake_child, monkeypatch):
    execute, started, real = fake_child
    monkeypatch.setattr(real, "wait", _short_wait(real.wait))
    out = execute("hang")
    assert out["timeout"] and "server timeout" in out["invalid"] and "unreaped" not in out
    assert all(p.poll() is not None for p in started)


def _short_wait(wait):
    """Popen.wait with the runner's long deadlines cut to 1 s, so a hanging child times out quickly."""
    def short(self, timeout=None):
        return wait(self, timeout=None if timeout is None else min(timeout, 1.0))
    return short


def test_execute_child_without_result_does_not_raise(fake_child):
    execute, _, _ = fake_child
    out = execute("noresult")
    assert not out["valid"] and "no result" in out["invalid"]


def test_unreaped_process_is_reported():
    class Stuck:
        pid = 42

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise cadence.subprocess.TimeoutExpired("x", timeout)
    out = cadence.failed("server timeout", [Stuck()], timeout=True)
    assert out["timeout"] and out["unreaped"] == [42] and "still running" in out["invalid"]


def test_pending_probe_is_not_valid(fake_child):
    execute, _, _ = fake_child
    out = execute("pending", kind="probe", feature="statvfs", period=30.0)
    assert out["valid"] is False and out["incomplete"] and out["probe"]["skipped_ticks"] == 3
    assert execute("done", kind="probe", feature="statvfs", period=30.0)["valid"]


def test_pooled_p95_differs_from_max_of_per_run_p95():
    def rec(values):
        rows = [[i, 0, 0, int(v * 1e6), 0] for i, v in enumerate(values, 1)]
        return {"valid": True, "serve": {"variant": "B1", "start": {"mono": 0}, "end": {"mono": 10**9},
                "tables": {"collect": {"fields": ["t", "prod_ns", "prod_cpu_ns", "passive_ns", "passive_cpu_ns"],
                                       "rows": rows},
                           "attempt": {"fields": ["t", "elapsed_ns"], "rows": []}}}}
    p = cadence.pooled([rec([1] * 94 + [100] * 6), rec([2] * 100)])["B1"]
    assert p["runs"] == 2 and p["passive_ms"]["n"] == 200
    assert p["passive_ms"]["p95"] == 2 and p["passive_ms"]["per_run_max_p95"] == 100


def test_pending_and_skipped_reach_the_report():
    probe = {"feature": "statvfs", "period_s": 30.0, "cpu_pct_one_core": 0.1, "skipped_ticks": 3,
             "pending_at_end": True, "mounts": 1,
             "table": {"fields": ["t", "step", "elapsed_ns", "cpu_ns", "count", "value_x1000", "ok"],
                       "rows": [[1, 0, 10**6, 10**6, 1, -10**12, 1]]}}
    rec = {"run_id": "r", "alias": "x", "valid": False, "invalid": "a probe call was still running at the end",
           "probe": probe}
    s = cadence.summarize_probe(rec)
    assert s["skipped_ticks"] == 3 and s["pending_at_end"] and not s["valid"]
    md = cadence.markdown({"comparisons": [], "runs": [], "probes": [s], "skipped": []})
    assert "| 3/True | no: a probe call was still running at the end |" in md


WRITER = r'''
import os, sys, time
# argv: pieces of stdout, each "<delay s>:<text>"; then stays alive with stdout open until killed
for piece in sys.argv[1:]:
    delay, text = piece.split(":", 1)
    time.sleep(float(delay))
    os.write(1, text.encode().replace(b"\\n", b"\n"))
time.sleep(30)
'''


@pytest.fixture
def writer(tmp_path):
    script = tmp_path / "writer.py"
    script.write_text(WRITER)
    procs = []

    def start(*pieces):
        procs.append(subprocess.Popen([sys.executable, str(script), *pieces], stdout=subprocess.PIPE))
        return procs[-1]
    yield start
    for p in procs:
        assert cadence.stop_process(p)
        p.stdout.close()


def timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, time.monotonic() - t0


# The child's start-up (an interpreter launch) happens before its first delay, so every deadline below
# leaves it time: timeouts are 2 s and more, the quiet periods well past them.

def test_wait_line_no_output_times_out(writer):
    out, took = timed(lambda: cadence.wait_line(writer(), 2.0))
    assert out is None and 1.9 < took < 4


def test_wait_line_partial_line_does_not_block(writer):
    p = writer("0:rea", "6:dy\\n")  # no newline within the deadline
    out, took = timed(lambda: cadence.wait_line(p, 2.0))
    assert out is None and took < 4


def test_wait_line_pieces_within_deadline(writer):
    p = writer("0:re", "0.2:ad", "0.2:y 1\\nmore")
    assert cadence.wait_line(p, 5.0) == "ready 1\n"


def test_wait_line_trickle_does_not_extend_deadline(writer):
    p = writer(*["0.3:x"] * 30)  # a byte every 0.3 s, never a newline
    out, took = timed(lambda: cadence.wait_line(p, 2.0))
    assert out is None and took < 4


def test_wait_line_eof_without_newline(tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "import os; os.write(1, b'ready')"], stdout=subprocess.PIPE)
    try:
        assert cadence.wait_line(p, 5.0) is None
    finally:
        assert cadence.stop_process(p)
        p.stdout.close()


def test_execute_partial_ready_is_bounded_and_reaped(fake_child, monkeypatch):
    execute, started, _ = fake_child
    out, took = timed(lambda: execute("partial"))
    assert not out["valid"] and "not ready" in out["invalid"] and took < 15
    assert all(p.poll() is not None for p in started)


def test_execute_unsupported_mode_is_not_a_measurement(fake_child):
    execute, started, _ = fake_child
    out = execute("unsupported")
    assert out["valid"] is False and out["unsupported"] and "unsupported" in out["invalid"]
    assert "timeout" not in out and all(p.poll() is not None for p in started)


def test_passive_rebaseline_and_parts_plans():
    conds = cadence.plan("passive-rebaseline", [])
    assert len(conds) == 16 and {c["instr"] for c in conds} == {"reference"}
    assert [c["block"] for c in conds[::4]] == ["c1-1", "c0-1", "c1-2", "c0-2"]
    assert [c["variant"] for c in conds[:4]] == ["B0", "B1", "B1", "B0"]
    assert all(c["clients"] == int(c["block"][1]) and c["interval"] == 1.0 and c["measure"] == 120 for c in conds)
    one = cadence.plan("passive-rebaseline", [], clients=(1,), blocks=1)
    assert [c["block"] for c in one] == ["c1-1"] * 4
    parts = cadence.plan("passive-parts", [])
    assert [c["instr"] for c in parts] == ["reference", "passive-parts", "passive-parts", "reference"]
    assert {(c["variant"], c["clients"]) for c in parts} == {("B1", 0)}
    assert all(c["phase"] == "passive-parts" for c in parts) and cadence.plan("ab", [])[0]["variant"] == "B0"
    done = [{"wall_s": 150}] * 20
    assert cadence.budget_left(done, conds[0], 30, 3600, 24) is None
    assert "window" in cadence.budget_left([{"wall_s": 1}] * 24, conds[0], 30, 3600, 24)


def test_parts_timing_keeps_reads_and_results(fake, tmp_path):
    p, write, clock = fake
    twin = Passive(p.proc, p.sys, clock)
    table = cadence.Table(["t", "part", "elapsed_ns", "cpu_ns", "state", "reads"], 50)
    cadence.time_parts(twin, table)
    write(some=1000)
    for t in (1, 1_000_000_001):
        clock.t = t
        assert twin() == p()
    assert (twin.reads, twin.read_bytes, twin.lookups) == (p.reads, p.read_bytes, p.lookups)
    got = cadence.rows(table.dump())
    assert [r["part"] for r in got] == [0, 1, 2, 3, 4] * 2
    assert [(r["state"], r["reads"]) for r in got[:5]] == [(1, 1), (1, 1), (1, 1), (2, 0), (2, 0)]  # memory/io absent
    assert all(r["elapsed_ns"] >= 0 for r in got)
    (tmp_path / "proc/pressure/cpu").write_text("some avg10=bad total=1\n")  # a parse error is not an OSError
    with pytest.raises(ValueError):
        twin()
    last = cadence.rows(table.dump())[-1]  # network and disk, then the PSI cpu call that raised
    assert table.n == 13 and (last["part"], last["state"]) == (2, 0)  # timed although it raised


def test_block_deltas_and_parts_summary():
    runs = [dict(base, variant=v, cpu_pct_one_core=x, block=b) for v, x, b in
            (("B0", 1.0, "c1-1"), ("B1", 1.2, "c1-1"), ("B1", 1.3, "c1-1"), ("B0", 1.1, "c1-1"),
             ("B0", 1.0, "c1-2"), ("B1", 1.05, "c1-2"))]
    c = cadence.compare(runs)
    assert c["blocks"]["c1-1"]["delta_pp"] == pytest.approx(0.2) and c["blocks"]["c1-2"]["delta_pp"] == pytest.approx(0.05)
    s = {"measured_elapsed_s": 2.0, "tables": {"parts": {"fields": ["t", "part", "elapsed_ns", "cpu_ns", "state", "reads"],
                                                         "rows": [[5, 0, 300, 200, 1, 1], [6, 0, 500, 400, 1, 1],
                                                                  [7, 3, 90, 80, 2, 0], [99, 0, 9, 9, 1, 1]]}}}
    out = cadence.parts_summary(s, 0, 10)  # the row at t=99 is outside the window
    assert out["network"]["calls"] == 2 and out["network"]["cpu_pct_one_core"] == pytest.approx(100 * 600e-9 / 2)
    assert out["pressure.memory"]["states"] == {"ok": 0, "unsupported": 1, "error": 0, "other": 0}
    assert out["disk"]["calls"] == 0 and out["disk"]["cpu_ms"]["mean"] is None
    assert cadence.parts_summary({"tables": {}}, 0, 1) is None


def test_block_delta_refuses_runs_that_are_not_comparable():
    runs = [dict(base, variant="B0", cpu_pct_one_core=1.0, block="c1-1"),
            dict(base, variant="B1", cpu_pct_one_core=1.2, block="c1-1", source_hash="other"),
            dict(base, variant="B0", cpu_pct_one_core=1.0, block="c1-2"),
            dict(base, variant="B1", cpu_pct_one_core=1.1, block="c1-2", scope={"interfaces": 3}),
            dict(base, variant="B0", cpu_pct_one_core=1.0, block="c1-3"),
            dict(base, variant="B1", cpu_pct_one_core=1.1, block="c1-3")]
    b = cadence.compare(runs)["blocks"]
    assert b["c1-1"]["delta_pp"] is None and "source_hash" in b["c1-1"]["problems"][0]
    assert b["c1-2"]["delta_pp"] is None and "scope" in b["c1-2"]["problems"][0]
    assert b["c1-3"]["delta_pp"] == pytest.approx(0.1) and b["c1-3"]["problems"] == []


def test_parts_run_reports_apart(tmp_path, capsys):
    """A short real passive-parts serve run: parts are timed once per collection, inside the window only."""
    out = tmp_path / "s.json"
    cadence.main(["serve", "--variant", "B1", "--interval", "0.1", "--warmup", "0.3", "--measure", "0.6",
                  "--port", "0", "--instrumentation", "passive-parts", "--out", str(out)])
    serve = json.loads(out.read_text())
    rec = {"kind": "run", "run_id": "r", "alias": "t", "valid": True, "order": 0, "serve": serve,
           "cond": {"phase": "passive-parts", "clients": 0, "poll": 1.0, "block": "parts-1"},
           "before": {"temps_mC": {}, "cpufreq_khz": {}}, "after": {"temps_mC": {}, "cpufreq_khz": {}}}
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps(rec) + "\n")
    capsys.readouterr()
    cadence.main(["report", str(results)])
    run = json.loads(capsys.readouterr().out)["runs"][0]
    assert run["instr"] == "passive-parts" and run["block"] == "parts-1" and run["passive_calls"] > 0
    # parts are windowed by their own start, collections by theirs: one call can fall either side of an edge
    assert all(abs(p["calls"] - run["passive_calls"]) <= 1 for p in run["parts"].values())
    cadence.main(["report", "--md", str(results)])
    assert "| pressure.io |" in capsys.readouterr().out


# ---------- the product's Network off/on (phase network-product) ----------

def short_serve(tmp_path, net, *extra):
    out = tmp_path / f"{net}.json"
    cadence.main(["serve", "--variant", "B0", "--product-network", net, "--interval", "0.1", "--warmup", "0.2",
                  "--measure", "0.5", "--port", "0", "--out", str(out), *extra])
    return json.loads(out.read_text())


def run_rec(serve, block="c0-1"):
    return {"kind": "run", "run_id": serve["product_network"], "alias": "t", "valid": True, "order": 0, "serve": serve,
            "cond": {"phase": "network-product", "clients": 0, "poll": 1.0, "block": block},
            "before": {"temps_mC": {}, "cpufreq_khz": {}}, "after": {"temps_mC": {}, "cpufreq_khz": {}}}


def test_product_network_on_and_off_change_the_real_calls(tmp_path, capsys):
    on, off = short_serve(tmp_path, "on"), short_serve(tmp_path, "off")
    attempts = len(on["tables"]["attempt"]["rows"])
    assert len(on["tables"]["network"]["rows"]) == attempts > 0  # one product network call per collection
    assert off["tables"]["network"]["rows"] == []
    assert on["scope"]["product_network"]["interfaces"] > 0 and off["scope"]["product_network"]["interfaces"] > 0
    assert "network" not in on["scope"]["collectors"]  # the intended difference is not a scope change
    assert {k: v for k, v in on["scope"].items() if k != "product_network"} == \
           {k: v for k, v in off["scope"].items() if k != "product_network"}
    nics_changed = on["scope"]["product_network"] != off["scope"]["product_network"]  # a shared CI host can
    assert all(r[-1] == 0 for r in on["tables"]["attempt"]["rows"])  # no data-age fallback
    results = tmp_path / "results.jsonl"
    results.write_text("".join(json.dumps(run_rec(s)) + "\n" for s in (off, on, on, off)))
    capsys.readouterr()
    cadence.main(["report", str(results)])
    out = json.loads(capsys.readouterr().out)
    assert out["comparisons"] == [] and len(out["network_comparisons"]) == 1
    c = out["network_comparisons"][0]
    assert c["arms"] == {"b0": "network off", "b1": "network on"}
    if nics_changed:  # ... add or drop an interface between the two runs: then they must not be compared
        assert "scope differs" in c["problems"][0] and c["blocks"]["c0-1"]["delta_pp"] is None
    else:
        assert c["problems"] == [] and c["blocks"]["c0-1"]["delta_pp"] is not None
    assert c["network_calls_when_off"] == 0 and c["network_ms"]["n"] > 0
    cadence.main(["report", "--md", str(results)])
    assert "| t | 0 |" in capsys.readouterr().out


def test_product_network_never_runs_with_the_passive_prototype(tmp_path):
    with pytest.raises(SystemExit):
        cadence.main(["serve", "--variant", "B1", "--product-network", "on", "--interval", "1", "--out", str(tmp_path / "x")])


def test_polling_does_not_add_collections(tmp_path):
    """A server polled as fast as it answers: still one product network call per collection."""
    import socket
    import urllib.request
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    out = tmp_path / "s.json"
    p = subprocess.Popen([sys.executable, cadence.__file__, "serve", "--variant", "B0", "--product-network", "on",
                          "--interval", "0.2", "--warmup", "0.2", "--measure", "1.0", "--port", str(port),
                          "--expect-rps", "500", "--out", str(out)], stdout=subprocess.PIPE)
    try:
        assert cadence.wait_line(p, 20).startswith("ready")
        n = 0
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=5) as r:
                assert json.loads(r.read())["network"]["interfaces"]
            n += 1
        assert p.wait(timeout=30) == 0
    finally:
        cadence.stop_process(p)
    serve = json.loads(out.read_text())
    assert n > 20 and len(serve["tables"]["http"]["rows"]) >= n
    assert len(serve["tables"]["network"]["rows"]) == len(serve["tables"]["attempt"]["rows"]) <= 8


def test_source_hash_covers_the_product_code_and_web_assets(monkeypatch):
    seen = []
    monkeypatch.setattr(cadence, "sha256_files", lambda paths: seen.extend(paths) or "x")
    cadence.source_hash()
    for path in ("collector/network.py", "runtime/sampler.py", "runtime/slow.py", "runtime/history.py",
                 "frontends/web/index.html", "frontends/web/app.js", "frontends/web/styles.css", "platmon.py"):
        assert any(p.endswith(path) for p in seen), path


def test_network_product_plan_and_comparability():
    plan = cadence.plan("network-product", [], clients=(1,), blocks=2)
    assert [(c["net"], c["block"], c["variant"]) for c in plan] == [
        (v, b, "B0") for b in ("c1-1", "c1-2") for v in ("off", "on", "on", "off")]
    assert all(c["interval"] == 1.0 and c["poll"] == 1.0 and c["measure"] == 120 for c in plan)
    scope = {"collectors": {"core": "ok"}, "product_network": {"namespace": "netns1:a", "interfaces": 3, "names_sha": "x"}}
    run = dict(base, variant="B0", scope=scope, block="c1-1")
    ok = [dict(run, product_network="off", cpu_pct_one_core=1.0), dict(run, product_network="on", cpu_pct_one_core=1.1)]
    assert cadence.compare([dict(r, variant={"off": "B0", "on": "B1"}[r["product_network"]]) for r in ok])["problems"] == []
    for change in ({"python": "3.9.0"}, {"source_hash": "other"}, {"bench_hash": "other"},
                   {"scope": dict(scope, product_network=dict(scope["product_network"], interfaces=4))},
                   {"scope": dict(scope, product_network=dict(scope["product_network"], namespace="netns1:b"))},
                   {"scope": dict(scope, collectors={"core": "ok", "temperature": "ok"})}):
        bad = [ok[0], dict(ok[1], **change)]
        assert cadence.mismatches(bad), change


# ---------- the product's Disk I/O off/on on top of Network on (phase disk-product) ----------

def test_product_disk_on_and_off_change_the_real_calls(tmp_path, capsys):
    runs = {}
    for v in ("off", "on"):
        runs[v] = short_serve(tmp_path, "on", "--product-disk", v)
        assert runs[v]["product_disk"] == v and len(runs[v]["tables"]["network"]["rows"]) > 0  # network on in both
    on, off = runs["on"], runs["off"]
    assert len(on["tables"]["disk"]["rows"]) == len(on["tables"]["attempt"]["rows"]) > 0 and off["tables"]["disk"]["rows"] == []
    assert "disk_io" not in on["scope"]["collectors"] and on["scope"]["product_disk"] == off["scope"]["product_disk"]
    results = tmp_path / "results.jsonl"
    rec = lambda s: dict(run_rec(s), cond={"phase": "disk-product", "clients": 0, "poll": 1.0, "block": "c0-1"})  # noqa: E731
    results.write_text("".join(json.dumps(rec(s)) + "\n" for s in (off, on, on, off)))
    capsys.readouterr()
    cadence.main(["report", str(results)])
    out = json.loads(capsys.readouterr().out)
    assert out["network_comparisons"] == [] and len(out["disk_comparisons"]) == 1
    c = out["disk_comparisons"][0]
    assert c["arms"] == {"b0": "disk off", "b1": "disk on"} and c["disk_calls_when_off"] == 0 and c["disk_ms"]["n"] > 0
    cadence.main(["report", "--md", str(results)])
    assert "disk call p95" in capsys.readouterr().out


def test_product_disk_needs_a_stated_network_setting(tmp_path):
    with pytest.raises(SystemExit):
        cadence.main(["serve", "--variant", "B0", "--product-disk", "on", "--interval", "1", "--out", str(tmp_path / "x")])


def test_disk_product_plan_and_source_hash(monkeypatch):
    plan = cadence.plan("disk-product", [], clients=(1,), blocks=2)
    assert [(c["net"], c["disk"], c["block"]) for c in plan] == [
        ("on", v, b) for b in ("c1-1", "c1-2") for v in ("off", "on", "on", "off")]
    seen = []
    monkeypatch.setattr(cadence, "sha256_files", lambda paths: seen.extend(paths) or "x")
    cadence.source_hash()
    assert any(p.endswith("collector/disk_io.py") for p in seen)


def test_slow_cost_times_each_real_observation(tmp_path):
    from benchmarks import slow_cost
    out = tmp_path / "s.json"
    slow_cost.main(["--group", "wifi", "--period", "0.01", "--count", "3", "--out", str(out)])
    r = json.loads(out.read_text())
    assert r["count"] == 3 and len(r["samples"]) == 3 and r["observe_cpu_ms"]["n"] == 3
    assert all(s["state"] in ("ok", "partial", "unavailable") for s in r["samples"])
    assert r["source_hash"] and r["bench_hash"]


def test_service_cpu_measures_the_real_service(tmp_path):
    import socket
    from benchmarks import service_cpu
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    ini = tmp_path / "t.ini"
    ini.write_text(f"[history]\nenabled = yes\n[http]\nbind = 127.0.0.1\nport = {port}\n")
    out = tmp_path / "s.json"
    service_cpu.main(["--ini", str(ini), "--port", str(port), "--warmup", "1", "--measure", "2", "--page", "--out", str(out)])
    r = json.loads(out.read_text())
    assert r["exit"] == 0 and r["requests"]["stats"]["n"] >= 2 and r["requests"]["stats"]["errors"] == 0
    assert r["cpu_pct_one_core"] >= 0 and r["memory_end"]["rss_kib"] > 0
    assert r['pid_gone'] and r['port_closed'] and not r['forced_kill']
    assert r['requests']['history']['statuses'] == {}  # 2 s window: next optional poll not due
    assert r['history_samples'] and len(r['health']) == 2
    assert r['final_documents']['history_full']['retention_s'] == 600
    # HTTP polling and collection both run every second, on independent clocks. Variable
    # sensor latency can repeat or skip a published sequence without a collection failure.
    assert 1 <= r['observed_samples']['count'] <= r['requests']['stats']['n']
    assert r['observed_samples']['missing_sequences'] >= 0
    assert r['health'][1]['sample']['sequence'] > r['health'][0]['sample']['sequence']
    assert r['collection_failures_logged'] == r['stale_collections_dropped_logged'] == 0
    # A stopped HTTPServer may leave TIME_WAIT sockets; the next window must still start.
    service_cpu.main(["--ini", str(ini), "--port", str(port), "--warmup", "0", "--measure", "1",
                      "--out", str(out)])
    restarted = json.loads(out.read_text())
    assert restarted['exit'] == 0 and restarted['port_closed'] and restarted['pid_gone']
    assert restarted['health'][0]['instance_id'] != r['health'][0]['instance_id']


def test_service_bench_rejects_an_occupied_port(tmp_path):
    import socket
    from benchmarks import service_cpu
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        ini = tmp_path / 'test.ini'
        ini.write_text('[http]\nbind = 127.0.0.1\n')
        with pytest.raises(OSError):
            service_cpu.main(['--ini', str(ini), '--port', str(occupied.getsockname()[1]),
                              '--out', str(tmp_path / 'result.json')])
