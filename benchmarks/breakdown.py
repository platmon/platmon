"""Stage timing for benchmarks/cadence.py (`serve --instrumentation stages`): where one collection's and
one request's CPU time goes. Bench only: the product modules are wrapped in place for the bench process's
lifetime (install() restores them), never changed on disk, and nothing here is imported by the product.

Each stage records, per unit (one collection on the sampler thread, one request on its worker thread,
one accept/dispatch on the listener thread): calls, inclusive elapsed, inclusive thread CPU and self CPU
(inclusive minus the stages nested inside it). Only the outermost call of a stage that recurses into
itself is counted. Totals are kept in thread-local accumulators and written to a fixed-size table when
the unit ends, never per call.
"""
import contextlib
import cProfile
import functools
import io
import itertools
import json
import pstats
import re
import sys
import threading
import time
import types

mono = time.monotonic_ns
tcpu = time.thread_time_ns


class Recorder:
    def __init__(self, table):
        """table: a factory (fields, capacity) -> cadence.Table."""
        self.names, self.ids = [], {}
        self.local = threading.local()
        self.rows = table(["unit", "name", "calls", "incl_ns", "incl_cpu_ns", "self_cpu_ns"], 0)
        self.units = table(["t", "end", "kind", "unit", "cpu_ns", "thread_cpu_ns", "wrapped_calls"], 0)
        self._seq = itertools.count(1)
        self._table = table

    def allocate(self, rows, units):
        """Fixed buffers, allocated before the warmup."""
        self.rows = self._table(self.rows.fields, rows)
        self.units = self._table(self.units.fields, units)

    def _state(self):
        st = getattr(self.local, "st", None)
        if st is None:
            st = self.local.st = {"stack": [], "acc": {}, "active": set(), "unit": None}
        return st

    def wrap(self, name, fn):
        """fn, timed as stage name. Calls fn exactly once and passes its result and exceptions through."""
        if name not in self.ids:
            self.ids[name] = len(self.names)
            self.names.append(name)
        i = self.ids[name]

        @functools.wraps(fn)
        def stage(*args, **kwargs):
            st = self._state()
            if i in st["active"]:  # recursion into the same stage: only the outermost call counts
                return fn(*args, **kwargs)
            child = [0]
            st["stack"].append(child)
            st["active"].add(i)
            t0, c0 = mono(), tcpu()
            try:
                return fn(*args, **kwargs)
            finally:
                c1, t1 = tcpu(), mono()
                st["stack"].pop()
                st["active"].discard(i)
                cpu = c1 - c0
                if st["stack"]:
                    st["stack"][-1][0] += cpu
                acc = st["acc"].get(i)
                if acc is None:
                    acc = st["acc"][i] = [0, 0, 0, 0]
                acc[0] += 1
                acc[1] += t1 - t0
                acc[2] += cpu
                acc[3] += cpu - child[0]
        stage.__wrapped_stage__ = fn
        return stage

    def begin(self):
        """Starts a unit on this thread; returns (t, cpu) for end()."""
        st = self._state()
        st["acc"] = {}
        return mono(), tcpu()

    def end(self, kind, begun):
        """Ends this thread's unit: writes its stage totals and one unit row. kind: a KINDS name; for a
        request, thread_cpu_ns is the worker thread's whole CPU so far."""
        kind = KINDS[kind]
        t0, c0 = begun
        c1, t1 = tcpu(), mono()
        st = self._state()
        unit = next(self._seq)
        calls = 0
        for i, (n, incl, cpu, self_cpu) in st["acc"].items():
            self.rows.add(unit, i, n, incl, cpu, self_cpu)
            calls += n
        self.units.add(t0, t1, kind, unit, c1 - c0, c1, calls)
        st["acc"] = {}

    def dump(self):
        return {"names": self.names, "rows": self.rows.dump(), "units": self.units.dump()}


KINDS = {"collection": 0, "request": 1, "listener": 2}


def _shim(module, **replace):
    """A stand-in for a module name inside one product module: the given attributes replaced, the rest
    passed through, so only that module's own calls are timed (deepcopy's recursion stays untimed)."""
    ns = types.SimpleNamespace(**{k: getattr(module, k) for k in dir(module) if not k.startswith("__")})
    for k, v in replace.items():
        setattr(ns, k, v)
    return ns


def targets():
    """(owner, attribute, stage name) of every product call site timed in stages mode. A function imported
    by name into several modules is wrapped at each, under one stage name."""
    from collector import core as collector
    from collector import common, jetson, metadata, sysfs
    from runtime import sampler
    from runtime import server
    return [
        # collection, by part
        (collector, "_collect", "collect._collect"),
        (collector, "sensor_entries", "collect.sensor_entries"),
        (metadata, "groups", "collect.groups"),
        (metadata, "summarize", "collect.summarize"),
        (common, "collect", "common.collect"),
        (common.CpuCounters, "sample", "common.cpu_sample"),
        (common, "thermal_zones", "common.thermal_zones"),
        (common, "hwmon_sensors", "common.hwmon_sensors"),
        (common, "meminfo", "common.meminfo"),
        (common, "system_info", "common.system_info"),
        (common, "read", "common.read (uptime, model, os-release)"),
        (jetson, "extend", "jetson.extend"),
        (jetson, "gpu", "jetson.gpu"),
        (jetson, "tach_fans", "jetson.tach_fans"),
        # collection, by kind of work (nested inside the parts above)
        (metadata.Group, "read", "work.Group.read"),
        (metadata.Group, "sensor", "work.Group.sensor"),
        (metadata.Group, "status", "work.Group.status"),
        (sysfs, "entries", "work.entries"),
        (common, "entries", "work.entries"),
        (jetson, "entries", "work.entries"),
        (sysfs, "hwmon_chips", "work.hwmon_chips"),
        (common, "hwmon_chips", "work.hwmon_chips"),
        (jetson, "hwmon_chips", "work.hwmon_chips"),
        (sysfs, "chip_identities", "work.chip_identities"),
        (sysfs, "canonical", "work.canonical"),
        (metadata, "source", "work.source"),
        # Sampler
        (sampler.Sampler, "_provenance", "sampler._provenance"),
        (sampler.Sampler, "_check", "sampler._check"),
        (sampler.Sampler, "read", "sampler.read"),
    ], [
        (sampler, "copy", "deepcopy", "deepcopy"),
        (server, "json", "dumps", "json.dumps"),
        (common, "shutil", "disk_usage", "common.disk_usage"),
    ]


# stages-lite: the frequent per-value calls left out (each wrapper call costs clock reads), keeping the
# parts, the Sampler and HTTP stages and the identity work (canonical, chip_identities, source)
LITE_SKIP = ("work.Group.read", "work.Group.sensor", "work.Group.status", "work.entries", "work.hwmon_chips")


@contextlib.contextmanager
def install(rec, skip=()):
    """Wraps the targets (except the stage names in skip) in place; restores every original on exit, also
    after an exception."""
    plain, shims = targets()
    plain = [t for t in plain if t[2] not in skip]
    undo = []
    try:
        for owner, attr, name in plain:
            original = owner.__dict__[attr] if isinstance(owner, type) else getattr(owner, attr)
            undo.append((owner, attr, original))
            setattr(owner, attr, rec.wrap(name, original))
        for owner, attr, fn, name in shims:
            original = getattr(owner, attr)
            undo.append((owner, attr, original))
            setattr(owner, attr, _shim(original, **{fn: rec.wrap(name, getattr(original, fn))}))
        yield rec
    finally:
        for owner, attr, original in reversed(undo):
            setattr(owner, attr, original)


def wrapper_cost(n=20000):
    """(elapsed ns, thread CPU ns) one stage wrapper adds per call, from a no-op timed n times against the
    same no-op unwrapped, on a recorder of its own."""
    def noop():
        return None

    class Throwaway:
        def __init__(self, fields, capacity):
            self.fields = fields

        def add(self, *values):
            pass
    rec = Recorder(Throwaway)
    wrapped = rec.wrap("noop", noop)
    rec.begin()
    out = []
    for fn in (noop, wrapped):
        t0, c0 = mono(), tcpu()
        for _ in range(n):
            fn()
        out.append((mono() - t0, tcpu() - c0))
    return {"calls": n, "elapsed_ns_per_call": (out[1][0] - out[0][0]) / n,
            "cpu_ns_per_call": (out[1][1] - out[0][1]) / n, "clock_calls_per_wrapped_call": 4}


PROFILE_UNSUPPORTED = (
    "cProfile records every thread on Python 3.12+ (it uses sys.monitoring, whose events are global), so a "
    "per-thread profile on the thread CPU clock mixes threads (functions of other threads, negative times) "
    "and a second profile cannot be enabled while one is active")


def profile_support(version=sys.version_info):
    """None if per-thread cProfile works on this Python (3.11 and older: one profile function per thread),
    else why not. Only the profile modes depend on it; reference and stages do not use cProfile."""
    return None if tuple(version[:2]) < (3, 12) else PROFILE_UNSUPPORTED


class Profiles:
    """cProfile for the profile modes: one Profile per thread (never shared), on the thread CPU clock,
    enabled only while the window is open; merged when the run ends. Refuses to start where per-thread
    profiling does not hold (profile_support)."""
    timer = "time.thread_time_ns (thread CPU), timeunit 1e-9"

    def __init__(self):
        why = profile_support()
        if why:
            raise RuntimeError(f"profile modes unsupported on Python {sys.version.split()[0]}: {why}")
        self.on = threading.Event()
        self.done, self.lock = [], threading.Lock()

    @contextlib.contextmanager
    def profiled(self):
        if not self.on.is_set():
            yield
            return
        p = cProfile.Profile(time.thread_time_ns, 1e-9)
        p.enable()
        try:
            yield
        finally:
            p.disable()
            with self.lock:
                self.done.append(p)

    def report(self, top=30):
        if not self.done:
            return {"units": 0, "timer": self.timer, "text": ""}
        out = io.StringIO()
        stats = pstats.Stats(self.done[0], stream=out)
        for p in self.done[1:]:
            stats.add(p)
        stats.strip_dirs()
        for key in ("tottime", "cumulative"):
            out.write(f"\n==== sorted by {key} ====\n")
            stats.sort_stats(key).print_stats(top)
        out.write("\n==== callers of the top 10 by tottime ====\n")
        stats.sort_stats("tottime").print_callers(10)
        return {"units": len(self.done), "timer": self.timer, "total_s": stats.total_tt, "text": out.getvalue()}


# ---- report ----

def window_units(serve, kind):
    """Unit rows of one kind that started in the measured window, and how many of them ended after it."""
    b = serve["breakdown"]
    t0, t1 = serve["start"]["mono"], serve["end"]["mono"]
    units = [dict(zip(b["units"]["fields"], r)) for r in b["units"]["rows"]]
    inside = [u for u in units if u["kind"] == KINDS[kind] and t0 <= u["t"] < t1]
    return inside, sum(1 for u in inside if u["end"] > t1)


def stage_table(serves, kind):
    """Per stage over the units of one kind of several runs (pooled): calls per unit, CPU and self CPU per
    unit (sum / units), inclusive elapsed per unit p50/p95 over the units where it ran, and its CPU over
    the runs' window time as % of one core."""
    acc, per_unit, n_units, window = {}, {}, 0, 0.0
    for s in serves:
        units, _ = window_units(s, kind)
        ids = {u["unit"] for u in units}
        n_units += len(units)
        window += s["measured_elapsed_s"]
        b = s["breakdown"]
        for r in b["rows"]["rows"]:
            row = dict(zip(b["rows"]["fields"], r))
            if row["unit"] not in ids:
                continue
            name = b["names"][row["name"]]
            a = acc.setdefault(name, [0, 0, 0])
            a[0] += row["calls"]
            a[1] += row["incl_cpu_ns"]
            a[2] += row["self_cpu_ns"]
            per_unit.setdefault(name, []).append(row["incl_ns"] / 1e6)
    from benchmarks.cadence import percentile
    out = []
    for name, (calls, cpu, self_cpu) in acc.items():
        out.append({"stage": name, "calls_per_unit": calls / n_units, "cpu_ms_per_unit": cpu / 1e6 / n_units,
                    "self_cpu_ms_per_unit": self_cpu / 1e6 / n_units,
                    "elapsed_p50_ms": percentile(per_unit[name], 50), "elapsed_p95_ms": percentile(per_unit[name], 95),
                    "units_with_stage": len(per_unit[name]), "cpu_pct_one_core": 100 * cpu / 1e9 / window})
    return sorted(out, key=lambda r: -r["cpu_ms_per_unit"]), n_units


def accounting(serve):
    """Process CPU of one stages run split by thread: collections (sampler thread, per attempt), request
    workers (each worker thread's whole CPU), listener (accept and dispatch), and what is left. The left
    part (main thread, interpreter, units across the window edges) is not attributed to anything."""
    elapsed = serve["measured_elapsed_s"]
    st, en = serve["start"]["self"], serve["end"]["self"]
    process = (en["ru_utime"] + en["ru_stime"]) - (st["ru_utime"] + st["ru_stime"])
    parts = {}
    for kind in KINDS:
        units, straddling = window_units(serve, kind)
        field = "thread_cpu_ns" if kind == "request" else "cpu_ns"
        parts[kind] = {"units": len(units), "straddling": straddling, "cpu_s": sum(u[field] for u in units) / 1e9}
    left = process - sum(p["cpu_s"] for p in parts.values())  # kept negative if negative
    return {"process_cpu_s": process, "elapsed_s": elapsed, "parts": parts, "unattributed_cpu_s": left,
            "unattributed_pct_one_core": 100 * left / elapsed}


def staged_run(r):
    return r["serve"].get("instrumentation", "").startswith("stages")


def profile_validity(serve):
    """None if a profile run's result can be read, else why not: a Python where per-thread profiling does
    not hold, or a negative total time (records of several threads on one thread's clock)."""
    p = serve["profile"]
    why = profile_support(tuple(int(x) for x in serve["python"].split(".")[:2]))
    if why:
        return why
    total = p.get("total_s")
    if total is None:  # results from before total_s was recorded: read it from the text
        m = re.search(r"in (-?[\d.]+) seconds", p["text"])
        total = float(m.group(1)) if m else None
    if total is None or total < 0:
        return f"total time {total} s is not a valid CPU total"
    return None


def comparison_sets(valid, summaries):
    """Reference and staged runs that may be compared: equal in every condition (cadence.mismatches:
    interval, poll, clients, Python, product and bench hash, scope) and in phase and variant; staged runs
    split by mode. Yields (key, mode, references, staged runs, problems); problems explain a staged set
    with no matching reference (it is then not compared, never matched with other references)."""
    from benchmarks.cadence import mismatches
    sets = {}
    for r in valid:
        s = summaries[r["run_id"]]
        key = (r["alias"], r["cond"]["phase"], r["serve"]["variant"], s["clients"], s["interval_s"], s["poll_s"],
               s["python"], s["source_hash"], s["bench_hash"], json.dumps(s["scope"], sort_keys=True))
        sets.setdefault(key, []).append(r)
    for key, rs in sets.items():
        refs = [r for r in rs if r["serve"]["instrumentation"] == "reference"]
        for mode in sorted({r["serve"]["instrumentation"] for r in rs if staged_run(r)}):
            staged = [r for r in rs if r["serve"]["instrumentation"] == mode]
            problems = []
            if not refs:
                nearby = [r for r in valid if r["serve"]["instrumentation"] == "reference" and r["alias"] == key[0]
                          and r["cond"]["phase"] == key[1] and r["serve"]["variant"] == key[2]]
                problems = (mismatches([summaries[r["run_id"]] for r in nearby + staged]) if nearby else []) \
                    or ["no reference run with the same conditions"]
            yield key, mode, refs, staged, problems


def report(paths, md=False, profiles=None):
    """Tables of docs/performance/collection-response-breakdown.md from cadence results.jsonl files.
    profiles: a directory to write each profile run's cProfile text to (marked when it is not valid)."""
    import os

    from benchmarks.cadence import dist, summarize_serve
    recs = []
    for path in paths:
        with open(path) as f:
            recs += [json.loads(l) for l in f if l.strip()]
    runs = [r for r in recs if r["kind"] == "run" and "serve" in r]
    out = {"invalid": [{"run_id": r["run_id"], "cond": r["cond"], "why": r.get("invalid") or r.get("skipped")}
                       for r in recs if r["kind"] == "run" and not r.get("valid")],
           "impact": [], "collection": [], "request": [], "accounting": [], "profiles": []}
    valid = [r for r in runs if r["valid"] and not r["serve"].get("profile")]  # profiles are never compared
    summaries = {r["run_id"]: summarize_serve(r) for r in valid}
    mean = (lambda v: sum(v) / len(v) if v else None)
    for key, mode, refs, staged_runs, problems in comparison_sets(valid, summaries):
        alias, phase, variant, clients = key[:4]
        sums = {"reference": [summaries[r["run_id"]] for r in refs],
                "stages": [summaries[r["run_id"]] for r in staged_runs]}
        ref = [s["cpu_pct_one_core"] for s in sums["reference"]]
        stg = [s["cpu_pct_one_core"] for s in sums["stages"]]
        delta = None if problems else mean(stg) - mean(ref)
        common = {"alias": alias, "phase": phase, "mode": mode, "variant": variant, "clients": clients,
                  "bench_hash": key[8]}
        out["impact"].append({
            **common, "reference": ref, "stages": stg, "problems": problems,
            "delta_pp": delta, "delta_pct": None if delta is None or not mean(ref) else 100 * delta / mean(ref),
            "interval_p50_ms": {m: [s["actual_interval_ms"]["p50"] for s in v] for m, v in sums.items()},
            "interval_max_ms": {m: [s["actual_interval_ms"]["max"] for s in v] for m, v in sums.items()},
            "wrapper_cost": [r["serve"]["wrapper_cost"] for r in staged_runs if r["serve"].get("wrapper_cost")][:1]})
        staged = [r["serve"] for r in staged_runs]
        table, n = stage_table(staged, "collection")
        out["collection"].append({**common, "units": n, "runs": len(staged), "stages": table})
        if clients:
            table, n = stage_table(staged, "request")
            http = sums["stages"]
            out["request"].append({**common, "units": n, "runs": len(staged), "stages": table,
                                   "bytes_per_request": dist([h["response_bytes"]["mean"] for h in http])["mean"],
                                   "requests": sum(h["requests"] for h in http)})
        for r in staged_runs:
            out["accounting"].append({"run_id": r["run_id"], **common, **accounting(r["serve"])})
    for r in runs:
        p = r["serve"].get("profile")
        if not (p and r["valid"]):
            continue
        why = profile_validity(r["serve"])
        out["profiles"].append({"run_id": r["run_id"], "alias": r["alias"], "python": r["serve"]["python"],
                                "mode": r["serve"]["instrumentation"], "units": p["units"], "timer": p["timer"],
                                "valid": why is None, "invalid": why})
        if profiles:
            os.makedirs(profiles, exist_ok=True)
            with open(os.path.join(profiles, f"{r['alias']}-{r['serve']['instrumentation']}.txt"), "w") as f:
                f.write((f"INVALID, not for analysis: {why}\n\n" if why else "") + p["text"])
    if md:
        return markdown(out)
    return json.dumps(out, indent=1, default=str)


def markdown(out):
    def f(v, nd=2):
        return "-" if v is None else f"{v:.{nd}f}"
    lines = ["| platform | mode | variant | clients | reference CPU % | stages CPU % | stages - reference pp (%) "
             "| actual interval p50 ms ref / stages | max ms ref / stages | wrapper cost ns/call (cpu) | quality |",
             "|" + "---|" * 11]
    for i in out["impact"]:
        q = (f"not comparable: {'; '.join(i['problems'])}" if i["problems"] else
             "ok" if i["delta_pct"] < 10 else "attribution less reliable (>= 10 %)")
        wc = i["wrapper_cost"][0]["cpu_ns_per_call"] if i["wrapper_cost"] else None
        lines.append("| " + " | ".join([
            i["alias"], i["mode"], i["variant"], str(i["clients"]), ", ".join(f(x) for x in i["reference"]) or "-",
            ", ".join(f(x) for x in i["stages"]) or "-",
            f"{f(i['delta_pp'], 3)} ({f(i['delta_pct'], 1)} %)",
            " / ".join(", ".join(f(x, 1) for x in i["interval_p50_ms"][m]) or "-" for m in ("reference", "stages")),
            " / ".join(", ".join(f(x, 1) for x in i["interval_max_ms"][m]) or "-" for m in ("reference", "stages")),
            f(wc, 0), q]) + " |")
    for title, key in (("collection", "collection"), ("request", "request")):
        for t in out[key]:
            extra = (f", {f(t['bytes_per_request'] / 1024, 1)} KiB/request, {t['requests']} requests"
                     if key == "request" else "")
            lines += ["", f"**{t['alias']} {t['variant']}, {t['clients']} client(s), {t['mode']}: per {title}** "
                          f"({t['units']} {title}s, {t['runs']} stages runs{extra})", "",
                      "| stage | calls / unit | CPU ms / unit (inclusive) | self CPU ms / unit | elapsed p50 / p95 ms "
                      "| CPU % of one core |", "|" + "---|" * 6]
            for s in t["stages"]:
                lines.append(f"| {s['stage']} | {f(s['calls_per_unit'], 1)} | {f(s['cpu_ms_per_unit'], 3)} | "
                             f"{f(s['self_cpu_ms_per_unit'], 3)} | {f(s['elapsed_p50_ms'], 3)} / {f(s['elapsed_p95_ms'], 3)} | "
                             f"{f(s['cpu_pct_one_core'], 3)} |")
    lines += ["", "| run | platform | mode | variant | clients | process CPU % | collections CPU % | request workers CPU % "
              "(n) | listener CPU % | unattributed CPU % | units across the window end |", "|" + "---|" * 11]
    for a in out["accounting"]:
        e = a["elapsed_s"]
        p = a["parts"]
        lines.append("| " + " | ".join([
            a["run_id"][-6:], a["alias"], a["mode"], a["variant"], str(a["clients"]), f(100 * a["process_cpu_s"] / e),
            f(100 * p["collection"]["cpu_s"] / e), f"{f(100 * p['request']['cpu_s'] / e)} ({p['request']['units']})",
            f(100 * p["listener"]["cpu_s"] / e), f(a["unattributed_pct_one_core"], 3),
            str(sum(x["straddling"] for x in p.values()))]) + " |")
    if out["profiles"]:
        lines += ["", "| profile run | platform | Python | mode | units | usable |", "|" + "---|" * 6]
        lines += [f"| {p['run_id'][-6:]} | {p['alias']} | {p['python']} | {p['mode']} | {p['units']} | "
                  f"{'yes' if p['valid'] else 'no: ' + p['invalid']} |" for p in out["profiles"]]
    if out["invalid"]:
        lines += ["", "Not run or invalid:"] + [f"- {x['run_id']}: {x['cond']} — {x['why']}" for x in out["invalid"]]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = argparse.ArgumentParser(description="stage breakdown report from cadence results.jsonl")
    p.add_argument("cmd", choices=("report",))
    p.add_argument("results", nargs="+")
    p.add_argument("--md", action="store_true")
    p.add_argument("--profiles", help="directory for the cProfile text of profile runs")
    a = p.parse_args()
    print(report(a.results, a.md, a.profiles))
