"""The Python side of fixture.py: `platmon-native script` for the Python collector. For each stdin line
"BASE_NS WALL_US", one collect_recorded on those clocks, printed as one JSON line {"stats", "sensors",
"required"} or {"error": ...}. The elapsed clock returns BASE_NS, BASE_NS + 1000, ... call by call and
time.time() returns WALL_US / 1e6, as platmon-native's set clocks do (metadata::set_clocks).

  PYTHONPATH=<platmon tree> python3 fixture_py.py
Read-only."""
import json
import sys
import time

FAKE_TICK = 1000
state = {"base": 0, "calls": 0, "wall": 0.0}


def clock():
    state["calls"] += 1
    return state["base"] + FAKE_TICK * (state["calls"] - 1)


time.time = lambda: state["wall"]  # stats["time"]; only this process, which runs nothing else

from collector import collect_recorded  # noqa: E402  (after the patch; the import reads the tree)
from collector.common import CpuCounters  # noqa: E402
from collector.gpu import Gpu  # noqa: E402
from collector.npu import Npu  # noqa: E402


def main():
    cpu, gpu, npu = CpuCounters(clock, max_gap=5.0), Gpu(clock, max_gap=5.0), Npu(clock, max_gap=5.0)
    logged = {}
    for line in sys.stdin:
        base, wall = (int(x) for x in line.split())
        state.update(base=base, calls=0, wall=wall / 1e6)
        try:
            col = collect_recorded(cpu, logged, clock, None, None, None, gpu, npu)
            out = {"stats": col.stats, "sensors": col.sensors, "required": col.required}
        except Exception as e:  # noqa: BLE001  the harness compares only that both failed
            out = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
