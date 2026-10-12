"""Graphics processors through the DRM class and NVIDIA's optional nvidia-smi command.
(/sys/class/drm/cardN). What a card publishes depends on its driver, so one short reader per driver turns
its attributes into the same fields; NVIDIA is queried when no supported DRM reader is available.
Other cards are still reported with their driver name and no numbers. Tegra's GPU keeps its own
source in collector/jetson.py, which is used first where it answers. Contract: docs/api.md.
"""
import csv
import math
import os
import re
import shutil
import subprocess
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

    def __init__(self, clock=time.monotonic_ns, root="/sys", max_gap=5.0, nvidia_smi=None):
        self.clock, self._root, self._max_gap = clock, root, round(max_gap * 1e9)
        self._idle = {}  # card path -> (elapsed ns of the reading, cumulative idle ms)
        # Discover once, including WSL's location (not in a systemd service's PATH).
        # Fake sysfs trees never consult the host unless a command is supplied explicitly.
        self._nvidia_smi = nvidia_smi
        if nvidia_smi is None and root == "/sys":
            self._nvidia_smi = shutil.which("nvidia-smi")
            wsl = "/usr/lib/wsl/lib/nvidia-smi"
            if self._nvidia_smi is None and os.path.isfile(wsl) and os.access(wsl, os.X_OK):
                self._nvidia_smi = wsl

    def sample(self, group):
        drm = f"{self._root}/class/drm"
        cards = [n for n in entries(drm, group) if CARD.fullmatch(n)]  # connectors (card1-DP-1) are not cards
        if not cards:
            if self._nvidia_smi:
                return self._nvidia(group)
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
        if self._nvidia_smi:
            nvidia = self._nvidia(group)
            if nvidia is not None:
                return nvidia
        group.note(driver or name, "not_exposed")
        return {"usage": None, "freq": None, "max_freq": None, "driver": driver, "provider": None}

    def _nvidia(self, group):
        """One bounded, read-only query; UUID keeps history stable when GPU indices change.
        NVIDIA is a fallback for hosts without readable DRM metrics, including WSL2.
        Unavailable fields stay null, and a failed query never reuses an old value."""
        fields = ("utilization.gpu", "clocks.current.graphics", "clocks.max.graphics")
        clock = group.trace.clock if group.trace else None
        start = clock() if clock else None
        try:
            result = subprocess.run(
                [self._nvidia_smi, "--query-gpu=uuid," + ",".join(fields),
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, encoding="utf-8", timeout=0.5, check=False)
        except subprocess.TimeoutExpired:
            return group.note("nvidia-smi", "io_error", "query timed out after 0.5 s")
        except OSError as e:
            return group.note("nvidia-smi", sysfs.reason_of(e), str(e))
        except UnicodeError:
            return group.note("nvidia-smi", "invalid_data", "query is not UTF-8")
        if result.returncode:
            return group.note("nvidia-smi", "io_error", f"query exit code {result.returncode}")
        span = (start, clock()) if clock else None
        try:
            rows = list(csv.reader(result.stdout.splitlines(), skipinitialspace=True, strict=True))
            if not rows:
                return group.note("nvidia-smi", "not_detected")
            if any(len(row) != 4 or not re.fullmatch(r"GPU-[A-Za-z0-9-]+", row[0].strip())
                   for row in rows):
                raise ValueError("invalid GPU row")
        except (csv.Error, ValueError):
            return group.note("nvidia-smi", "invalid_data", "malformed query response")
        uuid, *values = min(rows, key=lambda row: row[0].strip())
        entry = {"driver": "nvidia", "provider": "nvidia-smi"}
        for key, attr, text in zip(("usage", "freq", "max_freq"), fields, values):
            value = None
            if text.strip() in ("N/A", "[N/A]", "[Not Supported]", "Not Supported"):
                group.note(attr, "not_exposed")
            else:
                try:
                    value = float(text)
                    if (not math.isfinite(value) or value < 0 or (key == "usage" and value > 100)
                            or (key != "usage" and not math.isfinite(value * 10**6))):
                        raise ValueError("out of range")
                except ValueError:
                    value = group.note(attr, "invalid_data")
            entry[key] = None if value is None else group.got(
                round(value, 1) if key == "usage" else round(value * 10**6))
            source = sysfs.source("nvidia-smi", "percent" if key == "usage" else "hertz",
                                  "device_channel", device=uuid.strip(), channel=attr)
            group.sensor(key, source, span if value is not None else None)
        return entry

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
