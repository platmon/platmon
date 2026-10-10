"""Neural processing units on any Linux host, through the accel class the kernel gives compute
accelerators (/sys/class/accel/accelN, DRM accel). As with the GPU, the class is standard and the
numbers are not, so one short reader per driver turns what it publishes into the same fields; an
accelerator whose driver has no reader here is still reported, with its driver name and no numbers.
Contract: docs/api.md.
"""
import re
import time

from . import sysfs
from .sysfs import driver_of, entries

ACCEL = re.compile(r"accel\d+")
BUSY = "device/npu_busy_time_us"               # intel_vpu: cumulative µs the NPU spent working
FREQ = "device/npu_current_frequency_mhz"      # 0 while the device is suspended
MAX_FREQ = "device/npu_max_frequency_mhz"
READERS = ("intel_vpu",)                       # drivers this module knows how to read, by their uevent DRIVER


class Npu:
    """sample(group) -> the "npu" entry, or None where the host has no accelerator. Kept between
    collections by the service: the busy time is a counter, so the share is measured from one collection
    to the next, as the CPU counters are. Reading these attributes does not resume a suspended NPU
    (measured on Meteor Lake: it stays in D3hot through repeated reads), so the cost is three small
    sysfs reads."""

    def __init__(self, clock=time.monotonic_ns, root="/sys", max_gap=5.0):
        self.clock, self._root, self._max_gap = clock, root, round(max_gap * 1e9)
        self._busy = {}  # device path -> (elapsed ns of the reading, cumulative busy µs)

    def sample(self, group):
        accel = f"{self._root}/class/accel"
        devices = [n for n in entries(accel, group) if ACCEL.fullmatch(n)]
        if not devices:
            group.note("npu", "not_detected")
            return None
        drivers = [(name, driver_of(f"{accel}/{name}", group)) for name in devices]
        # The first accelerator this module can read; with none, the first one is still named.
        name, driver = next(((n, d) for n, d in drivers if d in READERS), drivers[0])
        if driver in READERS:
            return self._intel(f"{accel}/{name}", driver, group)
        group.note(driver or name, "not_exposed")
        return {"usage": None, "freq": None, "max_freq": None, "driver": driver, "provider": None}

    def _intel(self, device, driver, group):
        """intel_vpu: the share of the window the NPU was busy, with its clock. A firmware that keeps the
        counter at zero reports an idle NPU, which is what an unused one is."""
        busy = group.read(f"{device}/{BUSY}", "npu_busy_time", found=device)
        span, usage = group.span, None
        if busy is not None:
            now = self.clock()
            before = self._busy.get(device)
            self._busy[device] = (now, busy)
            if before is not None:
                elapsed, worked = now - before[0], (busy - before[1]) * 10**3  # µs -> ns
                if 0 < elapsed <= self._max_gap and worked >= 0:
                    usage = round(100 * max(0.0, min(1.0, worked / elapsed)), 1)
        group.sensor("usage", self._source("percent", f"{device}/{BUSY}", group), span)
        return {"usage": None if usage is None else group.got(usage),
                "freq": self._clock(device, FREQ, "freq", group),
                "max_freq": self._clock(device, MAX_FREQ, "max_freq", group),
                "driver": driver, "provider": "npu_busy_time"}

    def _clock(self, device, attr, key, group):
        """A clock attribute in MHz as Hz, with the record of its read."""
        mhz = group.read(f"{device}/{attr}", attr.rsplit("/", 1)[-1], found=device)
        group.sensor(key, self._source("hertz", f"{device}/{attr}", group), group.span)
        return None if mhz is None else group.got(mhz * 10**6)

    def _source(self, unit, path, group):
        """Where a value came from: the path inside /sys once resolved, as the GPU readers record it."""
        if group.trace is None:
            return None
        rel = sysfs.canonical(path, self._root)
        return sysfs.source("accel", unit, "resolved_path" if rel else "unresolved", path=rel)
