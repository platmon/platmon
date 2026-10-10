"""Graphics processors on any Linux host, through the DRM class every driver registers
(/sys/class/drm/cardN). What a card publishes depends on its driver, so one short reader per driver turns
its attributes into the same fields; a card whose driver has no reader here is still reported, with its
driver name and no numbers. Tegra's GPU is not read here: it has no DRM load attribute and keeps its own
source in collector/jetson.py, which is used first where it answers. Contract: docs/api.md.
"""
import re
import time

from . import sysfs
from .sysfs import driver_of, entries

CARD = re.compile(r"card\d+")
RC6 = "gt/gt0/rc6_residency_ms"   # i915: cumulative ms the GPU spent in its RC6 idle state
BUSY = "device/gpu_busy_percent"  # amdgpu: the busy share of the moment, already a percentage
READERS = ("i915", "amdgpu")      # drivers this module knows how to read, by their uevent DRIVER


class Gpu:
    """sample(group) -> the "gpu" entry, or None where the host has no card. Kept between collections by
    the service: i915 publishes idle time, not load, so the share is measured from one collection to the
    next, as the CPU counters are, and a longer break than max_gap is not averaged over. Reading these
    attributes does not disturb the GPU (measured on Meteor Lake: the same RC6 share at 0, 1 and 100
    reads per second)."""

    def __init__(self, clock=time.monotonic_ns, root="/sys", max_gap=5.0):
        self.clock, self._root, self._max_gap = clock, root, round(max_gap * 1e9)
        self._idle = {}  # card path -> (elapsed ns of the reading, cumulative idle ms)

    def sample(self, group):
        drm = f"{self._root}/class/drm"
        cards = [n for n in entries(drm, group) if CARD.fullmatch(n)]  # connectors (card1-DP-1) are not cards
        if not cards:
            group.note("gpu", "not_detected")
            return None
        drivers = [(name, driver_of(f"{drm}/{name}", group)) for name in cards]
        # The first card this module can read; with none, the first card is still named, so a host with a
        # GPU platmon cannot measure says so instead of looking like a host without one.
        name, driver = next(((n, d) for n, d in drivers if d in READERS), drivers[0])
        card = f"{drm}/{name}"
        if driver == "i915":
            return self._intel(card, driver, group)
        if driver == "amdgpu":
            return self._amd(card, driver, group)
        group.note(driver or name, "not_exposed")
        return {"usage": None, "freq": None, "max_freq": None, "driver": driver, "provider": None}

    def _intel(self, card, driver, group):
        """i915: the share of the window the GPU was not in RC6, with its actual clock and the ceiling.
        RC6 is idle time, so the first collection has no share to report, like a CPU warming up, and a
        counter that went backwards (a reset) starts again instead of reporting a jump."""
        idle = group.read(f"{card}/{RC6}", "rc6_residency", found=card)
        span, usage = group.span, None
        if idle is not None:
            now = self.clock()
            before = self._idle.get(card)
            self._idle[card] = (now, idle)
            if before is not None:
                elapsed, idled = now - before[0], (idle - before[1]) * 10**6  # ms -> ns
                if 0 < elapsed <= self._max_gap and idled >= 0:
                    usage = round(100 * max(0.0, min(1.0, 1 - idled / elapsed)), 1)  # a clock gated off is 100 % idle
        group.sensor("usage", self._source("percent", f"{card}/{RC6}", group), span)
        return {"usage": None if usage is None else group.got(usage),
                "freq": self._clock(card, "gt_act_freq_mhz", "freq", group),
                "max_freq": self._clock(card, "gt_max_freq_mhz", "max_freq", group),
                "driver": driver, "provider": "rc6_residency"}

    def _amd(self, card, driver, group):
        """amdgpu publishes the busy share itself (percent, in the kernel's sysfs ABI). No clock: it takes
        parsing the pp_dpm_sclk table, which this has no hardware to check against."""
        busy = group.read(f"{card}/{BUSY}", "gpu_busy_percent", found=card)
        group.sensor("usage", self._source("percent", f"{card}/{BUSY}", group), group.span)
        return {"usage": None if busy is None else group.got(float(busy)), "freq": None, "max_freq": None,
                "driver": driver, "provider": "gpu_busy_percent"}

    def _clock(self, card, attr, key, group):
        """A clock attribute in MHz as Hz, with the record of its read."""
        mhz = group.read(f"{card}/{attr}", attr, found=card)
        group.sensor(key, self._source("hertz", f"{card}/{attr}", group), group.span)
        return None if mhz is None else group.got(mhz * 10**6)

    def _source(self, unit, path, group):
        """Where a value came from: the path inside /sys once resolved, as the Jetson GPU does, because
        these attributes sit at driver-specific paths and a card can be renumbered."""
        if group.trace is None:
            return None
        rel = sysfs.canonical(path, self._root)
        return sysfs.source("drm", unit, "resolved_path" if rel else "unresolved", path=rel)
