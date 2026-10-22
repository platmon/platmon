"""CPU of the Python collector over the scope platmon-native covers: collect_recorded every 1 s with CPU,
GPU, NPU, network and disk counters kept between collections and pressure on (the service's default wiring,
as compare.py). Prints its own user/sys CPU, RSS, collection times, missed deadlines, group states and what
was read (source ids, interfaces, disks) as JSON, like `platmon-native loop`.

  PYTHONPATH=<platmon tree> python3 pyloop.py WARMUP_SECONDS SECONDS
Read-only."""
import hashlib
import json
import resource
import sys
import time

from collector import collect_recorded
from collector.common import CpuCounters
from collector.disk_io import DiskCounters
from collector.gpu import Gpu
from collector.network import NetworkCounters
from collector.npu import Npu
from collector.pressure import Pressure
from runtime.sampler import pick_clock


def rss_kib():
    """(VmRSS, VmHWM) in KiB."""
    fields = {}
    with open("/proc/self/status") as f:
        for line in f:
            k, _, v = line.partition(":")
            fields[k] = v.split()[0] if v.split() else "-1"
    return int(fields.get("VmRSS", -1)), int(fields.get("VmHWM", -1))


def read_scope(col):
    """What one collection read, comparable across both programs: the SHA-256 (16 hex digits) of its sorted
    source ids, and the names of its interfaces, disks and pressure resources. The loop reports how many
    different ones it saw and the last."""
    ids = sorted(m["id"] for m in col.sensors.values() if m.get("id") is not None)
    s = col.stats
    return {"source_ids": hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16],
            "interfaces": [i["name"] for i in s["network"]["interfaces"]], "disks": [d["name"] for d in s["disk_io"]["disks"]],
            "pressure": list(s["pressure"]["resources"])}


def pick(values, q):
    """The same quantile as platmon-native loop: the sorted value at round((n - 1) * q)."""
    v = sorted(values)
    return round(v[round((len(v) - 1) * q)], 3) if v else None


def main():
    warmup, seconds = float(sys.argv[1]), float(sys.argv[2])
    clock = pick_clock()[0]
    cpu, gpu, npu, logged = CpuCounters(clock, max_gap=5.0), Gpu(clock, max_gap=5.0), Npu(clock, max_gap=5.0), {}
    net, dio, psi = NetworkCounters(clock, max_gap=5.0), DiskCounters(clock, max_gap=5.0), Pressure(clock)

    def collect():
        return collect_recorded(cpu, logged, clock, net, dio, psi, gpu, npu)

    elapsed, rss, states, sources, missed, scopes = [], [], {}, [0], [0], []

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
                    scope = read_scope(col)
                    if scope not in scopes:
                        scopes.append(scope)
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
                      "missed_deadlines": missed[0], "rss_kib": rss, "groups": states, "source_ids": sources[0],
                      "read_scopes": len(scopes), "read_scope": scopes[-1] if scopes else None}))

if __name__ == "__main__":
    main()
