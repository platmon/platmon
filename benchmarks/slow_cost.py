#!/usr/bin/env python3
"""Cost of one low-frequency group's observations at a real cadence (bench tool, not product code).

Runs the product's Slow.observe_once for one group (--group storage or wifi) every --period seconds, --count times, in this
process, and records each observation's thread CPU and elapsed time, its C1 state and what it found, plus
the process CPU and RSS over the whole run. A 30 s group gives too few observations per 120 s window for
an off/on process comparison; this measures the observation itself.

  python3 benchmarks/slow_cost.py --group storage --period 30 --count 20 --out storage-30s.json
"""
import argparse
import json
import os
import platform
import resource
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from benchmarks.cadence import bench_hash, dist, source_hash, statm_rss  # noqa: E402
from runtime.sampler import pick_clock  # noqa: E402
from runtime.slow import Slow  # noqa: E402
from collector.storage import Storage  # noqa: E402
from collector.wifi import Wifi  # noqa: E402

GROUPS = {"storage": (Storage, ("filesystems", "partitions")), "wifi": (Wifi, ("interfaces",))}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--group", choices=GROUPS, default="storage")
    p.add_argument("--period", type=float, required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    make, counted = GROUPS[a.group]
    observe, samples = make(), []

    def timed(group):
        t0, c0 = time.monotonic_ns(), time.thread_time_ns()
        try:
            return observe(group)
        finally:
            samples.append({"elapsed_ns": time.monotonic_ns() - t0, "cpu_ns": time.thread_time_ns() - c0})
    clock = pick_clock()
    g = Slow(a.group, timed, a.period, clock[0])
    r0, w0, rss0 = resource.getrusage(resource.RUSAGE_SELF), time.monotonic(), statm_rss()
    nxt = time.monotonic()
    for _ in range(a.count):
        time.sleep(max(0.0, nxt - time.monotonic()))
        g.observe_once()
        v = g.view()
        samples[-1].update(state=v["collector"]["state"], **{k: len(v["data"][k]) for k in counted})
        nxt += a.period
    r1, w1 = resource.getrusage(resource.RUSAGE_SELF), time.monotonic()
    elapsed = w1 - w0
    cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
    out = {"group": a.group, "period_s": a.period, "count": a.count, "elapsed_s": elapsed, "process_cpu_s": cpu,
           "process_cpu_pct_one_core": 100 * cpu / elapsed, "rss_start": rss0, "rss_end": statm_rss(),
           "observe_cpu_ms": dist([s["cpu_ns"] / 1e6 for s in samples]),
           "observe_elapsed_ms": dist([s["elapsed_ns"] / 1e6 for s in samples]),
           "states": sorted({s["state"] for s in samples}), "found": {k: samples[-1][k] for k in counted},
           "samples": samples, "python": platform.python_version(), "source_hash": source_hash(), "bench_hash": bench_hash()}
    with open(a.out, "w") as f:
        json.dump(out, f)
    print(json.dumps({k: out[k] for k in ("group", "period_s", "count", "process_cpu_pct_one_core", "observe_cpu_ms",
                                          "observe_elapsed_ms", "states", "found")}))


if __name__ == "__main__":
    main()
