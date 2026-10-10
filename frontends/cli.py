#!/usr/bin/env python3
"""platmon terminal client: live view of a platmon service, this device's by default.
Standalone (standard library only); scripts/install-cli.sh installs it as the `platmon` command.

Usage: platmon [host[:port]] [interval_sec] [--once]   (default localhost:9797, 1s)
The service itself runs in the background: scripts/systemd/install.sh or scripts/docker/start.sh.

On an interactive terminal the live view is screen(): colored levels, a layout fitted to the terminal size,
redrawn in place on the alternate screen. --once, pipes and /text keep render(), plain text as before.
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
import time
import urllib.error
import urllib.request
from collections import deque

try:  # keys without Enter; not on Windows, where the live view stays the plain one
    import termios
    import tty
except ImportError:
    termios = tty = None

# The release version lives here so the copied, single-file client stays standalone.
VERSION = "1.0.0"
PORT = 9797


def bar(pct, width=20):
    n = round(width * max(0, min(pct, 100)) / 100)
    return "█" * n + "·" * (width - n)


def gib(b):
    return f"{b / 2**30:.1f}G"


SYSTEM_LABELS = {"os": "", "kernel": "kernel ", "arch": "", "hostname": "host ", "l4t": "L4T "}  # others: "key value"


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
             f"I/O time {100 * r['io_time_ratio']:.1f}%" + window_text(d) if r else
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
        out.append("PSI   " + "  ".join(f"{name} some {r['some']['avg10']:.1f}%" + (f" full {r['full']['avg10']:.1f}%" if r.get("full") else "")
                                        for name, r in res.items()) + "  (avg10, time stalled)")
    return out


def storage_lines(obs):
    """Filesystems and partitions from /api/observations, with the observation's own age; nothing when the
    group is off, has not been observed yet, or the server has no such endpoint."""
    groups = obs.get("groups") if isinstance(obs, dict) else None
    g = groups.get("storage") if isinstance(groups, dict) else None
    data, o = (g.get("data"), g.get("observation")) if isinstance(g, dict) else (None, None)
    if not isinstance(data, dict) or not isinstance(o, dict):
        return []
    head = f"STORAGE  observed {o['data_age_ms'] / 1000:.0f} s ago" + (" (not current)" if o.get("stale") else "")
    out = [head]
    for f in data.get("filesystems") or []:
        first, more = f["mount_points"][0], len(f["mount_points"]) - 1  # bind mounts can be dozens
        where = first["path"] + (" (ro)" if first["read_only"] else "") + (f" +{more} more" if more else "")
        dev = f["device"] or f"{f['major']}:{f['minor']}"
        if f["total_bytes"] is not None:  # a measured 0 is shown as 0
            pct = f" ({100 * f['used_bytes'] / f['total_bytes']:.0f}%)" if f["total_bytes"] else ""
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


def wifi_lines(obs):
    """Wireless interfaces from /api/observations: the signal while connected, never the last one after."""
    groups = obs.get("groups") if isinstance(obs, dict) else None
    g = groups.get("wifi") if isinstance(groups, dict) else None
    data, o = (g.get("data"), g.get("observation")) if isinstance(g, dict) else (None, None)
    items = data.get("interfaces") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(o, dict):
        return []
    age = f"observed {o['data_age_ms'] / 1000:.0f} s ago" + (" (not current)" if o.get("stale") else "")
    out = [f"WIFI  {age}"]
    for i in items:
        if i.get("connected") is not True:
            state = "not connected" if i.get("connected") is False else i.get("connection_state", "unknown")
            out.append(f"  {i['name']}  {state}")
            continue
        signal = (f"{i['signal_dbm']} dBm" if i["signal_dbm"] is not None
                  else f"signal {i['signal_raw']} (unit not reported)" if i["signal_raw"] is not None else "signal n/a")
        extra = [f"link quality {i['link_quality']}" if i["link_quality"] is not None else "",
                 f"noise {i['noise_dbm']} dBm" if i["noise_dbm"] is not None else ""]
        out.append(f"  {i['name']}  {signal}" + "".join(f"  {e}" for e in extra if e))
    return out


def probe_lines(obs):
    """TCP connect times from /api/observations (not ICMP ping): the latest attempt per target, and the
    failures among its recent attempts."""
    groups = obs.get("groups") if isinstance(obs, dict) else None
    g = groups.get("probe") if isinstance(groups, dict) else None
    data, o = (g.get("data"), g.get("observation")) if isinstance(g, dict) else (None, None)
    items = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(o, dict):
        return []
    age = f"observed {o['data_age_ms'] / 1000:.0f} s ago" + (" (not current)" if o.get("stale") else "")
    out = [f"TCP   connect time, {age}"]
    for t in items:
        where = f"[{t['address']}]:{t['port']}" if ":" in t["address"] else f"{t['address']}:{t['port']}"
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
    is given. Plain text: --once, pipes and the server's /text."""
    up = int(s["uptime"])
    system = system_line(s.get("system"))
    lines = [
        f"{s['model']}",
        f"{s['platform']}   " + (f"mode {s['power_mode']}   " if s["power_mode"] else "") + f"up {up // 86400}d {up % 86400 // 3600:02}:{up % 3600 // 60:02}",
        *([system] if system else []),
        *([note] if (note := collection_note(s)) else []),
        "",
    ]
    for c in s["cpu"]:
        freq = f"  {c['freq'] / 1e6:4.0f} MHz" if c["freq"] else ""
        lines.append(f"CPU{c['id']:<3}{bar(c['usage'])} {c['usage']:5.1f}%{freq}")
    note = cpu_note(s)
    if note:
        lines.append(note)
    g = s["gpu"]
    if g:
        freq = f"  {g['freq'] / 1e6:4.0f} MHz" if g.get("freq") else ""  # None or 0: clock not readable
        # a card whose driver publishes no load, or the first reading of a counter: a clock, no share
        load = f"{bar(g['usage'])} {g['usage']:5.1f}%" if g.get("usage") is not None else f"{bar(0)}    n/a"
        lines.append(f"GPU   {load}{freq}")
    n = s.get("npu")
    if n:
        freq = f"  {n['freq'] / 1e6:4.0f} MHz" if n.get("freq") else ""
        load = f"{bar(n['usage'])} {n['usage']:5.1f}%" if n.get("usage") is not None else f"{bar(0)}    n/a"
        lines.append(f"NPU   {load}{freq}")
    m = s["memory"]
    lines.append(f"RAM   {bar(100 * m['used'] / m['total'])} {gib(m['used'])}/{gib(m['total'])}")
    if m["swap_total"]:
        lines.append(f"SWAP  {bar(100 * m['swap_used'] / m['swap_total'])} {gib(m['swap_used'])}/{gib(m['swap_total'])}")
    d = s["disk"]
    lines.append(f"DISK  {bar(100 * d['used'] / d['total'])} {gib(d['used'])}/{gib(d['total'])}")
    lines.append("")
    if s["temperature"]:
        lines.append("TEMP  " + "  ".join(f"{k} {v:.1f}°C" for k, v in s["temperature"].items()))
    if s["power"]:
        lines.append("POWER " + "  ".join(f"{k} {v:.2f}W" for k, v in s["power"].items()))
    for f in s["fans"]:
        vals = [f"{f['rpm']} rpm" if f["rpm"] is not None else "", f"{f['percent']}%" if f["percent"] is not None else ""]
        lines.append(f"FAN   {f['name']}  " + ("  ".join(v for v in vals if v) or "n/a"))  # n/a: unreadable
    extra = counters_lines(s) + storage_lines(obs) + wifi_lines(obs) + probe_lines(obs)
    if extra and lines[-1]:
        lines.append("")
    return "\n".join(lines + extra)


# ---------------------------------------------------------------------------------------------------------
# Live terminal screen (interactive terminals only). Same data, wording and levels as the web page; no
# requests beyond render()'s: the sparklines are the last SPARK_POINTS /api/stats answers kept here.

# The web page's defaults; use_levels() replaces them with the server's [thresholds] (/api/status "levels").
LIMITS = {"cpu": (80, 90), "gpu": (80, 90), "memory": (80, 90), "filesystem": (80, 90), "temperature": None}
# Servers from #57 on send the temperature level they use; this copy of platmon.py BOARD_TEMPERATURE serves
# older ones that send none (tests/test_cli_screen.py checks that the two match).
TEMP_LIMITS = {"Jetson Orin": (85, 95), "Raspberry Pi": (75, 80)}
SPARK_POINTS = 24
WIDE = 100  # columns from which everything is on one screen; below, the overview and a few rows
ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def temp_limits(s):
    return LIMITS["temperature"] or TEMP_LIMITS.get(s.get("platform"), (80, 90))


def use_levels(levels):
    """Takes the server's levels ({name: [warning, critical]}, temperature may be null); anything else in
    them, or a server without them (None), keeps the defaults."""
    for name, value in (levels if isinstance(levels, dict) else {}).items():
        if name in LIMITS and (value is None and name == "temperature" or isinstance(value, list) and len(value) == 2
                               and all(isinstance(v, (int, float)) for v in value) and value[0] < value[1]):
            LIMITS[name] = tuple(value) if value else None


def level(v, limits):
    """Without limits (a reading [thresholds] does not set levels for) nothing is a warning."""
    return "ok" if not limits else "crit" if v >= limits[1] else "warn" if v >= limits[0] else "ok"


def tone(lv):
    """Text color for a level: ok text stays the terminal's own color."""
    return None if lv == "ok" else lv


class Style:
    """Basic ANSI colors (when color is on) and the glyphs: block characters, or ASCII without UTF-8."""
    CODES = {"ok": "32", "warn": "33", "crit": "31", "muted": "90", "dim": "2", "bold": "1", "crit_bg": "30;41"}

    def __init__(self, color=True, utf8=True):
        self.color = color
        (self.full, self.track, self.tick, self.rule, self.dot, self.cross, self.up, self.gap,
         self.sparks) = (("█", "░", "╎", "─", "●", "✕", "▲", "·", "▁▂▃▄▅▆▇█") if utf8 else
                         ("#", "-", "|", "-", "*", "x", "!", ".", "_.-:=+*#"))

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


def spread(left, right, width):
    return left + " " * max(1, width - vlen(left) - vlen(right)) + right


def meter(st, pct, width, limits=None, ticks=True):
    """A bar on a 0–100 scale in its level's color; limits also mark the warning and critical points."""
    pct = max(0.0, min(float(pct), 100.0))
    n = round(width * pct / 100)
    marks = {min(width - 1, int(width * t / 100)) for t in limits} if limits and ticks else set()
    track = "".join(st.tick if i in marks else st.track for i in range(n, width))
    return st(st.full * n, level(pct, limits) if limits else "ok") + st(track, "muted")


def spark(st, values, width, lv="ok"):
    """The latest values on a fixed 0–100 scale (percent, °C), oldest left; a missing answer is a gap."""
    vals = list(values)[-width:]
    chars = "".join(st.gap if v is None else st.sparks[min(7, int(max(0.0, min(v, 100.0)) * 0.08))] for v in vals)
    return " " * (width - len(vals)) + st(chars, lv)


def metrics(s):
    """The web Overview's readings: (label, value or None, unit, limits, detail)."""
    cpus = [c["usage"] for c in s["cpu"] if isinstance(c.get("usage"), (int, float))]
    m, d, g, n = s["memory"], s["disk"], s.get("gpu"), s.get("npu")
    out = [("CPU", sum(cpus) / len(cpus) if cpus else None, "%", LIMITS["cpu"], f"mean of {len(cpus)} cores")]
    if g:
        out.append(("GPU", g["usage"], "%", LIMITS["gpu"], f"{g['freq'] / 1e6:.0f} MHz" if g.get("freq") else ""))
    if n:  # no [thresholds] entry sets levels for an NPU, so it is shown without one
        out.append(("NPU", n["usage"], "%", None, f"{n['freq'] / 1e6:.0f} MHz" if n.get("freq") else ""))
    out.append(("RAM", 100 * m["used"] / m["total"] if m["total"] else None, "%", LIMITS["memory"],
                f"{gib(m['used'])} / {gib(m['total'])}"))
    out.append(("DISK", 100 * d["used"] / d["total"] if d["total"] else None, "%", LIMITS["filesystem"],
                f"{gib(d['used'])} / {gib(d['total'])}"))
    temps = hottest_first(s)
    if temps:
        out.append(("TEMP", temps[0][1], "°C", temp_limits(s), f"{temps[0][0]} · hottest of {len(temps)}"))
    return out


def hottest_first(s):
    return sorted(((k, v) for k, v in (s.get("temperature") or {}).items() if isinstance(v, (int, float))),
                  key=lambda kv: -kv[1])


def busiest_first(interfaces):
    """Interfaces by traffic (rx + tx), those without rates last; otherwise in the server's order."""
    def traffic(i):
        r = i.get("rates")
        return -(r["rx_bytes_per_s"] + r["tx_bytes_per_s"]) if r else 1
    return sorted(interfaces, key=traffic)


def filesystems(obs):
    """(data, observation) of the storage group, or (None, None)."""
    groups = obs.get("groups") if isinstance(obs, dict) else None
    g = groups.get("storage") if isinstance(groups, dict) else None
    data, o = (g.get("data"), g.get("observation")) if isinstance(g, dict) else (None, None)
    return (data, o) if isinstance(data, dict) and isinstance(o, dict) else (None, None)


def fs_pct(f):
    return 100 * f["used_bytes"] / f["total_bytes"] if f.get("total_bytes") else None


def attention(s, obs):
    """Readings at or above their warning level, critical first, as on the web page: the Overview readings,
    every temperature sensor, and filesystems other than / from the storage observation."""
    out = [("/" if label == "DISK" else label, f"{v:.1f} {unit}", level(v, lim))
           for label, v, unit, lim, _ in metrics(s) if v is not None and label != "TEMP"]
    out += [(k, f"{v:.1f} °C", level(v, temp_limits(s))) for k, v in hottest_first(s)]
    data, _ = filesystems(obs)
    for f in (data or {}).get("filesystems") or []:
        pct = fs_pct(f)
        if pct is not None and not any(p["path"] == "/" for p in f["mount_points"]):
            out.append((f["mount_points"][0]["path"], f"{pct:.0f} %", level(pct, LIMITS["filesystem"])))
    return sorted((a for a in out if a[2] != "ok"), key=lambda a: a[2] != "crit")


def screen(s, obs, width, height, st, status, history):
    """The live view as lines of at most width columns and at most height lines.
    status: state (live, partial, offline), age (seconds), error (explain() text or None), addr, interval.
    history: label -> the latest values of that Overview reading (None for a missed answer)."""
    wide = width >= WIDE
    sys_ = s.get("system") or {}
    up = int(s["uptime"])
    uptime = f"up {up // 86400}d {up % 86400 // 3600:02}:{up % 3600 // 60:02}"

    def title(name, right=""):
        left = st(f"  {name}", "muted", "bold")
        return spread(left, st(right + " ", "muted"), width) if right else left

    def kv(label, text):
        return st(f"  {label:<10}", "muted", "bold") + text

    state = status["state"]
    color = {"live": "ok", "partial": "warn", "offline": "crit"}[state]
    sample = s.get("sample") if isinstance(s.get("sample"), dict) else {}
    seq = f"#{sample['sequence']} · " if isinstance(sample.get("sequence"), int) and wide else ""
    age = (f"data {status['age']:.0f} s old" if state == "offline" else
           f"{status['age']:.1f} s ago" if wide else f"{status['age']:.1f} s")
    head = [spread(st(" p_", "ok", "bold") + st(" platmon", "bold") + "   " + st(s["model"] if wide else sys_.get("hostname") or s["model"], "bold"),
                   st(f"{st.dot} {state.upper()}", color) + st(f"  {seq}{age} ", "muted"), width),
            st("             " + " · ".join(str(v) for v in (
                ([sys_.get("hostname")] if wide else []) + [s["platform"], s["power_mode"] and f"mode {s['power_mode']}", uptime]
                + ([sys_.get("os"), sys_.get("l4t") and f"L4T {sys_['l4t']}", sys_.get("arch")] if wide else [sys_.get("l4t") and f"L4T {sys_['l4t']}"]))
                if v), "muted"),
            st(st.rule * width, "muted")]

    banner = []
    if status.get("error"):
        first, *rest = status["error"].split("\n")
        banner.append(st(pad(f"  {st.cross} NOT CURRENT  {first} · retrying every {status['interval']:g} s", width), "crit_bg"))
        banner += [st("                 " + r.strip(), "crit") for r in rest]

    body = []
    alerts = attention(s, obs)
    if alerts:
        body.append(st(f"  {st.up} NEEDS ATTENTION   ", tone(alerts[0][2]), "bold") + "  ".join(
            st(f" {name} {value} ", "crit_bg") if lv == "crit" else st(f"{name} {value}", "warn", "bold") for name, value, lv in alerts))
        body.append("")
    note = collection_note(s)
    if note:
        body += [st(f"  {note}", "warn"), ""]

    bw, dw, sw = (20, 22, SPARK_POINTS) if wide else (10, 16, 12 if width >= 62 else 0)
    if wide:
        body.append(pad(title("OVERVIEW"), 2 + 6 + 7 + 1 + bw + 2 + dw) + st(f"last {SPARK_POINTS} updates", "muted"))
    for label, v, unit, lim, detail in metrics(s):
        if v is None:
            body.append(st(f"  {label:<6}", "muted") + st("unavailable", "muted"))
            continue
        lv = level(v, lim)
        body.append(st(f"  {label:<6}", "muted") + st(pad(f"{v:.1f} {unit}", 7), "bold", tone(lv)) + " "
                    + meter(st, v, bw, lim) + "  " + st(pad(detail[:dw], dw), "muted")
                    + (spark(st, history.get(label, ()), sw, lv) if sw else ""))
    body.append("")

    if s["cpu"]:
        cell = 25 if wide else 12
        cols = max(1, (width + 3) // (cell + 3)) if wide else max(1, (width + 1) // (cell + 1))
        cells = []
        for c in s["cpu"]:
            lv = tone(level(c["usage"], LIMITS["cpu"]))
            if wide:
                freq = f"{c['freq'] / 1e6:4.0f}" if c.get("freq") else "    "
                cells.append(st(f"{c['id']:>4} ", lv or "muted", "bold" if lv else None) + meter(st, c["usage"], 8, LIMITS["cpu"], False)
                             + st(f" {c['usage']:5.1f}%", lv, "bold" if lv else None) + st(f" {freq}", "muted"))
            else:
                cells.append(st(f"{c['id']:>3} ", lv or "muted") + meter(st, c["usage"], 4, LIMITS["cpu"], False)
                             + st(f" {c['usage']:3.0f}%", lv))
        if wide:
            body.append(title("CORES", f"%  ·  MHz  ·  {len(s['cpu'])} measured"))
        sep = "   " if wide else " "
        body += [sep.join(cells[i:i + cols]) for i in range(0, len(cells), cols)]
    if cpu_note(s):
        body.append(st(f"  {cpu_note(s)}", "muted"))
    body.append("")

    net = s.get("network") if isinstance(s.get("network"), dict) else {}
    ifaces = busiest_first(net.get("interfaces") if isinstance(net.get("interfaces"), list) else [])
    if not wide:
        ifaces = [i for i in ifaces if i["name"] != "lo"]
    shown = ifaces[:MAX_ROWS if wide else 2]
    name_w = 4 + max([len(i["name"]) for i in shown] + [6]) + 2
    widths = [max(26, name_w), 12, 14, 18, 15] if wide else [max(18, name_w), 11, 12]

    def row(cells, tones):
        return "".join(st(pad(c, w, i > 0), *(t or "").split()) for i, (c, w, t) in enumerate(zip(cells, widths, tones)))

    def no_rate(name, item):
        return st(pad(f"    {name}", widths[0]) + RATE_REASONS.get(item.get("reason"), str(item.get("reason"))), "muted")

    if shown:
        body.append(row(["  NETWORK", "↓ RX", "↑ TX", "PKT/S rx · tx", "ERR · DROP"], ["muted bold"] + ["muted"] * 4)
                    + (st("   this netns · err/drop = totals", "muted") if wide else ""))
        for i in shown:
            r = i["rates"]
            if not r:
                body.append(no_rate(i["name"], i))
                continue
            err = (i.get("rx") or {}).get("errors", 0) + (i.get("tx") or {}).get("errors", 0)
            drop = (i.get("rx") or {}).get("dropped", 0) + (i.get("tx") or {}).get("dropped", 0)
            idle = not r["rx_bytes_per_s"] and not r["tx_bytes_per_s"]
            body.append(row([f"    {i['name']}", per_s(r["rx_bytes_per_s"]), per_s(r["tx_bytes_per_s"]),
                             f"{r['rx_packets_per_s']:.0f} · {r['tx_packets_per_s']:.0f}", f"{err} · {drop}"],
                            ["muted"] * 5 if idle else [None, None, None, "muted", "warn" if err or drop else "muted"]))
        if len(ifaces) > len(shown):
            body.append(st(f"    +{len(ifaces) - len(shown)} more interfaces" + (" (all in /api/stats)" if wide else " · widen the terminal for all"), "muted"))
        body.append("")

    dio = s.get("disk_io") if isinstance(s.get("disk_io"), dict) else {}
    disks = dio.get("disks") if isinstance(dio.get("disks"), list) else []
    if wide and disks:
        body.append(row(["  DISK I/O", "READ", "WRITE", "OPS/S r · w", "I/O TIME"], ["muted bold"] + ["muted"] * 4))
        for d in disks[:MAX_ROWS]:
            r = d["rates"]
            if not r:
                body.append(no_rate(d["name"], d))
                continue
            idle = not r["read_bytes_per_s"] and not r["write_bytes_per_s"]
            body.append(row([f"    {d['name']}", per_s(r["read_bytes_per_s"]), per_s(r["write_bytes_per_s"]),
                             f"{r['reads_per_s']:.0f} · {r['writes_per_s']:.0f}", f"{100 * r['io_time_ratio']:.1f} %"],
                            ["muted"] * 5 if idle else [None, None, None, "muted", None]))
        if len(disks) > MAX_ROWS:
            body.append(st(f"    +{len(disks) - MAX_ROWS} more disks (all in /api/stats)", "muted"))
        body.append("")

    if wide:
        temps = hottest_first(s)
        if temps:
            body.append(kv("THERMAL", "   ".join(st(f"{k} {v:.1f}", tone(level(v, temp_limits(s)))) for k, v in temps) + st("  °C", "muted")))
        power = [f"{k} {v:.2f} W" for k, v in (s.get("power") or {}).items()]
        fans = [f"fan {f['name']} " + (" · ".join(x for x in (f["rpm"] is not None and f"{f['rpm']} rpm",
                                                               f["percent"] is not None and f"{f['percent']} %") if x) or "n/a")
                for f in s.get("fans") or []]
        if power or fans:
            body.append(kv("POWER", "   ".join(power + fans)))
        psi = s.get("pressure")
        res = psi.get("resources") if isinstance(psi, dict) else None
        if isinstance(res, dict) and res:
            body.append(kv("PRESSURE", "   ".join(f"{name} {r['some']['avg10']:.1f}" + (f" / {r['full']['avg10']:.1f}" if r.get("full") else "") + " %"
                                                  for name, r in res.items()) + st("      some / full · avg10, time stalled", "muted")))

    data, o = filesystems(obs)
    if data:
        seen = st(f"observed {o['data_age_ms'] / 1000:.0f} s ago" + (" (not current)" if o.get("stale") else "") + " ", "muted")
        fss = data.get("filesystems") or []
        if wide:
            path_w = max([len(f["mount_points"][0]["path"]) for f in fss] + [1]) + 2
            for n, f in enumerate(fss):
                m, pct = f["mount_points"], fs_pct(f)
                where = pad(m[0]["path"], path_w) + ("(ro) " if m[0]["read_only"] else "") + (st(f"+{len(m) - 1} ", "muted") if len(m) > 1 else "")
                dev = f["device"] or f"{f['major']}:{f['minor']}"
                text = (meter(st, pct, 20, LIMITS["filesystem"]) + st(f"  {pct:3.0f} %", tone(level(pct, LIMITS["filesystem"])))
                        + f"   {gib(f['used_bytes'])} / {gib(f['total_bytes'])}" + st(f"   {gib(f['available_bytes'])} free   {f['fstype']} {dev}", "muted")
                        if pct is not None else st(f"capacity unknown   {f['fstype']} {dev}", "muted"))
                line = kv("STORAGE" if n == 0 else "", where + text)
                body.append(spread(line, seen, width) if n == 0 else line)
        else:
            parts = [f"{f['mount_points'][0]['path']} " + st(f"{fs_pct(f):.0f} %", tone(level(fs_pct(f), LIMITS["filesystem"])))
                     for f in fss if fs_pct(f) is not None]
            if parts:
                body.append(spread(kv("STORAGE", "   ".join(parts)), seen, width))
    if wide:
        for lines, label in ((wifi_lines(obs), "WIFI"), (probe_lines(obs), "TCP")):
            for n, line in enumerate(lines[1:]):
                body.append(kv(label if n == 0 else "", line.strip()))

    while body and not body[-1]:
        body.pop()
    if status.get("error"):  # the last data, dimmed: shown as what it is, not as current
        body = [st(ANSI.sub("", line), "dim") for line in body]

    keys = st("  ") + st("q", "bold") + st(" quit", "muted")
    foot = [st(st.rule * width, "muted"),
            spread(keys, st((f"platmon {VERSION} · {status['addr']} · every {status['interval']:g} s" if wide
                             else f"{VERSION} · {status['addr']} · {status['interval']:g} s") + " ", "muted"), width)]
    room = max(1, height - len(head) - len(banner) - len(foot))
    if len(body) > room:
        hidden = len(body) - room + 1
        body = body[:room - 1] + [st(f"  +{hidden} more lines · enlarge the terminal", "muted")]
    # A terminal too short for the header, one line and the footer still gets only the lines it has:
    # drawing past the last row scrolls the screen and leaves the frame in pieces.
    return [fit(line, width) for line in (head + banner + body + foot)[:max(height, 1)]]


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


def live(addr, interval, fetch, first):
    """The live view on an interactive terminal: one request per interval as before, nothing in between."""
    st = make_style(os.environ, sys.stdout.encoding)
    history = {}

    def remember(s):
        seen = set()
        for label, v, *_ in metrics(s):
            history.setdefault(label, deque(maxlen=SPARK_POINTS)).append(v)
            seen.add(label)
        for label in history.keys() - seen:
            history[label].append(None)

    (s, obs), got, error = first, time.monotonic(), None
    remember(s)
    with Terminal() as term:
        deadline = time.monotonic() + interval
        while True:
            cols, rows = shutil.get_terminal_size()
            state = "offline" if error else "partial" if collection_note(s) else "live"
            status = {"state": state, "age": data_age(s, got, time.monotonic()), "error": error, "addr": addr,
                      "interval": interval}
            term.draw(screen(s, obs, cols - 1, rows, st, status, history))  # cols - 1: never wraps the last column
            event = term.wait(deadline - time.monotonic())
            if event == "resize":
                continue
            if event != "tick":
                if "q" in event.lower():
                    return
                continue
            try:
                s, obs = fetch()
                got, error = time.monotonic(), None
                remember(s)
            except (OSError, ValueError) as e:  # lost while watching: keep the last data, keep retrying
                error = explain(addr, e)
                for values in history.values():
                    values.append(None)
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
                "  start the service from the platmon repo: scripts/systemd/install.sh or scripts/docker/start.sh")
    return f"cannot reach platmon at {addr} ({reason})\n  check that its service runs there and the port is reachable"


def main(argv=None):
    p = argparse.ArgumentParser(prog="platmon", description="Live view of a platmon service (this device by default).",
                                epilog="The service runs in the background: scripts/systemd/install.sh or "
                                       "scripts/docker/start.sh in the platmon repo. NO_COLOR=1 turns colors off; "
                                       "PLATMON_ASCII=1 draws with ASCII only.")
    p.add_argument("host", nargs="?", default="localhost", help=f"host[:port], default localhost:{PORT}")
    p.add_argument("interval", nargs="?", type=float, default=1.0, help="seconds between updates, default 1")
    p.add_argument("--once", action="store_true", help="print one snapshot without clearing the screen, then exit")
    p.add_argument("--version", action="version", version=f"platmon {VERSION}")
    a = p.parse_args(argv)
    if not (math.isfinite(a.interval) and a.interval > 0):  # nan and inf would crash in time.sleep()
        p.error("interval must be a number greater than 0")
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
            live(addr, a.interval, fetch, first)
            return
        out = render(*first)  # not a terminal (a pipe, a log, Windows): the plain screen as before
        while True:
            print("\033[H\033[J" + out, flush=True)
            time.sleep(a.interval)
            try:
                out = render(*fetch())
            except (OSError, ValueError) as e:  # lost while watching: keep the screen, keep retrying
                out = f"{explain(addr, e)}\n(retrying every {a.interval:g}s, Ctrl+C to quit)"
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
