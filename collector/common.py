"""Collectors for any Linux host: standard /proc, cpufreq, thermal and hwmon interfaces only.

Board-specific paths and tools belong in a board module (see jetson.py).
"""
import os
import re
import shutil
import time

from . import metadata
from . import sysfs
from .sysfs import DEVICE_TREE, HOST_ROOT, entries, host_path, hwmon_chips, numbered, read
from .metadata import guard


def unique_key(table, key, d):
    """key, or "key (hwmonN)" when another chip already reported the same name (two nvme drives, two ina219s)."""
    return f"{key} ({os.path.basename(d)})" if key in table else key


def hwmon_sensors(skip=(), root="/sys/class/hwmon", groups=None, skip_devices=()):
    """Temps, power and fans from every hwmon chip, in one pass.
    skip: chip names already read elsewhere; skip_devices: resolved sysfs paths (sysfs.canonical) whose
    hwmon children are already read elsewhere. A thermal zone's hwmon twin is left out by its path,
    because the kernel may name it after the zone type or after the zone itself (acpitz_0); the name
    stays the fallback for a tree whose paths do not resolve, e.g. a container's partial /sys mount.
    groups: the temperature/power/fans metadata.Group to note unreadable channels in; a channel that cannot be
    read is left out (a fan's unreadable rpm or pwm is None), the others are kept."""
    g = groups or metadata.groups("temperature", "power", "fans")
    temp_g, power_g, fan_g = g["temperature"], g["power"], g["fans"]
    temps, power, fans = {}, {}, []
    track = temp_g.trace is not None  # the service records each value's source and read span
    sysroot = os.path.dirname(os.path.dirname(root))
    with guard(temp_g, power_g, fan_g):
        chips = hwmon_chips(root, temp_g, power_g, fan_g)
        ids = sysfs.chip_identities(chips, sysroot) if track else {}
        for d, chip in chips:
            if chip in skip:
                continue
            # a zone's twin sits inside the zone's own directory, whatever the chip is named
            if skip_devices and (rel := sysfs.canonical(d, sysroot)) and os.path.dirname(rel) in skip_devices:
                continue
            dev = os.path.basename(d)
            basis, where = ids.get(d, ("unresolved", {}))

            def src(unit, **attr):
                return metadata.source("hwmon", unit, basis, **where, **attr) if track else None
            # one listing per chip for all its channels; if it cannot be listed, this chip is left out
            names = entries(d, temp_g, power_g, fan_g, listed=True)

            def label(group, kind, n, default):
                """A display label; unreadable or absent, the default name is used (a read failure is noted)."""
                return group.read(f"{d}/{kind}{n}_label", f"{dev}.{kind}{n}_label", parse=str, found=d) or default

            for n in numbered(names, "temp", "_input"):
                path = f"{d}/temp{n}_input"
                t = temp_g.read(path, f"{dev}.temp{n}", found=path)
                span = temp_g.span  # the number's read; the label read after it does not count
                if t is not None:
                    key = unique_key(temps, f"{chip} {label(temp_g, 'temp', n, f'temp{n}')}", d)
                    temps[key] = temp_g.got(t / 1000)  # m°C
                    temp_g.sensor(key, src("celsius", attr=f"temp{n}_input"), span)
            for n in numbered(names, "power", "_input"):
                path = f"{d}/power{n}_input"
                p = power_g.read(path, f"{dev}.power{n}", found=path)
                span = power_g.span
                if p is not None:
                    key = unique_key(power, label(power_g, "power", n, f"{chip} power{n}"), d)
                    power[key] = power_g.got(round(p / 1e6, 2))  # µW
                    power_g.sensor(key, src("watt", attr=f"power{n}_input"), span)
            for n in numbered(names, "curr", "_input"):  # ina3221-style rails; label required, unlabeled are sums
                lbl = power_g.read(f"{d}/in{n}_label", f"{dev}.in{n}_label", parse=str, found=d)
                if not lbl:  # no label (a sum channel), or noted if it could not be read
                    continue
                path = f"{d}/curr{n}_input"
                ma = power_g.read(path, f"{dev}.curr{n}", found=path)
                span_a = power_g.span
                mv = power_g.read(f"{d}/in{n}_input", f"{dev}.in{n}", found=d)
                span_v = power_g.span
                if ma is not None and mv is not None:
                    key = unique_key(power, lbl, d)
                    power[key] = power_g.got(round(mv * ma / 1e6, 2))  # mV*mA -> W
                    # computed from two reads: from the earlier start to the later end, not one instant
                    span = (min(span_a[0], span_v[0]), max(span_a[1], span_v[1])) if span_a and span_v else None
                    power_g.sensor(key, src("watt", calc="product",
                                            inputs=[["voltage", f"in{n}_input"], ["current", f"curr{n}_input"]]), span)

            def add_fan(name, n, rpm, rpm_span, found):
                pwm = fan_g.read(f"{d}/pwm{n}", f"{dev}.pwm{n}", found=found)
                fan = {"name": name, "rpm": rpm, "percent": None if pwm is None else fan_g.got(round(pwm * 100 / 255))}
                fans.append(fan)
                if rpm_span is not False:  # False: this fan has no rpm attribute at all
                    fan_g.sensor((id(fan), "rpm"), src("rpm", attr=f"fan{n}_input"), rpm_span)
                fan_g.sensor((id(fan), "percent"), src("percent", attr=f"pwm{n}"), fan_g.span)

            fan_idx = numbered(names, "fan", "_input")
            for n in fan_idx:
                path = f"{d}/fan{n}_input"
                # None if unreadable, not 0. io_absent: a driver exposes the attribute whenever the
                # firmware declares a fan (acpi_fan from an ACPI PNP0C0B object), and reading it returns
                # EIO where the firmware reports no speed at all, as on boards whose embedded controller
                # keeps the fan to itself. That is a fan without a tachometer, not a broken sensor.
                rpm = fan_g.read(path, f"{dev}.fan{n}", found=path, io_absent=True)
                span = fan_g.span
                add_fan(label(fan_g, "fan", n, f"{chip} fan{n}"), n, None if rpm is None else fan_g.got(rpm), span, d)
            if not fan_idx and "pwm1" in names:  # pwm-fan without a tachometer
                add_fan(chip, 1, None, False, f"{d}/pwm1")
    return temps, power, fans


def thermal_zones(root="/sys/class/thermal", group=None):
    """Zone temps, the zone types their hwmon twins may be named after (kernel turns "-" into "_"), and the
    zones' resolved paths; hwmon_sensors leaves the twins out by both.
    group: the temperature metadata.Group; inactive zones (ENODATA) count as no_data, not as errors."""
    g = group or metadata.Group("temperature")
    temps, zone_types, zone_paths = {}, set(), set()
    sysroot = os.path.dirname(os.path.dirname(root))
    with guard(g):
        zones = sorted(numbered(entries(root, g), "thermal_zone", ""))
        for z in (f"{root}/thermal_zone{n}" for n in zones):
            # zones are numbered virtual devices: identified by their path only, not their type
            rel = sysfs.canonical(z, sysroot)
            if rel:
                zone_paths.add(rel)
            # the zone's name; unreadable, the zone is named after its directory (a read failure is noted)
            zt = g.read(f"{z}/type", f"{os.path.basename(z)}.type", parse=str, found=z) or os.path.basename(z)
            zone_types.add(zt.replace("-", "_"))
            t = g.read(f"{z}/temp", os.path.basename(z), found=z)
            span = g.span
            if t is not None:
                key = zt.removesuffix("-thermal")
                if key in temps:  # e.g. several acpitz zones
                    key += f" ({os.path.basename(z)})"
                temps[key] = g.got(t / 1000)
                if g.trace:
                    g.sensor(key, metadata.source("thermal", "celsius", "resolved_path" if rel else "unresolved",
                                               path=rel, attr="temp"), span)
    return temps, zone_types, zone_paths


def cpu_times(path="/proc/stat"):
    """{cpu id: jiffies} for online cores; offline cores are absent from /proc/stat.
    Every cpuN line must have at least user..steal (8 counters, Linux 2.6.11+); guest fields may follow.
    Raises ValueError otherwise, so a broken reading fails the collection instead of becoming a baseline."""
    times = {}
    with open(path) as f:
        for line in f:
            name, *fields = line.split() or [""]
            if re.fullmatch(r"cpu\d+", name):
                if len(fields) < 8:
                    raise ValueError(f"{name}: {len(fields)} counters in /proc/stat, need at least 8")
                times[int(name[3:])] = list(map(int, fields))
    if not times:  # never a host with 0 CPUs: the file could not be read as expected
        raise ValueError("no per-cpu lines in /proc/stat")
    return times


def cpu_percent(before, after):
    """Busy % per core id from two /proc/stat samples (idle + iowait count as idle), and why the other cores
    of `after` have none: {id: usage}, {id: reason}. Cores gone in `after` are left out.
    Only user..steal (the first 8 fields) make up the total: guest and guest_nice are already counted
    in user and nice, so adding them again overstates the load on hosts running virtual machines."""
    usage, unavailable = {}, {}
    for n in sorted(after):
        if n not in before:  # new, or back online: no earlier reading of this core to compare with
            unavailable[n] = "warmup"
            continue
        a, b = after[n][:8], before[n][:8]
        if len(a) != len(b) or len(a) < 5:  # never cut short by zip: callers pass whole readings
            raise ValueError(f"cpu{n}: readings with {len(b)} and {len(a)} counters cannot be compared")
        delta = [x - y for x, y in zip(a, b)]
        total, idle = sum(delta), delta[3] + delta[4]
        if any(d < 0 for d in delta):  # e.g. iowait can go backwards; not a reboot, not 0 %, just unusable
            unavailable[n] = "counter_regressed"
        elif not total:  # nothing measured, which is not the same as measured idle
            unavailable[n] = "no_ticks"
        else:
            usage[n] = round(100 * (total - idle) / total, 1)
    return usage, unavailable


class CpuCounters:
    """CPU usage from consecutive /proc/stat readings: each sample() reads it once and compares with the
    previous successful reading, so a running service needs no wait inside its collection.
    The usage is the busy share of the ticks between the two readings, over window_ms."""

    def __init__(self, clock=time.monotonic_ns, max_gap=None, mode="interval"):
        """clock: elapsed nanoseconds (the service passes the Sampler's); max_gap: seconds, a longer window
        between readings is not averaged over (None: no limit)."""
        self._clock, self.mode = clock, mode
        self._max_gap = None if max_gap is None else round(max_gap * 1e9)
        self._last = None  # (elapsed ns when the reading began, cpu_times()) of the last successful reading
        self.span = None   # (start, end) elapsed ns of the latest reading

    def sample(self):
        """({id: usage}, cpu_sampling). Every valid reading becomes the next baseline, also when it gives no
        usage (counter_regressed, no_ticks, invalid_interval, gap); a failed or invalid one clears the
        baseline and raises, so the collection fails and the next reading restarts warmup."""
        try:
            now = self._clock()
            times = cpu_times()
            self.span = (now, self._clock())  # this reading, for the data age; the window starts at the last one
            last = self._last
            window = None if last is None else now - last[0]
            if last is None:
                usage, unavailable = {}, dict.fromkeys(times, "warmup")
            elif window <= 0:
                usage, unavailable, window = {}, dict.fromkeys(times, "invalid_interval"), None
            elif self._max_gap is not None and window > self._max_gap:
                usage, unavailable = {}, dict.fromkeys(times, "gap")
            else:
                usage, unavailable = cpu_percent(last[1], times)
        except Exception:
            self._last = None
            raise
        self._last = (now, times)  # only once the reading was usable
        return usage, {"mode": self.mode, "window_ms": None if window is None else round(window / 1e6, 3),
                       "unavailable": [{"id": n, "reason": r} for n, r in sorted(unavailable.items())]}


def meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for l in f:
            k, v = l.split(":")
            m[k] = int(v.split()[0]) * 1024
    return m


def os_release(text):
    """Distribution name from os-release text (freedesktop format): PRETTY_NAME, else NAME VERSION."""
    fields = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip().strip("\"'")
    return fields.get("PRETTY_NAME") or " ".join(filter(None, (fields.get("NAME"), fields.get("VERSION")))) or None


def system_info():
    """OS of the host (read through HOST_ROOT, so a container reports the host, not its image), kernel, arch, hostname.
    Board modules may add their own entries (e.g. Jetson: l4t)."""
    u = os.uname()
    return {"os": os_release(read(host_path("/etc/os-release")) or read(host_path("/usr/lib/os-release"))),
            "kernel": u.release, "arch": u.machine, "hostname": u.nodename}


def collect(cpu=None, groups=None, trace=None):
    """Fields every host has. gpu and power_mode stay None unless a board module fills them.
    cpu: the service's CpuCounters. Without one (a one-off call) the CPU usage comes from two readings
    250 ms apart with a baseline of its own.
    groups: metadata.Group per optional group, for collectors; without them this call reports its own.
    CPU counters, memory, disk and identity are required: their failures raise. The optional parts
    (CPU clocks, temperatures, power, fans) note what they could not read and the rest is kept.
    trace: the service's metadata.Trace (shares cpu's clock); it gets the required reads' spans."""
    own = groups is None
    if own:
        groups = metadata.groups("cpu_frequency", "temperature", "power", "fans")
    if cpu is None:
        cpu = CpuCounters(mode="oneshot")
        cpu.sample()
        time.sleep(0.25)
    usage, sampling = cpu.sample()
    if trace:
        trace.required["cpu"] = cpu.span

    freq_g, freqs = groups["cpu_frequency"], {}
    if not usage:  # e.g. the service's first reading: no CPU rows to read clocks for (see cpu_sampling)
        freq_g.note("cpu", "no_data")
    with guard(freq_g):
        for n in usage:
            f = freq_g.read(f"/sys/devices/system/cpu/cpu{n}/cpufreq/scaling_cur_freq", f"cpu{n}")
            freqs[n] = None if f is None else freq_g.got(f * 1000)  # kHz
            if freq_g.trace:  # logical CPU n, not its position in the list
                freq_g.sensor(n, metadata.source("cpufreq", "hertz", "device_channel", cpu=n, attr="scaling_cur_freq"),
                              freq_g.span)

    temps, zone_types, zone_paths = thermal_zones(group=groups["temperature"])
    # zones (acpitz, cpu-thermal, ...) also show up as hwmon chips; skip them to avoid duplicates
    hw_temps, power, fans = hwmon_sensors(skip=zone_types, skip_devices=zone_paths, groups=groups)
    temps.update(hw_temps)

    timed = trace.timed if trace else (lambda name, fn, *args: fn(*args))
    mem = timed("memory", meminfo)
    disk = timed("disk", shutil.disk_usage, HOST_ROOT)
    uptime = timed("uptime", read, "/proc/uptime")

    return {
        "time": time.time(),
        "model": (read(f"{DEVICE_TREE}/model") or read("/sys/class/dmi/id/product_name")
                  or os.uname().nodename).rstrip("\0"),
        "system": system_info(),
        "uptime": float(uptime.split()[0]),
        "power_mode": None,
        "cpu": [{"id": n, "usage": u, "freq": freqs.get(n)} for n, u in usage.items()],
        "cpu_sampling": sampling,  # cores missing from cpu, and why
        "gpu": None,
        "memory": {"total": mem["MemTotal"], "used": mem["MemTotal"] - mem["MemAvailable"],
                   "swap_total": mem["SwapTotal"], "swap_used": mem["SwapTotal"] - mem["SwapFree"]},
        "disk": {"total": disk.total, "used": disk.used},
        "temperature": temps,
        "power": power,
        "fans": fans,
        **({"collectors": metadata.summarize(groups)} if own else {}),
    }
