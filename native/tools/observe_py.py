"""The Python side of fixture.py's slow-group scenario: `platmon-native observe-script` for the Python
readers. For each stdin line, one observation of storage, wifi, battery and probe (with the [probe]
targets and timeout given as arguments), each as runtime/slow.py makes it (its own metadata.Group, an
exception being its internal_error), printed as one JSON line {"observations": {group: {"collector", "data"}}}.

  PYTHONPATH=<platmon tree> python3 observe_py.py TARGETS TIMEOUT
Read-only, apart from the probe's TCP connections."""
import json
import sys

from collector import metadata
from collector.battery import Battery
from collector.probe import Probe, parse_targets
from collector.storage import Storage
from collector.wifi import Wifi


def main():
    groups = {"storage": Storage(), "wifi": Wifi(), "battery": Battery(),
              "probe": Probe(parse_targets(sys.argv[1]), float(sys.argv[2]))}
    logged = {}
    for _ in sys.stdin:
        out = {}
        for name, observe in groups.items():
            group, data = metadata.Group(name), None
            with metadata.guard(group):
                data = observe(group)
            out[name] = {"collector": metadata.summarize({name: group}, logged)[name], "data": data}
        print(json.dumps({"observations": out}), flush=True)


if __name__ == "__main__":
    main()
