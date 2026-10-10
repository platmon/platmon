#!/usr/bin/env python3
"""platmon server: start the collector core once, then the outputs enabled in the config (today: the HTTP API
and web page). It runs as a background service (scripts/systemd, scripts/docker); look at it with the `platmon`
command (frontends/cli.py), a browser, or http://<host>:9797/text.

Usage: python3 platmon.py [config.ini] [--frontends http]
Without a config file the built-in defaults apply (see platmon.ini).
"""
import argparse
import configparser
import functools
import signal
import sys

from collector import BOARD, PLATFORM, collect_recorded
from collector.common import CpuCounters
from collector.disk_io import DiskCounters
from collector.gpu import Gpu
from collector.npu import Npu
from collector.history import History
from collector.network import NetworkCounters
from collector.probe import Probe, parse_targets
from collector.pressure import Pressure
from collector.sampler import Sampler, pick_clock
from collector.slow import Observations, Slow
from collector.storage import Storage
from collector.wifi import Wifi
from frontends import server
from frontends.cli import VERSION

FRONTENDS = {"http": server.start}  # name -> start(sampler, cfg section, ...); outputs that run inside the core

DEFAULTS = {  # the type of each default is the type its config value must parse as
    "core": {"interval": 1.0},
    "network": {"enabled": True},  # measured cost: docs/performance/budget.md
    "disk_io": {"enabled": True},
    "storage": {"enabled": True, "interval": 30.0},
    "wifi": {"enabled": True, "interval": 5.0},
    "probe": {"enabled": True, "interval": 10.0, "timeout": 2.0, "targets": ""},
    "history": {"enabled": True, "retention": 600.0},
    "pressure": {"enabled": True},
    "http": {"enabled": True, "bind": "0.0.0.0", "port": 9797, "web": True},
    # the web page's warning and critical levels; temperature empty: the board default the page picks
    "thresholds": {"cpu": "80, 90", "gpu": "80, 90", "memory": "80, 90", "filesystem": "80, 90", "temperature": ""},
}
REMOVED = {"terminal": "the terminal view is now the `platmon` command (frontends/cli.py); delete this section"}
RANGES = {("core", "interval"): (0.1, 3600), ("storage", "interval"): (5, 3600), ("wifi", "interval"): (1, 3600), ("probe", "interval"): (1, 3600),
          ("probe", "timeout"): (0.1, 10), ("history", "retention"): (60, 3600), ("http", "port"): (1, 65535)}


def check(cfg):
    """Reject unknown sections and keys (typos), values of the wrong type and values out of range."""
    for section in set(cfg.sections()) & REMOVED.keys():
        raise ValueError(f"[{section}] is no longer supported: {REMOVED[section]}")
    unknown = set(cfg.sections()) - DEFAULTS.keys()
    if unknown:
        raise ValueError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    for section, options in DEFAULTS.items():
        unknown = cfg[section].keys() - options.keys()
        if unknown:
            raise ValueError(f"[{section}] unknown key(s): {', '.join(sorted(unknown))}; "
                             f"valid: {', '.join(options)}")
        for key, default in options.items():
            get = {bool: cfg[section].getboolean, int: cfg[section].getint,
                   float: cfg[section].getfloat, str: cfg[section].get}[type(default)]
            try:
                value = get(key)
            except ValueError:
                raise ValueError(f"[{section}] {key} = {cfg[section][key]!r} is not a valid {type(default).__name__}")
            if (section, key) in RANGES:
                lo, hi = RANGES[section, key]
                if not lo <= value <= hi:
                    raise ValueError(f"[{section}] {key} = {value} is out of range ({lo} to {hi})")


# Temperature levels when [thresholds] leaves them empty: below the board's throttling points (Jetson Orin
# software throttling ≈99 °C; Raspberry Pi firmware throttling from 80 °C, not exposed as a trip point),
# else a generic fallback. The one table: the web page and /api/status get the result, and the platmon
# command keeps a copy only for servers older than this (tests check that it matches).
BOARD_TEMPERATURE = {"Jetson Orin": [85.0, 95.0], "Raspberry Pi": [75.0, 80.0]}
OTHER_TEMPERATURE = [80.0, 90.0]


def levels(cfg, platform=None):
    """[thresholds] as {name: [warning, critical]}. An empty temperature is the board default for platform,
    or None without a platform (when only checking the config). Raises ValueError."""
    out = {}
    for name in DEFAULTS["thresholds"]:
        text = cfg["thresholds"][name].strip()
        if name == "temperature" and not text:
            out[name] = list(BOARD_TEMPERATURE.get(platform, OTHER_TEMPERATURE)) if platform else None
            continue
        try:
            warn, crit = (float(v) for v in text.split(","))
        except ValueError:
            raise ValueError(f"[thresholds] {name} = {text!r}: give two numbers, warning then critical, e.g. 80, 90")
        top = 150 if name == "temperature" else 100  # °C, else percent
        if not 0 < warn < crit <= top:  # also rejects nan and inf
            raise ValueError(f"[thresholds] {name} = {text!r}: need 0 < warning < critical <= {top}")
        out[name] = [warn, crit]
    return out


def load_config(path=None, frontends=None):
    """Defaults < config file < frontends (list of names; enables exactly those). Raises ValueError, also when
    the result would start nothing: scripts/systemd/install.sh relies on this to refuse before changing anything."""
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({s: {k: ("yes" if v else "no") if isinstance(v, bool) else str(v) for k, v in o.items()}
                   for s, o in DEFAULTS.items()})
    try:
        if path and not cfg.read(path):
            raise ValueError(f"cannot read config {path}")
    except configparser.Error as e:
        raise ValueError(f"cannot parse config {path}: {e}")
    check(cfg)
    levels(cfg)
    probe = cfg["probe"]
    try:
        targets = parse_targets(probe.get("targets"))
    except ValueError as e:
        raise ValueError(f"[probe] targets: {e}")
    if targets and len(targets) * probe.getfloat("timeout") >= probe.getfloat("interval"):
        raise ValueError("[probe] interval must be longer than timeout × number of targets (they are probed one after another)")
    if frontends is not None:
        unknown = set(frontends) - FRONTENDS.keys()
        if unknown:
            raise ValueError(f"unknown frontend(s): {', '.join(sorted(unknown))}; choose from {', '.join(FRONTENDS)}")
        for name in FRONTENDS:
            cfg[name]["enabled"] = "yes" if name in frontends else "no"
    if not any(cfg[name].getboolean("enabled") for name in FRONTENDS):
        raise ValueError(f"nothing to run: every output is disabled; set enabled = yes in [{'] or ['.join(FRONTENDS)}]"
                         " (or pass --frontends)")
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(description="platmon server: collector core + HTTP API (view it with the platmon command)")
    p.add_argument("config", nargs="?", help="INI file; built-in defaults apply without one")
    p.add_argument("--frontends", help=f"comma-separated, overrides enabled= in the config ({','.join(FRONTENDS)})")
    p.add_argument("--version", action="version", version=f"platmon server {VERSION}")
    a = p.parse_args(argv)
    try:
        cfg = load_config(a.config, a.frontends.split(",") if a.frontends else None)
    except ValueError as e:
        p.error(str(e))

    # exit cleanly on SIGTERM (systemd stop, docker stop); as PID 1 in a container Python would ignore it
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # a container missing its device-tree mount shows up here as "PC"/"Linux" instead of the board
    print(f"platmon: platform {PLATFORM}, board module {BOARD.__name__ if BOARD else 'none'}, version {VERSION}", flush=True)
    interval = cfg["core"].getfloat("interval")
    clock = pick_clock()
    # CPU usage over the time since the previous reading; a longer break is not averaged over. This limit
    # matches the Sampler's default stale_after, but it is a CPU window rule, not snapshot freshness.
    cpu = CpuCounters(clock[0], max_gap=max(3 * interval, 5.0))
    # interface counters of this process's network namespace, with rates over the same kind of window
    network = NetworkCounters(clock[0], max_gap=max(3 * interval, 5.0)) if cfg["network"].getboolean("enabled") else None
    # per physical disk I/O counters of the host, the same way
    disk_io = DiskCounters(clock[0], max_gap=max(3 * interval, 5.0)) if cfg["disk_io"].getboolean("enabled") else None
    # pressure stall information, where the kernel provides it (see docs/pressure.md)
    pressure = Pressure(clock[0]) if cfg["pressure"].getboolean("enabled") else None
    # the GPU of the standard DRM class, kept between collections because i915 publishes idle time
    graphics = Gpu(clock[0], max_gap=max(3 * interval, 5.0))
    # the NPU of the standard accel class, kept the same way: its busy time is a counter too
    accelerator = Npu(clock[0], max_gap=max(3 * interval, 5.0))
    # {} keeps what was logged about unreadable optional sensors, so a lasting failure is logged once.
    # The same clock times the CPU readings, every sensor read and the Sampler's collections.
    sampler = Sampler(functools.partial(collect_recorded, cpu, {}, clock[0], network, disk_io, pressure,
                                        graphics, accelerator), interval, clock=clock)
    # Low-frequency groups, each on its own thread and cadence. With no configured targets,
    # Probe stays idle: no worker, observation group or connection is created.
    slow = [Slow(name, observe(), cfg[name].getfloat("interval"), clock[0])
            for name, observe in (("storage", Storage), ("wifi", Wifi),
                                  ("probe", lambda: Probe(parse_targets(cfg["probe"].get("targets")),
                                                          cfg["probe"].getfloat("timeout"))))
            if cfg[name].getboolean("enabled")
            and (name != "probe" or parse_targets(cfg["probe"].get("targets")))]
    observations = Observations(slow, sampler.instance_id, clock[1])
    # recent numbers of the published snapshots, in memory only (docs/history.md)
    history = None
    if cfg["history"].getboolean("enabled"):
        history = History(cfg["history"].getfloat("retention"), interval)
        sampler.on_publish = history.append
        for group in slow:
            group.on_publish = history.append_observation
    sampler.start()
    observations.start()
    threads = [t for name, start in FRONTENDS.items()
               if cfg[name].getboolean("enabled") and (t := start(sampler, cfg[name], observations, history, levels(cfg, PLATFORM)))]
    if not threads:
        p.error("no frontend is running; enable one in the config or with --frontends")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
