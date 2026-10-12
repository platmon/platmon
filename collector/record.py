"""Recorded collection values and read spans shared with the runtime."""
import dataclasses

from .metadata import pointer

REQUIRED_READS = ("cpu", "memory", "disk", "uptime")  # the reads every recorded collection must time


@dataclasses.dataclass
class Collected:
    """What a recording collect() returns instead of a plain dict (collector.collect_recorded): the stats
    and, for the Sampler to check, {JSON Pointer: sensor metadata with "span"} and the (start, end) of each
    of REQUIRED_READS by name (None if not timed), all on clock. Spans are elapsed ns; only the Sampler turns
    them into offsets. Which values need a record comes from the stats (sensor_values), not from here.
    network, disk_io: the (start, end) of the one read all of stats["network"] / stats["disk_io"] comes
    from, or None."""
    stats: dict
    sensors: dict
    required: dict
    clock: object
    network: object = None
    disk_io: object = None
    pressure: object = None


PASSIVE = (("network", "interfaces"), ("disk_io", "disks"), ("pressure", "resources"))  # stats key, its list of values: one read each


def sensor_values(stats):
    """(JSON Pointer, kind, key, value) of every value of recorded stats that needs a read record:
    temperature and power values, fan rpm/percent, CPU freq, GPU and NPU usage/freq/max_freq. The collector attaches
    its records by these, and the Sampler checks that none is missing, so both use the same list."""
    for table in ("temperature", "power"):
        values = stats.get(table)
        for key, value in (values.items() if isinstance(values, dict) else ()):
            yield pointer(table, key), table, key, value
    fans = stats.get("fans")
    for i, fan in enumerate(fans if isinstance(fans, list) else ()):
        for field in ("rpm", "percent"):
            if isinstance(fan, dict) and field in fan:
                yield pointer("fans", i, field), "fans", (i, field), fan[field]
    cpus = stats.get("cpu")
    for i, c in enumerate(cpus if isinstance(cpus, list) else ()):  # i is the row, c["id"] the CPU
        if isinstance(c, dict) and "freq" in c:
            yield pointer("cpu", i, "freq"), "cpu", c.get("id"), c["freq"]
    for kind in ("gpu", "npu"):  # one accelerator each, with the same three values
        values = stats.get(kind)
        for field in ("usage", "freq", "max_freq"):
            if isinstance(values, dict) and field in values:
                yield pointer(kind, field), kind, field, values[field]
