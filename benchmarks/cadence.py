#!/usr/bin/env python3
"""Measure what platmon costs per collection interval, and what the passive Network/Disk/PSI prototype
(benchmarks/passive.py) adds. Bench tool only: nothing here changes the product or its defaults.

  serve   one bench server: the product Sampler, collect_recorded and HTTP handler, plus a timed passive
          callback (B0: no-op, B1: Passive). Records into fixed-size buffers, writes one JSON at the end.
  client  a synthetic API client in its own process: GET /api/stats every --period seconds.
  probe   a standalone periodic read (noop, wireless, statvfs) for the conditional features.
  run     runs a plan of the above one at a time with a time/window budget and guards; appends results.jsonl.
  report  aggregates results.jsonl into the tables of docs/performance/low-overhead-cadence.md.

Usage, see docs/performance/low-overhead-cadence.md:
  python3 benchmarks/cadence.py run --out DIR --alias orin --phase matrix --intervals 0.5,1,2,5
  python3 benchmarks/cadence.py report DIR/results.jsonl
Passive off/on in one tree, see docs/performance/passive-cost-rebaseline.md:
  python3 benchmarks/cadence.py run --out DIR --alias orin --phase passive-rebaseline --budget-min 60 --max-windows 24
  python3 benchmarks/cadence.py run --out DIR --alias orin --phase passive-parts --budget-min 60 --max-windows 24
The product's own Network off/on (no passive prototype), see docs/performance/network-product.md:
  python3 benchmarks/cadence.py run --out DIR --alias orin --phase network-product --clients 1 --blocks 2 --budget-min 50 --max-windows 16
  python3 benchmarks/cadence.py run --out DIR --alias orin --phase network-product --clients 0 --blocks 1 --budget-min 50 --max-windows 16
The product's Disk I/O off/on with Network on in both, see docs/performance/disk-io-product.md:
  python3 benchmarks/cadence.py run --out DIR --alias orin --phase disk-product --clients 1 --blocks 2 --budget-min 50 --max-windows 12
"""
import argparse
import array
import contextlib
import errno
import glob
import hashlib
import json
import math
import os
import platform
import resource
import select
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
mono = time.monotonic_ns
tcpu = time.thread_time_ns
INSTRUMENTATION = ("reference", "stages", "stages-lite", "profile-collect", "profile-request", "passive-parts")
PARTS = ("network", "disk", "pressure.cpu", "pressure.memory", "pressure.io")  # codes 0-4 in the parts table
PART_STATES = {"ok": 1, "unsupported": 2, "error": 3}
LOCAL_FS = ("ext4", "ext3", "ext2", "xfs", "btrfs", "f2fs", "vfat", "exfat")  # statvfs allowlist by type


# ---- calculations (tested in tests/benchmarks/test_bench_cadence.py) ----

def cpu_pct_one_core(start, end, elapsed_s):
    """Process CPU (user+system) over elapsed_s as % of ONE core; never divided by the CPU count."""
    used = (end["ru_utime"] + end["ru_stime"]) - (start["ru_utime"] + start["ru_stime"])
    return 100 * used / elapsed_s


def percentile(values, p):
    """Nearest-rank percentile; None for no values (never 0)."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)]


def dist(values):
    v = [x for x in values if x is not None]
    return {"n": len(v), "mean": sum(v) / len(v) if v else None, "p50": percentile(v, 50),
            "p95": percentile(v, 95), "max": max(v) if v else None}


def slope_per_min(points):
    """Least-squares slope of (t_ns, value) points per minute; None for fewer than 3 points."""
    if len(points) < 3:
        return None
    n = len(points)
    xs = [t / 60e9 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else None


def rusage(who=resource.RUSAGE_SELF):
    r = resource.getrusage(who)
    return {k: getattr(r, k) for k in ("ru_utime", "ru_stime", "ru_maxrss", "ru_minflt", "ru_majflt",
                                       "ru_nvcsw", "ru_nivcsw")}


def sha256_files(paths):
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as f:
            h.update(os.path.relpath(p, ROOT).encode() + b"\0" + f.read())
    return h.hexdigest()[:16]


def source_hash():
    """The product code the bench runs (collector, runtime, frontends, platmon.py)."""
    return sha256_files(glob.glob(f"{ROOT}/collector/*.py") + glob.glob(f"{ROOT}/runtime/*.py")
                        + glob.glob(f"{ROOT}/frontends/*.py")
                        + glob.glob(f"{ROOT}/frontends/web/index.html")
                        + glob.glob(f"{ROOT}/frontends/web/*.js") + glob.glob(f"{ROOT}/frontends/web/*.css") + [f"{ROOT}/platmon.py"])


def bench_hash():
    return sha256_files(glob.glob(f"{HERE}/*.py"))


class Table:
    """Fixed-size int64 columns allocated up front, so recording does not grow memory with samples."""

    def __init__(self, fields, capacity):
        self.fields, self.capacity, self.n, self.dropped = fields, capacity, 0, 0
        self.cols = [array.array("q", bytes(8 * capacity)) for _ in fields]
        self.lock = threading.Lock()

    def add(self, *values):
        with self.lock:
            if self.n >= self.capacity:
                self.dropped += 1
                return
            for col, v in zip(self.cols, values):
                col[self.n] = v
            self.n += 1

    def nbytes(self):
        return sum(c.itemsize * len(c) for c in self.cols)

    def dump(self):
        return {"fields": self.fields, "dropped": self.dropped,
                "rows": [[c[i] for c in self.cols] for i in range(self.n)]}


def rows(table, t0=None, t1=None):
    """Dumped table rows as dicts, keeping those whose first field (a mono ns time) is in [t0, t1)."""
    out = [dict(zip(table["fields"], r)) for r in table["rows"]]
    return [r for r in out if (t0 is None or r[table["fields"][0]] >= t0) and (t1 is None or r[table["fields"][0]] < t1)]


def statm_rss():
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")


def smaps_rollup():
    out = {}
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("Rss", "Pss", "Private_Dirty", "Anonymous"):
                    out[k.lower() + "_bytes"] = int(v.split()[0]) * 1024
    except OSError as e:
        out["error"] = errno.errorcode.get(e.errno, str(e))
    return out


def boottime_ns():
    try:
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return None


def mark():
    """Process counters at a window edge."""
    return {"mono": mono(), "boot": boottime_ns(), "process_time_ns": time.process_time_ns(),
            "self": rusage(), "children": rusage(resource.RUSAGE_CHILDREN), "rss_bytes": statm_rss(),
            "smaps": smaps_rollup(), "threads": threading.active_count()}


def scope_of(stats, passive):
    """Counts that must match between runs that are compared (see mismatches)."""
    net = passive.get("network", {})
    disk = passive.get("disk", {})
    return {
        # The online cores: a core whose counters could not be used for one window (cpu_sampling.unavailable:
        # no_ticks, counter_regressed) has no row and no /cpu/ sensor entry then, but is still the same host.
        "cpu_rows": len(stats.get("cpu") or []) + len((stats.get("cpu_sampling") or {}).get("unavailable") or []),
        "temperature": len(stats.get("temperature") or {}),
        "power": len(stats.get("power") or {}), "fans": len(stats.get("fans") or []),
        "sensor_meta": sum(1 for k in stats.get("sensor_meta") or {} if not k.startswith("/cpu/")),
        "collectors": {k: v.get("state") for k, v in (stats.get("collectors") or {}).items()},
        "interfaces": len(net.get("interfaces", {})),
        "physical_interfaces": sum(1 for i in net.get("interfaces", {}).values() if i["physical"]),
        "block_devices": len(disk.get("devices", {})),
        "psi": {k: v["state"] for k, v in passive.get("pressure", {}).items()},
    }


def names_sha(names):
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:16]


def product_scope(stats, net, disk=None):
    """scope_of() for --product-network (and --product-disk) runs: the same sensor counts, with
    collectors.network / disk_io (there only when on, the intended difference) left out and what their output
    describes added: the namespace id and interface names, the disk names. net, disk: the run's own output
    (on) or one reading after the window (off); disk None without --product-disk."""
    scope = scope_of(stats, {})
    scope["collectors"].pop("network", None)
    names = [i["name"] for i in net["interfaces"]]
    scope["product_network"] = {"namespace": net["scope"]["id"], "interfaces": len(names), "names_sha": names_sha(names)}
    if disk is not None:
        scope["collectors"].pop("disk_io", None)
        disks = [d["name"] for d in disk["disks"]]
        scope["product_disk"] = {"disks": len(disks), "names_sha": names_sha(disks)}
    return scope


def time_network(network, table):
    """--product-network / --product-disk on: times each call of the product's NetworkCounters.sample or
    DiskCounters.sample where collect_recorded makes it, nothing else changed."""
    sample = network.sample

    def timed(group):
        t0, c0 = mono(), tcpu()
        try:
            return sample(group)
        finally:
            table.add(t0, mono() - t0, tcpu() - c0)
    network.sample = timed
    return network


def time_parts(probe, table):
    """passive-parts: times each of Passive's guarded calls (network, disk, each PSI file) on the instance,
    error conversion included, with the same calls, reads and results. Nothing is read twice."""
    guarded = probe._guarded

    def timed(fn):
        part = PARTS.index(fn.__name__ if fn.__name__ != "<lambda>" else f"pressure.{fn.__defaults__[0]}")
        reads, t0, c0 = probe.reads, mono(), tcpu()
        out = None
        try:
            out = guarded(fn)
            return out
        finally:  # also when it raises: the time is recorded either way
            state = PART_STATES.get(out.get("state"), 0) if isinstance(out, dict) else 0
            table.add(t0, part, mono() - t0, tcpu() - c0, state, probe.reads - reads)
    probe._guarded = timed
    return probe


# ---- serve ----

def serve(a):
    sys.path.insert(0, ROOT)
    import functools
    from http.server import ThreadingHTTPServer

    from benchmarks import breakdown
    from benchmarks.passive import Passive
    from collector import BOARD, PLATFORM, collect_recorded
    from collector.common import CpuCounters
    from collector.disk_io import DiskCounters
    from collector.network import NetworkCounters
    from runtime.sampler import Sampler, pick_clock
    from runtime import server

    cap = int((a.warmup + a.measure) / a.interval) + 64
    t_collect = Table(["t", "prod_ns", "prod_cpu_ns", "passive_ns", "passive_cpu_ns"], cap)
    t_attempt = Table(["t", "started", "elapsed_ns", "cpu_ns", "duration_ns", "ok", "sequence", "fallback"], cap)
    t_network = Table(["t", "elapsed_ns", "cpu_ns"], cap if a.product_network == "on" else 0)
    t_disk = Table(["t", "elapsed_ns", "cpu_ns"], cap if a.product_disk == "on" else 0)
    req_cap = int((a.warmup + a.measure + 10) * max(a.expect_rps, 1)) + 64
    t_http = Table(["t", "elapsed_ns", "cpu_ns", "read_ns", "read_cpu_ns", "bytes", "code", "sequence"], req_cap)
    t_mem = Table(["t", "rss_bytes"], int((a.warmup + a.measure) / 10) + 8)
    t_parts = Table(["t", "part", "elapsed_ns", "cpu_ns", "state", "reads"],
                    cap * len(PARTS) if a.instrumentation == "passive-parts" else 0)

    rec = prof = cost = None
    if a.instrumentation.startswith("stages"):
        cost = breakdown.wrapper_cost()
        rec = breakdown.Recorder(Table)
        rec.allocate(rows=cap * 40 + req_cap * 12, units=cap + 2 * req_cap)
    elif a.instrumentation.startswith("profile"):
        why = breakdown.profile_support()
        if why:  # before any work: the runner records the run as unsupported, not as a measurement
            print(f"unsupported: {why}", flush=True)
            sys.exit(3)
        prof = breakdown.Profiles()
    profiled = (prof.profiled if prof else contextlib.nullcontext)

    clock = pick_clock()
    cpu = CpuCounters(clock[0], max_gap=max(3 * a.interval, 5.0))
    network = None  # without --product-network: collect_recorded exactly as the phases before it ran it
    if a.product_network == "on":
        network = time_network(NetworkCounters(clock[0], max_gap=max(3 * a.interval, 5.0)), t_network)
    disk_io = None
    if a.product_disk == "on":
        disk_io = time_network(DiskCounters(clock[0], max_gap=max(3 * a.interval, 5.0)), t_disk)
    product = functools.partial(collect_recorded, cpu, {}, clock[0], network, disk_io)
    probe = Passive() if a.variant == "B1" else (lambda: None)
    if a.instrumentation == "passive-parts" and a.variant == "B1":
        time_parts(probe, t_parts)

    def collect():  # same wrapper in B0 and B1; only the callback differs
        t0, c0 = mono(), tcpu()
        c = product()
        t1, c1 = mono(), tcpu()
        extra = probe()
        t2, c2 = mono(), tcpu()
        if extra is not None:
            c.stats["_bench_passive"] = extra  # experimental, not a product field
        t_collect.add(t0, t1 - t0, c1 - c0, t2 - t1, c2 - c1)
        return c

    local = threading.local()

    class BenchSampler(Sampler):
        def _attempt(self):
            begun = rec and rec.begin()
            t0, c0 = mono(), tcpu()
            with (profiled() if a.instrumentation == "profile-collect" else contextlib.nullcontext()):
                started = super()._attempt()
            r = self._attempt_result
            published = r["state"] == "ok"  # this writer thread is the only one that changes _record
            t_attempt.add(t0, started, mono() - t0, tcpu() - c0, r["duration_ns"], published, self._sequence,
                          published and self._record["oldest_read"] is None)
            if rec:
                rec.end("collection", begun)
            return started

        def read(self, timeout=0.0):
            t0, c0 = mono(), tcpu()
            out = super().read(timeout)
            local.read = (mono() - t0, tcpu() - c0, (out[0] or {}).get("sample", {}).get("sequence", 0))
            return out

        if hasattr(Sampler, "_stats_json"):  # product code that serializes /api/stats without read()
            def _stats_json(self, timeout=0.0):  # timed as "read": the snapshot step of a request
                t0, c0 = mono(), tcpu()
                out = super()._stats_json(timeout)
                local.read = (mono() - t0, tcpu() - c0,
                              (out[1]["sample"] or {}).get("sequence", 0) if out[0] is not None else 0)
                return out

    sampler = BenchSampler(collect, a.interval, clock=clock)
    base = server.make_handler(sampler, True)

    class Handler(base):
        def do_GET(self):
            self._bytes, self._code, local.read = 0, 0, (0, 0, 0)
            t0, c0 = mono(), tcpu()
            super().do_GET()
            t_http.add(t0, mono() - t0, tcpu() - c0, *local.read[:2], self._bytes, self._code, local.read[2])

        def reply(self, code, body, ctype):
            self._bytes, self._code = len(body), code
            super().reply(code, body, ctype)

        def handle(self):
            with (profiled() if a.instrumentation == "profile-request" else contextlib.nullcontext()):
                super().handle()

    class Server(ThreadingHTTPServer):
        def process_request_thread(self, request, client_address):  # a request's whole worker thread
            begun = rec and rec.begin()
            try:
                super().process_request_thread(request, client_address)
            finally:
                if rec:
                    rec.end("request", begun)

        def _handle_request_noblock(self):  # the listener thread's accept and dispatch of one request
            begun = rec and rec.begin()
            try:
                super()._handle_request_noblock()
            finally:
                if rec:
                    rec.end("listener", begun)

    if rec:
        Handler.handle = rec.wrap("http.handle (parse, do_GET)", Handler.handle)
        Handler.do_GET = rec.wrap("http.do_GET", Handler.do_GET)
        Handler.reply = rec.wrap("http.reply (headers, write)", Handler.reply)
    installed = contextlib.ExitStack()
    if rec:
        installed.enter_context(breakdown.install(rec, breakdown.LITE_SKIP if a.instrumentation == "stages-lite" else ()))
    httpd = Server(("127.0.0.1", a.port), Handler)
    sampler.start()
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    print(f"ready {PLATFORM} {BOARD.__name__ if BOARD else 'none'}", flush=True)

    def sleep_until(deadline):  # main thread: an RSS sample every 10 s, nothing else
        while (left := deadline - mono()) > 0:
            time.sleep(min(10.0, left / 1e9))
            t_mem.add(mono(), statm_rss())

    begin = mono()
    sleep_until(begin + int(a.warmup * 1e9))
    start = mark()
    if prof:
        prof.on.set()
    sleep_until(start["mono"] + int(a.measure * 1e9))
    end = mark()
    if prof:
        prof.on.clear()
    sampler.stop()
    httpd.shutdown()
    installed.close()
    stats = sampler.read()[0] or {}
    if a.product_network:  # an off part is read once after the window, never inside it, for the scope only
        from collector import metadata
        net = (stats.get("network") or {"scope": {"id": None}, "interfaces": []} if a.product_network == "on"
               else NetworkCounters().sample(metadata.Group("network"))[0])
        disk = None if not a.product_disk else (stats.get("disk_io") or {"disks": []} if a.product_disk == "on"
                                                 else DiskCounters().sample(metadata.Group("disk_io"))[0])
        scope = product_scope(stats, net, disk)
    else:
        scope = scope_of(stats, Passive()() if a.variant == "B0" else stats.get("_bench_passive", {}))
    elapsed = (end["mono"] - start["mono"]) / 1e9
    boot = None if start["boot"] is None else (end["boot"] - start["boot"]) / 1e9
    out = {
        "kind": "serve", "run_id": a.run_id, "variant": a.variant, "instrumentation": a.instrumentation,
        "product_network": a.product_network, "product_disk": a.product_disk,
        "breakdown": rec and rec.dump(), "wrapper_cost": cost, "profile": prof and prof.report(),
        "interval_s": a.interval, "warmup_s": a.warmup,
        "measure_s": a.measure, "platform": PLATFORM, "begin_mono": begin, "start": start, "end": end,
        "measured_elapsed_s": elapsed, "boottime_elapsed_s": boot,
        "clock_jump": boot is not None and abs(boot - elapsed) > 1.0,  # suspend or a large clock step
        "cpu_pct_one_core": cpu_pct_one_core(start["self"], end["self"], elapsed),
        "cpu_pct_all_cores": cpu_pct_one_core(start["self"], end["self"], elapsed) / os.cpu_count(),
        "children_cpu_s": (end["children"]["ru_utime"] + end["children"]["ru_stime"])
                          - (start["children"]["ru_utime"] + start["children"]["ru_stime"]),
        "buffer_bytes": sum(t.nbytes() for t in (t_collect, t_attempt, t_http, t_mem, t_parts, t_network, t_disk)),
        "scope": scope, "passive_io": {k: getattr(probe, k, None) for k in ("reads", "read_bytes", "lookups")},
        "tables": {"collect": t_collect.dump(), "attempt": t_attempt.dump(), "http": t_http.dump(),
                   "mem": t_mem.dump(), "parts": t_parts.dump(), "network": t_network.dump(), "disk": t_disk.dump()},
        "python": platform.python_version(), "source_hash": source_hash(), "bench_hash": bench_hash(),
    }
    with open(a.out, "w") as f:
        json.dump(out, f)


# ---- client ----

def client(a):
    n = int(a.duration / a.period) + 8
    t = Table(["t", "latency_ns", "bytes", "status"], n)
    deadline = mono() + int(a.duration * 1e9)
    nxt = mono()
    while nxt < deadline:
        time.sleep(max(0, (nxt - mono()) / 1e9))
        t0, size, status = mono(), 0, 0
        try:
            with urllib.request.urlopen(a.url, timeout=5) as r:
                size, status = len(r.read()), r.status
        except OSError as e:
            status = getattr(e, "code", -1) or -1  # -1: no HTTP answer (refused, timeout)
        t.add(t0, mono() - t0, size, status)
        nxt = max(nxt + int(a.period * 1e9), mono())  # a late request does not trigger a burst of catch-ups
    with open(a.out, "w") as f:
        json.dump({"kind": "client", "period_s": a.period, "rusage": rusage(), "table": t.dump()}, f)


# ---- probe (conditional features) ----

def parse_wireless(text):
    """{iface: signal level dBm} from /proc/net/wireless; a row is only there while associated."""
    out = {}
    for line in text.splitlines()[2:]:
        name, sep, rest = line.partition(":")
        f = rest.split()
        if sep and len(f) >= 3:
            out[name.strip()] = float(f[2].rstrip("."))
    return out


def local_mounts(text):
    """Mount points of local block filesystems from /proc/self/mountinfo; network, FUSE, autofs, pseudo and
    unknown types are left out, so statvfs never wakes an automount or a remote server."""
    out = []
    for line in text.splitlines():
        left, _, right = line.partition(" - ")
        fstype, source = (right.split() + ["", ""])[:2]
        if fstype in LOCAL_FS and source.startswith("/dev/"):
            out.append(left.split()[4].replace("\\040", " "))
    return sorted(set(out))


def probe(a):
    """Calls one feature every --period seconds for --duration. statvfs runs in one worker thread; while a
    call is still running the next tick is skipped (counted), never stacked."""
    def noop():
        return 1, None

    def wireless():
        with open("/proc/net/wireless") as f:
            levels = parse_wireless(f.read())
        return len(levels), (max(levels.values()) if levels else None)

    mounts = []

    def mountinfo():
        with open("/proc/self/mountinfo") as f:
            mounts[:] = local_mounts(f.read())
        return len(mounts), None

    def statvfs():
        for m in mounts:
            os.statvfs(m)
        return len(mounts), None

    steps = {"noop": [noop], "wireless": [wireless], "statvfs": [mountinfo, statvfs]}[a.feature]
    n = int(a.duration / a.period) + 8
    t = Table(["t", "step", "elapsed_ns", "cpu_ns", "count", "value_x1000", "ok"], n * len(steps) + 2 * 100 + 8)
    busy = threading.Event()
    skipped = [0]

    def tick(label_offset=0):  # busy is set by the caller, before the thread starts
        try:
            for i, fn in enumerate(steps):
                t0, c0 = mono(), tcpu()
                try:
                    count, value = fn()
                    ok = 1
                except OSError:
                    count, value, ok = 0, None, 0
                t.add(t0, i + label_offset, mono() - t0, tcpu() - c0, count,
                      -10**12 if value is None else int(value * 1000), ok)
        finally:
            busy.clear()

    start = mark()
    deadline = start["mono"] + int(a.duration * 1e9)
    nxt = mono()
    while nxt < deadline:
        time.sleep(max(0, (nxt - mono()) / 1e9))
        if busy.is_set():
            skipped[0] += 1
        else:
            busy.set()
            threading.Thread(target=tick, daemon=True).start()
        nxt += int(a.period * 1e9)
    time.sleep(max(0, (deadline - mono()) / 1e9))  # the window is the full duration, also for long periods
    end = mark()
    elapsed = (end["mono"] - start["mono"]) / 1e9
    for _ in range(50):  # a call still running is waited for up to 5 s, never killed or repeated
        if not busy.is_set():
            break
        time.sleep(0.1)
    pending = busy.is_set()
    if a.burst and not pending:  # back-to-back calls, reported apart from the real cadence (step + 10)
        for _ in range(a.burst):
            busy.set()
            tick(10)
    with open(a.out, "w") as f:
        json.dump({"kind": "probe", "run_id": a.run_id, "feature": a.feature, "period_s": a.period,
                   "measured_elapsed_s": elapsed, "cpu_pct_one_core": cpu_pct_one_core(start["self"], end["self"], elapsed),
                   "start": start, "end": end, "skipped_ticks": skipped[0], "pending_at_end": pending,
                   "mounts": len(mounts), "table": t.dump(), "python": platform.python_version(),
                   "bench_hash": bench_hash()}, f)


# ---- run (orchestrator) ----

def plan(phase, intervals, final=None, clients=(1, 0), blocks=2):
    """The conditions of one phase, in run order. B0 B1 B1 B0 per interval, so a drift over time does not
    favour one variant."""
    out = []
    if phase == "matrix":
        for iv in intervals:
            out += [dict(kind="serve", variant=v, interval=iv, clients=1, poll=1.0, measure=120) for v in ("B0", "B1", "B1", "B0")]
    elif phase == "clients":
        for clients, poll in ((0, 1.0), (3, 1.0), (1, 2.0)):
            out += [dict(kind="serve", variant=v, interval=1.0, clients=clients, poll=poll, measure=120) for v in ("B0", "B1")]
    elif phase == "final":
        measure = min(600, max(300, 120 * final))
        out += [dict(kind="serve", variant=v, interval=final, clients=1, poll=1.0, measure=measure) for v in ("B0", "B1")]
    elif phase == "breakdown":  # interval 1 s, priority order; reference/stages pairs bracket each other
        rr = ("reference", "stages", "stages", "reference")
        out += [dict(kind="serve", variant="B0", instr=m, interval=1.0, clients=1, poll=1.0, measure=120) for m in rr]
        out += [dict(kind="serve", variant="B0", instr=m, interval=1.0, clients=0, poll=1.0, measure=120) for m in rr]
        out += [dict(kind="serve", variant="B0", instr=m, interval=1.0, clients=1, poll=1.0, measure=50)
                for m in ("profile-collect", "profile-request")]
        out += [dict(kind="serve", variant="B0", instr=m, interval=1.0, clients=3, poll=1.0, measure=120) for m in rr]
        out += [dict(kind="serve", variant="B1", instr=m, interval=1.0, clients=1, poll=1.0, measure=120) for m in rr]
    elif phase == "ab":  # one reference window; run from two trees in turn (A B B A) to compare product code
        out += [dict(kind="serve", variant="B0", instr="reference", interval=1.0, clients=1, poll=1.0, measure=120)]
    elif phase == "breakdown-lite":  # the one retry with fewer stages, when stages cost >= 10 % over reference
        out += [dict(kind="serve", variant="B0", instr=m, interval=1.0, clients=1, poll=1.0, measure=120)
                for m in ("reference", "stages-lite", "stages-lite", "reference")]
    elif phase == "passive-rebaseline":  # B0/B1 = passive off/on in one tree; blocks alternate client counts
        for b in range(blocks):
            for n in clients:
                out += [dict(kind="serve", variant=v, instr="reference", interval=1.0, clients=n, poll=1.0, measure=120,
                             block=f"c{n}-{b + 1}") for v in ("B0", "B1", "B1", "B0")]
    elif phase == "network-product":  # A/B = the product's Network off/on, passive callback a no-op in both
        for b in range(blocks):
            for n in clients:
                out += [dict(kind="serve", variant="B0", net=v, instr="reference", interval=1.0, clients=n, poll=1.0,
                             measure=120, block=f"c{n}-{b + 1}") for v in ("off", "on", "on", "off")]
    elif phase == "disk-product":  # A/B = the product's Disk I/O off/on; Network on and the passive no-op in both
        for b in range(blocks):
            for n in clients:
                out += [dict(kind="serve", variant="B0", net="on", disk=v, instr="reference", interval=1.0, clients=n,
                             poll=1.0, measure=120, block=f"c{n}-{b + 1}") for v in ("off", "on", "on", "off")]
    elif phase == "passive-parts":  # diagnostic only: B1 with each passive part timed, bracketed by reference
        out += [dict(kind="serve", variant="B1", instr=m, interval=1.0, clients=0, poll=1.0, measure=120, block="parts-1")
                for m in ("reference", "passive-parts", "passive-parts", "reference")]
    elif phase == "probes":
        out += [dict(kind="probe", feature="noop", period=1.0, measure=120)]
        out += [dict(kind="probe", feature="wireless", period=p, measure=120) for p in (1.0, 2.0, 5.0)]
        out += [dict(kind="probe", feature="statvfs", period=p, measure=120) for p in (30.0, 60.0)]
    else:
        raise ValueError(f"unknown phase {phase}")
    for c in out:
        c["phase"] = phase
    return out


def budget_left(done, cond, warmup, budget_s, max_windows):
    """None if cond fits the remaining budget (wall time and windows), else why not."""
    used = sum(r.get("wall_s", 0) for r in done)
    windows = sum(1 for r in done if r.get("counted", True))
    need = cond["measure"] + (warmup if cond["kind"] == "serve" else 0) + 15
    if windows + 1 > max_windows:
        return f"window budget spent ({windows}/{max_windows})"
    if used + need > budget_s:
        return f"time budget spent ({used:.0f}+{need:.0f} s > {budget_s:.0f} s)"
    return None


def device_state():
    """Temperatures, clocks and throttle flags, read between runs only (never inside a window)."""
    def cat(p):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            return None
    temps = {os.path.basename(z): cat(f"{z}/temp") for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*"))}
    freqs = {os.path.basename(os.path.dirname(c)): cat(c)
             for c in sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq"))}
    out = {"mono": mono(), "temps_mC": temps, "cpufreq_khz": freqs, "loadavg": cat("/proc/loadavg")}
    if shutil.which("vcgencmd"):
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True)
        out["throttled"] = r.stdout.strip()
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            mem[k] = int(v.split()[0]) * 1024
    out["mem_available"] = mem["MemAvailable"]
    return out


def throttled_now(state):
    """Current (not sticky) throttle bits from vcgencmd get_throttled, or None without vcgencmd."""
    t = state.get("throttled")
    return None if not t or "=" not in t else int(t.split("=")[1], 16) & 0xF


def guard(state, out_dir, watch_url):
    """None if it is safe to start the next run, else why not."""
    if throttled_now(state):
        return f"current throttle/undervoltage bits set: {state['throttled']}"
    if state["mem_available"] < 300 * 2**20:
        return "less than 300 MiB available memory"
    if shutil.disk_usage(out_dir).free < 500 * 2**20:
        return "less than 500 MiB free disk"
    if watch_url:
        try:
            with urllib.request.urlopen(watch_url, timeout=5) as r:
                if r.status != 200:
                    return f"watched service answered {r.status}"
        except OSError as e:
            return f"watched service did not answer: {e}"
    return None


def manifest(alias):
    def cat(p):
        try:
            with open(p) as f:
                return f.read().strip().rstrip("\0")
        except OSError:
            return None

    def cmd(*args):
        if not shutil.which(args[0]):
            return None
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return (r.stdout or r.stderr).strip()
    sys.path.insert(0, ROOT)
    from collector import PLATFORM, collect
    stats = collect()  # one-off reading for the sensor counts
    nets = sorted(os.listdir("/sys/class/net"))
    release = os.uname().release
    mem = cat("/proc/meminfo").split("\n")[0]
    return {
        "kind": "manifest", "alias": alias, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "platform": PLATFORM,
        "model": cat("/proc/device-tree/model") or cat("/sys/class/dmi/id/product_name"),
        "os": stats["system"]["os"], "kernel": release, "arch": platform.machine(),
        "python": platform.python_version(), "python_bits": 64 if sys.maxsize > 2**32 else 32,
        "userland_bits": cmd("getconf", "LONG_BIT"), "online_cpus": cat("/sys/devices/system/cpu/online"),
        "cpu_count": os.cpu_count(), "mem_total": mem,
        "governors": sorted(set(filter(None, (cat(p) for p in glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"))))),
        "nvpmodel": cmd("nvpmodel", "-q"), "l4t": (cat("/etc/nv_tegra_release") or "").split("\n")[0] or None,
        "throttled": cmd("vcgencmd", "get_throttled"),
        "block_devices": {d: {"removable": cat(f"/sys/block/{d}/removable"), "rotational": cat(f"/sys/block/{d}/queue/rotational")}
                          for d in sorted(os.listdir("/sys/block"))},
        "root_source": next((l.split()[0] for l in cat("/proc/mounts").split("\n") if l.split()[1] == "/"), None),
        "interfaces": {n: os.path.exists(f"/sys/class/net/{n}/device") for n in nets},
        "psi": {n: os.path.exists(f"/proc/pressure/{n}") for n in ("cpu", "memory", "io")},
        "wireless": parse_wireless(cat("/proc/net/wireless") or ""),
        "sensors": {k: len(stats.get(k) or []) for k in ("temperature", "power", "fans", "cpu")},
        "mode": "wsl" if "microsoft" in release.lower() else ("container" if os.path.exists("/.dockerenv") else "native"),
        "top_processes": cmd("ps", "-eo", "pcpu,comm", "--sort=-pcpu", "--no-headers")[:400],
        "clocks": {n: dict(vars(time.get_clock_info(n))) for n in ("monotonic", "process_time", "thread_time", "perf_counter")},
        "source_hash": source_hash(), "bench_hash": bench_hash(),
    }


def run(a):
    os.makedirs(a.out, exist_ok=True)
    results = os.path.join(a.out, "results.jsonl")
    done = []
    if os.path.exists(results):  # the budget counts every earlier phase in this directory
        with open(results) as f:
            done = [json.loads(l) for l in f if l.strip()]
    if not any(r["kind"] == "manifest" for r in done):
        done.append(manifest(a.alias))
        with open(results, "a") as f:
            f.write(json.dumps(done[-1]) + "\n")
    conds = plan(a.phase, [float(x) for x in a.intervals.split(",")] if a.intervals else [], a.final,
                 tuple(int(x) for x in a.clients.split(",")), a.blocks)
    for c in conds:
        c["measure"] = a.measure or c["measure"]  # a shorter window is for smoke tests only
    me = sys.executable
    stop = None
    for i, c in enumerate(conds):
        run_id = f"{a.alias}-{a.phase}-{i:02d}-{uuid.uuid4().hex[:6]}"
        rec = {"kind": "run", "run_id": run_id, "alias": a.alias, "cond": c, "source_sha": a.source_sha,
               "order": i, "counted": False}
        why = stop or budget_left([r for r in done if r["kind"] == "run"], c, a.warmup, a.budget_min * 60, a.max_windows)
        if not why and c.get("instr", "").startswith("profile"):
            sys.path.insert(0, ROOT)
            from benchmarks import breakdown
            unsupported = breakdown.profile_support()
            if unsupported:  # skipped, not a failure: the other runs go on
                rec.update(valid=False, unsupported=True, skipped=f"unsupported: {unsupported}")
                print(f"{run_id}: skipped: unsupported profile mode", flush=True)
                done.append(rec)
                with open(results, "a") as f:
                    f.write(json.dumps(rec) + "\n")
                continue
        before = device_state()
        why = why or guard(before, a.out, a.watch_url)
        if why:
            rec.update(valid=False, skipped=why)
            stop = stop or why
            print(f"{run_id}: skipped: {why}", flush=True)
        else:
            t0 = time.time()
            rec.update(started=time.strftime("%Y-%m-%dT%H:%M:%S%z"), counted=True, before=before)
            rec.update(execute(me, run_id, c, a))
            after = device_state()
            rec.update(after=after, wall_s=time.time() - t0)
            new_throttle = throttled_now(after)
            if new_throttle:
                rec.update(valid=False, invalid=f"throttle bits during run: {after['throttled']}")
                stop = rec["invalid"]
            print(f"{run_id}: {c} valid={rec['valid']} {rec.get('invalid', '')}", flush=True)
            if rec.get("timeout"):
                stop = "a measurement process did not end in time"
        done.append(rec)
        with open(results, "a") as f:
            f.write(json.dumps(rec) + "\n")


def child_cmd(me, *args):
    """The command line of a bench child process (replaced in tests)."""
    return [me, __file__, *args]


def stop_process(p, grace=10.0):
    """Kills p (a process this runner started) and waits up to grace s for it to end. True if it ended."""
    if p.poll() is None:
        p.kill()
    try:
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return False
    return True


def wait_line(p, timeout, limit=4096):
    """The first stdout line of p (a binary pipe), or None unless a whole line came within timeout s: one
    deadline for the whole line, so output without a newline does not extend it (nor does EOF or a line
    longer than limit count as one)."""
    deadline, buf, fd = time.monotonic() + timeout, b"", p.stdout.fileno()
    while b"\n" not in buf and len(buf) < limit:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return None
        chunk = os.read(fd, limit - len(buf))  # returns what is there; never waits for a newline
        if not chunk:
            break
        buf += chunk
    line, newline, _ = buf.partition(b"\n")
    return line.decode(errors="replace") + "\n" if newline else None


def load(path):
    """A child's result file, or None if it is missing or unreadable (the child failed before writing it)."""
    try:
        with open(path) as f:
            out = json.load(f)
    except (OSError, ValueError):
        return None
    os.remove(path)
    return out


def failed(why, procs, **extra):
    """An invalid result after stopping procs. If one of them does not end, the result says so and the
    runner starts no further run (timeout)."""
    alive = [p.pid for p in procs if not stop_process(p)]
    out = {"valid": False, "invalid": why, **extra}
    if alive:
        out.update(timeout=True, unreaped=alive, invalid=f"{why}; still running after kill: {alive}")
    return out


def execute(me, run_id, c, a):
    """One condition: the server (or probe) and its clients as separate processes; returns their results.
    Every wait has a deadline, from the start (ready line) to the processes' end."""
    tmp = os.path.join(a.out, "tmp")
    os.makedirs(tmp, exist_ok=True)
    out = {"valid": True}
    if c["kind"] == "probe":
        path = f"{tmp}/{run_id}.json"
        p = subprocess.Popen(child_cmd(me, "probe", "--feature", c["feature"], "--period", str(c["period"]),
                                       "--duration", str(c["measure"]), "--out", path, "--run-id", run_id,
                                       "--burst", "100" if c["feature"] == "statvfs" else "0"))
        try:
            p.wait(timeout=c["measure"] + 60)
        except subprocess.TimeoutExpired:
            return failed("probe timeout", [p], timeout=True)
        result = None if p.returncode else load(path)
        if result is None:
            return {"valid": False, "invalid": f"probe exit {p.returncode}, no result"}
        out["probe"] = result
        if result["pending_at_end"]:  # the slowest call never finished: its time is not in the samples
            out.update(valid=False, incomplete=True, invalid="a probe call was still running at the end")
        return out
    path = f"{tmp}/{run_id}.serve.json"
    srv = subprocess.Popen(child_cmd(me, "serve", "--variant", c["variant"], "--interval", str(c["interval"]),
                                     "--warmup", str(a.warmup), "--measure", str(c["measure"]), "--port", str(a.port),
                                     "--instrumentation", c.get("instr", "reference"),
                                     "--out", path, "--run-id", run_id, "--expect-rps", str(c["clients"] / c["poll"]),
                                     *(("--product-network", c["net"]) if c.get("net") else ()),
                                     *(("--product-disk", c["disk"]) if c.get("disk") else ())),
                           stdout=subprocess.PIPE)
    ready = wait_line(srv, a.ready_timeout)
    if ready and ready.startswith("unsupported"):
        return failed(ready.strip(), [srv], unsupported=True)
    if not ready or not ready.startswith("ready"):
        return failed(f"server not ready: {ready!r}", [srv])
    clients = [subprocess.Popen(child_cmd(me, "client", "--url", f"http://127.0.0.1:{a.port}/api/stats",
                                          "--period", str(c["poll"]), "--duration", str(a.warmup + c["measure"] + 2),
                                          "--out", f"{tmp}/{run_id}.client{k}.json")) for k in range(c["clients"])]
    try:
        srv.wait(timeout=a.warmup + c["measure"] + 60)
    except subprocess.TimeoutExpired:
        return failed("server timeout", [srv, *clients], timeout=True)
    for p in clients:
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            return failed("client timeout", clients, timeout=True)
    result = None if srv.returncode else load(path)
    if result is None:
        return {"valid": False, "invalid": f"server exit {srv.returncode}, no result"}
    out["serve"] = result
    out["clients"] = [load(f"{tmp}/{run_id}.client{k}.json") for k in range(c["clients"])]
    if None in out["clients"]:
        out.update(valid=False, invalid="a client wrote no result")
        out["clients"] = [x for x in out["clients"] if x is not None]
    if out["serve"]["clock_jump"]:
        out.update(valid=False, invalid="clock jump (suspend?) during the window")
    return out


# ---- report ----

def summarize_serve(rec):
    """One run's numbers, from the measured window only (warmup excluded, actual elapsed time)."""
    s = rec["serve"]
    t0, t1 = s["start"]["mono"], s["end"]["mono"]
    att = rows(s["tables"]["attempt"], t0, t1)
    col = rows(s["tables"]["collect"], t0, t1)
    http = rows(s["tables"]["http"], t0, t1)
    mem = rows(s["tables"]["mem"], t0, t1 + 1)
    started = [r["started"] for r in att]
    gaps = [(b - a) / 1e6 for a, b in zip(started, started[1:])]
    interval_ms = s["interval_s"] * 1000
    lat = [r for c in rec.get("clients", []) for r in rows(c["table"], t0, t1)]
    st, en = s["start"]["self"], s["end"]["self"]
    return {
        "run_id": rec["run_id"], "alias": rec["alias"], "phase": rec["cond"]["phase"], "variant": s["variant"],
        "instr": s.get("instrumentation", "reference"), "block": rec["cond"].get("block"),
        "product_network": s.get("product_network"), "fallback_samples": sum(r.get("fallback", 0) for r in att),
        "network_ms": dist([r["elapsed_ns"] / 1e6 for r in rows(s["tables"]["network"], t0, t1)])
        if s["tables"].get("network") else None,
        "network_cpu_ms": dist([r["cpu_ns"] / 1e6 for r in rows(s["tables"]["network"], t0, t1)])
        if s["tables"].get("network") else None,
        "product_disk": s.get("product_disk"),
        "disk_ms": dist([r["elapsed_ns"] / 1e6 for r in rows(s["tables"]["disk"], t0, t1)])
        if s["tables"].get("disk") else None,
        "disk_cpu_ms": dist([r["cpu_ns"] / 1e6 for r in rows(s["tables"]["disk"], t0, t1)])
        if s["tables"].get("disk") else None,
        "passive_calls": len(col), "parts": parts_summary(s, t0, t1),
        "interval_s": s["interval_s"], "clients": rec["cond"]["clients"], "poll_s": rec["cond"]["poll"],
        "valid": rec["valid"], "invalid": rec.get("invalid"), "order": rec["order"],
        "measured_elapsed_s": s["measured_elapsed_s"], "sample_count": sum(r["ok"] for r in att),
        "failed_attempts": sum(1 for r in att if not r["ok"]),
        "process_user_s": en["ru_utime"] - st["ru_utime"], "process_system_s": en["ru_stime"] - st["ru_stime"],
        "cpu_pct_one_core": s["cpu_pct_one_core"],
        "process_time_pct": 100 * (s["end"]["process_time_ns"] - s["start"]["process_time_ns"]) / 1e9 / s["measured_elapsed_s"],
        "children_cpu_s": s["children_cpu_s"],
        "passive_ms": dist([r["passive_ns"] / 1e6 for r in col]),
        "passive_cpu_ms": dist([r["passive_cpu_ns"] / 1e6 for r in col]),
        "product_collect_ms": dist([r["prod_ns"] / 1e6 for r in col]),
        "product_collect_cpu_ms": dist([r["prod_cpu_ns"] / 1e6 for r in col]),
        "attempt_ms": dist([r["elapsed_ns"] / 1e6 for r in att]),
        "attempt_cpu_ms": dist([r["cpu_ns"] / 1e6 for r in att]),
        "sample_duration_ms": dist([r["duration_ns"] / 1e6 for r in att]),
        "actual_interval_ms": dist(gaps),
        "start_delay_ms": dist([g - interval_ms for g in gaps]),
        "overrun_count": sum(1 for r in att if r["elapsed_ns"] / 1e6 > interval_ms),
        "rss_start": s["start"]["rss_bytes"], "rss_end": s["end"]["rss_bytes"],
        "pss_start": s["start"]["smaps"].get("pss_bytes"), "pss_end": s["end"]["smaps"].get("pss_bytes"),
        "maxrss_kib": s["end"]["self"]["ru_maxrss"], "rss_slope_kib_per_min": (lambda v: v and v / 1024)(
            slope_per_min([(r["t"], r["rss_bytes"]) for r in mem])),
        # growth over the window's second half: a one-off allocation step early on is not a trend
        "rss_second_half_kib": (lambda pts: (pts[-1]["rss_bytes"] - min(pts, key=lambda r: abs(r["t"] - (t0 + t1) / 2))["rss_bytes"]) / 1024
                                if len(pts) >= 3 else None)(mem),
        "buffer_bytes": s["buffer_bytes"],
        "ctx_voluntary": en["ru_nvcsw"] - st["ru_nvcsw"], "ctx_involuntary": en["ru_nivcsw"] - st["ru_nivcsw"],
        "minor_faults": en["ru_minflt"] - st["ru_minflt"], "major_faults": en["ru_majflt"] - st["ru_majflt"],
        "requests": len(http), "http_errors": sum(1 for r in http if r["code"] != 200),
        "response_bytes": dist([r["bytes"] for r in http]),
        "handler_ms": dist([r["elapsed_ns"] / 1e6 for r in http]),
        "handler_cpu_ms": dist([r["cpu_ns"] / 1e6 for r in http]),
        "read_cpu_ms": dist([r["read_cpu_ns"] / 1e6 for r in http]),
        "distinct_sequences_served": len({r["sequence"] for r in http}),
        "client_latency_ms": dist([r["latency_ns"] / 1e6 for r in lat]),
        "client_requests": len(lat), "client_failures": sum(1 for r in lat if r["status"] != 200),
        "client_cpu_s": sum(c["rusage"]["ru_utime"] + c["rusage"]["ru_stime"] for c in rec.get("clients", [])),
        "passive_io": s["passive_io"], "scope": s["scope"], "python": s["python"], "source_hash": s["source_hash"],
        "bench_hash": s["bench_hash"], "temps_before": rec["before"]["temps_mC"], "temps_after": rec["after"]["temps_mC"],
        "throttled_before": rec["before"].get("throttled"), "throttled_after": rec["after"].get("throttled"),
        "freq_before": rec["before"]["cpufreq_khz"], "freq_after": rec["after"]["cpufreq_khz"],
    }


def parts_summary(s, t0, t1):
    """passive-parts: per part, the window's calls, their CPU as % of one core over the window (the sum of
    the calls' thread CPU, never a percentile times calls), mean, p50/p95 and the results' states."""
    table = s["tables"].get("parts")
    if not table or not table["rows"]:
        return None
    rs = rows(table, t0, t1)
    out = {}
    for code, name in enumerate(PARTS):
        mine = [r for r in rs if r["part"] == code]
        out[name] = {"calls": len(mine), "cpu_pct_one_core": 100 * sum(r["cpu_ns"] for r in mine) / 1e9 / s["measured_elapsed_s"],
                     "cpu_ms": dist([r["cpu_ns"] / 1e6 for r in mine]), "elapsed_ms": dist([r["elapsed_ns"] / 1e6 for r in mine]),
                     "states": {k: sum(1 for r in mine if r["state"] == v) for k, v in {**PART_STATES, "other": 0}.items()},
                     "successful_reads": sum(r["reads"] for r in mine)}
    return out


def blocks(runs):
    """Per block (one A B B A of one client count): mean B1 - mean B0 of that block's valid runs; None, with
    the reasons, when the block's runs are not comparable (see mismatches)."""
    groups = {}
    for r in runs:
        if r["valid"] and r.get("block"):
            groups.setdefault(r["block"], []).append(r)
    out = {}
    for b, rs in groups.items():
        v = {k: [r["cpu_pct_one_core"] for r in rs if r["variant"] == k] for k in ("B0", "B1")}
        problems = mismatches(rs)
        delta = sum(v["B1"]) / len(v["B1"]) - sum(v["B0"]) / len(v["B0"]) if v["B0"] and v["B1"] and not problems else None
        out[b] = {**v, "delta_pp": delta, "problems": problems}
    return out


def mismatches(runs):
    """Why runs compared as B0 vs B1 are not comparable (empty if they are)."""
    keys = ("alias", "interval_s", "clients", "poll_s", "python", "source_hash", "bench_hash", "scope")
    out = []
    for k in keys:
        values = {json.dumps(r[k], sort_keys=True) for r in runs}
        if len(values) > 1:
            out.append(f"{k} differs: {sorted(values)}")
    return out


def compare(runs):
    """B1 - B0 of one condition, from per-run values; 'indistinguishable' when the difference is not larger
    than the spread between repeats of the same variant (or there are no repeats to tell)."""
    valid = [r for r in runs if r["valid"]]
    b0 = [r["cpu_pct_one_core"] for r in valid if r["variant"] == "B0"]
    b1 = [r["cpu_pct_one_core"] for r in valid if r["variant"] == "B1"]
    if not b0 or not b1:
        return {"b0": b0, "b1": b1, "delta_pp": None, "verdict": "insufficient", "problems": mismatches(valid),
                "blocks": blocks(valid)}
    delta = sum(b1) / len(b1) - sum(b0) / len(b0)  # kept negative if negative
    spread = max(max(b0) - min(b0), max(b1) - min(b1))
    repeats = len(b0) > 1 and len(b1) > 1
    return {"b0": b0, "b1": b1, "delta_pp": delta, "spread_pp": spread if repeats else None,
            "distinguishable": repeats and abs(delta) > spread,
            "rel_pct": 100 * delta / (sum(b0) / len(b0)) if sum(b0) else None, "problems": mismatches(valid),
            "blocks": blocks(valid)}


def network_compare(runs, recs, label="network"):
    """network-product (label network) / disk-product (label disk): compare() with the product feature off
    as b0 and on as b1 (the variant is B0, the passive no-op, in both), plus what only these runs have: the
    feature's call itself (on runs; off runs must have none) and per-arm means of the costs around it."""
    axis = f"product_{label}"
    arm = {"off": "B0", "on": "B1"}
    out = {"arms": {"b0": f"{label} off", "b1": f"{label} on"},
           **compare([dict(r, variant=arm[r[axis]]) for r in runs])}
    valid = [r for r in runs if r["valid"]]
    on = [r for r in recs if r["valid"] and r["serve"].get(axis) == "on"]
    for name, field, scale in ((f"{label}_ms", "elapsed_ns", 1e6), (f"{label}_cpu_ms", "cpu_ns", 1e6)):
        per_run = [[x[field] / scale for x in rows(r["serve"]["tables"][label], r["serve"]["start"]["mono"],
                                                   r["serve"]["end"]["mono"])] for r in on]
        out[name] = {**dist([x for w in per_run for x in w]),
                     "per_run_max_p95": max((percentile(w, 95) for w in per_run if w), default=None)}
    out[f"{label}_calls_when_off"] = sum((r[f"{label}_ms"] or {"n": 0})["n"] for r in valid if r[axis] == "off")

    def mean(values):
        v = [x for x in values if x is not None]
        return sum(v) / len(v) if v else None
    out["arm_means"] = {side: {
        "runs": len(rs), "attempt_cpu_ms": mean([r["attempt_cpu_ms"]["mean"] for r in rs]),
        "attempt_ms_p95": mean([r["attempt_ms"]["p95"] for r in rs]),
        "handler_cpu_ms": mean([r["handler_cpu_ms"]["mean"] for r in rs]),
        "handler_ms_p95": mean([r["handler_ms"]["p95"] for r in rs]),
        "client_latency_ms_p95": mean([r["client_latency_ms"]["p95"] for r in rs]),
        "response_bytes": mean([r["response_bytes"]["mean"] for r in rs]),
        "rss_end_mib": mean([r["rss_end"] / 2**20 for r in rs]),
        "pss_end_mib": mean([r["pss_end"] and r["pss_end"] / 2**20 for r in rs]),
        "rss_second_half_kib_max": max((r["rss_second_half_kib"] for r in rs if r["rss_second_half_kib"] is not None),
                                       default=None),
        "samples": sum(r["sample_count"] for r in rs), "requests": sum(r["requests"] for r in rs),
        "failed": sum(r["failed_attempts"] for r in rs), "overruns": sum(r["overrun_count"] for r in rs),
        "fallback": sum(r["fallback_samples"] for r in rs), "http_errors": sum(r["http_errors"] for r in rs),
        "client_failures": sum(r["client_failures"] for r in rs),
        "actual_interval_ms_p50": mean([r["actual_interval_ms"]["p50"] for r in rs]),
        "actual_interval_ms_max": max((r["actual_interval_ms"]["max"] or 0 for r in rs), default=None),
    } for side, rs in (("off", [r for r in valid if r[axis] == "off"]),
                       ("on", [r for r in valid if r[axis] == "on"]))}
    return out


def summarize_probe(rec):
    p = rec["probe"]
    t = rows(p["table"])
    periodic = [r for r in t if r["step"] < 10]
    out = {"run_id": rec["run_id"], "alias": rec["alias"], "feature": p["feature"], "period_s": p["period_s"],
           "valid": rec["valid"], "invalid": rec.get("invalid"), "cpu_pct_one_core": p["cpu_pct_one_core"], "skipped_ticks": p["skipped_ticks"],
           "pending_at_end": p["pending_at_end"], "mounts": p["mounts"], "steps": {}}
    for step in sorted({r["step"] for r in t}):
        rs = [r for r in t if r["step"] == step]
        out["steps"][str(step)] = {"elapsed_ms": dist([r["elapsed_ns"] / 1e6 for r in rs]),
                                   "cpu_ms": dist([r["cpu_ns"] / 1e6 for r in rs]),
                                   "failures": sum(1 for r in rs if not r["ok"]),
                                   "with_value": sum(1 for r in rs if r["value_x1000"] != -10**12),
                                   "distinct_values": len({r["value_x1000"] for r in rs})}
    out["calls"] = len(periodic)
    return out


def window_samples(rec):
    """Per-collection values of one run's measured window, for pooling across runs."""
    s = rec["serve"]
    t0, t1 = s["start"]["mono"], s["end"]["mono"]
    col = rows(s["tables"]["collect"], t0, t1)
    return {"passive_ms": [r["passive_ns"] / 1e6 for r in col], "passive_cpu_ms": [r["passive_cpu_ns"] / 1e6 for r in col],
            "attempt_ms": [r["elapsed_ns"] / 1e6 for r in rows(s["tables"]["attempt"], t0, t1)]}


def pooled(recs):
    """Per variant: distributions over the samples of all valid runs of one condition taken together
    (pooled), next to the largest of the runs' own p95 (per_run_max_p95). The two are different numbers."""
    out = {}
    for variant in ("B0", "B1"):
        runs = [r for r in recs if r["valid"] and r["serve"]["variant"] == variant]
        per_run = [window_samples(r) for r in runs]
        out[variant] = {"runs": len(runs), **{
            k: {**dist([x for w in per_run for x in w[k]]),
                "per_run_max_p95": max((percentile(w[k], 95) for w in per_run if w[k]), default=None)}
            for k in ("passive_ms", "passive_cpu_ms", "attempt_ms")}}
    return out


def report(a):
    recs = []
    for path in a.results:
        with open(path) as f:
            recs += [json.loads(l) for l in f if l.strip()]
    with_serve = [r for r in recs if r["kind"] == "run" and "serve" in r]
    serves = [summarize_serve(r) for r in with_serve]
    groups, raw = {}, {}
    for s, r in zip(serves, with_serve):
        key = (s["alias"], s["phase"], s["interval_s"], s["clients"], s["poll_s"], s["instr"])
        groups.setdefault(key, []).append(s)
        raw.setdefault(key, []).append(r)
    out = {"manifests": [r for r in recs if r["kind"] == "manifest"],
           "skipped": [{"run_id": r["run_id"], "cond": r["cond"], "why": r.get("skipped") or r.get("invalid")}
                       for r in recs if r["kind"] == "run" and not r.get("valid")],
           "runs": serves,
           "comparisons": [{"key": list(k), **compare(v), "pooled": pooled(raw[k])} for k, v in groups.items()
                           if k[1] not in ("network-product", "disk-product")],
           "network_comparisons": [{"key": list(k), **network_compare(v, raw[k])} for k, v in groups.items()
                                   if k[1] == "network-product"],
           "disk_comparisons": [{"key": list(k), **network_compare(v, raw[k], "disk")} for k, v in groups.items()
                                if k[1] == "disk-product"],
           "probes": [summarize_probe(r) for r in recs if r["kind"] == "run" and "probe" in r]}
    if a.md:
        print(markdown(out))
    else:
        json.dump(out, sys.stdout, indent=1, default=str)
        print()


def markdown(out):
    """Tables 1-3 of the report from report()'s output. Empty values stay '-', never 0."""
    def f(v, nd=2):
        return "-" if v is None else f"{v:.{nd}f}"

    def runs_of(key):
        return [r for r in out["runs"] if [r["alias"], r["phase"], r["interval_s"], r["clients"], r["poll_s"], r["instr"]] == key
                and r["valid"]]

    def mean(v):
        v = [x for x in v if x is not None]
        return sum(v) / len(v) if v else None

    lines = ["| platform | phase | interval s | clients/poll s | B0 CPU % (runs) | B1 CPU % (runs) | B1-B0 pp | spread pp "
             "| B1 passive p95 ms: pooled elapsed/cpu (samples, runs) | B1 passive: max of per-run p95 ms | attempt p95 ms pooled B0/B1 | RSS end B1-B0 MiB | RSS 2nd-half growth KiB B0/B1 "
             "| failed/overrun | actual interval p50/max ms | problems |", "|" + "---|" * 16]
    for c in out["comparisons"]:
        rs = runs_of(c["key"])
        b0 = [r for r in rs if r["variant"] == "B0"]
        b1 = [r for r in rs if r["variant"] == "B1"]
        pb0, pb1 = c["pooled"]["B0"], c["pooled"]["B1"]
        rss = None if not b0 or not b1 else (mean([r["rss_end"] for r in b1]) - mean([r["rss_end"] for r in b0])) / 2**20
        lines.append("| " + " | ".join([
            c["key"][0], c["key"][1], f(c["key"][2], 1), f"{c['key'][3]}/{f(c['key'][4], 0)}",
            ", ".join(f(x) for x in c["b0"]) or "-", ", ".join(f(x) for x in c["b1"]) or "-",
            f(c.get("delta_pp"), 3), f(c.get("spread_pp"), 3) + ("" if c.get("distinguishable") else " (no repeats)" if c.get("spread_pp") is None
                                       else " (indistinguishable)"),
            f"{f(pb1['passive_ms']['p95'])}/{f(pb1['passive_cpu_ms']['p95'])} ({pb1['passive_ms']['n']}, {pb1['runs']})",
            f(pb1["passive_ms"]["per_run_max_p95"]),
            f"{f(pb0['attempt_ms']['p95'])}/{f(pb1['attempt_ms']['p95'])}",
            f(rss), f"{f(max((r['rss_second_half_kib'] for r in b0), default=None), 0)}/{f(max((r['rss_second_half_kib'] for r in b1), default=None), 0)}",
            f"{sum(r['failed_attempts'] for r in rs)}/{sum(r['overrun_count'] for r in rs)}",
            f"{f(mean([r['actual_interval_ms']['p50'] for r in rs]), 1)}/{f(max((r['actual_interval_ms']['max'] or 0) for r in rs), 1)}",
            "; ".join(c["problems"]) or "-"]) + " |")
    lines += ["", "| platform | phase | interval s | clients | poll s | variant | CPU % | server requests | handler p95 ms "
              "(elapsed/cpu) | client latency p50/p95 ms (n) | client failures | response KiB | distinct samples served |",
              "|" + "---|" * 13]
    for r in out["runs"]:
        lines.append("| " + " | ".join([
            r["alias"], r["phase"], f(r["interval_s"], 1), str(r["clients"]), f(r["poll_s"], 0), r["variant"],
            f(r["cpu_pct_one_core"]), str(r["requests"]),
            f"{f(r['handler_ms']['p95'])}/{f(r['handler_cpu_ms']['p95'])}",
            f"{f(r['client_latency_ms']['p50'])}/{f(r['client_latency_ms']['p95'])} ({r['client_latency_ms']['n']})",
            str(r["client_failures"]), f((r["response_bytes"]["mean"] or 0) / 1024 if r["response_bytes"]["n"] else None, 1),
            str(r["distinct_sequences_served"])]) + " |")
    lines += ["", "| platform | feature | period s | step | n | CPU % (process) | elapsed p50/p95 ms | cpu p50/p95 ms "
              "| failures | with value | skipped/pending | valid |", "|" + "---|" * 12]
    names = {"noop": ["noop"], "wireless": ["/proc/net/wireless"], "statvfs": ["mountinfo", "statvfs"]}
    for p in out["probes"]:
        for step, s in p["steps"].items():
            i = int(step)
            name = names[p["feature"]][i % 10] + (" (burst)" if i >= 10 else "")
            lines.append("| " + " | ".join([
                p["alias"], p["feature"], f(p["period_s"], 0), name, str(s["elapsed_ms"]["n"]),
                f(p["cpu_pct_one_core"], 3) if i < 10 else "-",
                f"{f(s['elapsed_ms']['p50'], 3)}/{f(s['elapsed_ms']['p95'], 3)}",
                f"{f(s['cpu_ms']['p50'], 3)}/{f(s['cpu_ms']['p95'], 3)}", str(s["failures"]), str(s["with_value"]),
                f"{p['skipped_ticks']}/{p['pending_at_end']}", "yes" if p["valid"] else f"no: {p['invalid']}"]) + " |")
    with_blocks = [c for c in out["comparisons"] if c.get("blocks")]
    if with_blocks:
        lines += ["", "| platform | phase | clients | block | B0 CPU % | B1 CPU % | B1-B0 pp | problems |", "|" + "---|" * 8]
        for c in with_blocks:
            for name, v in c["blocks"].items():
                lines.append(f"| {c['key'][0]} | {c['key'][1]} | {c['key'][3]} | {name} | {', '.join(f(x) for x in v['B0']) or '-'} "
                             f"| {', '.join(f(x) for x in v['B1']) or '-'} | {f(v['delta_pp'], 3)} | {'; '.join(v['problems']) or '-'} |")
    for label in ("network", "disk"):
        if not out.get(f"{label}_comparisons"):
            continue
        lines += ["", f"| platform | clients | {label} off CPU % (runs) | on CPU % (runs) | on-off pp | spread pp | blocks on-off pp "
                  f"| {label} call p95 ms pooled elapsed/cpu (n) | max of per-run p95 ms | calls when off "
                  "| attempt cpu mean ms off/on | handler cpu mean ms off/on | response KiB off/on | RSS end MiB off/on "
                  "| failed/overrun/fallback/http errors off;on | problems |", "|" + "---|" * 16]
        for c in out[f"{label}_comparisons"]:
            m, nm = c["arm_means"], c[f"{label}_ms"]
            spread = f(c.get("spread_pp"), 3) + ("" if c.get("distinguishable") else " (no repeats)" if c.get("spread_pp") is None
                                                else " (indistinguishable)")
            lines.append("| " + " | ".join([
                c["key"][0], str(c["key"][3]), ", ".join(f(x, 3) for x in c["b0"]) or "-",
                ", ".join(f(x, 3) for x in c["b1"]) or "-", f(c.get("delta_pp"), 3), spread,
                ", ".join(f"{b}: {f(v['delta_pp'], 3)}" for b, v in c["blocks"].items()) or "-",
                f"{f(nm['p95'], 3)}/{f(c[f'{label}_cpu_ms']['p95'], 3)} ({nm['n']})", f(nm["per_run_max_p95"], 3),
                str(c[f"{label}_calls_when_off"]),
                f"{f(m['off']['attempt_cpu_ms'], 3)}/{f(m['on']['attempt_cpu_ms'], 3)}",
                f"{f(m['off']['handler_cpu_ms'], 3)}/{f(m['on']['handler_cpu_ms'], 3)}",
                "/".join(f(m[k]["response_bytes"] and m[k]["response_bytes"] / 1024, 1) for k in ("off", "on")),
                "/".join(f(m[k]["rss_end_mib"]) for k in ("off", "on")),
                ";".join("/".join(str(m[k][x]) for x in ("failed", "overruns", "fallback", "http_errors")) for k in ("off", "on")),
                "; ".join(c["problems"]) or "-"]) + " |")
    with_parts = [r for r in out["runs"] if r.get("parts")]
    if with_parts:
        lines += ["", "| platform | run | CPU % (process) | part | calls | CPU % of one core (sum) | cpu mean ms "
                  "| elapsed p50/p95 ms | states ok/unsupported/error/other | successful reads |", "|" + "---|" * 10]
        for r in with_parts:
            for name, p in r["parts"].items():
                lines.append("| " + " | ".join([
                    r["alias"], r["run_id"], f(r["cpu_pct_one_core"]), name, str(p["calls"]), f(p["cpu_pct_one_core"], 4),
                    f(p["cpu_ms"]["mean"], 4), f"{f(p['elapsed_ms']['p50'], 4)}/{f(p['elapsed_ms']['p95'], 4)}",
                    "/".join(str(p["states"][k]) for k in ("ok", "unsupported", "error", "other")),
                    str(p["successful_reads"])]) + " |")
    if out["skipped"]:
        lines += ["", "Not run or invalid:"] + [f"- {s['run_id']}: {s['cond']} — {s['why']}" for s in out["skipped"]]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--variant", choices=("B0", "B1"), required=True)
    s.add_argument("--instrumentation", choices=INSTRUMENTATION, default="reference",
                   help="reference: the coarse timers only; stages: benchmarks/breakdown.py; profile-*: cProfile")
    s.add_argument("--product-network", choices=("off", "on"),
                   help="the product's own network collection (with variant B0 only); not given: as before it existed")
    s.add_argument("--product-disk", choices=("off", "on"),
                   help="the product's disk I/O collection (with --product-network only)")
    s.add_argument("--interval", type=float, required=True)
    s.add_argument("--warmup", type=float, default=30)
    s.add_argument("--measure", type=float, default=120)
    s.add_argument("--port", type=int, default=19798)
    s.add_argument("--expect-rps", type=float, default=1)
    s.add_argument("--run-id", default="manual")
    s.add_argument("--out", required=True)
    c = sub.add_parser("client")
    c.add_argument("--url", required=True)
    c.add_argument("--period", type=float, default=1.0)
    c.add_argument("--duration", type=float, required=True)
    c.add_argument("--out", required=True)
    pr = sub.add_parser("probe")
    pr.add_argument("--feature", choices=("noop", "wireless", "statvfs"), required=True)
    pr.add_argument("--period", type=float, required=True)
    pr.add_argument("--duration", type=float, default=120)
    pr.add_argument("--burst", type=int, default=0)
    pr.add_argument("--run-id", default="manual")
    pr.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--alias", required=True)
    r.add_argument("--phase", choices=("matrix", "clients", "final", "probes", "breakdown", "breakdown-lite", "ab",
                                       "passive-rebaseline", "passive-parts", "network-product", "disk-product"),
                   required=True)
    r.add_argument("--intervals", default="1,2,0.5,5")
    r.add_argument("--final", type=float)
    r.add_argument("--clients", default="1,0", help="passive-rebaseline: client counts, alternated per block")
    r.add_argument("--blocks", type=int, default=2, help="passive-rebaseline: A B B A blocks per client count")
    r.add_argument("--warmup", type=float, default=30)
    r.add_argument("--measure", type=float, help="override every window length (smoke tests only)")
    r.add_argument("--port", type=int, default=19798)
    r.add_argument("--ready-timeout", type=float, default=30, help="seconds to wait for a bench server to start")
    r.add_argument("--budget-min", type=float, default=90)
    r.add_argument("--max-windows", type=int, default=32)
    r.add_argument("--watch-url", help="a running service that must keep answering between runs")
    r.add_argument("--source-sha", default="unknown")
    rp = sub.add_parser("report")
    rp.add_argument("results", nargs="+")
    rp.add_argument("--md", action="store_true", help="Markdown tables instead of JSON")
    a = p.parse_args(argv)
    if a.cmd == "serve" and a.product_disk and not a.product_network:
        p.error("--product-disk needs --product-network: the comparison runs on a stated network setting")
    if a.cmd == "serve" and a.product_network and a.variant != "B0":
        p.error("--product-network runs with variant B0 only: never the product's network and the passive prototype together")
    {"serve": serve, "client": client, "probe": probe, "run": run, "report": report}[a.cmd](a)


if __name__ == "__main__":
    main()
