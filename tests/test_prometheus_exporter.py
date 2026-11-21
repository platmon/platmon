"""scripts/prometheus_exporter.py: what it makes of /api/stats and /api/observations, and its text format."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from scripts import prometheus_exporter as pe

STATS = {  # a Linux board's snapshot, cut down: nulls where a value could not be read
    "uptime": 100.5,
    "sample": {"data_age_ms": 250.0},
    "cpu": [{"id": 0, "usage": 12.5, "freq": 1500000000}, {"id": 1, "usage": 0.0, "freq": None}],
    "gpu": {"usage": None, "freq": 306000000, "max_freq": 1020000000, "driver": "gv11b", "provider": "devfreq"},
    "npu": None,
    "memory": {"total": 8000, "used": 3000, "swap_total": 0, "swap_used": 0},
    "disk": {"total": 64000, "used": 32000},
    "temperature": {"cpu": 51.2, 'odd "name"\\': 40.0},
    "power": {"VDD_IN": 4.25},
    "fans": [{"name": "pwmfan", "rpm": None, "percent": 30}],
    "network": {"interfaces": [{"name": "eth0", "rx": {"bytes": 10, "packets": 2, "errors": 0, "dropped": None},
                                "tx": {"bytes": 20, "packets": 3, "errors": 0, "dropped": 0}}]},
    "disk_io": {"disks": [{"name": "nvme0n1", "read": {"ios": 5, "bytes": 4096}, "write": {"ios": 1, "bytes": 512},
                           "io_time_ms": 1500}]},
    "pressure": {"resources": {"cpu": {"some": {"avg10": 1.25, "avg60": 0.5, "avg300": 0.1, "total_us": 2000000}, "full": None}}},
    "collectors": {"core": {"state": "ok", "reason": None}, "fans": {"state": "partial", "reason": "some_unreadable"}},
}
OBSERVATIONS = {"groups": {
    "storage": {"observation": {"data_age_ms": 1000.0}, "data": {"filesystems": [
        {"fstype": "ext4", "mount_points": [{"path": "/"}], "total_bytes": 100, "used_bytes": 40, "available_bytes": 55},
        {"fstype": None, "mount_points": [], "total_bytes": None}]}},
    "wifi": {"observation": {"data_age_ms": 500.0}, "data": {"interfaces": [{"name": "wlan0", "connected": True, "signal_dbm": -55}]}},
    "battery": {"observation": None, "data": {"batteries": [{"name": "BAT0", "percent": 89, "temp_c": None}],
                                              "external": [{"name": "AC", "online": False}]}},
    "probe": {"observation": {"data_age_ms": 2000.0}, "data": {"targets": [
        {"address": "192.0.2.10", "port": 443, "connect_ms": 12.5, "failure_ratio": 0.1},
        {"address": "2001:db8::1", "port": 22, "connect_ms": None, "failure_ratio": 1.0}]}},
}}
LINE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\\n]|\\["\\n])*",?)*\})? -?[0-9.e+-]+$')


def metrics():
    m = pe.Metrics()
    pe.stats_metrics(m, STATS)
    pe.observations_metrics(m, OBSERVATIONS)
    return m.text()


def test_readings_become_metrics_and_nulls_are_left_out():
    lines = metrics().splitlines()
    for expected in ['platmon_data_age_seconds 0.25', 'platmon_cpu_usage_percent{cpu="0"} 12.5',
                     'platmon_cpu_usage_percent{cpu="1"} 0.0', 'platmon_cpu_frequency_hertz{cpu="0"} 1500000000',
                     'platmon_gpu_frequency_hertz{device="gv11b"} 306000000', 'platmon_memory_bytes{kind="used"} 3000',
                     'platmon_root_filesystem_bytes{kind="total"} 64000', 'platmon_temperature_celsius{sensor="cpu"} 51.2',
                     'platmon_power_watts{rail="VDD_IN"} 4.25', 'platmon_fan_percent{fan="pwmfan"} 30',
                     'platmon_network_bytes_total{interface="eth0",direction="rx"} 10',
                     'platmon_disk_io_bytes_total{disk="nvme0n1",direction="read"} 4096',
                     'platmon_disk_io_time_seconds_total{disk="nvme0n1"} 1.5',
                     'platmon_pressure_stall_percent{resource="cpu",kind="some",window="10s"} 1.25',
                     'platmon_pressure_stall_seconds_total{resource="cpu",kind="some"} 2.0',
                     'platmon_collector_state{collector="fans",state="partial"} 1',
                     'platmon_filesystem_bytes{mount="/",fstype="ext4",kind="available"} 55',
                     'platmon_wifi_connected{interface="wlan0"} 1', 'platmon_wifi_signal_dbm{interface="wlan0"} -55',
                     'platmon_battery_percent{battery="BAT0"} 89', 'platmon_power_supply_online{supply="AC"} 0',
                     'platmon_probe_connect_seconds{target="192.0.2.10:443"} 0.0125',
                     'platmon_probe_failure_ratio{target="[2001:db8::1]:22"} 1.0',
                     'platmon_observation_age_seconds{group="probe"} 2.0']:
        assert expected in lines, expected
    text = "\n".join(lines)
    # null is never written as a number: CPU 1's clock, the GPU's usage, the NPU, the fan's rpm, a dropped count,
    # PSI's full, the battery's temperature, a filesystem without a mount point, the failed probe's connect time
    for absent in ['platmon_cpu_frequency_hertz{cpu="1"}', "platmon_gpu_usage_percent", "platmon_npu_", "platmon_fan_rpm",
                   'direction="rx"} None', 'kind="full"', "platmon_battery_temperature_celsius", 'platmon_probe_connect_seconds{target="[2001']:
        assert absent not in text, absent


def test_the_text_format():
    text = metrics()
    assert text.endswith("\n")
    seen = set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            name, kind = line.split()[2:4]
            assert name not in seen and kind in ("gauge", "counter"), line  # each family once
            assert name.endswith("_total") == (kind == "counter"), line
            seen.add(name)
        elif not line.startswith("# HELP "):
            assert LINE.match(line), line
    # a label value's backslash and double quote are escaped
    assert 'platmon_temperature_celsius{sensor="odd \\"name\\"\\\\"} 40.0' in text


def test_scrape_reads_a_service_and_reports_one_that_is_down():
    answers = {"/api/stats": STATS, "/api/observations": OBSERVATIONS}

    class Fake(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(answers[self.path]).encode() if self.path in answers else b"{}"
            self.send_response(200 if self.path in answers else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        text = pe.scrape(f"http://127.0.0.1:{server.server_address[1]}")
        assert text.splitlines()[2] == "platmon_up 1" and "platmon_wifi_signal_dbm" in text
        answers["/api/stats"] = None  # 503 in a real service; here not JSON of a snapshot
        assert pe.scrape(f"http://127.0.0.1:{server.server_address[1]}").splitlines()[2:] == ["platmon_up 0"]
    finally:
        server.shutdown()
    assert pe.scrape("http://127.0.0.1:1", timeout=1).splitlines()[2:] == ["platmon_up 0"]
