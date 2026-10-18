"""Live smoke test: the Python collector and platmon-native on a real host, run at the same time. It is not
an equivalence test of the numbers: two processes do not read the same values, so readings that move are
only checked to be plausible. Exact numeric equivalence is tools/fixture.py's job (fake trees, set clocks).

  PYTHONPATH=<platmon tree> python3 compare.py PATH/TO/platmon-native [--json OUT]

Both take a CPU baseline, wait 1 s and record one collection without network, disk_io and pressure (what
platmon-native covers).
- Exact: structure, every JSON type, identity fields, totals, names and order of temperatures/power/fans,
  collectors (states, reasons, issues), sensor source ids, which values have a read span and which
  required reads were timed.
- Plausible only (PLAUSIBLE below): readings that change between two reads moments apart.
- Spans: start <= end on both sides. Both processes must use the same elapsed clock (CLOCK_BOOTTIME where
  there is one, as sampler.pick_clock), so their spans of the same read lie within SPAN_SKEW_NS.
Exit 1 on any difference.
Read-only."""
import json
import subprocess
import sys
import time

from collector import collect_recorded
from collector.common import CpuCounters
from collector.gpu import Gpu
from collector.npu import Npu
from runtime.sampler import pick_clock


SPAN_SKEW_NS = 5 * 10**9  # both collections run within a few seconds of each other


def python_side():
    clock, about = pick_clock()
    collect = lambda c=[CpuCounters(clock, max_gap=5.0), Gpu(clock, max_gap=5.0), Npu(clock, max_gap=5.0)]: \
        collect_recorded(c[0], {}, clock, None, None, None, c[1], c[2])
    collect()
    time.sleep(1)
    col = collect()
    out = {"stats": col.stats, "sensors": col.sensors, "required": col.required, "clock": about["source"]}
    return json.loads(json.dumps(out))  # spans as lists, as platmon-native prints them


# pointer prefix -> plausibility check for leaf values that may differ between the two collections.
# Not equivalence: e.g. two GPU clocks pass whatever they are, as long as neither is negative.
PLAUSIBLE = {
    "/stats/time": lambda a, b: abs(a - b) < 30,
    "/stats/uptime": lambda a, b: abs(a - b) < 30,
    "/stats/memory/used": lambda a, b: abs(a - b) <= max(0.1 * a, 64 << 20),
    "/stats/memory/swap_used": lambda a, b: abs(a - b) <= max(0.1 * a, 64 << 20),
    "/stats/disk/used": lambda a, b: abs(a - b) <= max(0.01 * a, 256 << 20),
    "/stats/temperature/": lambda a, b: abs(a - b) <= 10,  # a busy core moves several °C in a moment
    "/stats/power/": lambda a, b: abs(a - b) <= max(0.5 * abs(a), 0.5),
    "/stats/cpu_sampling/window_ms": lambda a, b: 900 <= a <= 1500 and 900 <= b <= 1500,
    "/stats/gpu/usage": lambda a, b: 0 <= a <= 100 and 0 <= b <= 100,
    "/stats/npu/usage": lambda a, b: 0 <= a <= 100 and 0 <= b <= 100,
    "/stats/gpu/freq": lambda a, b: a >= 0 and b >= 0,  # i915 reports 0 MHz while the GPU sits in RC6
    "/stats/npu/freq": lambda a, b: a >= 0 and b >= 0,
}


def plausible(ptr, a, b):
    if ptr.startswith("/stats/cpu/") and ptr.endswith(("/usage", "/freq")):
        return 0 <= a and 0 <= b and (ptr.endswith("/freq") or (a <= 100 and b <= 100))
    if ptr.startswith("/stats/fans/") and ptr.endswith("/rpm"):
        return abs(a - b) <= max(0.3 * a, 300)
    if ptr.startswith("/stats/fans/") and ptr.endswith("/percent"):
        return abs(a - b) <= 10
    for prefix, ok in PLAUSIBLE.items():
        if ptr == prefix or (prefix.endswith("/") and ptr.startswith(prefix)):
            return ok(a, b)
    return None


def kind(v):
    return "null" if v is None else type(v).__name__


def is_span(ptr):
    parts = ptr.split("/")
    return (ptr.startswith("/sensors/") and parts[-1] == "span") or (ptr.startswith("/required/") and len(parts) == 3)


def check_span(a, b, ptr, out):
    """Both null, or both [start, end] with start <= end and within SPAN_SKEW_NS of the other side."""
    if (a is None) != (b is None):
        out.append(f"{ptr}: span {a!r} (python) != {b!r} (rust)")
        return
    if a is None:
        return
    for side, s in (("python", a), ("rust", b)):
        if not (isinstance(s, list) and len(s) == 2 and all(type(x) is int for x in s) and s[0] <= s[1]):
            out.append(f"{ptr}: {side} span {s!r} is not [start, end]")
            return
    if abs(a[0] - b[0]) > SPAN_SKEW_NS:
        out.append(f"{ptr}: spans {(b[0] - a[0]) / 1e9:+.3f} s apart: not the same clock")


def diff(a, b, ptr, out):
    if is_span(ptr):
        check_span(a, b, ptr, out)
        return
    if kind(a) != kind(b):
        # a reading may be missing in one collection only where the other side also allows null
        out.append(f"{ptr}: type {kind(a)} (python) != {kind(b)} (rust): {a!r} / {b!r}")
        return
    if isinstance(a, dict):
        if list(a) != list(b):
            out.append(f"{ptr}: keys {list(a)} (python) != {list(b)} (rust)")
        for k in a:
            if k in b:
                diff(a[k], b[k], f"{ptr}/{k}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{ptr}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{ptr}/{i}", out)
    elif a != b:
        ok = plausible(ptr, a, b) if isinstance(a, (int, float)) and not isinstance(a, bool) else None
        if not ok:
            out.append(f"{ptr}: {a!r} (python) != {b!r} (rust)")


def main():
    binary = sys.argv[1]
    rust = subprocess.Popen([binary, "collect"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    py = python_side()  # at the same time, so both read the same moment as far as possible
    stdout, _ = rust.communicate(timeout=30)
    if rust.returncode:
        sys.exit(f"platmon-native failed with exit code {rust.returncode}")
    rs = json.loads(stdout)
    out = []
    diff(py, rs, "", out)
    report = {"host_platform": py["stats"]["platform"], "sensors": len(py["sensors"]),
              "temperatures": len(py["stats"]["temperature"]), "power": len(py["stats"]["power"]),
              "fans": len(py["stats"]["fans"]), "cpus": len(py["stats"]["cpu"]),
              "spans": sum(m["span"] is not None for m in py["sensors"].values()), "clock": py["clock"], "differences": out}
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as f:
            json.dump({"report": report, "python": py, "rust": rs}, f, indent=1)
    print(json.dumps(report, indent=1))
    sys.exit(1 if out else 0)


if __name__ == "__main__":
    main()
