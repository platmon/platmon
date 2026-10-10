"""platmon core: collect() returns one dict of board status. No HTTP or output code.

common.collect() fills the fields every Linux host has; the detected board's module,
if any, fills its own optional fields through extend(stats). Viewers never branch on the board.
"""
import os

from . import common, jetson, sysfs
from . import disk_io as dio
from . import network as net
from .gpu import Gpu
from .sampler import REQUIRED_READS, Collected, sensor_values
from .sysfs import DEVICE_TREE, read

BOARDS = (  # (device-tree compatible match, display name, board module or None)
    ("nvidia,tegra234", "Jetson Orin", jetson),
    ("nvidia,tegra194", "Jetson Xavier", jetson),
    ("nvidia,tegra", "Jetson", jetson),
    ("raspberrypi", "Raspberry Pi", None),
)


def detect(compatible, has_dmi):
    """compatible: <device tree>/compatible (NUL-separated), empty on x86."""
    for key, name, module in BOARDS:
        if key in compatible:
            return name, module
    return ("PC" if has_dmi else "Linux"), None


PLATFORM, BOARD = detect(read(f"{DEVICE_TREE}/compatible", ""), os.path.isdir("/sys/class/dmi/id"))


def collect(cpu=None, logged=None, network=None, disk_io=None, pressure=None, gpu=None):
    """cpu: a common.CpuCounters kept between calls (the service); None for a one-off reading.
    logged: a dict the service keeps between calls, so lasting read problems are logged once (sysfs.summarize).
    network: a network.NetworkCounters kept between calls; None leaves out "network" and its collector.
    disk_io: a disk_io.DiskCounters, the same way for "disk_io"; pressure: a pressure.Pressure for "pressure".
    gpu: a gpu.Gpu kept between calls; a one-off reading makes its own, which has no earlier reading to
    measure a load against (the clocks are still read).
    stats["collectors"] has the optional groups' state; the Sampler adds "core"."""
    return _collect(cpu, logged, None, network, disk_io, pressure, gpu)[0]


def collect_recorded(cpu, logged, clock, network=None, disk_io=None, pressure=None, gpu=None):
    """The service's collect(): the same stats, plus for the Sampler where each sensor value came from and
    when it and the required data were read (all on clock, the Sampler's and cpu's elapsed clock).
    network, disk_io, pressure, gpu: as for collect(); their reads are recorded only if they read on the same clock."""
    trace = sysfs.Trace(clock)
    stats, groups = _collect(cpu, logged, trace, network, disk_io, pressure, gpu)
    return Collected(stats, sensor_entries(stats, groups), {name: trace.required.get(name) for name in REQUIRED_READS},
                     clock, trace.network, trace.disk_io, trace.pressure)


def sensor_entries(stats, groups):
    """{JSON Pointer: metadata} for the sensor values in the final stats (sampler.sensor_values). Pointers
    come from the output itself, so they follow label fallbacks, duplicate names and the fan list as they
    ended up; a value left out gets no pointer, a null one no read. The Sampler checks them."""
    out = {}
    for ptr, kind, key, value in sensor_values(stats):
        if kind == "fans":  # the fan's records are kept by the fan object, its position is only known now
            i, field = key
            info = groups["fans"].sensors.get((id(stats["fans"][i]), field))
        else:
            info = groups["cpu_frequency" if kind == "cpu" else kind].sensors.get(key)
        if info is not None:
            out[ptr] = info if value is not None else {**info, "span": None}
    return out


def _collect(cpu, logged, trace, network=None, disk_io=None, pressure=None, gpu=None):
    groups = sysfs.groups(trace=trace)
    stats = {"platform": PLATFORM, **common.collect(cpu, groups, trace)}
    if BOARD:
        BOARD.extend(stats, groups)
    else:  # no board module: nothing on this platform provides these
        for name in ("power_mode", "board_info"):
            groups[name].note(name, "unsupported_platform")
    if stats.get("gpu") is None:  # the standard DRM class: every host, and where a board source gave nothing
        g = groups["gpu"]
        with sysfs.guard(g):
            stats["gpu"] = (gpu if gpu is not None else Gpu()).sample(g)
    if network is not None:  # after the required data: a failed collection never reaches it
        g = groups["network"] = sysfs.Group("network", trace)
        stats["network"], span = net.unavailable(), None
        with sysfs.guard(g):
            stats["network"], span = network.sample(g)
        if trace and network.clock is trace.clock:
            trace.network = span
    if disk_io is not None:  # likewise
        g = groups["disk_io"] = sysfs.Group("disk_io", trace)
        stats["disk_io"], span = dio.unavailable(), None
        with sysfs.guard(g):
            stats["disk_io"], span = disk_io.sample(g)
        if trace and disk_io.clock is trace.clock:
            trace.disk_io = span
    if pressure is not None:  # likewise
        g = groups["pressure"] = sysfs.Group("pressure", trace)
        stats["pressure"], span = {"scope": {"kind": "host"}, "provider": "proc_pressure", "resources": {}, "read": None}, None
        with sysfs.guard(g):
            stats["pressure"], span = pressure.sample(g)
        if trace and pressure.clock is trace.clock:
            trace.pressure = span
    stats["collectors"] = sysfs.summarize(groups, logged)
    return stats, groups
