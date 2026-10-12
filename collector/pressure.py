"""Pressure stall information (PSI) from /proc/pressure: the kernel's own averages of the time tasks waited
for CPU, memory and I/O. Read once per collection by the service's sampler thread. Contract:
docs/reference/pressure.md (format: Documentation/accounting/psi.rst).
"""
import os
import re
import time

from .sysfs import reason_of

PROVIDER = "proc_pressure"
ROOT = "/proc/pressure"
RESOURCES = ("cpu", "memory", "io")
LINE = re.compile(r"(some|full) avg10=(\d+\.\d+) avg60=(\d+\.\d+) avg300=(\d+\.\d+) total=(\d+)")


def parse(text):
    """{"some": {...}, "full": {...}} from one pressure file; ValueError if a line is not the known format or
    a kind is missing or repeated."""
    out = {}
    for line in text.split("\n"):
        if not line.strip():
            continue
        m = LINE.fullmatch(line.strip())
        if not m or m.group(1) in out:
            raise ValueError(line)
        out[m.group(1)] = {"avg10": float(m.group(2)), "avg60": float(m.group(3)), "avg300": float(m.group(4)),
                           "total_us": int(m.group(5))}
    if "some" not in out:
        raise ValueError("no some line")
    return out


class Pressure:
    """sample(group) -> (output, read span or None). Nothing is kept between calls: the averages are the
    kernel's; total_us is cumulative and given as read."""

    def __init__(self, clock=time.monotonic_ns, root=ROOT, read=None, exists=os.path.isdir):
        self.clock, self._root, self._exists = clock, root, exists
        self._read = read or _read

    def sample(self, group):
        out = {"scope": {"kind": "host"}, "provider": PROVIDER, "resources": {}, "read": None}
        if not self._exists(self._root):  # kernel without PSI, or with it disabled: one check, no file opened
            group.note("pressure", "not_exposed")
            return out, None
        start, end = self.clock(), None
        for name in RESOURCES:
            path = f"{self._root}/{name}"
            try:
                text = self._read(path)
                end = self.clock()
                kinds = parse(text)
            except FileNotFoundError:
                group.note(name, "not_exposed")
                continue
            except OSError as e:
                group.note(name, reason_of(e), f"{path}: {e.strerror}")
                continue
            except (UnicodeDecodeError, ValueError):
                group.note(name, "invalid_data", path)
                continue
            if name == "cpu":  # "CPU full is undefined at the system level" (psi.rst): reported as 0, not a value
                kinds.pop("full", None)
            out["resources"][name] = group.got({"some": kinds["some"], "full": kinds.get("full")})
        return out, (start, end) if out["resources"] else None


def _read(path):
    with open(path, encoding="ascii") as f:
        return f.read()
