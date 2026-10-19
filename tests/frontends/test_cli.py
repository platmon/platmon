import pytest

from frontends.cli import render
from collector import collect

FULL = {
    "platform": "Jetson Orin", "model": "Test Board", "uptime": 90061, "power_mode": "MAXN_SUPER",
    "system": {"os": "Ubuntu 22.04.5 LTS", "kernel": "5.15.199-tegra", "arch": "aarch64", "hostname": "orin",
               "l4t": "R36.5.2"},
    "cpu": [{"id": 0, "usage": 12.5, "freq": 1728000000}, {"id": 2, "usage": 0.0, "freq": 729600000}],
    "gpu": {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000},
    "memory": {"total": 8 * 2**30, "used": 2 * 2**30, "swap_total": 4 * 2**30, "swap_used": 0},
    "disk": {"total": 100 * 2**30, "used": 25 * 2**30},
    "temperature": {"cpu": 51.2},
    "power": {"VDD_IN": 4.82},
    "fans": [{"name": "pwmfan", "rpm": None, "percent": 31}, {"name": "pwm_tach", "rpm": 1741, "percent": None}],
}

# bare Linux host (e.g. WSL): no GPU, power mode, cpufreq, swap or sensors
BARE = dict(FULL, platform="Linux", power_mode=None, gpu=None, temperature={}, power={}, fans=[],
            cpu=[{"id": 0, "usage": 3.0, "freq": None}],
            memory={"total": 8 * 2**30, "used": 2 * 2**30, "swap_total": 0, "swap_used": 0})


@pytest.mark.parametrize("expected", [
    "Jetson Orin   mode MAXN_SUPER   up 1d 01:01",
    "Ubuntu 22.04.5 LTS · kernel 5.15.199-tegra · aarch64 · host orin · L4T R36.5.2",
    "12.5%  1728 MHz",
    "CPU2  ",  # labelled by core id, not position (cpu1 offline)
    "50.0%   306 MHz",
    "RAM   █████··············· 2.0G/8.0G",
    "SWAP  ",
    "cpu 51.2°C",
    "VDD_IN 4.82W",
    "FAN   pwmfan  31%",
    "FAN   pwm_tach  1741 rpm",
])
def test_render_full(expected):
    assert expected in render(FULL)


def test_render_bare_shows_basics():
    out = render(BARE)
    assert "Linux   up 1d 01:01" in out
    assert "CPU0  " in out and "DISK  " in out


@pytest.mark.parametrize("absent", ["mode", "MHz", "GPU", "SWAP", "TEMP", "POWER", "FAN"])
def test_render_bare_hides_missing(absent):
    assert absent not in render(BARE)


def test_render_live_collect():
    """The viewer accepts whatever the collector really returns on this host (CI runner, WSL, boards)."""
    s = collect()
    out = render(s)
    assert s["platform"] in out
    assert all(f"CPU{c['id']}" in out for c in s["cpu"])


def test_render_server_without_system_info():
    """An older server has no "system" entry: no system line, no empty extra line."""
    old = {k: v for k, v in FULL.items() if k != "system"}
    assert render(old).split("\n")[:3] == ["Test Board", "Jetson Orin   mode MAXN_SUPER   up 1d 01:01", ""]


def test_render_unknown_system_entry():
    """Entries added by a future board module show up as "key value" without changing the viewer."""
    assert "firmware 1.2" in render(dict(FULL, system={"os": "X", "firmware": "1.2"}))


CLI = str(__import__("pathlib").Path(__file__).parents[2] / "frontends" / "cli.py")


def run_cli(*args):
    import subprocess
    import sys
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, timeout=20)


def test_once_prints_one_snapshot():
    from tests.runtime.test_server import serve
    httpd, base = serve(FULL)
    try:
        r = run_cli("--once", base[len("http://"):])
    finally:
        httpd.shutdown()
    assert r.returncode == 0
    assert r.stdout == render(FULL) + "\n"  # the metadata the server adds changes nothing, no screen clearing


def test_view_only_selects_a_live_view():
    """--view picks the live screen's first view; --once prints every section, so the two do not mix."""
    bad = run_cli("--once", "--view", "network")
    assert bad.returncode == 2 and "--view selects a view of the live screen" in bad.stderr
    unknown = run_cli("--view", "cpu")
    assert unknown.returncode == 2 and "invalid choice" in unknown.stderr
    assert "1-7" in run_cli("--help").stdout  # the keys are documented where the options are


def test_not_running_explains_and_exits():
    import socket
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = run_cli(f"localhost:{port}")  # without --once too: fail once, no endless retry loop
    assert r.returncode == 1
    assert f"platmon is not running on localhost:{port}" in r.stderr and "deploy/docker/start.sh" in r.stderr


def test_no_current_data_is_explained():
    from tests.runtime.test_server import serve
    httpd, base = serve(None)  # service up, but no snapshot: 503
    try:
        r = run_cli("--once", base[len("http://"):])
    finally:
        httpd.shutdown()
    assert r.returncode == 1 and "answered 503" in r.stderr and "no current data" in r.stderr


def test_render_unreadable_gpu_clock_and_fan():
    """None means "could not read": no 0 MHz / 0 rpm, and no crash."""
    out = render(dict(FULL, gpu={"usage": 50.0, "freq": None, "max_freq": None},
                      fans=[{"name": "CPU fan", "rpm": None, "percent": None}]))
    assert "GPU   " in out and "50.0%" in out and "MHz" not in out.split("GPU")[1].split("\n")[0]
    assert "FAN   CPU fan  n/a" in out


@pytest.mark.parametrize("interval", ["0", "-1", "nan", "inf"])
def test_bad_interval_is_a_usage_error(interval):
    r = run_cli("localhost", interval)  # refused before connecting or sleeping
    assert r.returncode == 2 and "interval must be a number greater than 0" in r.stderr


SAMPLING = {"mode": "interval", "window_ms": 1000.0, "unavailable": []}


def test_render_with_cpu_sampling_unchanged():
    """All cores measured: the screen is the same as for a server without cpu_sampling."""
    assert render(dict(FULL, cpu_sampling=SAMPLING)) == render(FULL)


def test_render_cpu_warmup():
    """First reading of the service: no CPU rows yet, said as such; everything else still shows."""
    out = render(dict(FULL, cpu=[], cpu_sampling=dict(SAMPLING, window_ms=None, unavailable=[
        {"id": 0, "reason": "warmup"}, {"id": 2, "reason": "warmup"}])))
    assert "CPU sampling: warming up" in out.split("\n")
    assert "CPU0" not in out and "  0.0%" not in out.split("GPU")[0]  # no made-up 0 % bars
    assert "RAM   " in out and "GPU   " in out


def test_render_some_cpus_unavailable():
    out = render(dict(FULL, cpu=FULL["cpu"][:1], cpu_sampling=dict(SAMPLING, unavailable=[
        {"id": 2, "reason": "warmup"}, {"id": 3, "reason": "counter_regressed"}, {"id": 4, "reason": "new_code"}])))
    assert "CPU0  " in out and "CPU2  " not in out
    assert "CPU sampling: CPU2 warming up; CPU3 counter went backwards; CPU4 new_code" in out


def g(state, reason=None):
    return {"state": state, "reason": reason, "issues": [], "issues_truncated": 0}


def test_render_collection_note_only_for_read_failures():
    fine = dict(FULL, collectors={"core": g("ok"), "temperature": g("ok"), "gpu": g("unavailable", "not_detected"),
                                  "fans": g("unavailable", "not_exposed")})
    assert render(fine) == render(FULL)  # absent sensors are not a warning
    out = render(dict(FULL, collectors={"core": g("ok"), "temperature": g("partial", "some_unreadable"),
                                        "gpu": g("error", "io_error"), "fans": g("ok")}))
    assert out.split("\n")[3] == "Collection: temperature partial, gpu error"
    assert out.split("\n")[4] == "" and "CPU0  " in out  # everything else as before


@pytest.mark.parametrize("collectors", ["x", None, {"odd": 5, "x": {"state": None}}, {"new_group": g("error", "new_code")}])
def test_render_odd_collectors(collectors):
    out = render(dict(FULL, collectors=collectors))  # no exception for unknown shapes, groups or reasons
    assert out.startswith("Test Board\n")


C0 = {"errors": 0, "dropped": 0}


def iface(name, rates=None, reason=None, rx=C0, tx=C0):
    return {"name": name, "ifindex": 2, "rx": {"bytes": 1, "packets": 1, **rx}, "tx": {"bytes": 1, "packets": 1, **tx},
            "rates": rates, "window_ms": 1000.0 if rates else None, "reason": reason}


NET_RATES = {"rx_bytes_per_s": 1536.0, "tx_bytes_per_s": 0.0, "rx_packets_per_s": 2.0, "tx_packets_per_s": 0.4}
DISK_RATES = {"read_bytes_per_s": 3 * 2**20, "write_bytes_per_s": 512.0, "reads_per_s": 4.0, "writes_per_s": 0.5,
              "io_time_ratio": 0.034}


def test_render_network_and_disk_io():
    s = dict(FULL, network={"interfaces": [iface("eth0", NET_RATES, rx={"errors": 3, "dropped": 0}),
                                           iface("wlan0", reason="warmup"), iface("x", reason="new_code")]},
             disk_io={"disks": [{"name": "nvme0n1", "in_flight": 2, "rates": DISK_RATES, "reason": None},
                                {"name": "sda", "in_flight": 0, "rates": None, "reason": "counter_regressed"}]})
    out = render(s).splitlines()
    i = out.index("NET   bytes/s, this process's network namespace")
    assert out[i + 1:i + 4] == ["  eth0   rx 1.5 KiB/s  tx 0 B/s  2.0/0.4 pkt/s  window 1.000 s  errors 3/0 (rx/tx, total)",
                                "  wlan0  warming up", "  x      new_code"]
    assert out[i + 4:] == ["IO    bytes/s per disk",
                           "  nvme0n1  read 3.0 MiB/s (4.0/s)  write 512 B/s (0.5/s)  I/O time 3.4%  in flight 2",
                           "  sda      counter went backwards  in flight 0"]
    assert out[i - 1] == ""  # one blank line before, never two


def test_render_limits_rows_and_says_how_many_were_left_out():
    names = [f"veth{n:02d}" for n in range(15)] + ["a-very-long-interface-name"]
    out = render(dict(FULL, network={"interfaces": [iface(n, NET_RATES) for n in names]})).splitlines()
    rows = out[out.index("NET   bytes/s, this process's network namespace") + 1:]
    assert len(rows) == 13 and rows[-1] == "  +4 more interfaces (all in /api/stats)"
    assert rows[0].startswith("  veth00            rx")  # names padded to at most 16 columns


@pytest.mark.parametrize("extra", [{}, {"network": None, "disk_io": None}, {"network": {"interfaces": []}},
                                   {"network": {"interfaces": "odd"}, "disk_io": {"disks": 5}}, {"network": "odd"}])
def test_render_without_counters_is_unchanged(extra):
    assert render(dict(FULL, **extra)) == render(FULL)


def test_render_pressure_without_cpu_full():
    psi = {"resources": {"cpu": {"some": {"avg10": 1.25}, "full": None},
                         "io": {"some": {"avg10": 3.0}, "full": {"avg10": 0.5}}}}
    assert render(dict(FULL, pressure=psi)).splitlines()[-1] == "PSI   cpu some 1.2%  io some 3.0% full 0.5%  (avg10, time stalled)"
    assert render(dict(FULL, pressure={"resources": {}})) == render(FULL) == render(dict(FULL, pressure="odd"))


def test_counter_window_and_zero_in_flight_are_observed_values():
    data = dict(FULL, network={"interfaces": [dict(iface("eth0", NET_RATES), window_ms=1250)]},
                disk_io={"disks": [{"name": "sda", "rates": None, "reason": "warmup", "in_flight": 0}]})
    out = render(data)
    assert "window 1.250 s" in out
    assert "warming up  in flight 0" in out


def test_cached_observation_crosses_stale_threshold_without_another_reply():
    from frontends.cli import aged
    body = {"groups": {"wifi": {"state": "ok", "stale_after_ms": 15000,
                               "observation": {"data_age_ms": 14000, "stale": False}}}}
    current = aged(body, 2)
    assert current["groups"]["wifi"]["observation"]["stale"] is True
    assert body["groups"]["wifi"]["observation"]["stale"] is False
