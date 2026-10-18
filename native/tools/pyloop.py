"""CPU of the Python collector over the scope platmon-native covers: collect_recorded every 1 s with CPU,
GPU and NPU counters kept between collections, and no network, disk_io or pressure (the same wiring as
compare.py). Prints its own user/sys CPU and peak RSS as JSON, like `platmon-native loop`.

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


def main():
    warmup, seconds = float(sys.argv[1]), float(sys.argv[2])
    clock = pick_clock()[0]
    cpu, gpu, npu, logged = CpuCounters(clock, max_gap=5.0), Gpu(clock, max_gap=5.0), Npu(clock, max_gap=5.0), {}

    def collect():
        return collect_recorded(cpu, logged, clock, None, None, None, gpu, npu)

    def run(duration):
        start = deadline = time.monotonic()
        n = failed = 0
        while time.monotonic() - start < duration:
            try:
                collect()
            except Exception:
                failed += 1
            n += 1
            deadline += 1.0
            time.sleep(max(0.0, deadline - time.monotonic()))
        return n, failed
    run(warmup)
    r0, t0 = resource.getrusage(resource.RUSAGE_SELF), time.monotonic()
    n, failed = run(seconds)
    r1, wall = resource.getrusage(resource.RUSAGE_SELF), time.monotonic() - t0
    print(json.dumps({"variant": "python-collector", "collections": n, "failed": failed, "wall_s": round(wall, 3),
                      "user_pct": round(100 * (r1.ru_utime - r0.ru_utime) / wall, 4),
                      "sys_pct": round(100 * (r1.ru_stime - r0.ru_stime) / wall, 4), "maxrss_kib": r1.ru_maxrss}))


if __name__ == "__main__":
    main()
