#!/usr/bin/env python3
"""A Prometheus exporter for platmon, outside the server: at each scrape it reads one /api/stats and one
/api/observations from a platmon service and answers /metrics in Prometheus's text format. Standard library
only, Python 3.8+. platmon itself serves no /metrics (docs/guides/prometheus.md says why); requests never
make it collect, so scraping does not change what it measures.

  prometheus_exporter.py [--platmon http://127.0.0.1:9797] [--listen 127.0.0.1:9798]

Every metric is named platmon_*. Counters (network, disk I/O, pressure stall time) are exported as the
kernel's totals for Prometheus's rate(); a value platmon reports as null is left out, never written as 0.
platmon_up is 0, with nothing else, when platmon did not answer or has no current snapshot."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def escape(value):
    """A label value as the text format writes it: backslash, double quote and line feed escaped."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """Samples grouped by metric, each metric with its HELP and TYPE once, in the order first added."""

    def __init__(self):
        self.families = {}

    def add(self, name, mtype, help_text, value, **labels):
        """One sample; a null or non-numeric value is left out (a bool counts as 1 or 0)."""
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, (int, float)):
            return
        family = self.families.setdefault(name, (mtype, help_text, []))
        family[2].append((labels, value))

    def text(self):
        out = []
        for name, (mtype, help_text, samples) in self.families.items():
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {mtype}")
            for labels, value in samples:
                tags = ",".join(f'{k}="{escape(v)}"' for k, v in labels.items())
                out.append(f"{name}{{{tags}}} {value!r}" if tags else f"{name} {value!r}")
        return "\n".join(out) + "\n"


def stats_metrics(m, s):
    """What /api/stats holds: the snapshot's age, CPU, GPU, NPU, memory, root filesystem, temperatures, power,
    fans, network and disk I/O counters, pressure and each collector's state."""
    sample = s.get("sample") or {}
    if sample.get("data_age_ms") is not None:
        m.add("platmon_data_age_seconds", "gauge", "Age of the snapshot's oldest current read.", sample["data_age_ms"] / 1000)
    m.add("platmon_uptime_seconds", "gauge", "Host uptime.", s.get("uptime"))
    for c in s.get("cpu") or []:
        m.add("platmon_cpu_usage_percent", "gauge", "CPU usage over the last interval.", c.get("usage"), cpu=c.get("id"))
        m.add("platmon_cpu_frequency_hertz", "gauge", "CPU clock.", c.get("freq"), cpu=c.get("id"))
    for group in ("gpu", "npu"):
        g = s.get(group)
        if isinstance(g, dict):
            device = g.get("driver") or ""
            m.add(f"platmon_{group}_usage_percent", "gauge", f"{group.upper()} usage.", g.get("usage"), device=device)
            m.add(f"platmon_{group}_frequency_hertz", "gauge", f"{group.upper()} clock.", g.get("freq"), device=device)
            m.add(f"platmon_{group}_max_frequency_hertz", "gauge", f"{group.upper()} highest clock.", g.get("max_freq"), device=device)
    mem = s.get("memory") or {}
    for key, kind in (("total", "total"), ("used", "used")):
        m.add("platmon_memory_bytes", "gauge", "Memory.", mem.get(key), kind=kind)
        m.add("platmon_swap_bytes", "gauge", "Swap.", mem.get("swap_" + key), kind=kind)
    disk = s.get("disk") or {}
    for key in ("total", "used"):
        m.add("platmon_root_filesystem_bytes", "gauge", "The root filesystem (the system volume on Windows).", disk.get(key), kind=key)
    for sensor, value in (s.get("temperature") or {}).items():
        m.add("platmon_temperature_celsius", "gauge", "Temperature sensors.", value, sensor=sensor)
    for rail, value in (s.get("power") or {}).items():
        m.add("platmon_power_watts", "gauge", "Power rails.", value, rail=rail)
    for f in s.get("fans") or []:
        m.add("platmon_fan_rpm", "gauge", "Fan speed.", f.get("rpm"), fan=f.get("name"))
        m.add("platmon_fan_percent", "gauge", "Fan duty.", f.get("percent"), fan=f.get("name"))
    for i in (s.get("network") or {}).get("interfaces") or []:
        for direction in ("rx", "tx"):
            c = i.get(direction) or {}
            for key, name in (("bytes", "bytes"), ("packets", "packets"), ("errors", "errors"), ("dropped", "dropped")):
                m.add(f"platmon_network_{name}_total", "counter", f"Network interface {name}.", c.get(key),
                      interface=i.get("name"), direction=direction)
    for d in (s.get("disk_io") or {}).get("disks") or []:
        for direction in ("read", "write"):
            c = d.get(direction) or {}
            m.add("platmon_disk_io_bytes_total", "counter", "Disk bytes read or written.", c.get("bytes"), disk=d.get("name"), direction=direction)
            m.add("platmon_disk_io_operations_total", "counter", "Disk reads or writes completed.", c.get("ios"), disk=d.get("name"), direction=direction)
        if d.get("io_time_ms") is not None:
            m.add("platmon_disk_io_time_seconds_total", "counter", "Time with I/O in flight.", d["io_time_ms"] / 1000, disk=d.get("name"))
    for resource, r in ((s.get("pressure") or {}).get("resources") or {}).items():
        for kind in ("some", "full"):
            p = r.get(kind) if isinstance(r, dict) else None
            if not isinstance(p, dict):
                continue
            for window in ("avg10", "avg60", "avg300"):
                m.add("platmon_pressure_stall_percent", "gauge", "Pressure stall information (PSI), share of time stalled.",
                      p.get(window), resource=resource, kind=kind, window=window[3:] + "s")
            if p.get("total_us") is not None:
                m.add("platmon_pressure_stall_seconds_total", "counter", "Pressure stall information (PSI), total stall time.",
                      p["total_us"] / 1e6, resource=resource, kind=kind)
    for name, c in (s.get("collectors") or {}).items():
        if isinstance(c, dict) and c.get("state"):
            m.add("platmon_collector_state", "gauge", "Each collector's state (1 for the one it is in).", 1, collector=name, state=c["state"])


def observations_metrics(m, o):
    """What /api/observations holds: each group's age, the filesystems, Wi-Fi, batteries and power supplies,
    and the TCP connect probe."""
    for group, g in (o.get("groups") or {}).items():
        obs = g.get("observation") or {}
        if obs.get("data_age_ms") is not None:
            m.add("platmon_observation_age_seconds", "gauge", "Age of each slow group's data.", obs["data_age_ms"] / 1000, group=group)
        data = g.get("data") or {}
        if group == "storage":
            for f in data.get("filesystems") or []:
                mounts = [p.get("path") for p in f.get("mount_points") or []]
                if not mounts:
                    continue
                for key in ("total", "used", "available"):
                    m.add("platmon_filesystem_bytes", "gauge", "Mounted filesystems.", f.get(key + "_bytes"),
                          mount=mounts[0], fstype=f.get("fstype") or "", kind=key)
        elif group == "wifi":
            for i in data.get("interfaces") or []:
                m.add("platmon_wifi_connected", "gauge", "Wi-Fi interface connected (1) or not (0).", bool(i.get("connected")), interface=i.get("name"))
                m.add("platmon_wifi_signal_dbm", "gauge", "Wi-Fi signal.", i.get("signal_dbm"), interface=i.get("name"))
        elif group == "battery":
            for b in data.get("batteries") or []:
                m.add("platmon_battery_percent", "gauge", "Battery charge.", b.get("percent"), battery=b.get("name"))
                m.add("platmon_battery_temperature_celsius", "gauge", "Battery temperature.", b.get("temp_c"), battery=b.get("name"))
            for e in data.get("external") or []:
                if e.get("online") is not None:
                    m.add("platmon_power_supply_online", "gauge", "External power supply online (1) or not (0).", bool(e["online"]), supply=e.get("name"))
        elif group == "probe":
            for t in data.get("targets") or []:
                target = f"[{t.get('address')}]:{t.get('port')}" if ":" in str(t.get("address")) else f"{t.get('address')}:{t.get('port')}"
                if t.get("connect_ms") is not None:
                    m.add("platmon_probe_connect_seconds", "gauge", "TCP connect time.", t["connect_ms"] / 1000, target=target)
                m.add("platmon_probe_failure_ratio", "gauge", "Share of failed connects in the window.", t.get("failure_ratio"), target=target)


def get(base, path, timeout):
    """(status code, decoded JSON body or None)."""
    try:
        with OPENER.open(base + path, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return None, None


def scrape(base, timeout=5.0):
    """The /metrics body for the platmon service at base."""
    m = Metrics()
    code, stats = get(base, "/api/stats", timeout)
    if code != 200 or not isinstance(stats, dict):
        m.add("platmon_up", "gauge", "platmon answered with a current snapshot (1) or not (0).", 0)
        return m.text()
    m.add("platmon_up", "gauge", "platmon answered with a current snapshot (1) or not (0).", 1)
    stats_metrics(m, stats)
    code, obs = get(base, "/api/observations", timeout)
    if code == 200 and isinstance(obs, dict):
        observations_metrics(m, obs)
    return m.text()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--platmon", default="http://127.0.0.1:9797", help="the platmon service to read")
    p.add_argument("--listen", default="127.0.0.1:9798", help="IPv4 address:port to serve /metrics on")
    a = p.parse_args(argv)
    host, _, port = a.listen.rpartition(":")
    base = a.platmon.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            body = scrape(base).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer((host or "127.0.0.1", int(port)), Handler)
    print(f"serving /metrics on {a.listen} from {base}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
