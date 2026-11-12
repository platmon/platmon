#!/usr/bin/env python3
"""platmon terminal client: live view of a platmon service, this device's by default.
Standalone (standard library only); scripts/install-cli.sh installs it as the `platmon` command.

Usage: platmon [host[:port]] [interval_sec] [--once | --view VIEW]   (default localhost:9797, 1s)
The service itself runs in the background: `systemctl status platmon` checks it (from a checkout:
deploy/systemd/install.sh or deploy/docker/start.sh).

On an interactive terminal the live view is screen(): colored levels, a layout fitted to the terminal size,
redrawn in place on the alternate screen. It shows one view at a time, as the web page's tabs do (VIEWS);
1-7, Tab and the arrow keys switch them. --once and pipes keep render(), plain text as before; the
server's /text answers with the same screen (native/src/render.rs).
"""
# platmon-cli: marker that lets scripts/install-cli.sh recognise (and only replace) its own command
import argparse
import json
import math
import os
import re
import select
import shutil
import signal
import sys
import textwrap
import time
import urllib.error
import urllib.request
from collections import deque

try:  # keys without Enter; not on Windows, where the live view stays the plain one
    import termios
    import tty
except ImportError:
    termios = tty = None

VERSION = "1.8.1"  # copy of VERSION, so this file stays standalone
PORT = 9797


def bar(pct, width=20):
    n = round(width * max(0, min(pct, 100)) / 100)
    return "█" * n + "·" * (width - n)


def gib(b):
    return f"{b / 2**30:.1f}G"


SYSTEM_LABELS = {"os": "", "kernel": "kernel ", "arch": "", "hostname": "host ", "l4t": "L4T "}  # ui-spec system_fields; others: "key value"


def uptime_text(seconds):
    """"3d 04:05" for an uptime in seconds."""
    up = int(seconds)
    return f"{up // 86400}d {up % 86400 // 3600:02}:{up % 3600 // 60:02}"


def system_line(system):
    """One line from the system entries, whatever the board added; empty for servers without them."""
    return " · ".join(SYSTEM_LABELS.get(k, f"{k} ") + str(v) for k, v in (system or {}).items() if v)


CPU_REASONS = {"warmup": "warming up", "counter_regressed": "counter went backwards", "no_ticks": "no ticks counted",
               "invalid_interval": "invalid interval", "gap": "restarting after a gap"}  # others: the code itself


def cpu_note(s):
    """Why CPU rows are missing (the service compares readings, so the first has none); empty if none are."""
    groups = {}
    for u in (s.get("cpu_sampling") or {}).get("unavailable") or []:
        groups.setdefault(CPU_REASONS.get(u["reason"], u["reason"]), []).append(f"CPU{u['id']}")
    if not groups:
        return ""
    if not s["cpu"] and len(groups) == 1:  # every core, one reason
        return f"CPU sampling: {next(iter(groups))}"
    return "CPU sampling: " + "; ".join(f"{' '.join(ids)} {why}" for why, ids in groups.items())


RATE_REASONS = {"warmup": "warming up", "gap": "restarting after a gap", "counter_regressed": "counter went backwards",
                "invalid_interval": "invalid interval", "identity_unavailable": "identity unknown",
                "namespace_changed": "namespace changed"}  # why rates are null; others: the code itself
MAX_ROWS = 12  # interfaces or disks listed; the rest are counted, all are in /api/stats


def per_s(v):
    """Bytes per second, binary units: 512 B/s, 1.5 KiB/s, 20.0 MiB/s."""
    for unit in ("B/s", "KiB/s", "MiB/s"):
        if abs(v) < 1024:
            return f"{v:.0f} {unit}" if unit == "B/s" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GiB/s"


def rows_of(items, kind, line):
    """One line per item (as the server sorted them): its name, then line(item) if it has rates, else why
    not. At most MAX_ROWS, then how many were left out."""
    width = min(16, max(len(i["name"]) for i in items))
    out = [f"  {i['name']:<{width}}  " + line(i)
           for i in items[:MAX_ROWS]]
    if len(items) > MAX_ROWS:
        out.append(f"  +{len(items) - MAX_ROWS} more {kind} (all in /api/stats)")
    return out


def network_line(i):
    r = i["rates"]
    if not r:
        return RATE_REASONS.get(i.get("reason"), str(i.get("reason")))
    bad = [f"{k} {i['rx'][k]}/{i['tx'][k]}" for k in ("errors", "dropped") if i["rx"][k] or i["tx"][k]]
    return (f"rx {per_s(r['rx_bytes_per_s'])}  tx {per_s(r['tx_bytes_per_s'])}  "
            f"{r['rx_packets_per_s']:.1f}/{r['tx_packets_per_s']:.1f} pkt/s" + window_text(i)
            + ("  " + "  ".join(bad) + " (rx/tx, total)" if bad else ""))


def window_text(item):
    window = item.get("window_ms")
    return f"  window {window / 1000:.3f} s" if isinstance(window, (int, float)) else ""


def disk_line(d):
    r = d["rates"]
    value = (f"read {per_s(r['read_bytes_per_s'])} ({r['reads_per_s']:.1f}/s)  "
             f"write {per_s(r['write_bytes_per_s'])} ({r['writes_per_s']:.1f}/s)  "
             f"I/O time {100 * r['io_time_ratio']:.1f} %" + window_text(d) if r else
             RATE_REASONS.get(d.get("reason"), str(d.get("reason"))))
    return value + (f"  in flight {d['in_flight']}" if d.get("in_flight") is not None else "")


def counters_lines(s):
    """Network interfaces (this process's namespace) and physical disks, with the rates the server computed;
    nothing for servers without them or with them off. I/O time is the share of the window with I/O in
    flight, not how saturated a disk is."""
    out = []
    net, dio = s.get("network"), s.get("disk_io")
    if isinstance(net, dict) and isinstance(net.get("interfaces"), list) and net["interfaces"]:
        out += ["NET   bytes/s, this process's network namespace"] + rows_of(net["interfaces"], "interfaces", network_line)
    if isinstance(dio, dict) and isinstance(dio.get("disks"), list) and dio["disks"]:
        out += ["IO    bytes/s per disk"] + rows_of(dio["disks"], "disks", disk_line)
    psi = s.get("pressure")
    res = psi.get("resources") if isinstance(psi, dict) else None
    if isinstance(res, dict) and res:  # the kernel's 10 s averages: share of time tasks were stalled
        out.append("PSI   " + "  ".join(f"{name} some {r['some']['avg10']:.1f} %" + (f" full {r['full']['avg10']:.1f} %" if r.get("full") else "")
                                        for name, r in res.items()) + "  (avg10, time stalled)")
    return out


def group_data(obs, name):
    """(data, observation) of an /api/observations group, or (None, None) when it is off, not observed
    yet, or the server has no such endpoint."""
    groups = obs.get("groups") if isinstance(obs, dict) else None
    g = groups.get(name) if isinstance(groups, dict) else None
    data, o = (g.get("data"), g.get("observation")) if isinstance(g, dict) else (None, None)
    return (data, o) if isinstance(data, dict) and isinstance(o, dict) else (None, None)


def observed(o):
    """How old an observation is: "observed 4 s ago", "(not current)" when stale."""
    return f"observed {o['data_age_ms'] / 1000:.0f} s ago" + (" (not current)" if o.get("stale") else "")


def stale_text(o):
    """An observation's age as a view's caption only when it is no longer current: a group read every few
    seconds is current by default, and a counting age beside it would be noise (render() keeps
    observed_text, so --once and /text are unchanged)."""
    return observed_text(o) if o and o.get("stale") else ""


def observed_text(o):
    """observed() as a section's caption; empty when the observation reports no age."""
    return observed(o) if isinstance(o, dict) and isinstance(o.get("data_age_ms"), (int, float)) else ""


def storage_lines(obs):
    """Filesystems and partitions from /api/observations, with the observation's own age; nothing when the
    group is off, has not been observed yet, or the server has no such endpoint."""
    data, o = group_data(obs, "storage")
    if data is None:
        return []
    out = [f"STORAGE  {observed(o)}"]
    for f in data.get("filesystems") or []:
        first, more = f["mount_points"][0], len(f["mount_points"]) - 1  # bind mounts can be dozens
        where = first["path"] + (" (ro)" if first["read_only"] else "") + (f" +{more} more" if more else "")
        dev = f["device"] or f"{f['major']}:{f['minor']}"
        if f["total_bytes"] is not None:  # a measured 0 is shown as 0
            pct = f" ({100 * f['used_bytes'] / f['total_bytes']:.0f} %)" if f["total_bytes"] else ""
            out.append(f"  {where}  {f['fstype']} {dev}  used {gib(f['used_bytes'])}/{gib(f['total_bytes'])}{pct}"
                       f"  available {gib(f['available_bytes'])}")
        else:
            out.append(f"  {where}  {f['fstype']} {dev}  capacity unknown")
    disks = {}
    for p in data.get("partitions") or []:
        disks.setdefault(p["disk"], []).append(p)
    for disk, parts in disks.items():
        mounted = sum(1 for p in parts if p["mount_points"])
        out.append(f"  {disk}: {len(parts)} partitions, {mounted} mounted")
    return out


def aged(body, seconds):
    """An /api/observations answer as of seconds after it arrived: each observation's ages grow by that
    much (a copy; the answer itself is kept as received)."""
    if not isinstance(body, dict) or not isinstance(body.get("groups"), dict):
        return body
    groups = {}
    for name, g in body["groups"].items():
        o = g.get("observation") if isinstance(g, dict) else None
        if isinstance(o, dict) and isinstance(o.get("data_age_ms"), (int, float)):
            age = o["data_age_ms"] + 1000 * seconds
            threshold = g.get("stale_after_ms")
            stale = bool(o.get("stale")) or (isinstance(threshold, (int, float)) and age > threshold)
            g = {**g, "state": "stale" if stale else g.get("state"),
                 "observation": {**o, "data_age_ms": age, "stale": stale}}
        groups[name] = g
    return {**body, "groups": groups}


def wifi_signal(i):
    """(signal, the other readings present) of a connected wireless interface."""
    signal = (f"{i['signal_dbm']} dBm" if i["signal_dbm"] is not None
              else f"signal {i['signal_raw']} (unit not reported)" if i["signal_raw"] is not None else "signal n/a")
    extra = [f"link quality {i['link_quality']}" if i["link_quality"] is not None else "",
             f"noise {i['noise_dbm']} dBm" if i["noise_dbm"] is not None else ""]
    return signal, [e for e in extra if e]


def wifi_lines(obs):
    """Wireless interfaces from /api/observations: the signal while connected, never the last one after."""
    data, o = group_data(obs, "wifi")
    items = data.get("interfaces") if data else None
    if not isinstance(items, list) or not items:
        return []
    out = [f"WIFI  {observed(o)}"]
    for i in items:
        if i.get("connected") is not True:
            state = "not connected" if i.get("connected") is False else i.get("connection_state", "unknown")
            out.append(f"  {i['name']}  {state}")
            continue
        signal, extra = wifi_signal(i)
        out.append(f"  {i['name']}  {signal}" + "".join(f"  {e}" for e in extra))
    return out


def battery_readings(b):
    """A battery's status and readings, those it reports, in order."""
    full = b.get("charge_full_uah") or b.get("energy_full_uwh")
    design = b.get("charge_full_design_uah") or b.get("energy_full_design_uwh")
    parts = [b.get("status") or "",
             f"{b['voltage_uv'] / 1e6:.2f} V" if b.get("voltage_uv") is not None else "",
             f"{b['current_ua'] / 1e6:+.2f} A" if b.get("current_ua") is not None else "",
             f"{b['power_uw'] / 1e6:.2f} W" if b.get("power_uw") is not None else "",
             f"{b['cycle_count']} cycles" if b.get("cycle_count") is not None else "",
             f"{b['temp_c']:.1f} °C" if b.get("temp_c") is not None else "",
             # wear: what the gauge now calls full against what it was designed for
             f"{full * 100 / design:.0f} % of design" if full and design else ""]
    return [x for x in parts if x]


def battery_lines(obs):
    """Batteries and external supplies from /api/observations. Observed on its own slow cadence, so the
    age is shown: a battery reading is one firmware call and is not taken every second."""
    data, o = group_data(obs, "battery")
    if data is None:
        return []
    cells, external = data.get("batteries"), data.get("external")
    if not isinstance(cells, list) or not isinstance(external, list) or not (cells or external):
        return []
    out = [f"BATT  {observed(o)}"]
    for b in cells:
        if b.get("present") is False:
            out.append(f"  {b['name']}  not present")
            continue
        charge = f"{b['percent']} %" if b.get("percent") is not None else b.get("capacity_level") or "charge n/a"
        out.append(f"  {b['name']}  " + "  ".join([charge] + battery_readings(b)))
    if external:
        shown = "  ".join(f"{e['name']} {'on' if e['online'] else 'off' if e['online'] is False else 'n/a'}"
                          for e in external)
        out.append(f"  external  {shown}")
    return out


def target_text(t):
    """A probe target as host:port, an IPv6 address in brackets."""
    return f"[{t['address']}]:{t['port']}" if ":" in t["address"] else f"{t['address']}:{t['port']}"


def probe_lines(obs):
    """TCP connect times from /api/observations (not ICMP ping): the latest attempt per target, and the
    failures among its recent attempts."""
    data, o = group_data(obs, "probe")
    items = data.get("targets") if data else None
    if not isinstance(items, list) or not items:
        return []
    out = [f"TCP   connect time, {observed(o)}"]
    for t in items:
        where = target_text(t)
        now = f"{t['connect_ms']:.1f} ms" if t["connect_ms"] is not None else t["reason"]
        out.append(f"  {where}  {now}  failed {t['failures']}/{t['attempts']} recent")
    return out


def collection_note(s):
    """Optional groups that could not read everything ("Collection: temperature partial, gpu error");
    empty when all read fine or are just absent (no GPU, no fans), and for servers without collectors."""
    collectors = s.get("collectors")
    bad = [f"{name} {c['state']}" for name, c in (collectors.items() if isinstance(collectors, dict) else ())
           if name != "core" and isinstance(c, dict) and c.get("state") in ("partial", "error")]
    return "Collection: " + ", ".join(bad) if bad else ""


def render(s, obs=None):
    """The screen for a /api/stats answer s, and the storage lines when obs (an /api/observations answer)
    is given. Plain text: --once and pipes; the server's /text is its copy."""
    system = system_line(s.get("system"))
    lines = [
        f"{s['model']}",
        f"{s['platform']}   " + (f"mode {s['power_mode']}   " if s["power_mode"] else "") + f"up {uptime_text(s['uptime'])}",
        *([system] if system else []),
        *([note] if (note := collection_note(s)) else []),
        "",
    ]
    for c in s["cpu"]:
        freq = f"  {c['freq'] / 1e6:4.0f} MHz" if c["freq"] else ""
        lines.append(f"CPU{c['id']:<3}{bar(c['usage'])} {c['usage']:5.1f} %{freq}")
    note = cpu_note(s)
    if note:
        lines.append(note)
    g = s["gpu"]
    if g:
        freq = f"  {g['freq'] / 1e6:4.0f} MHz" if g.get("freq") else ""  # None or 0: clock not readable
        # a card whose driver publishes no load, or the first reading of a counter: a clock, no share
        load = f"{bar(g['usage'])} {g['usage']:5.1f} %" if g.get("usage") is not None else f"{bar(0)}    n/a"
        lines.append(f"GPU   {load}{freq}")
    n = s.get("npu")
    if n:
        freq = f"  {n['freq'] / 1e6:4.0f} MHz" if n.get("freq") else ""
        load = f"{bar(n['usage'])} {n['usage']:5.1f} %" if n.get("usage") is not None else f"{bar(0)}    n/a"
        lines.append(f"NPU   {load}{freq}")
    m = s["memory"]
    lines.append(f"RAM   {bar(100 * m['used'] / m['total'])} {gib(m['used'])}/{gib(m['total'])}")
    if m["swap_total"]:
        lines.append(f"SWAP  {bar(100 * m['swap_used'] / m['swap_total'])} {gib(m['swap_used'])}/{gib(m['swap_total'])}")
    d = s["disk"]
    lines.append(f"DISK  {bar(100 * d['used'] / d['total'])} {gib(d['used'])}/{gib(d['total'])}")
    lines.append("")
    if s["temperature"]:
        lines.append("TEMP  " + "  ".join(f"{k} {v:.1f} °C" for k, v in s["temperature"].items()))
    if s["power"]:
        lines.append("POWER " + "  ".join(f"{k} {v:.2f}W" for k, v in s["power"].items()))
    for f in s["fans"]:
        vals = [f"{f['rpm']} rpm" if f["rpm"] is not None else "", f"{f['percent']} %" if f["percent"] is not None else ""]
        lines.append(f"FAN   {f['name']}  " + ("  ".join(v for v in vals if v) or "n/a"))  # n/a: unreadable
    extra = counters_lines(s) + storage_lines(obs) + wifi_lines(obs) + battery_lines(obs) + probe_lines(obs)
    if extra and lines[-1]:
        lines.append("")
    return "\n".join(lines + extra)


# ---------------------------------------------------------------------------------------------------------
# Live terminal screen (interactive terminals only). Same data, wording and levels as the web page; no
# requests beyond render()'s; the Overview's busiest interfaces are ranked by the last few answers kept here.

# Copy of frontends/ui-spec.json without "about" (the design tokens shared with the web page), so this file
# stays standalone; tests/frontends/test_ui_spec.py checks it. The terminal adapts them below: views and
# levels as they are, the color roles as the ANSI codes of Style.
UI_SPEC = {
    "views": [
        {"id": "overview", "labels": ["Overview", "Over", "Ovr", "Ov"]},
        {"id": "resources", "labels": ["Resources", "Res", "Res", "Re"]},
        {"id": "network", "labels": ["Network", "Net", "Net", "Ne"]},
        {"id": "storage", "labels": ["Storage", "Store", "Sto", "St"]},
        {"id": "thermal", "labels": ["Temperature", "Temp", "Tmp", "Tm"]},
        {"id": "power", "labels": ["Power & battery", "Power", "Pwr", "Pw"]},
        {"id": "system", "labels": ["System", "Sys", "Sys", "Sy"]},
    ],
    "metrics": [
        {"id": "cpu", "labels": ["CPU", "CPU"], "unit": "%"},
        {"id": "memory", "labels": ["RAM", "RAM"], "unit": "%"},
        {"id": "gpu", "labels": ["GPU", "GPU"], "unit": "%"},
        {"id": "filesystem", "labels": ["Root filesystem", "DISK"], "unit": "%"},
        {"id": "temperature", "labels": ["Temperature", "TEMP"], "unit": "°C"},
    ],
    "columns": {
        "rx": ["↓ Receive", "↓ RX"],
        "tx": ["↑ Send", "↑ TX"],
        "packets": ["Packets/s rx · tx", "PKT/S rx · tx"],
        "errors": ["Errors · drops", "ERR · DROP"],
        "read": ["Read", "READ"],
        "write": ["Write", "WRITE"],
        "ops": ["Ops/s r · w", "OPS/S r · w"],
        "io_time": ["I/O time", "I/O TIME"],
    },
    "formats": {"per_s_decimals": 0, "size_units": ["G", "M", "K"]},
    "system_fields": ["os", "kernel", "arch", "hostname", "l4t"],
    "levels": {
        "names": ["ok", "warn", "crit"],
        "low_is_bad": ["battery", "wifi"],
        "defaults": {"cpu": [80, 90], "gpu": [80, 90], "memory": [80, 90], "filesystem": [80, 90],
                     "temperature": [80, 90], "battery": [20, 10], "wifi": [-70, -80]},
    },
    "scales": {"wifi_dbm": [-90, -30]},
    "states": {"live": "ok", "partial": "warn", "offline": "crit"},
    "roles": {
        "ok": {"light": "#2f7d5b", "dark": "#4fb286", "ansi": "32"},
        "warn": {"light": "#d9a21b", "dark": "#e3b341", "ansi": "33"},
        "crit": {"light": "#c62828", "dark": "#f47067", "ansi": "31"},
        "muted": {"light": "#6b7280", "dark": "#9aa1ab", "ansi": "90"},
        "fg": {"light": "#1d2127", "dark": "#e6e8eb"},
        "bg": {"light": "#f6f7f9", "dark": "#121417"},
    },
}

# The shared defaults; use_levels() replaces them with the server's [thresholds] (/api/status "levels").
# Temperature is left to the server (its board's level); temp_limits() falls back to the generic one.
LIMITS = {**{name: tuple(pair) for name, pair in UI_SPEC["levels"]["defaults"].items()}, "temperature": None}
# Levels where the low reading is the bad one: warning above critical (native/src/config.rs LOW_LEVELS).
LOW = tuple(UI_SPEC["levels"]["low_is_bad"])
LABEL = {m["id"]: m["labels"][1] for m in UI_SPEC["metrics"]}  # the Overview readings' short labels: the terminal's
COL = {key: labels[1] for key, labels in UI_SPEC["columns"].items()}  # the table columns' short labels
PER_S = UI_SPEC["formats"]["per_s_decimals"]  # packets and operations per second
WIDE = 100  # columns from which bars and details are at full length
NARROW = 60  # columns below which the screen gives up decoration first: the name beside the logo, indents, bar length
ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def per_sec(v):
    """Packets or operations per second as the web page writes them: half up, as JavaScript's toFixed does
    (Python's format rounds half to even, so 0.5 would read 0 here and 1 there)."""
    return f"{math.floor(v * 10 ** PER_S + 0.5) / 10 ** PER_S:.{PER_S}f}"


def size(b):
    """Bytes in the unit that fits them, as the web page writes them: 45.1G, 14M, 512K (render() keeps
    gib(), so --once and /text are unchanged)."""
    for unit, power in zip(UI_SPEC["formats"]["size_units"], (30, 20, 10)):
        if b >= 2 ** power:
            return f"{b / 2 ** power:.1f}{unit}" if power == 30 else f"{b / 2 ** power:.0f}{unit}"
    return f"{b:.0f}B" if b else "0"  # none at all needs no unit: "0 / 15.3G"


def temp_limits():
    """The server's temperature level (its board's default unless [thresholds] sets one); the web page's
    generic default when /api/status could not be read."""
    return LIMITS["temperature"] or tuple(UI_SPEC["levels"]["defaults"]["temperature"])


def use_levels(levels):
    """Takes the server's levels ({name: [warning, critical]}, temperature may be null); anything else in
    them, or a server without them (None), keeps the defaults."""
    for name, value in (levels if isinstance(levels, dict) else {}).items():
        if name in LIMITS and (value is None and name == "temperature" or isinstance(value, list) and len(value) == 2
                               and all(isinstance(v, (int, float)) for v in value)
                               and (value[0] > value[1] if name in LOW else value[0] < value[1])):
            LIMITS[name] = tuple(value) if value else None


def level(v, limits):
    """Without limits (a reading [thresholds] does not set levels for) nothing is a warning."""
    return "ok" if not limits else "crit" if v >= limits[1] else "warn" if v >= limits[0] else "ok"


def low_level(v, limits):
    """level() for a reading whose low value is the bad one: at or below a level, not at or above."""
    return "ok" if not limits else "crit" if v <= limits[1] else "warn" if v <= limits[0] else "ok"


def level_of(name, v):
    """A named reading's level against its LIMITS, low or high as UI_SPEC's low_is_bad says (LOW); the only
    caller of low_level."""
    return (low_level if name in LOW else level)(v, LIMITS[name])


def tone(lv):
    """Text color for a level: ok text stays the terminal's own color."""
    return None if lv == "ok" else lv


class Style:
    """Basic ANSI colors (when color is on) and the glyphs: block characters, or ASCII without UTF-8."""
    CODES = {**{role: value["ansi"] for role, value in UI_SPEC["roles"].items() if "ansi" in value},
             "dim": "2", "bold": "1", "under": "4", "crit_bg": "30;41"}

    def __init__(self, color=True, utf8=True):
        self.color = color
        (self.full, self.track, self.tick, self.rule, self.dot, self.cross, self.up, self.gap,
         self.larr, self.rarr, self.uarr, self.darr, self.ell, self.ge) = (
            ("█", "░", "╎", "─", "●", "✕", "▲", "·", "←", "→", "↑", "↓", "…", "≥") if utf8 else
            ("#", "-", "|", "-", "*", "x", "!", ".", "<", ">", "^", "v", "...", ">="))

    def __call__(self, text, *names):
        codes = ";".join(self.CODES[n] for n in names if n)
        return f"\033[{codes}m{text}\033[0m" if self.color and codes and text else text


def vlen(text):
    return len(ANSI.sub("", text))


def pad(text, width, right=False):
    gap = " " * max(0, width - vlen(text))
    return gap + text if right else text + gap


def fit(line, width):
    """line cut to width visible columns; escapes are kept, colors reset after a cut."""
    if vlen(line) <= width:
        return line
    out, n = [], 0
    for part in re.split(f"({ANSI.pattern})", line):
        if ANSI.fullmatch(part):
            out.append(part)
        elif n < width:
            out.append(part[:width - n])
            n += len(out[-1])
    return "".join(out) + ("\033[0m" if "\033" in line else "")


def clip(st, line, width):
    """line cut to width visible columns with an ellipsis where it was cut, so a shortened reading says so."""
    return line if vlen(line) <= width else fit(line, max(0, width - len(st.ell))) + st(st.ell, "muted")


def reading(st, v, unit, lv):
    """A value and its unit in fixed columns ("  9.6 %  ", " 86.2 °C"): the decimal point stays put as the
    value changes, so the bars after it never move."""
    return st(f"{v:5.1f} {unit:<2}", "bold", tone(lv))


def spread(left, right, width):
    return left + " " * max(1, width - vlen(left) - vlen(right)) + right


def meter(st, pct, width, limits=None, ticks=True, lv=None):
    """A bar on a 0–100 scale: grey while its level is ok and in the level's color from warning on, so a
    quiet screen makes the one bar that needs attention stand out; the empty part is a step darker. limits
    also mark the warning and critical points. lv, when given, is the level to color it with (a
    low-is-bad reading's, see low_level)."""
    pct = max(0.0, min(float(pct), 100.0))
    n = round(width * pct / 100)
    marks = {min(width - 1, int(width * t / 100)) for t in limits} if limits and ticks else set()
    track = "".join(st.tick if i in marks else st.track for i in range(n, width))
    lv = lv or (level(pct, limits) if limits else "ok")
    return st(st.full * n, tone(lv) or "muted") + st(track, "muted", "dim")


def metrics(s):
    """The web Overview's readings: (label, value or None, unit, limits, detail)."""
    cpus = [c["usage"] for c in s["cpu"] if isinstance(c.get("usage"), (int, float))]
    m, d, g, n = s["memory"], s["disk"], s.get("gpu"), s.get("npu")
    out = [(LABEL["cpu"], sum(cpus) / len(cpus) if cpus else None, "%", LIMITS["cpu"], f"mean of {len(cpus)} cores"),
           (LABEL["memory"], 100 * m["used"] / m["total"] if m["total"] else None, "%", LIMITS["memory"],
            f"{size(m['used'])} / {size(m['total'])}")]  # the web page's card order: CPU, RAM, GPU, disk
    if g:
        out.append((LABEL["gpu"], g["usage"], "%", LIMITS["gpu"], f"{g['freq'] / 1e6:.0f} MHz" if g.get("freq") else ""))
    if n:  # no [thresholds] entry sets levels for an NPU, so it is shown without one
        out.append(("NPU", n["usage"], "%", None, f"{n['freq'] / 1e6:.0f} MHz" if n.get("freq") else ""))
    out.append((LABEL["filesystem"], 100 * d["used"] / d["total"] if d["total"] else None, "%", LIMITS["filesystem"],
                f"{size(d['used'])} / {size(d['total'])}"))
    temps = hottest_first(s)
    if temps:
        out.append((LABEL["temperature"], temps[0][1], "°C", temp_limits(), f"{temps[0][0]} · hottest of {len(temps)}"))
    return out


def hottest_first(s):
    return sorted(((k, v) for k, v in (s.get("temperature") or {}).items() if isinstance(v, (int, float))),
                  key=lambda kv: -kv[1])


def busiest_first(interfaces, traffic=None):
    """Interfaces by traffic (rx + tx), those without rates last; otherwise in the server's order. traffic:
    {name: recent mean of rx + tx} from the live view, so two interfaces with similar traffic do not swap
    places every second; the current rates when there is none."""
    def busy(i):
        r = i.get("rates")
        if not r:
            return 1
        return -(traffic[i["name"]] if traffic and i["name"] in traffic else r["rx_bytes_per_s"] + r["tx_bytes_per_s"])
    return sorted(interfaces, key=busy)


def fs_pct(f):
    return 100 * f["used_bytes"] / f["total_bytes"] if f.get("total_bytes") else None


def attention(s, obs, views=False):
    """Readings at their warning level or worse, critical first, as on the web page: the Overview readings,
    every temperature sensor, and from current observations filesystems other than /, the batteries' charge
    and the connected Wi-Fi signals, where the low reading is the bad one. views: each entry also names the
    view that shows it (the tab the bar marks, the web page's link)."""
    where = {LABEL["cpu"]: "resources", LABEL["memory"]: "resources", LABEL["gpu"]: "resources",
             LABEL["filesystem"]: "storage"}
    out = [("/" if label == LABEL["filesystem"] else label, f"{v:.1f} {unit}", level(v, lim), where.get(label, "resources"))
           for label, v, unit, lim, _ in metrics(s) if v is not None and label != LABEL["temperature"]]
    out += [(k, f"{v:.1f} °C", level(v, temp_limits()), "thermal") for k, v in hottest_first(s)]
    data, o = group_data(obs, "storage")
    for f in (data or {}).get("filesystems") or [] if o and not o.get("stale") else []:
        pct = fs_pct(f)
        if pct is not None and not any(p["path"] == "/" for p in f["mount_points"]):
            out.append((f["mount_points"][0]["path"], f"{pct:.0f} %", level(pct, LIMITS["filesystem"]), "storage"))
    out += low_attention(obs)
    out = sorted((a for a in out if a[2] != "ok"), key=lambda a: a[2] != "crit")
    return out if views else [a[:3] for a in out]


def low_attention(obs):
    """(name, value, level, view) of the batteries' charge and connected Wi-Fi signals; a stale observation
    is not a current reading, so it raises nothing."""
    out = []
    battery, o = group_data(obs, "battery")
    for b in (battery or {}).get("batteries") or [] if o and not o.get("stale") else []:
        if b.get("present") is not False and isinstance(b.get("percent"), (int, float)):
            out.append((b["name"], f"{b['percent']} %", level_of("battery", b["percent"]), "power"))
    wifi, o = group_data(obs, "wifi")
    for i in (wifi or {}).get("interfaces") or [] if o and not o.get("stale") else []:
        if i.get("connected") is True and isinstance(i.get("signal_dbm"), (int, float)):
            out.append((i["name"], f"{i['signal_dbm']} dBm", level_of("wifi", i["signal_dbm"]), "network"))
    return out


# The views of the live screen, one at a time, as the web page's tabs: the same data, wording and levels.
# Overview is the whole screen as before; the detail views show their own kind in full, so a narrow
# terminal reaches every reading by switching views instead of being enlarged.
VIEWS = tuple(view["id"] for view in UI_SPEC["views"])
# Label sets from the widest that fits; the web page shortens its tab labels on narrow screens too.
VIEW_LABELS = {view["id"]: tuple(view["labels"]) for view in UI_SPEC["views"]}


def next_view(keys, view):
    """The view a key press selects: 1-7 directly, Tab or the right arrow the next, Shift-Tab or the left
    arrow the previous; any other key keeps the view. Escape sequences carry digits of their own, so a
    key press is only read as a number when it is not one."""
    index = VIEWS.index(view)
    if "\033" not in keys:
        for number, name in enumerate(VIEWS, 1):
            if str(number) in keys:
                return name
    finals = re.findall(r"\033\[[0-9;?]*([A-Za-z])", keys)  # the key, whatever modifier it carries
    if "Z" in finals or "D" in finals:  # Shift-Tab, left arrow (with any modifier)
        return VIEWS[index - 1]
    if "C" in finals or "\t" in keys:  # right arrow, Tab
        return VIEWS[(index + 1) % len(VIEWS)]
    return view


KEYS = re.compile(r"\033(?:\[([0-9;?]*)([A-Za-z~])|O([A-Za-z]))")  # CSI and SS3: the arrows and PgUp/PgDn
BOTTOM = 1 << 30  # End: further down than any body, which screen() clamps to the last line


def next_offset(keys, offset, page):
    """Where the body is scrolled to after a key press: the up and down arrows a line, PgUp and PgDn a
    page, Home and End its two ends. Other keys leave it alone; screen() clamps what comes back, so a
    key held down cannot run the offset away past the end."""
    for params, final, ss3 in KEYS.findall(keys):
        key = final or ss3
        if key == "A":
            offset -= 1
        elif key == "B":
            offset += 1
        elif key == "~" and params == "5":
            offset -= page
        elif key == "~" and params == "6":
            offset += page
        elif key in ("H", "h") or (key == "~" and params == "1"):
            offset = 0
        elif key in ("F", "f") or (key == "~" and params == "4"):
            offset = BOTTOM
    return max(0, offset)


def tabbar(st, width, view, marks=None):
    """The views on one line, the active one marked, in the header's rule line: the widest label set that
    fits, and the position alone when even the shortest does not. marks: {view: level} of the views whose
    readings need attention, drawn with the attention sign in their level's color, so the tab says where
    to look. Without color the active label is bracketed, so it stays visible in a plain terminal."""
    marks = marks or {}
    for size, gap in enumerate(("   ", "   ", " ", " ")):  # the shortest labels also give up their spacing
        cells = []
        for n, name in enumerate(VIEWS, 1):
            text = f"{st.up if name in marks else ''}{n} {VIEW_LABELS[name][size]}"
            if name == view:
                cells.append(st(text if st.color else f"[{text}]", "ok", "bold", "under"))
            else:
                cells.append(st(text, marks[name], "bold") if name in marks else st(text, "muted"))
        line = "  " + gap.join(cells)
        if vlen(line) <= width:
            return line
    return st(f"  {VIEWS.index(view) + 1}/{len(VIEWS)} {VIEW_LABELS[view][2]} {st.larr}/{st.rarr}", "ok", "bold")


def core_cell(st, c, clock=True):
    """One core as the Resources view draws it: id, bar, usage and, unless every core runs at the same
    clock (then the title says it once), its clock."""
    lv = tone(level(c["usage"], LIMITS["cpu"]))
    freq = f"{c['freq'] / 1e6:4.0f}" if c.get("freq") else "    "
    return (st(f"{c['id']:>4} ", lv or "muted", "bold" if lv else None) + meter(st, c["usage"], 8, LIMITS["cpu"], False)
            + st(f" {c['usage']:5.1f} %", lv, "bold" if lv else None) + (st(f" {freq}", "muted") if clock else ""))


def mhz(hz):
    """"918 MHz" for a frequency in hertz; empty when the clock is not readable (None or 0)."""
    return f"{hz / 1e6:.0f} MHz" if hz else ""


def detail_widths(width, items, columns):
    """Column widths for a detail table: the name column holds the longest name (interfaces like
    enx80691aa55882 are long), the value columns a narrow terminal cannot hold fall off the right, and
    where even one value column leaves the names less than they need, the names are the ones cut."""
    names = 4 + max([len(i["name"]) for i in items] + [6]) + 2
    while len(columns) > 1 and names + sum(columns) > width:
        columns = columns[:-1]
    return [max(8, min(width - sum(columns), max(names, 28)))] + columns


def joined(width, parts):
    """The parts that fit in width columns, in order: a narrow terminal keeps the first ones whole rather
    than cutting the next one in half."""
    line = ""
    for part in parts:
        if vlen(line) + vlen(part) > width:
            break
        line += part
    return line


class View:
    """What a view body draws with: the data, the terminal width and the style, plus the layout helpers
    the sections share."""

    def __init__(self, s, obs, width, st, status):
        self.s, self.obs, self.width, self.st, self.status = s, obs, width, st, status
        self.wide = width >= WIDE
        self.bar = 20 if self.wide else 10 if width >= NARROW else 8  # bars are the first to shrink

    def title(self, name, right=""):
        left = self.st(f"  {name}", "muted", "bold")
        if not right or len(name) + len(right) + 5 > self.width:  # no room for both: the name alone
            return left
        return spread(left, self.st(right + " ", "muted"), self.width)

    def kv(self, label, text):
        return self.st(f"  {label:<10}", "muted", "bold") + text

    def row(self, widths, cells, tones):
        """One table line; cells and tones past the widths a narrow terminal kept are left out, and a cell
        too long for its column is cut there rather than at the edge of the screen."""
        return "".join(self.st(pad(c[:w], w, i > 0), *(t or "").split())
                       for i, (c, w, t) in enumerate(zip(cells, widths, tones)))

    def no_rate(self, widths, name, item):
        return self.st(pad(f"    {name}", widths[0])
                       + RATE_REASONS.get(item.get("reason"), str(item.get("reason"))), "muted")

    def net_row(self, widths, i):
        """An interface's line of the network table: its rates, or why it has none."""
        r = i["rates"]
        if not r:
            return self.no_rate(widths, i["name"], i)
        err = (i.get("rx") or {}).get("errors", 0) + (i.get("tx") or {}).get("errors", 0)
        drop = (i.get("rx") or {}).get("dropped", 0) + (i.get("tx") or {}).get("dropped", 0)
        idle = not r["rx_bytes_per_s"] and not r["tx_bytes_per_s"]
        return self.row(widths, [f"    {i['name']}", per_s(r["rx_bytes_per_s"]), per_s(r["tx_bytes_per_s"]),
                                 f"{per_sec(r['rx_packets_per_s'])} · {per_sec(r['tx_packets_per_s'])}", f"{err} · {drop}"],
                        ["muted"] * 5 if idle else [None, None, None, "muted", "warn" if err or drop else "muted"])

    def disk_rows(self, widths, disks):
        """The disk I/O table's lines: at most MAX_ROWS disks, then how many were left out."""
        out = []
        for d in disks[:MAX_ROWS]:
            r = d["rates"]
            if not r:
                out.append(self.no_rate(widths, d["name"], d))
                continue
            idle = not r["read_bytes_per_s"] and not r["write_bytes_per_s"]
            out.append(self.row(widths, [f"    {d['name']}", per_s(r["read_bytes_per_s"]), per_s(r["write_bytes_per_s"]),
                                         f"{per_sec(r['reads_per_s'])} · {per_sec(r['writes_per_s'])}",
                                         f"{100 * r['io_time_ratio']:.1f} %"],
                                ["muted"] * 5 if idle else [None, None, None, "muted", None]))
        if len(disks) > MAX_ROWS:
            out.append(self.st(f"    +{len(disks) - MAX_ROWS} more disks (all in /api/stats)", "muted"))
        return out

    def meter_row(self, label, pct, limits, detail, label_w=6, indent=4, unit="%"):
        """A reading as the Overview draws it: label, value, bar, detail. A narrow terminal shortens the bar
        first and then cuts the detail at the edge, marked; it never drops the detail whole."""
        head = self.st(" " * indent + f"{label:<{label_w}}", "muted")
        if pct is None:
            return head + self.st("unavailable", "muted")
        lv = level(pct, limits) if limits else "ok"
        return clip(self.st, head + reading(self.st, pct, unit, lv) + " " + meter(self.st, pct, self.bar, limits)
                    + (self.st(f"  {detail}", "muted") if detail else ""), self.width)

    def empty(self, text):
        return [self.st(f"  {text}", "muted")]


def body_overview(v):
    """The web Overview, line for line: the five readings (CPU with its busiest core) and the busiest
    interfaces. No graphs and nothing else: every other reading is in its view (the tabs say which one needs
    attention), so the Overview fits 80x24 and a phone without scrolling."""
    s, st, width = v.s, v.st, v.width
    body = []
    cpus = [c for c in s["cpu"] if isinstance(c.get("usage"), (int, float))]
    for label, value, unit, lim, detail in metrics(s):
        if label == "NPU":  # no level and not on the web Overview: Resources shows it
            continue
        if label == LABEL["cpu"] and cpus:
            busy = max(cpus, key=lambda c: c["usage"])
            lv = tone(level(busy["usage"], lim))
            more = sum(1 for c in cpus if c is not busy and level(c["usage"], lim) != "ok")
            detail = (st(f"max CPU{busy['id']} {busy['usage']:.1f} %", lv or "muted", "bold" if lv else None)
                      + (st(f"  +{more} {st.ge}{lim[0]:g} %", "muted") if more else ""))
        else:
            detail = st(detail, "muted")
        body.append(v.meter_row(label, value, lim, "", indent=2, unit=unit) if value is None else
                    clip(st, st(f"  {label:<6}", "muted") + reading(st, value, unit, level(value, lim)) + " "
                         + meter(st, value, v.bar, lim) + "  " + detail, width))
    if cpu_note(s):
        body.append(st(f"  {cpu_note(s)}", "muted"))
    body.append("")

    net = s.get("network") if isinstance(s.get("network"), dict) else {}
    ifaces = [i for i in busiest_first(net.get("interfaces") if isinstance(net.get("interfaces"), list) else [],
                                       v.status.get("traffic")) if i["name"] != "lo"]
    if ifaces:
        def block(i):  # the rates in fixed columns, so a changing value never moves the next interface
            r = i.get("rates")
            if not r:
                return st(f"{i['name']} " + RATE_REASONS.get(i.get("reason"), str(i.get("reason"))), "muted")
            idle = not r["rx_bytes_per_s"] and not r["tx_bytes_per_s"]
            text = (f"{i['name']} " + st(st.darr, "muted") + pad(per_s(r["rx_bytes_per_s"]), 11, True)
                    + "  " + st(st.uarr, "muted") + pad(per_s(r["tx_bytes_per_s"]), 11, True))
            return st(ANSI.sub("", text), "muted", "dim") if idle else text

        line, shown = st("  NET   ", "muted"), 0
        for i in ifaces:
            rest = len(ifaces) - shown - 1
            candidate = line + ("    " if shown else "") + block(i)
            if shown and vlen(candidate) + (len(f"   +{rest}") if rest else 0) > width:
                break
            line, shown = candidate, shown + 1
        if shown < len(ifaces):
            line += st(f"   +{len(ifaces) - shown}", "muted")
        body.append(clip(st, line, width))
    return body


def body_resources(v):
    """The web Resources tab: every measured core with its clock, the accelerators, memory with swap, and
    resource pressure. Nothing is dropped for a narrow terminal; the cores wrap into fewer columns."""
    s, st = v.s, v.st
    body = []
    if s["cpu"]:
        clocks = {c.get("freq") for c in s["cpu"]}
        same = len(clocks) == 1 and None not in clocks and 0 not in clocks  # the same value twelve times is said once
        body.append(v.title("CPU CORES", f"{len(s['cpu'])} measured · all at {mhz(clocks.pop())}" if same
                            else f"%  ·  MHz  ·  {len(s['cpu'])} measured"))
        cells = [core_cell(st, c, clock=not same) for c in s["cpu"]]
        cols = max(1, (v.width + 3) // (23 if same else 28))
        body += ["   ".join(cells[i:i + cols]) for i in range(0, len(cells), cols)]
    if cpu_note(s):
        body.append(st(f"  {cpu_note(s)}", "muted"))

    g, n = s.get("gpu"), s.get("npu")
    if g or n:
        body += ["", v.title("ACCELERATORS")]
        if g:  # a card whose driver publishes no load has a clock and no share
            body.append(v.meter_row("GPU", g.get("usage"), LIMITS["gpu"],
                                    f"{g['freq'] / 1e6:.0f} / {mhz(g['max_freq'])}" if g.get("freq") and g.get("max_freq")
                                    else mhz(g.get("freq"))))
        if n:  # no [thresholds] entry sets levels for an NPU, so it is shown without one
            body.append(v.meter_row("NPU", n.get("usage"), None, mhz(n.get("freq"))))

    m = s["memory"]
    body += ["", v.title("MEMORY")]
    body.append(v.meter_row("RAM", 100 * m["used"] / m["total"] if m["total"] else None, LIMITS["memory"],
                            f"{size(m['used'])} / {size(m['total'])} · {size(m['total'] - m['used'])} free"))
    if m["swap_total"]:  # the web page gives swap the memory levels too
        body.append(v.meter_row("SWAP", 100 * m["swap_used"] / m["swap_total"], LIMITS["memory"],
                                f"{size(m['swap_used'])} / {size(m['swap_total'])}"))

    psi = s.get("pressure")
    res = psi.get("resources") if isinstance(psi, dict) else None
    if isinstance(res, dict) and res:
        body += ["", v.title("RESOURCE PRESSURE", "some / full · avg10, time stalled")]
        for name, r in res.items():
            full = f" / {r['full']['avg10']:.1f}" if r.get("full") else ""
            body.append(st(f"    {name:<10}", "muted") + f"{r['some']['avg10']:.1f}{full} %")
    return body


def body_network(v):
    """The web Network tab: every interface this process's namespace has, then the Wi-Fi signal and the TCP
    connect times from the slower observations."""
    s, obs, st = v.s, v.obs, v.st
    net = s.get("network") if isinstance(s.get("network"), dict) else {}
    # the server's order, loopback last: a row keeps its place whatever the traffic does
    ifaces = sorted(net.get("interfaces") if isinstance(net.get("interfaces"), list) else [], key=lambda i: i["name"] == "lo")
    wifi, wifi_o = group_data(obs, "wifi")
    probe, probe_o = group_data(obs, "probe")
    wlans = wifi.get("interfaces") if isinstance(wifi, dict) else None
    targets = probe.get("targets") if isinstance(probe, dict) else None
    if not ifaces and not wlans and not targets:
        return v.empty("No interface, Wi-Fi or TCP connect readings · they appear here when collected")

    body = []
    if ifaces:
        shown = ifaces[:MAX_ROWS]
        widths = detail_widths(v.width, shown, [13, 13, 17, 12])
        body.append(v.row(widths, ["  INTERFACES", COL["rx"], COL["tx"], COL["packets"], COL["errors"]],
                          ["muted bold"] + ["muted"] * 4))
        body += [v.net_row(widths, i) for i in shown]
        if len(ifaces) > len(shown):
            body.append(st(f"    +{len(ifaces) - len(shown)} more interfaces (all in /api/stats)", "muted"))
        body.append(st(pad("this netns · err/drop = totals", min(v.width, max(sum(widths), 32)), True), "muted"))

    if wlans:
        body += ["", v.title("WI-FI", stale_text(wifi_o))]
        for i in wlans:
            if i.get("connected") is not True:  # never the last signal of a connection that ended
                state = "not connected" if i.get("connected") is False else i.get("connection_state", "unknown")
                body.append(st(f"    {i['name']:<8}  {state}", "muted"))
                continue
            signal, extra = wifi_signal(i)
            if i["signal_dbm"] is None:  # a value without dBm is shown without a bar, as on the web page
                body.append(joined(v.width, [st(f"    {i['name']:<8}", "muted"), signal]
                                   + [st(f"   {e}", "muted") for e in extra]))
                continue
            lv = level_of("wifi", i["signal_dbm"])
            weak, strong = UI_SPEC["scales"]["wifi_dbm"]  # the web page's bar scale
            body.append(joined(v.width, [st(f"    {i['name']:<8}", "muted"), st(pad(signal, 9), tone(lv), "bold"),
                                         " " + meter(st, 100 * (i["signal_dbm"] - weak) / (strong - weak),
                                                     20 if v.wide else 11, ticks=False, lv=lv)]
                               + [st(f"   {e}", "muted") for e in extra]))

    if targets:
        body += ["", v.title("TCP CONNECT", stale_text(probe_o))]
        where_w = max(len(target_text(t)) for t in targets) + 2
        for t in targets:
            where = target_text(t)
            now = f"{t['connect_ms']:.1f} ms" if t["connect_ms"] is not None else str(t["reason"])
            body.append(joined(v.width, [st(pad(f"    {where}", 4 + where_w), "muted"), pad(now, 10, True),
                                         st(f"   failed {t['failures']}/{t['attempts']} recent",
                                            "warn" if t["failures"] else "muted")]))
    return body


def body_storage(v):
    """The web Storage tab: every filesystem the storage observation found, its disks' partition counts,
    and the per-disk I/O rates."""
    s, obs, st = v.s, v.obs, v.st
    data, o = group_data(obs, "storage")
    dio = s.get("disk_io") if isinstance(s.get("disk_io"), dict) else {}
    disks = dio.get("disks") if isinstance(dio.get("disks"), list) else []
    fss = (data or {}).get("filesystems") or []
    parts = (data or {}).get("partitions") or []
    if not fss and not parts and not disks:
        return v.empty("No filesystem or disk I/O readings · they appear here when collected")

    body = []
    if fss:
        body.append(v.title("FILESYSTEMS", stale_text(o)))
        path_w = max(len(f["mount_points"][0]["path"]) for f in fss) + 2
        cap_w = max(len(f"{size(f['used_bytes'])} / {size(f['total_bytes'])}") for f in fss
                    if f["total_bytes"] is not None) if any(f["total_bytes"] is not None for f in fss) else 0
        bw = min(20 if v.wide else 11, max(6, v.width - 20 - path_w))
        for f in fss:
            m, pct = f["mount_points"], fs_pct(f)
            where = pad(m[0]["path"], path_w) + ("(ro) " if m[0]["read_only"] else "")
            more = st(f"+{len(m) - 1} ", "muted") if len(m) > 1 else ""  # bind mounts can be dozens
            node = f"{f['major']}:{f['minor']}"  # a filesystem with no device name
            dev = st(f"   {f['fstype']} {f['device'] or node}", "muted")
            if pct is None:
                body.append(joined(v.width, [st(f"    {where}", "muted"), more,
                                             st("capacity unknown", "muted"), dev]))
                continue
            body.append(joined(v.width, [
                st(f"    {where}", "muted"), more, meter(st, pct, bw, LIMITS["filesystem"]),
                st(pad(f"{pct:.0f} %", 7, True), tone(level(pct, LIMITS["filesystem"]))),
                "   " + pad(f"{size(f['used_bytes'])} / {size(f['total_bytes'])}", cap_w),
                st(f"   {size(f['available_bytes'])} free", "muted"), dev]))

    if parts:
        by_disk = {}
        for p in parts:
            by_disk.setdefault(p["disk"], []).append(p)
        body += ["", v.title("PARTITIONS")]
        for disk, items in by_disk.items():
            mounted = sum(1 for p in items if p["mount_points"])
            body.append(st(f"    {disk:<12}", "muted")
                        + f"{len(items)} partition{'s' if len(items) != 1 else ''}, {mounted} mounted")

    if disks:
        widths = detail_widths(v.width, disks, [13, 13, 17, 12])
        body += ["", v.row(widths, ["  DISK I/O", COL["read"], COL["write"], COL["ops"], COL["io_time"]],
                           ["muted bold"] + ["muted"] * 4)]
        body += v.disk_rows(widths, disks)
        body.append(st(pad("I/O time = share of window with I/O in flight", min(v.width, max(sum(widths), 46)), True), "muted"))
    return body


def body_thermal(v):
    """The web Temperature tab: every sensor on the same 0-100 °C scale as the web page's bars, in the
    server's order, so a sensor keeps its line as temperatures cross; the hot ones carry their level's color
    (and the tab its mark). Power rails, fans and the batteries are their own view, so a host with dozens of
    channels here does not push what it draws and what it has left off the screen."""
    s, st = v.s, v.st
    temps = [(k, t) for k, t in (s.get("temperature") or {}).items() if isinstance(t, (int, float))]
    limits = temp_limits()
    if not temps:
        return v.empty("No temperature readings · this board reports none")

    body = [v.title("TEMPERATURE", f"0-100 °C · warning {limits[0]:g} · critical {limits[1]:g}" if limits
                    else "0-100 °C")]
    name_w = max(len(k) for k, _ in temps) + 2  # sensor names can be long ("coretemp Package id 0")
    bw = min(20, max(6, v.width - 13 - name_w))  # nothing follows the bar here, so it takes the room there is
    for k, t in temps:
        lv = tone(level(t, limits))
        body.append(clip(st, st(f"    {k:<{name_w}}", lv or "muted", "bold" if lv else None)
                         + reading(st, t, "°C", level(t, limits)) + " " + meter(st, t, bw, limits), v.width))
    return body


def body_power(v):
    """The web Power & battery tab: what the board draws on its rails, how it is cooled, its power mode,
    and the charge it has left. Measured together because that is how consumption is read: the draw over
    a window against the charge the window cost."""
    s, obs, st = v.s, v.obs, v.st
    power = list((s.get("power") or {}).items())
    fans = s.get("fans") or []
    battery, battery_o = group_data(obs, "battery")
    cells = (battery.get("batteries") or []) if battery else []
    supplies = (battery.get("external") or []) if battery else []
    if not power and not fans and not (cells or supplies):
        return v.empty("No power rail, fan or battery readings · this board reports none")

    body = []
    if power:
        mode = f"mode {s['power_mode']}" if s.get("power_mode") else ""
        body.append(v.title("POWER RAILS", mode))
        name_w = max([len(k) for k, _ in power] + [len("sum of rails")]) + 2
        for k, p in power:
            body.append(st(f"    {k:<{name_w}}", "muted") + f"{p:6.2f} W")
        if len(power) > 1:  # the last line in bold is the total, as a power meter reads
            body.append(st("    " + st.rule * (name_w + 8), "muted"))
            body.append(st(f"    {'sum of rails':<{name_w}}", "muted") + st(f"{sum(p for _, p in power):6.2f} W", "bold"))
    if fans:
        body += ([""] if body else []) + [v.title("FANS", "" if power else (f"mode {s['power_mode']}" if s.get("power_mode") else ""))]
        name_w = max(len(f["name"]) for f in fans) + 2
        for f in fans:
            if f["percent"] is not None:
                rpm = st(f"  {f['rpm']} rpm", "muted") if f["rpm"] is not None else ""
                body.append(clip(st, st(f"    {f['name']:<{name_w}}", "muted") + st(f"{f['percent']:3.0f} %  ", "bold")
                                 + meter(st, f["percent"], v.bar) + rpm, v.width))
            else:
                body.append(st(f"    {f['name']:<{name_w}}", "muted") + (f"{f['rpm']} rpm" if f["rpm"] is not None else "n/a"))
    if cells or supplies:
        # the group's own cadence, so its age is its caption, as for the other observations
        body += ([""] if body else []) + [v.title("BATTERY", stale_text(battery_o))]
        name_w = max([len(b["name"]) for b in cells] + [0]) + 2
        for b in cells:
            if b.get("present") is False:
                body.append(st(f"    {b['name']:<{name_w}}", "muted") + "not present")
                continue
            charge = (f"{b['percent']} %" if b.get("percent") is not None
                      else b.get("capacity_level") or "charge n/a")
            detail = " · ".join(battery_readings(b))
            lv = level_of("battery", b["percent"]) if b.get("percent") is not None else "ok"
            head = [st(f"    {b['name']:<{name_w}}", "muted"), st(pad(charge, 7), tone(lv) or "bold", "bold" if tone(lv) else None)]
            bw = min(14 if v.wide else 8, max(6, v.width - 10 - name_w - vlen(charge) - vlen(detail)))
            bar = ["  " + meter(st, b["percent"], bw, LIMITS["battery"], lv=lv)] if b.get("percent") is not None else []
            # the readings come before the bar, so a narrow terminal gives up the bar, never a reading;
            # where even that is not enough they wrap onto their own lines rather than being cut
            inline = vlen("".join(head)) + vlen(detail) + 2 <= v.width
            body.append(joined(v.width, head + ([st(detail, "muted")] if detail and inline else []) + bar))
            if detail and not inline:
                for part in textwrap.wrap(detail, max(12, v.width - 6)):
                    body.append(st("      " + part, "muted"))
        if supplies:  # one line each: a long driver name must not cut the next supply off the screen
            sup_w = max(len(e["name"]) for e in supplies) + 2
            for e in supplies:
                state = "on" if e["online"] else "off" if e["online"] is False else "n/a"
                body.append(joined(v.width, [st(f"    {e['name']:<{sup_w}}", "muted"), state,
                                             st(f"   {e['type']} power", "muted")]))
    return body


def body_system(v):
    """The web System tab: the platform, uptime and whatever the board reported about itself, then this
    client: its version and the service it reads, which the header and footer leave out. The power mode
    stays in Power & battery, as on the web page."""
    s, st = v.s, v.st
    system = s.get("system") if isinstance(s.get("system"), dict) else {}
    rows = [("Model", s["model"]), ("Platform", s["platform"]), ("Uptime", uptime_text(s["uptime"]))]
    rows += [(label, system[key]) for key, label in SYSTEM_ROWS if system.get(key)]
    # Board-specific fields keep their own key, as the web page adds them without rebuilding its rows.
    rows += [(key, value) for key, value in system.items() if value and key not in dict(SYSTEM_ROWS)]
    own = [("Version", VERSION), ("Service", f"{v.status['addr']} · every {v.status['interval']:g} s")]
    label_w = max(len(label) for label, _ in rows + own) + 3
    return ([st(f"  {label:<{label_w}}", "muted") + str(value) for label, value in rows]
            + ["", v.title("PLATMON")] + [st(f"  {label:<{label_w}}", "muted") + value for label, value in own])


# The web page's System rows, in its order; anything else the board sent follows under its own key.
SYSTEM_ROWS = (("os", "OS"), ("kernel", "Kernel"), ("arch", "Architecture"), ("hostname", "Hostname"),
               ("l4t", "L4T"))
BODIES = {"overview": body_overview, "resources": body_resources, "network": body_network,
          "storage": body_storage, "thermal": body_thermal, "power": body_power, "system": body_system}


def screen(s, obs, width, height, st, status, view="overview", offset=0):
    """The live view as lines of at most width columns and at most height lines: the header, the tabs, the
    status slot, the view's body and the footer, each where it always is.
    status: state (live, partial, offline), age (seconds), error (explain() text or None), addr, interval,
    and traffic ({interface: recent rx + tx}, optional) for the Overview's busiest interfaces.
    view: which of VIEWS to draw; the tab bar names the others and marks those that need attention.
    offset: how far down the view's body to start, clamped here to what there is to show. The clamped
    offset and the page size go back in status["scroll"], so the caller scrolls by what was drawn."""
    wide, narrow = width >= WIDE, width < NARROW
    sys_ = s.get("system") or {}

    state = status["state"]
    color = UI_SPEC["states"][state]
    # The age only when it says something: older than two updates, or the last data of a lost service.
    # A number that changes every second and means "fine" is noise next to the readings.
    stale = state == "offline" or status["age"] > 2 * status["interval"]
    name = s["model"] if wide else sys_.get("hostname") or s["model"]
    meta = ([sys_.get("hostname")] if wide else [s["platform"]]) + [s["power_mode"], f"up {uptime_text(s['uptime'])}"]
    alerts = attention(s, obs, views=True)
    marks = {}
    for *_, lv, where in alerts:
        marks.setdefault(where, lv)  # critical entries come first
    head = [spread(st(" p_", "ok", "bold") + ("  " if narrow else st(" platmon", "bold") + "   ") + st(name, "bold"),
                   st(f"{st.dot} {state.upper()}", color) + (st(f"  data {status['age']:.0f} s old", "warn") if stale else "")
                   + " ", width),
            st((" " * (4 if narrow else 13)) + " · ".join(str(x) for x in meta if x), "muted"),
            tabbar(st, width, view, marks)]

    slot = status_slot(st, width, status, alerts, collection_note(s))

    body = BODIES[view](View(s, obs, width, st, status))
    while body and not body[-1]:
        body.pop()
    if status.get("error"):  # the last data, dimmed: shown as what it is, not as current
        body = [st(ANSI.sub("", line), "dim") for line in body]

    local = status["addr"].rsplit(":", 1)[0].strip("[]") in ("localhost", "127.0.0.1", "::1")
    where = "" if local else f"{status['addr']} · "  # this device needs no address: the header names it
    keys = (st("  ") + st("q", "bold") + st(" quit", "muted")
            + (st(" · ", "muted") + st("1-7", "bold") + st(" view", "muted") if width >= 36 else "")
            + (st(" · ", "muted") + st(f"{st.uarr}/{st.darr}", "bold") + st(" scroll", "muted") if width >= 80 else ""))
    foot = [st(st.rule * width, "muted"),
            spread(keys, st(f"{where}every {status['interval']:g} s " if wide else f"{where}{status['interval']:g} s ", "muted"), width)]
    room = max(1, height - len(head) - len(slot) - len(foot))
    status["scroll"] = (0, max(1, room - 1))
    if len(body) > room:  # the last line says what is out of sight and how to reach it
        page = room - 1
        last = len(body) - page
        offset = max(0, min(offset, last))
        above, below = offset, last - offset
        status["scroll"] = (offset, max(1, page))
        shown = [f"{st.uarr} {above} above" if above else "", f"{st.darr} {below} below" if below else ""]
        body = body[offset:offset + page] + [
            joined(width, [st("  " + " · ".join(x for x in shown if x), "muted"),
                           st(f"  ·  {st.uarr}{st.darr} PgUp/PgDn Home/End scroll", "muted")])]
    # A terminal too short for the header, one line and the footer still gets only the lines it has:
    # drawing past the last row scrolls the screen and leaves the frame in pieces.
    return [fit(line, width) for line in (head + slot + body + foot)[:max(height, 1)]]


def status_slot(st, width, status, alerts, note):
    """The two lines under the tabs, on every view and always there, so nothing below them moves when they
    fill or empty: a lost service, else a collection that read only part of what it should, else the
    readings that need attention, else nothing. The lost service keeps every word of what failed and what
    to do (an errno, a script name), so it may take more lines; it is the one case where the body moves,
    and the body is old data then anyway."""
    if status.get("error"):
        first, *rest = status["error"].split("\n")
        lead = f"  {st.cross} NOT CURRENT  "
        indent = " " * len(lead)
        room = max(16, width - len(lead))
        said = textwrap.wrap(first, room) or [""]
        lines = [st(pad(lead + said[0], width), "crit_bg")] + [st(pad(indent + line, width), "crit_bg") for line in said[1:]]
        advice = " · ".join([f"retrying every {status['interval']:g} s"] + [r.strip() for r in rest])
        lines += [st(indent + line, "crit") for line in textwrap.wrap(advice, room)]
        return lines + [""] * (2 - len(lines))
    lines = []
    if note:
        why = note[len("Collection: "):] if note.startswith("Collection: ") else note
        lines.append(clip(st, st(f"  {st.up} PARTIAL  ", "warn", "bold") + st(why, "warn"), width))
    if alerts:
        lead = st(f"  {st.up} NEEDS ATTENTION   ", tone(alerts[0][2]), "bold")
        chips = [st(f" {name} {value} ", "crit_bg") if lv == "crit" else st(f"{name} {value}", "warn", "bold")
                 for name, value, lv, _ in alerts]
        line, n = lead, 0
        for chip in chips:  # whole entries only; what does not fit is counted
            rest = len(chips) - n - 1
            if n and vlen(line + "  " + chip) + (len(f"  +{rest}") if rest else 0) > width:
                break
            line, n = line + ("  " if n else "") + chip, n + 1
        lines.append(clip(st, line + (st(f"  +{len(chips) - n}", "muted") if n < len(chips) else ""), width))
    return (lines + ["", ""])[:2]


class Terminal:
    """Alternate screen, hidden cursor and keys without Enter; all restored on exit, Ctrl+C included."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)  # Ctrl+C still raises KeyboardInterrupt
        self.wake_r, self.wake_w = os.pipe()  # a resize wakes the wait at once
        os.set_blocking(self.wake_w, False)
        self.old_winch = signal.signal(signal.SIGWINCH, self._resized)
        self.shown, self.clear, self.keys = [], True, True
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\033[0m\033[?25h\033[?1049l")
        sys.stdout.flush()
        signal.signal(signal.SIGWINCH, self.old_winch)
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        os.close(self.wake_r)
        os.close(self.wake_w)

    def _resized(self, *_):
        try:
            os.write(self.wake_w, b"r")
        except OSError:  # already woken
            pass

    def draw(self, lines):
        """Rewrites only the lines that changed since the last frame."""
        out = ["\033[H\033[2J"] if self.clear else []
        out += [f"\033[{n + 1};1H{line}\033[K" for n, line in enumerate(lines)
                if self.clear or n >= len(self.shown) or self.shown[n] != line]
        if len(lines) < len(self.shown):
            out.append(f"\033[{len(lines) + 1};1H\033[J")
        if out:
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        self.shown, self.clear = lines, False

    def wait(self, timeout):
        """Sleeps until timeout ("tick"), a resize ("resize") or a key press (the keys). No polling."""
        watch = [self.fd, self.wake_r] if self.keys else [self.wake_r]
        ready, _, _ = select.select(watch, [], [], max(0.0, timeout))
        if self.wake_r in ready:
            os.read(self.wake_r, 64)
            self.clear = True
            return "resize"
        if self.fd in ready:
            keys = os.read(self.fd, 32)
            if not keys:  # stdin closed: stop watching it instead of waking at once forever
                self.keys = False
            return keys.decode(errors="ignore")
        return "tick"


def interactive():
    return (termios is not None and sys.stdin.isatty() and sys.stdout.isatty()
            and os.environ.get("TERM", "") not in ("", "dumb"))


def make_style(env, encoding):
    """NO_COLOR: no colors. ASCII glyphs without UTF-8, or with PLATMON_ASCII=1 for terminals that draw
    block and line characters two columns wide (East Asian ambiguous width)."""
    return Style(color=not env.get("NO_COLOR"),
                 utf8=(encoding or "").lower().replace("-", "") == "utf8" and env.get("PLATMON_ASCII") != "1")


def data_age(s, got, now):
    """Seconds since the data was read: the server's data_age_ms when it reports one, plus the time since
    the answer arrived (as on the web page); older servers: only the time since the answer."""
    sample = s.get("sample") if isinstance(s.get("sample"), dict) else {}
    age = sample.get("data_age_ms")
    return (age / 1000 if isinstance(age, (int, float)) and age >= 0 else 0) + now - got


def live(addr, interval, fetch, first, view="overview"):
    """The live view on an interactive terminal: one request per interval as before, nothing in between.
    view: the first of VIEWS to draw; keys switch it without asking the service for anything more."""
    st = make_style(os.environ, sys.stdout.encoding)
    traffic = {}  # interface -> its last rx + tx rates, about 10 s of them, for the Overview's busiest order

    def remember(s):
        net = s.get("network") if isinstance(s.get("network"), dict) else {}
        for i in net.get("interfaces") if isinstance(net.get("interfaces"), list) else []:
            r = i.get("rates")
            if r:
                traffic.setdefault(i["name"], deque(maxlen=max(1, round(10 / interval)))).append(
                    r["rx_bytes_per_s"] + r["tx_bytes_per_s"])

    (s, obs), got, error = first, time.monotonic(), None
    offset, page = 0, 1
    remember(s)
    with Terminal() as term:
        deadline = time.monotonic() + interval
        while True:
            cols, rows = shutil.get_terminal_size()
            state = "offline" if error else "partial" if collection_note(s) else "live"
            status = {"state": state, "age": data_age(s, got, time.monotonic()), "error": error, "addr": addr,
                      "interval": interval, "traffic": {k: sum(v) / len(v) for k, v in traffic.items()}}
            term.draw(screen(s, obs, cols - 1, rows, st, status, view, offset))  # cols - 1: never wraps the last column
            offset, page = status["scroll"]  # what was drawn, so scrolling starts from what is on screen
            event = term.wait(deadline - time.monotonic())
            if event == "resize":
                continue
            if event != "tick":
                if "q" in event.lower():
                    return
                moved = next_view(event, view)
                view, offset = moved, 0 if moved != view else next_offset(event, offset, page)
                continue
            try:
                s, obs = fetch()
                got, error = time.monotonic(), None
                remember(s)
            except (OSError, ValueError) as e:  # lost while watching: keep the last data, keep retrying
                error = explain(addr, e)
            deadline += interval
            if deadline < time.monotonic():  # a slow answer: start counting again, never catch up in bursts
                deadline = time.monotonic() + interval


def explain(addr, e):
    """Why the service could not be read, and what to do about it."""
    if isinstance(e, urllib.error.HTTPError):
        hint = "\n  it has no current data yet, or collection keeps failing; check the service log" if e.code == 503 else ""
        return f"platmon at {addr} answered {e.code} {e.reason}{hint}"
    reason = getattr(e, "reason", e)
    if addr.rsplit(":", 1)[0] in ("localhost", "127.0.0.1"):
        return (f"platmon is not running on {addr} ({reason})\n"
                "  check the service: systemctl status platmon    (log: journalctl -u platmon)\n"
                "  from a checkout: deploy/systemd/start.sh or deploy/docker/start.sh")
    return (f"cannot reach platmon at {addr} ({reason})\n"
            "  check that its service runs there and the port is reachable (the device's [http] bind, a firewall)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="platmon", description="Live view of a platmon service (this device by default).",
                                epilog="The service runs in the background: systemctl status platmon checks it "
                                       "(from a checkout: deploy/systemd/install.sh or deploy/docker/start.sh). "
                                       "In the live view 1-7, Tab "
                                       "or the arrow keys switch views and q quits. NO_COLOR=1 turns colors off; "
                                       "PLATMON_ASCII=1 draws with ASCII only.")
    p.add_argument("host", nargs="?", default="localhost", help=f"host[:port], default localhost:{PORT}")
    p.add_argument("interval", nargs="?", type=float, default=1.0, help="seconds between updates, default 1")
    p.add_argument("--once", action="store_true", help="print one snapshot without clearing the screen, then exit")
    p.add_argument("--view", choices=VIEWS, help="the live view's first view (default overview); switch with 1-7, "
                                                 "Tab or the arrow keys. Interactive terminals only")
    p.add_argument("--version", action="version", version=f"platmon {VERSION}")
    a = p.parse_args(argv)
    if not (math.isfinite(a.interval) and a.interval > 0):  # nan and inf would crash in time.sleep()
        p.error("interval must be a number greater than 0")
    if a.once and a.view:  # --once prints every section, so there is no view to choose
        p.error("--view selects a view of the live screen; --once prints every section")
    addr = a.host if ":" in a.host else f"{a.host}:{PORT}"
    url = f"http://{addr}/api/stats"

    obs = {"at": None, "body": None, "supported": True}

    def observations():
        """/api/observations at most every 10 s (it changes on its own, slower, cadence); none for servers
        without it, or when it cannot be read: the main view goes on."""
        now = time.monotonic()
        if obs["supported"] and (obs["at"] is None or now - obs["at"] >= 10):
            obs["at"] = now
            try:
                with urllib.request.urlopen(f"http://{addr}/api/observations", timeout=5) as r:
                    obs["body"] = json.load(r)
            except urllib.error.HTTPError as e:
                obs["supported"] = e.code != 404  # an older server: do not ask again
                obs["body"] = None
            except (OSError, ValueError):
                obs["body"] = None
        return aged(obs["body"], now - obs["at"]) if obs["body"] else None

    def fetch():
        """(the /api/stats answer, the observations or None)."""
        with urllib.request.urlopen(url, timeout=5) as r:
            stats = json.load(r)
        return stats, observations()

    try:
        first = fetch()
    except (OSError, ValueError) as e:  # not running / unreachable: say so once instead of retrying forever
        sys.exit(explain(addr, e))
    if a.once:
        print(render(*first))
        return
    try:
        if interactive():
            try:  # the server's [thresholds]; read once, a restart is needed to change them anyway
                with urllib.request.urlopen(f"http://{addr}/api/status", timeout=5) as r:
                    use_levels(json.load(r).get("levels"))
            except (OSError, ValueError, AttributeError):  # an older server or none: the defaults
                pass
            live(addr, a.interval, fetch, first, a.view or "overview")
            return
        out = render(*first)  # not a terminal (a pipe, a log, Windows): the plain screen as before
        # A console (Windows) redraws it in place; a pipe or a log gets no escapes, a blank line between snapshots.
        clear = "\033[H\033[J" if sys.stdout.isatty() else ""
        lead = clear
        while True:
            print(lead + out, flush=True)
            lead = clear or "\n"
            time.sleep(a.interval)
            try:
                out = render(*fetch())
            except (OSError, ValueError) as e:  # lost while watching: keep the screen, keep retrying
                out = f"{explain(addr, e)}\n(retrying every {a.interval:g}s, Ctrl+C to quit)"
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
