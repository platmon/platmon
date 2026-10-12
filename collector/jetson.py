"""Jetson-only data: GPU load and clock, nvpmodel power mode, pwm_tach fan speed, L4T release."""
import fnmatch
import os
import re

from . import metadata
from . import sysfs
from .sysfs import entries, host_path, hwmon_chips
from .metadata import guard


# GPU devfreq device names per BSP (NVIDIA L4T docs): Orin R36 17000000.gpu (verified on an Orin Nano),
# Orin R35 17000000.ga10b, Xavier 17000000.gv11b, TX2 17000000.gp10b, Nano 57000000.gpu.
GPU_DEVFREQ = ("*.gpu", "*.ga10b", "*.gv11b", "*.gp10b", "*.gm20b")
# load (per-mille) sits in the GPU device directory; gpu.0 links to it where devfreq is not found
GPU_LOAD = ("devices/platform/gpu.0/load", "devices/gpu.0/load")


def gpu(root="/sys", group=None):
    """GPU load and clocks. freq/max_freq are None (not 0) when the clock cannot be read.
    The load comes from the first candidate that reads; the others' failures only count if none does."""
    g = group or metadata.Group("gpu")
    # a devfreq listing failure also hides the clocks, so it is reported even when another path gives the load
    listing = metadata.Group("gpu")
    devices = entries(f"{root}/class/devfreq", listing)
    g.merge(listing)
    tried = metadata.Group("gpu", g.trace)  # load candidates that failed: reported only if none gives the load
    devfreq = next((f"{root}/class/devfreq/{n}" for pattern in GPU_DEVFREQ for n in devices
                    if fnmatch.fnmatchcase(n, pattern)), None)
    loads = ([f"{devfreq}/device/load"] if devfreq else []) + [f"{root}/{p}" for p in GPU_LOAD]
    load = used = None
    for path in loads:
        load = tried.read(path, "load")
        if load is not None:
            used = path
            break
    if load is None:
        g.merge(tried)
        return None

    def src(unit, path):
        """The file this value came from (resolved, so two paths to the same attribute share it); GPU paths
        differ per BSP, so it is identified by path only."""
        if g.trace is None:
            return None
        rel = sysfs.canonical(path, root)
        return metadata.source("gpu", unit, "resolved_path" if rel else "unresolved", path=rel)

    g.sensor("usage", src("percent", used), tried.span)  # only the candidate that gave the load

    def clock(name):
        if devfreq is None:
            return None
        value = g.read(f"{devfreq}/{name}", name, found=devfreq)
        g.sensor(name.replace("cur_", ""), src("hertz", f"{devfreq}/{name}"), g.span)
        return None if value is None else g.got(value)

    return {"usage": g.got(load / 10), "freq": clock("cur_freq"), "max_freq": clock("max_freq")}


def pmode(text):
    """The /var/lib/nvpmodel/status line ("pmode:0002") as it is; ValueError if it is not one."""
    if not re.fullmatch(r"pmode:\d+", text):
        raise ValueError(text)
    return text


def power_mode(status, conf_text):
    """status: /var/lib/nvpmodel/status ("pmode:0002"), conf_text: /etc/nvpmodel.conf."""
    mode_id = status.removeprefix("pmode:").lstrip("0") or "0"
    m = re.search(rf"POWER_MODEL\s+ID={mode_id}\s+NAME=(\S+?)\s*>", conf_text or "")
    return m.group(1) if m else mode_id


def l4t(text):
    """/etc/nv_tegra_release ("# R36 (release), REVISION: 5.2, ...") -> "R36.5.2"."""
    m = re.search(r"R(\d+) \(release\), REVISION: ([\d.]+)", text or "")
    return f"R{m.group(1)}.{m.group(2)}" if m else None


def l4t_release(text):
    """l4t() for a file that must hold the release line; ValueError otherwise."""
    release = l4t(text)
    if release is None:
        raise ValueError("no release line")
    return release


def tach_fans(root="/sys/class/hwmon", group=None):
    """pwm_tach reports fan speed in a non-standard "rpm" file; its pwm half is the pwmfan chip.
    A chip with an rpm file that cannot be read is still listed, with rpm None."""
    g = group or metadata.Group("fans")
    fans = []
    with guard(g):
        chips = hwmon_chips(root, g)
        ids = sysfs.chip_identities(chips, os.path.dirname(os.path.dirname(root))) if g.trace else {}
        for d, chip in chips:
            if not os.path.exists(f"{d}/rpm"):
                continue
            rpm = g.read(f"{d}/rpm", f"{os.path.basename(d)}.rpm", found=f"{d}/rpm")
            fan = {"name": chip, "rpm": None if rpm is None else g.got(rpm), "percent": None}
            fans.append(fan)
            if g.trace:
                basis, where = ids.get(d, ("unresolved", {}))
                g.sensor((id(fan), "rpm"), metadata.source("hwmon", "rpm", basis, **where, attr="rpm"), g.span)
    return fans


def extend(stats, groups=None):
    """Fills the Jetson fields; each part notes in its group what it could not read."""
    g = groups or metadata.groups("gpu", "fans", "power_mode", "board_info")
    stats["power_mode"] = stats["gpu"] = None
    with guard(g["power_mode"]):
        status = g["power_mode"].read("/var/lib/nvpmodel/status", "status", parse=pmode)
        if status is not None:
            conf = g["power_mode"].read("/etc/nvpmodel.conf", "conf", parse=str)  # names the mode; else its id
            stats["power_mode"] = g["power_mode"].got(power_mode(status, conf))
    with guard(g["gpu"]):
        stats["gpu"] = gpu(group=g["gpu"])
    stats["fans"] += tach_fans(group=g["fans"])
    with guard(g["board_info"]):
        release = g["board_info"].read(host_path("/etc/nv_tegra_release"), "l4t", parse=l4t_release)
        if release is not None:
            stats["system"]["l4t"] = g["board_info"].got(release)
