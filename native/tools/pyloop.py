"""CPU of the Python collector over the scope platmon-native covers: collect_recorded every 1 s with CPU,
GPU and NPU counters kept between collections, and no network, disk_io or pressure (the same wiring as
compare.py). Prints its own user/sys CPU, RSS, collection times, missed deadlines and group states as JSON,
like `platmon-native loop`.

  PYTHONPATH=<platmon tree> python3 pyloop.py WARMUP_SECONDS SECONDS
Read-only."""
import json
import resource
import sys
import time

from collector import collect_recorded
from collector.common import CpuCounters
from collector.gpu import Gpu
from collector.npu import Npu
from runtime.sampler import pick_clock


def rss_kib():
    """(VmRSS, VmHWM) in KiB."""
    fields = {}
    with open("/proc/self/status") as f:
        for line in f:
            k, _, v = line.partition(":")
            fields[k] = v.split()[0] if v.split() else "-1"
    return int(fields.get("VmRSS", -1)), int(fields.get("VmHWM", -1))


def pick(values, q):
    """The same quantile as platmon-native loop: the sorted value at round((n - 1) * q)."""
    v = sorted(values)
    return round(v[round((len(v) - 1) * q)], 3) if v else None


def main():
    warmup, seconds = float(sys.argv[1]), float(sys.argv[2])
    clock = pick_clock()[0]
    cpu, gpu, npu, logged = CpuCounters(clock, max_gap=5.0), Gpu(clock, max_gap=5.0), Npu(clock, max_gap=5.0), {}

    def collect():
        return collect_recorded(cpu, logged, clock, None, None, None, gpu, npu)

    elapsed, rss, states, sources, missed = [], [], {}, [0], [0]

    def run(duration, record):
        start = deadline = time.monotonic()
        n = failed = 0
        while time.monotonic() - start < duration:
            t = time.monotonic()
            try:
                col = collect()
            except Exception:
                failed += 1
            else:
                if record:
                    elapsed.append((time.monotonic() - t) * 1e3)
                    for name, status in col.stats["collectors"].items():
                        counts = states.setdefault(name, {})
                        counts[status["state"]] = counts.get(status["state"], 0) + 1
                    sources[0] = sum(m.get("id") is not None for m in col.sensors.values())
            if record and n % 30 == 0:
                rss.append(rss_kib()[0])
            n += 1
            deadline += 1.0
            wait = deadline - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            elif record:
                missed[0] += 1
        return n, failed
    run(warmup, False)
    r0, t0 = resource.getrusage(resource.RUSAGE_SELF), time.monotonic()
    n, failed = run(seconds, True)
    r1, wall = resource.getrusage(resource.RUSAGE_SELF), time.monotonic() - t0
    rss.append(rss_kib()[0])
    print(json.dumps({"variant": "python-collector", "collections": n, "failed": failed, "wall_s": round(wall, 3),
                      "user_pct": round(100 * (r1.ru_utime - r0.ru_utime) / wall, 4),
                      "sys_pct": round(100 * (r1.ru_stime - r0.ru_stime) / wall, 4), "maxrss_kib": rss_kib()[1],
                      "elapsed_ms": {"p50": pick(elapsed, 0.5), "p95": pick(elapsed, 0.95),
                                     "max": round(max(elapsed), 3) if elapsed else None},
                      "missed_deadlines": missed[0], "rss_kib": rss, "groups": states, "source_ids": sources[0]}))

if __name__ == "__main__":
    main()
