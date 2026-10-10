import os

import pytest

from collector import detect, jetson
from collector import common
from collector.common import CpuCounters, cpu_percent, cpu_times, hwmon_sensors, os_release, thermal_zones


@pytest.mark.parametrize("before, after, expected", [
    # user nice system idle iowait irq softirq steal
    ({0: [100, 0, 100, 700, 100, 0, 0, 0]}, {0: [150, 0, 150, 800, 100, 0, 0, 0]}, ({0: 50.0}, {})),
    ({0: [0] * 8}, {0: [0, 0, 0, 90, 10, 0, 0, 0]}, ({0: 0.0}, {})),  # measured idle (iowait counts as idle)
    ({0: [0] * 8}, {0: [40, 10, 20, 0, 0, 10, 10, 10]}, ({0: 100.0}, {})),
    ({0: [0] * 8}, {0: [1, 0, 0, 2, 0, 0, 0, 0]}, ({0: 33.3}, {})),  # one decimal, rounded once at the end
    # no ticks elapsed: nothing was measured, which is not 0 % (this used to read 0.0)
    ({0: [1, 1, 1, 1, 1]}, {0: [1, 1, 1, 1, 1]}, ({}, {0: "no_ticks"})),
    # cpu1 offline, cpu3 went offline between samples: ids kept, nothing mispaired
    ({0: [0, 0, 0, 0, 0], 2: [0, 0, 0, 0, 0], 3: [0, 0, 0, 0, 0]},
     {0: [10, 0, 0, 10, 0], 2: [0, 0, 0, 10, 0]}, ({0: 50.0, 2: 0.0}, {})),
    # VM host: guest (and guest_nice) time is already included in user (and nice); not counted twice
    #  user nice system idle iowait irq softirq steal guest guest_nice
    ({0: [0] * 10}, {0: [50, 0, 0, 50, 0, 0, 0, 0, 50, 0]}, ({0: 50.0}, {})),
    ({0: [0] * 10}, {0: [0, 50, 0, 50, 0, 0, 0, 0, 0, 50]}, ({0: 50.0}, {})),
    # a core with no earlier reading (new or back online) and one whose iowait went down: no usage for
    # either, and the other core is still measured; nothing negative, nothing over 100 %
    ({0: [0] * 8, 1: [0, 0, 0, 0, 50, 0, 0, 0]},
     {0: [10] + [0] * 7, 1: [0, 0, 0, 100, 40, 0, 0, 0], 2: [5] * 8},
     ({0: 100.0}, {1: "counter_regressed", 2: "warmup"})),
])
def test_cpu_percent(before, after, expected):
    assert cpu_percent(before, after) == expected


class Proc:
    """Fake /proc/stat readings (served in order) and a fake elapsed clock, for CpuCounters."""

    def __init__(self, monkeypatch):
        self.ns, self.readings, self.reads = 10**12, [], 0
        monkeypatch.setattr(common, "cpu_times", self.read)

    def read(self):
        self.reads += 1
        reading = self.readings.pop(0)
        if isinstance(reading, Exception):
            raise reading
        return reading

    def counters(self, max_gap=5.0):
        return CpuCounters(lambda: self.ns, max_gap=max_gap)

    def sample(self, counters, after_s, reading):
        self.ns += round(after_s * 1e9)
        self.readings.append(reading)
        return counters.sample()


def idle_busy(idle, busy):
    return [busy, 0, 0, idle, 0, 0, 0, 0]


def test_first_reading_warms_up_then_measures(monkeypatch):
    p = Proc(monkeypatch)
    c = p.counters()
    assert p.sample(c, 0, {0: idle_busy(0, 0), 1: idle_busy(0, 0)}) == (
        {}, {"mode": "interval", "window_ms": None,
             "unavailable": [{"id": 0, "reason": "warmup"}, {"id": 1, "reason": "warmup"}]})
    # interval 1 s configured, readings 1.25 s apart: the window is what was measured; usage is a tick ratio
    usage, sampling = p.sample(c, 1.25, {0: idle_busy(100, 100), 1: idle_busy(200, 0)})
    assert usage == {0: 50.0, 1: 0.0}
    assert sampling == {"mode": "interval", "window_ms": 1250.0, "unavailable": []}
    assert p.sample(c, 3, {0: idle_busy(200, 200), 1: idle_busy(400, 0)})[0] == usage  # same ticks, longer window


def test_cores_come_and_go(monkeypatch):
    p = Proc(monkeypatch)
    c = p.counters()
    p.sample(c, 0, {0: idle_busy(0, 0), 1: idle_busy(0, 0)})
    usage, sampling = p.sample(c, 1, {0: idle_busy(10, 10), 1: idle_busy(20, 0), 2: idle_busy(5, 5)})
    assert usage == {0: 50.0, 1: 0.0} and sampling["unavailable"] == [{"id": 2, "reason": "warmup"}]
    usage, sampling = p.sample(c, 1, {0: idle_busy(20, 20), 2: idle_busy(10, 10)})  # cpu1 went offline
    assert usage == {0: 50.0, 2: 50.0} and sampling["unavailable"] == []  # gone: neither shown nor listed
    usage, sampling = p.sample(c, 1, {0: idle_busy(30, 30), 1: idle_busy(500, 500), 2: idle_busy(20, 10)})
    assert usage == {0: 50.0, 2: 0.0}
    assert sampling["unavailable"] == [{"id": 1, "reason": "warmup"}]  # back: a fresh baseline, not the old one
    assert p.sample(c, 1, {0: idle_busy(40, 40), 1: idle_busy(510, 510), 2: idle_busy(30, 10)})[0][1] == 50.0


def test_counter_regression_is_dropped_and_rebased(monkeypatch):
    p = Proc(monkeypatch)
    c = p.counters()
    p.sample(c, 0, {0: idle_busy(100, 100), 1: idle_busy(100, 100)})
    usage, sampling = p.sample(c, 1, {0: idle_busy(50, 150), 1: idle_busy(110, 110)})
    assert usage == {1: 50.0} and sampling["unavailable"] == [{"id": 0, "reason": "counter_regressed"}]
    assert p.sample(c, 1, {0: idle_busy(60, 160), 1: idle_busy(120, 120)})[0] == {0: 50.0, 1: 50.0}


def test_no_ticks_is_not_zero(monkeypatch):
    p = Proc(monkeypatch)
    c = p.counters()
    p.sample(c, 0, {0: idle_busy(10, 10)})
    usage, sampling = p.sample(c, 1, {0: idle_busy(10, 10)})
    assert usage == {} and sampling["unavailable"] == [{"id": 0, "reason": "no_ticks"}]


@pytest.mark.parametrize("step", [0, -1])
def test_invalid_interval(monkeypatch, step):
    p = Proc(monkeypatch)
    c = p.counters()
    p.sample(c, 0, {0: idle_busy(0, 0)})
    usage, sampling = p.sample(c, step, {0: idle_busy(10, 10)})
    assert usage == {} and sampling == {"mode": "interval", "window_ms": None,
                                        "unavailable": [{"id": 0, "reason": "invalid_interval"}]}
    assert p.sample(c, 1, {0: idle_busy(20, 20)})[0] == {0: 50.0}  # rebased on the reading it could not use


def test_gap_boundary_and_recovery(monkeypatch):
    p = Proc(monkeypatch)
    c = p.counters(max_gap=5.0)
    p.sample(c, 0, {0: idle_busy(0, 0)})
    assert p.sample(c, 5, {0: idle_busy(10, 10)})[0] == {0: 50.0}  # exactly max_gap: still averaged
    p.ns += 1
    usage, sampling = p.sample(c, 5, {0: idle_busy(20, 20)})
    assert usage == {} and sampling == {"mode": "interval", "window_ms": 5000.0,
                                        "unavailable": [{"id": 0, "reason": "gap"}]}
    usage, sampling = p.sample(c, 1, {0: idle_busy(30, 20)})
    assert usage == {0: 0.0} and sampling["window_ms"] == 1000.0


def test_read_failure_raises_and_restarts_warmup(monkeypatch):
    p = Proc(monkeypatch)
    c = p.counters()
    p.sample(c, 0, {0: idle_busy(0, 0)})
    with pytest.raises(OSError):
        p.sample(c, 1, OSError("unreadable"))
    usage, sampling = p.sample(c, 1, {0: idle_busy(10, 10)})
    assert usage == {} and sampling["unavailable"] == [{"id": 0, "reason": "warmup"}]


@pytest.mark.parametrize("text", [
    "cpu  1 2 3 4 5 6 7 8\nintr 0\n",            # the total line only: not "0 CPUs"
    "cpu0 1 2 3 4 5 6 7 8\ncpu2 1 x 3 4 5 6 7 8\n",  # a counter that is not a number
    "cpu0\n",                                      # no counters
    "cpu0 1 0 0 1\n",                              # fewer than user..steal
    "cpu0 1 2 3 4 5 6 7 8\ncpu1 1 2 3 4 5 6 7",    # last line short, no trailing newline
])
def test_cpu_times_rejects_broken_readings(tmp_path, text):
    stat = tmp_path / "stat"
    stat.write_text(text)
    with pytest.raises(ValueError):
        cpu_times(str(stat))


def test_cpu_times_reads_eight_or_more_counters(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu  9 9 9 9 9 9 9 9 9 9\ncpu0 1 2 3 4 5 6 7 8\ncpu1 1 2 3 4 5 6 7 8 9 10\nctxt 5\n")
    assert cpu_times(str(stat)) == {0: [1, 2, 3, 4, 5, 6, 7, 8], 1: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}


def test_cpu_percent_does_not_cut_readings_short():
    with pytest.raises(ValueError):
        cpu_percent({0: [0] * 8}, {0: [1, 0, 0, 1, 0]})


def stat_files(tmp_path, monkeypatch, lines):
    """The real /proc/stat parser on a sequence of files, one per reading."""
    paths = []
    for i, line in enumerate(lines):
        paths.append(tmp_path / f"stat{i}")
        paths[-1].write_text("cpu  0 0 0 0 0 0 0 0\n" + line + "\n")
    real = common.cpu_times
    monkeypatch.setattr(common, "cpu_times", lambda: real(str(paths.pop(0))))


GOOD = ["cpu0 0 0 0 0 0 0 0 0", "cpu0 2 0 0 2 0 0 0 0", "cpu0 3 0 0 3 0 0 0 0"]


@pytest.mark.parametrize("first_ok", [True, False])
def test_broken_reading_is_not_a_baseline(tmp_path, monkeypatch, first_ok):
    """A short line fails that reading and clears the baseline; the next valid one warms up, then measures."""
    stat_files(tmp_path, monkeypatch, (GOOD[:1] if first_ok else []) + ["cpu0 1 0 0 1"] + GOOD[1:])
    now = [0]
    c = CpuCounters(lambda: now[0], max_gap=5.0)

    def sample():
        now[0] += 10**9
        return c.sample()

    if first_ok:
        assert sample()[1]["unavailable"] == [{"id": 0, "reason": "warmup"}]
    with pytest.raises(ValueError):
        sample()
    assert sample() == ({}, {"mode": "interval", "window_ms": None, "unavailable": [{"id": 0, "reason": "warmup"}]})
    assert sample() == ({0: 50.0}, {"mode": "interval", "window_ms": 1000.0, "unavailable": []})


def test_counters_are_independent(monkeypatch):
    p = Proc(monkeypatch)
    a, b = p.counters(), p.counters()
    p.sample(a, 0, {0: idle_busy(0, 0)})
    p.sample(a, 1, {0: idle_busy(10, 10)})
    assert p.sample(b, 0, {0: idle_busy(20, 20)})[1]["unavailable"] == [{"id": 0, "reason": "warmup"}]
    # a compares with its own last reading (10, 10): 33.3 %; against b's (20, 20) it would read 0 %
    assert p.sample(a, 1, {0: idle_busy(30, 20)})[0] == {0: 33.3}


@pytest.mark.parametrize("compatible, has_dmi, expected", [
    ("nvidia,p3767-0005\0nvidia,tegra234", False, ("Jetson Orin", jetson)),
    ("raspberrypi,5-model-b\0brcm,bcm2712", False, ("Raspberry Pi", None)),
    ("", True, ("PC", None)),
    ("rockchip,rk3588", False, ("Linux", None)),
])
def test_detect(compatible, has_dmi, expected):
    assert detect(compatible, has_dmi) == expected


def without_host_accelerators(monkeypatch, tmp_path):
    """Read the GPU and the NPU from empty trees, so the hardware of the machine running the tests does
    not decide what the collector reports."""
    import functools

    import collector
    empty = tmp_path / "no-accelerators"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(collector, "Gpu", functools.partial(collector.Gpu, root=str(empty)))
    monkeypatch.setattr(collector, "Npu", functools.partial(collector.Npu, root=str(empty)))
    return empty


def without_host_sensors(monkeypatch, tmp_path):
    """Read the optional sensors from an empty tree. Tests that run collect() against fake /proc/stat and
    fake counters otherwise pick up this host's thermal zones, hwmon chips and GPU, so one unreadable
    sensor (an EIO fan, a denied zone) makes the service degraded and the test fail on that machine alone."""
    import functools
    empty = tmp_path / "no-sensors"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(common, "thermal_zones", functools.partial(common.thermal_zones, root=str(empty)))
    monkeypatch.setattr(common, "hwmon_sensors", functools.partial(common.hwmon_sensors, root=str(empty)))
    without_host_accelerators(monkeypatch, tmp_path)
    return empty


def fake_hwmon(root, chips):
    for i, files in enumerate(chips):
        d = root / f"hwmon{i}"
        d.mkdir()
        for name, value in files.items():
            (d / name).write_text(str(value))
    return str(root)


def test_hwmon_rails_and_pwm_only_fan(tmp_path):
    """ina3221 rails, unlabeled sum channel ignored, pwm-fan without tach, non-standard rpm left to boards."""
    temps, power, fans = hwmon_sensors(root=fake_hwmon(tmp_path, [
        {"name": "pwmfan", "pwm1": 76},
        {"name": "ina3221", "in1_label": "VDD_IN", "in1_input": 5000, "curr1_input": 1200,
         "in4_input": 6400, "curr4_input": 2416},
        {"name": "pwm_tach", "rpm": 1636},
    ]))
    assert temps == {}
    assert power == {"VDD_IN": 6.0}
    assert fans == [{"name": "pwmfan", "rpm": None, "percent": 30}]


def test_hwmon_pc(tmp_path):
    """acpitz skipped (already a thermal zone), duplicate nvme labels kept apart, fan with pwm."""
    temps, power, fans = hwmon_sensors(skip={"acpitz"}, root=fake_hwmon(tmp_path, [
        {"name": "acpitz", "temp1_input": 27800},
        {"name": "coretemp", "temp1_label": "Package id 0", "temp1_input": 45000},
        {"name": "nvme", "temp1_label": "Composite", "temp1_input": 38850},
        {"name": "nvme", "temp1_label": "Composite", "temp1_input": 41850},
        {"name": "nct6775", "fan2_input": 950, "pwm2": 128, "power1_input": 12500000},
    ]))
    assert temps == {"coretemp Package id 0": 45.0, "nvme Composite": 38.85,
                     "nvme Composite (hwmon3)": 41.85}
    assert power == {"nct6775 power1": 12.5}
    assert fans == [{"name": "nct6775 fan2", "rpm": 950, "percent": 50}]


def test_thermal_zones(tmp_path):
    """Zone types reported as hwmon names ("-" -> "_"), duplicate zone types kept apart."""
    for i, (zt, t) in enumerate([("cpu-thermal", 51200), ("acpitz", 27800), ("acpitz", 30000)]):
        z = tmp_path / f"thermal_zone{i}"
        z.mkdir()
        (z / "type").write_text(zt)
        (z / "temp").write_text(str(t))
    temps, zone_types = thermal_zones(root=str(tmp_path))
    assert temps == {"cpu": 51.2, "acpitz": 27.8, "acpitz (thermal_zone2)": 30.0}
    assert zone_types == {"cpu_thermal", "acpitz"}


def test_host_paths_from_env(tmp_path):
    """Containers point PLATMON_DEVICE_TREE / PLATMON_HOST_ROOT at bind mounts of the host's paths:
    board detection, disk usage, OS name and (Jetson) L4T release all come from the host, not the image."""
    import json
    import os
    import subprocess
    import sys

    dt = tmp_path / "dt"
    dt.mkdir()
    (dt / "compatible").write_bytes(b"nvidia,p3767-0005\0nvidia,tegra234\0")
    (dt / "model").write_bytes(b"Test Board\0")
    root = tmp_path / "host"
    (root / "etc").mkdir(parents=True)
    (root / "usr/lib").mkdir(parents=True)
    (root / "usr/lib/os-release").write_text('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 22.04.5 LTS"\n')
    (root / "etc/os-release").symlink_to("../usr/lib/os-release")  # relative, as on Ubuntu/Debian
    (root / "etc/nv_tegra_release").write_text("# R36 (release), REVISION: 5.2, GCID: 1, BOARD: generic\n")
    code = ("import collector, json, shutil; s = collector.collect(); "
            "print(json.dumps([collector.PLATFORM, s['model'], s['disk']['total'], shutil.disk_usage(%r).total, "
            "s['system']['os'], s['system'].get('l4t')]))" % str(root))
    env = dict(os.environ, PLATMON_DEVICE_TREE=str(dt), PLATMON_HOST_ROOT=str(root))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    platform, model, disk_total, expected_total, os_name, l4t = json.loads(out.stdout)
    assert (platform, model) == ("Jetson Orin", "Test Board")
    assert disk_total == expected_total
    assert (os_name, l4t) == ("Ubuntu 22.04.5 LTS", "R36.5.2")


@pytest.mark.parametrize("text, expected", [
    ('NAME="Ubuntu"\nVERSION="22.04.5 LTS (Jammy Jellyfish)"\nPRETTY_NAME="Ubuntu 22.04.5 LTS"\n', "Ubuntu 22.04.5 LTS"),
    ("NAME='Raspbian GNU/Linux'\nVERSION='12 (bookworm)'\n", "Raspbian GNU/Linux 12 (bookworm)"),  # no PRETTY_NAME
    ("# comment only\n", None),
    (None, None),  # file missing
])
def test_os_release(text, expected):
    assert os_release(text) == expected


def test_hwmon_same_names_kept_apart(tmp_path):
    """Two chips reporting the same power sensor or rail name: both values stay (first keeps the plain name)."""
    temps, power, fans = hwmon_sensors(root=fake_hwmon(tmp_path, [
        {"name": "ina219", "power1_input": 1000000},
        {"name": "ina219", "power1_input": 2000000},
        {"name": "ina3221", "in1_label": "VDD_IN", "in1_input": 5000, "curr1_input": 1000},
        {"name": "ina3221", "in1_label": "VDD_IN", "in1_input": 5000, "curr1_input": 2000},
    ]))
    assert power == {"ina219 power1": 1.0, "ina219 power1 (hwmon1)": 2.0,
                     "VDD_IN": 5.0, "VDD_IN (hwmon3)": 10.0}


def test_hwmon_unreadable_fan_is_none_not_zero(tmp_path):
    """A fan whose speed cannot be read is unknown (None), not stopped (0)."""
    root = fake_hwmon(tmp_path, [{"name": "nct6775", "fan1_label": "CPU fan"}])
    (tmp_path / "hwmon0" / "fan1_input").mkdir()  # exists, but reading it fails
    assert hwmon_sensors(root=root)[2] == [{"name": "CPU fan", "rpm": None, "percent": None}]


def test_service_path_reads_once_and_does_not_wait(monkeypatch):
    import collector
    reads, real = [], common.cpu_times
    monkeypatch.setattr(common, "cpu_times", lambda: reads.append(1) or real())

    def no_sleep(seconds):
        raise AssertionError(f"slept {seconds} s in the service path")
    monkeypatch.setattr(common.time, "sleep", no_sleep)
    c = CpuCounters()
    first, second = collector.collect(c), collector.collect(c)
    assert len(reads) == 2  # one /proc/stat reading per collection
    assert first["cpu"] == [] and {u["reason"] for u in first["cpu_sampling"]["unavailable"]} == {"warmup"}
    assert second["cpu_sampling"]["mode"] == "interval" and second["cpu_sampling"]["window_ms"] > 0


def test_oneshot_does_not_touch_the_service_baseline(monkeypatch):
    p = Proc(monkeypatch)
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)
    c = p.counters()
    p.sample(c, 0, {0: idle_busy(0, 0)})
    p.readings += [{0: idle_busy(1000, 0)}, {0: idle_busy(1000, 1000)}]  # the one-off call's own two readings
    s = common.collect()
    assert s["cpu"][0]["usage"] == 100.0 and s["cpu_sampling"]["mode"] == "oneshot"
    assert p.sample(c, 1, {0: idle_busy(10, 10)})[0] == {0: 50.0}  # still its own baseline, not the one-shot's


def check_oneshot(s):
    """What a one-off reading must look like on any host: every CPU read is either measured or listed with
    a one-shot reason, once; there may be none measured (e.g. a single CPU whose iowait went down)."""
    sampling = s["cpu_sampling"]
    assert sampling["mode"] == "oneshot"
    measured = [c["id"] for c in s["cpu"]]
    missing = [u["id"] for u in sampling["unavailable"]]
    assert measured or missing
    assert len(set(measured + missing)) == len(measured) + len(missing)  # each CPU in one list
    assert all(0 <= c["usage"] <= 100 for c in s["cpu"])
    assert {u["reason"] for u in sampling["unavailable"]} <= {"warmup", "counter_regressed", "no_ticks"}


def test_direct_collect_is_a_oneshot_reading():
    """collect() without counters, on this host: two real readings 250 ms apart, nothing kept. Which CPUs
    have a usage depends on the host (exact values and reasons are tested with fixtures)."""
    import collector
    s = collector.collect()
    assert s["cpu_sampling"]["window_ms"] >= 250
    check_oneshot(s)


def test_oneshot_single_cpu_iowait_drop(monkeypatch):
    """A one-CPU host whose iowait went down between the two readings: no usage at all, not a made-up 0 %."""
    p = Proc(monkeypatch)
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)
    p.readings += [{0: [10, 0, 0, 100, 50, 0, 0, 0]}, {0: [20, 0, 0, 110, 40, 0, 0, 0]}]
    s = common.collect()
    assert s["cpu"] == [] and s["cpu_sampling"]["unavailable"] == [{"id": 0, "reason": "counter_regressed"}]
    check_oneshot(s)


def test_oneshot_reports_unusable_cpus(monkeypatch):
    """The one-off path uses the same rules: a CPU that appears between its two readings warms up, one whose
    iowait went down is counter_regressed, and the others are still measured."""
    p = Proc(monkeypatch)
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)
    p.readings += [{0: idle_busy(0, 0), 1: [0, 0, 0, 0, 50, 0, 0, 0]},
                   {0: idle_busy(10, 10), 1: [0, 0, 0, 100, 40, 0, 0, 0], 2: idle_busy(5, 5)}]
    s = common.collect()
    assert [(c["id"], c["usage"]) for c in s["cpu"]] == [(0, 50.0)]
    assert s["cpu_sampling"]["mode"] == "oneshot" and s["cpu_sampling"]["unavailable"] == [
        {"id": 1, "reason": "counter_regressed"}, {"id": 2, "reason": "warmup"}]
    check_oneshot(s)


# optional metrics: what could not be read is noted per group (collectors.<group>), the rest is kept

import errno as _errno
import json as _json

from collector import sysfs


def fail_reads(monkeypatch, errors):
    """Reads of the given paths raise the given OSError (EACCES, EIO, ENODATA ...); other paths read normally."""
    real = open

    def fake(path, *args, **kw):
        if str(path) in errors:
            code = errors[str(path)]
            raise OSError(code, _errno.errorcode[code]) if code != _errno.EACCES else PermissionError(code, "denied")
        return real(path, *args, **kw)
    monkeypatch.setattr(sysfs, "open", fake, raising=False)


@pytest.mark.parametrize("content, expected, reason", [
    ("0", 0, None),             # a measured 0 is a value
    ("42000", 42000, None),
    ("", None, "invalid_data"),  # an empty number is not 0
    ("4x", None, "invalid_data"),
])
def test_group_read_values(tmp_path, content, expected, reason):
    (tmp_path / "v").write_text(content)
    g = sysfs.Group("t")
    assert g.read(str(tmp_path / "v"), "v") == expected
    assert g.reasons == ({reason} if reason else set())


@pytest.mark.parametrize("code, reason", [(_errno.EACCES, "permission_denied"), (_errno.EPERM, "permission_denied"),
                                          (_errno.EIO, "io_error"), (_errno.ENODATA, "no_data")])
def test_group_read_errors(tmp_path, monkeypatch, code, reason):
    path = tmp_path / "v"
    path.write_text("1")
    fail_reads(monkeypatch, {str(path): code})
    g = sysfs.Group("t")
    assert g.read(str(path), "v") is None and g.reasons == {reason}
    assert g.issues == ([{"target": "v", "reason": reason}] if reason != "no_data" else [])  # no_data: not a problem
    assert str(tmp_path) not in _json.dumps(g.status())  # paths and error text stay out of the API


def test_group_read_missing_file(tmp_path):
    g = sysfs.Group("t")
    assert g.read(str(tmp_path / "pwm1"), "pwm1", found=str(tmp_path)) is None
    assert g.reasons == {"not_exposed"} and g.issues == []  # an attribute this device does not have
    assert g.read(str(tmp_path / "gone/temp1_input"), "temp1", found=str(tmp_path / "gone")) is None
    assert g.issues == [{"target": "temp1", "reason": "disappeared"}]  # listed, then gone before the read


@pytest.mark.parametrize("values, reasons, expected", [
    (2, [], ("ok", None)),
    (2, ["not_exposed"], ("ok", None)),                     # an attribute it does not have is fine
    (1, ["invalid_data"], ("partial", "some_unreadable")),
    (0, ["invalid_data", "permission_denied", "io_error"], ("error", "permission_denied")),  # most serious first
    (0, ["disappeared", "invalid_data"], ("error", "disappeared")),
    (0, ["internal_error", "permission_denied"], ("error", "internal_error")),
    (0, ["not_exposed", "no_data"], ("unavailable", "no_data")),
    (0, ["not_detected", "not_exposed"], ("unavailable", "not_exposed")),
    (0, ["unsupported_platform"], ("unavailable", "unsupported_platform")),
    (0, [], ("unavailable", "not_detected")),               # nothing tried is never ok
])
def test_group_status(values, reasons, expected):
    g = sysfs.Group("t")
    g.values = values
    for i, r in enumerate(reasons):
        g.note(f"x{i}", r)
    st = g.status()
    assert (st["state"], st["reason"]) == expected


def test_group_issues_are_capped():
    g = sysfs.Group("t")
    for i in range(40):
        g.note("hwmon0.temp%d" % i + "x" * 100, "io_error")
    st = g.status()
    assert len(st["issues"]) == 32 and st["issues_truncated"] == 8
    assert all(len(i["target"]) <= 64 for i in st["issues"]) and len(g.details) == 32


def test_one_bad_temperature_keeps_the_rest(tmp_path):
    temps, power, fans = hwmon_sensors(root=fake_hwmon(tmp_path, [
        {"name": "coretemp", "temp1_input": 45000, "temp2_input": "", "temp3_input": "garbage", "temp4_input": 0},
        {"name": "ina219", "power1_input": 1000000},
        {"name": "nct6775", "fan1_input": 900, "pwm1": 255},
    ]))
    assert temps == {"coretemp temp1": 45.0, "coretemp temp4": 0.0} and power == {"ina219 power1": 1.0}
    assert fans == [{"name": "nct6775 fan1", "rpm": 900, "percent": 100}]
    g = sysfs.groups("temperature", "power", "fans")
    hwmon_sensors(root=str(tmp_path), groups=g)
    assert g["temperature"].status() == {"state": "partial", "reason": "some_unreadable", "issues_truncated": 0,
                                         "issues": [{"target": "hwmon0.temp2", "reason": "invalid_data"},
                                                    {"target": "hwmon0.temp3", "reason": "invalid_data"}]}
    assert g["power"].status()["state"] == g["fans"].status()["state"] == "ok"


def test_partial_power_and_fan_channels(tmp_path, monkeypatch):
    """A rail whose voltage cannot be read is left out, the chip's other rail stays; a fan whose speed
    cannot be read keeps its pwm percent (and a measured 0 rpm stays 0)."""
    root = fake_hwmon(tmp_path, [
        {"name": "ina3221", "in1_label": "VDD_IN", "in1_input": 5000, "curr1_input": 1200,
         "in2_label": "VDD_SOC", "in2_input": 5000, "curr2_input": 400},
        {"name": "nct6775", "fan1_input": 950, "pwm1": 128, "fan2_input": 0, "pwm2": 0},
    ])
    fail_reads(monkeypatch, {f"{root}/hwmon0/in2_input": _errno.EIO, f"{root}/hwmon1/fan1_input": _errno.EACCES})
    g = sysfs.groups("temperature", "power", "fans")
    temps, power, fans = hwmon_sensors(root=root, groups=g)
    assert power == {"VDD_IN": 6.0}
    assert fans == [{"name": "nct6775 fan1", "rpm": None, "percent": 50},
                    {"name": "nct6775 fan2", "rpm": 0, "percent": 0}]
    assert g["power"].status()["issues"] == [{"target": "hwmon0.in2", "reason": "io_error"}]
    assert g["fans"].status()["issues"] == [{"target": "hwmon1.fan1", "reason": "permission_denied"}]
    assert g["temperature"].status()["state"] == "unavailable"  # no temperature channel: absent, not an error


def test_channel_that_disappears_then_recovers(tmp_path, monkeypatch):
    root = fake_hwmon(tmp_path, [{"name": "coretemp", "temp1_input": 45000}])
    real = common.numbered
    monkeypatch.setattr(common, "numbered", lambda names, prefix, suffix: real(names, prefix, suffix) + ([2] if prefix == "temp" else []))
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(root=root, groups=g)[0] == {"coretemp temp1": 45.0}  # temp2 listed, gone when read
    assert g["temperature"].status()["issues"] == [{"target": "hwmon0.temp2", "reason": "disappeared"}]
    monkeypatch.setattr(common, "numbered", real)
    g = sysfs.groups("temperature", "power", "fans")
    hwmon_sensors(root=root, groups=g)
    assert g["temperature"].status() == {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0}


def test_unlistable_hwmon_class(tmp_path, monkeypatch):
    def denied(path):
        raise PermissionError(_errno.EACCES, "denied")
    monkeypatch.setattr(sysfs.os, "listdir", denied)
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(root=str(tmp_path), groups=g) == ({}, {}, [])
    assert g["temperature"].status()["reason"] == "permission_denied"  # could not look, not "nothing there"


def test_inactive_thermal_zone_is_no_data(tmp_path, monkeypatch):
    for i, (zt, t) in enumerate([("cpu-thermal", 51200), ("cv0-thermal", 0)]):
        (tmp_path / f"thermal_zone{i}").mkdir()
        (tmp_path / f"thermal_zone{i}" / "type").write_text(zt)
        (tmp_path / f"thermal_zone{i}" / "temp").write_text(str(t))
    fail_reads(monkeypatch, {str(tmp_path / "thermal_zone1" / "temp"): _errno.ENODATA})
    g = sysfs.Group("temperature")
    assert thermal_zones(root=str(tmp_path), group=g)[0] == {"cpu": 51.2}
    assert g.status() == {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0}


def test_fan_speed_the_firmware_does_not_report_is_no_data(tmp_path, monkeypatch):
    """A board that declares a fan but reports no tachometer (acpi_fan, EIO on fan1_input) has no speed to
    read: an absent value like an inactive thermal zone, not a failure that makes the service degraded.
    Another failure on the same attribute (no permission) stays a failure."""
    root = fake_hwmon(tmp_path, [{"name": "acpi_fan", "fan1_input": 0}])
    fail_reads(monkeypatch, {f"{root}/hwmon0/fan1_input": _errno.EIO})
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(root=root, groups=g)[2] == [{"name": "acpi_fan fan1", "rpm": None, "percent": None}]
    assert g["fans"].status() == {"state": "unavailable", "reason": "no_data", "issues": [], "issues_truncated": 0}
    assert g["fans"].details == []  # no failure, so nothing for the service log either
    fail_reads(monkeypatch, {f"{root}/hwmon0/fan1_input": _errno.EACCES})
    g = sysfs.groups("temperature", "power", "fans")
    hwmon_sensors(root=root, groups=g)
    assert g["fans"].status()["state"] == "error"
    assert g["fans"].status()["issues"] == [{"target": "hwmon0.fan1", "reason": "permission_denied"}]


def test_unexpected_error_in_one_group_is_internal_error(monkeypatch):
    def broken(root, *groups):
        raise RuntimeError("bug with /secret/path")
    monkeypatch.setattr(common, "hwmon_chips", broken)
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(groups=g) == ({}, {}, [])
    for name in ("temperature", "power", "fans"):
        assert g[name].status()["issues"] == [{"target": name, "reason": "internal_error"}]
    assert "secret" not in _json.dumps({n: x.status() for n, x in g.items()})  # traceback only in the log
    assert "secret" in g["temperature"].details[0]


def test_cpu_frequency_failure_keeps_usage(monkeypatch):
    p = Proc(monkeypatch)
    c = p.counters()
    p.readings += [{0: idle_busy(0, 0), 1: idle_busy(0, 0)}, {0: idle_busy(10, 10), 1: idle_busy(20, 0)}]
    freq = "/sys/devices/system/cpu/cpu{}/cpufreq/scaling_cur_freq"
    real = open

    def fake(path, *args, **kw):
        if path == freq.format(0):
            raise OSError(_errno.EIO, "EIO")
        if path == freq.format(1):
            import io
            return io.StringIO("1200000\n")
        return real(path, *args, **kw)
    monkeypatch.setattr(sysfs, "open", fake, raising=False)
    first = common.collect(c)
    assert first["cpu"] == [] and first["collectors"]["cpu_frequency"]["reason"] == "no_data"  # warmup: no rows
    p.ns += 10**9
    s = common.collect(c)
    assert s["cpu"] == [{"id": 0, "usage": 50.0, "freq": None}, {"id": 1, "usage": 0.0, "freq": 1200000000}]
    assert s["collectors"]["cpu_frequency"] == {"state": "partial", "reason": "some_unreadable", "issues_truncated": 0,
                                                "issues": [{"target": "cpu0", "reason": "io_error"}]}
    assert s["cpu_sampling"]["window_ms"] is not None  # the CPU baseline went on as before


def test_no_optional_sensors_is_not_an_error(tmp_path, monkeypatch):
    """A plain Linux host without cpufreq, thermal zones, hwmon chips or a DRM card: everything required,
    nothing degraded."""
    import collector
    monkeypatch.setattr(collector, "BOARD", None)  # a plain Linux host, whatever board runs the tests
    without_host_sensors(monkeypatch, tmp_path)
    monkeypatch.setattr(sysfs, "open", lambda path, *a, **kw: (_ for _ in ()).throw(FileNotFoundError(path))
                        if "cpufreq" in str(path) else open(path, *a, **kw), raising=False)
    s = collector.collect()
    states = {k: v["state"] for k, v in s["collectors"].items()}
    assert set(states.values()) == {"unavailable"} and s["cpu"] and s["memory"]["total"] > 0


def test_problems_are_logged_when_they_change(capsys):
    logged = {}

    def run(reason=None):
        g = sysfs.groups("temperature")
        if reason:
            g["temperature"].note("hwmon0.temp1", reason, "/sys/class/hwmon/hwmon0/temp1_input: Input/output error")
        return sysfs.summarize(g, logged)

    run("io_error")
    assert "temperature: hwmon0.temp1: io_error (/sys/class/hwmon/hwmon0/temp1_input" in capsys.readouterr().err
    run("io_error")
    assert capsys.readouterr().err == ""  # the same problem again: not logged every second
    run()
    assert "temperature: readable again" in capsys.readouterr().err


# PR #24 review: bytes that do not decode, listing errors inside the walk, unreadable rail labels

def deny_listing(monkeypatch, path, code):
    """Listing this directory fails (os.listdir and os.scandir, which glob uses); others list normally."""
    real_listdir, real_scandir = os.listdir, os.scandir

    def check(p):
        if os.path.normpath(str(p)) == os.path.normpath(str(path)):
            raise OSError(code, _errno.errorcode[code]) if code != _errno.EACCES else PermissionError(code, "denied")
    monkeypatch.setattr(os, "listdir", lambda p=".": check(p) or real_listdir(p))
    monkeypatch.setattr(os, "scandir", lambda p=".": check(p) or real_scandir(p))


def test_undecodable_temperature_keeps_the_rest(tmp_path):
    root = fake_hwmon(tmp_path, [{"name": "chip", "temp1_input": 45000, "temp3_input": 42000,
                                  "power1_input": 1000000, "fan1_input": 900, "pwm1": 128}])
    (tmp_path / "hwmon0" / "temp2_input").write_bytes(b"\xff\n")
    g = sysfs.groups("temperature", "power", "fans")
    temps, power, fans = hwmon_sensors(root=root, groups=g)
    assert temps == {"chip temp1": 45.0, "chip temp3": 42.0} and power == {"chip power1": 1.0}
    assert fans == [{"name": "chip fan1", "rpm": 900, "percent": 50}]
    assert g["temperature"].status()["issues"] == [{"target": "hwmon0.temp2", "reason": "invalid_data"}]
    assert g["power"].status()["state"] == g["fans"].status()["state"] == "ok"


@pytest.mark.parametrize("code, reason", [(_errno.EACCES, "permission_denied"), (_errno.EIO, "io_error")])
def test_unlistable_chip_is_reported_and_the_others_kept(tmp_path, monkeypatch, code, reason):
    root = fake_hwmon(tmp_path, [{"name": "a", "temp1_input": 40000}, {"name": "b", "temp1_input": 50000}])
    deny_listing(monkeypatch, tmp_path / "hwmon0", code)
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(root=root, groups=g)[0] == {"b temp1": 50.0}
    assert g["temperature"].status() == {"state": "partial", "reason": "some_unreadable", "issues_truncated": 0,
                                         "issues": [{"target": "hwmon0", "reason": reason}]}


def test_unlistable_thermal_class(tmp_path, monkeypatch):
    (tmp_path / "thermal_zone0").mkdir()
    deny_listing(monkeypatch, tmp_path, _errno.EIO)
    g = sysfs.Group("temperature")
    assert thermal_zones(root=str(tmp_path), group=g)[0] == {}
    assert g.status()["reason"] == "io_error"


def test_empty_or_missing_classes_are_not_errors(tmp_path):
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(root=str(tmp_path), groups=g) == ({}, {}, [])  # empty class directory
    assert hwmon_sensors(root=str(tmp_path / "missing"), groups=g) == ({}, {}, [])
    assert thermal_zones(root=str(tmp_path / "missing"), group=g["temperature"])[0] == {}
    assert {x.status()["state"] for x in g.values()} == {"unavailable"}


def test_unreadable_rail_label_is_reported(tmp_path, monkeypatch):
    rails = [{"name": "ina3221", "in1_label": "VDD_IN", "in1_input": 5000, "curr1_input": 1000,
              "in2_label": "VDD_SOC", "in2_input": 5000, "curr2_input": 400, "in3_input": 5000, "curr3_input": 1400}]
    root = fake_hwmon(tmp_path, rails)  # channel 3 has no label: a sum channel, skipped as before
    fail_reads(monkeypatch, {f"{root}/hwmon0/in1_label": _errno.EIO})
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(root=root, groups=g)[1] == {"VDD_SOC": 2.0}
    assert g["power"].status() == {"state": "partial", "reason": "some_unreadable", "issues_truncated": 0,
                                   "issues": [{"target": "hwmon0.in1_label", "reason": "io_error"}]}
    monkeypatch.setattr(sysfs, "open", open, raising=False)  # next collection: readable again
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(root=root, groups=g)[1] == {"VDD_IN": 5.0, "VDD_SOC": 2.0}
    assert g["power"].status()["state"] == "ok"


# PR #24 re-review: names and display labels that do not decode

TWO_CHIPS = [{"name": "a", "temp1_input": 40000, "temp1_label": "Package", "power1_input": 1000000,
              "power1_label": "VIN", "fan1_input": 900, "fan1_label": "CPU fan", "pwm1": 255},
             {"name": "b", "temp1_input": 50000, "power1_input": 2000000, "fan1_input": 800, "pwm1": 0}]
CHIP_B = ({"b temp1": 50.0}, {"b power1": 2.0}, [{"name": "b fan1", "rpm": 800, "percent": 0}])


@pytest.mark.parametrize("file, kept, groups_with_issue", [
    # the chip name is used for every key: hwmon0's values are kept under its directory name
    ("name", ({"hwmon0 Package": 40.0}, {"VIN": 1.0}, [{"name": "CPU fan", "rpm": 900, "percent": 100}]),
     {"temperature", "power", "fans"}),
    ("temp1_label", ({"a temp1": 40.0}, {"VIN": 1.0}, [{"name": "CPU fan", "rpm": 900, "percent": 100}]), {"temperature"}),
    ("power1_label", ({"a Package": 40.0}, {"a power1": 1.0}, [{"name": "CPU fan", "rpm": 900, "percent": 100}]), {"power"}),
    ("fan1_label", ({"a Package": 40.0}, {"VIN": 1.0}, [{"name": "a fan1", "rpm": 900, "percent": 100}]), {"fans"}),
])
def test_undecodable_name_or_label_keeps_the_values(tmp_path, file, kept, groups_with_issue):
    root = fake_hwmon(tmp_path, TWO_CHIPS)
    good = (tmp_path / "hwmon0" / file).read_bytes()
    (tmp_path / "hwmon0" / file).write_bytes(b"\xff\n")
    g = sysfs.groups("temperature", "power", "fans")
    temps, power, fans = hwmon_sensors(root=root, groups=g)
    assert (temps, power, fans) == ({**kept[0], **CHIP_B[0]}, {**kept[1], **CHIP_B[1]}, kept[2] + CHIP_B[2])
    for name, grp in g.items():
        st = grp.status()
        if name in groups_with_issue:
            assert st["state"] == "partial" and st["issues"] == [{"target": f"hwmon0.{file}", "reason": "invalid_data"}]
        else:
            assert st["state"] == "ok"
    (tmp_path / "hwmon0" / file).write_bytes(good)  # readable again in the next collection
    g = sysfs.groups("temperature", "power", "fans")
    assert hwmon_sensors(root=root, groups=g) == hwmon_sensors(root=root) and len(hwmon_sensors(root=root)[0]) == 2
    assert {x.status()["state"] for x in g.values()} == {"ok"}


def test_undecodable_zone_type_keeps_the_zones(tmp_path):
    for i, (zt, t) in enumerate([("cpu-thermal", 51200), ("gpu-thermal", 52000)]):
        (tmp_path / f"thermal_zone{i}").mkdir()
        (tmp_path / f"thermal_zone{i}" / "type").write_text(zt)
        (tmp_path / f"thermal_zone{i}" / "temp").write_text(str(t))
    (tmp_path / "thermal_zone0" / "type").write_bytes(b"\xff\n")
    g = sysfs.Group("temperature")
    assert thermal_zones(root=str(tmp_path), group=g)[0] == {"thermal_zone0": 51.2, "gpu": 52.0}
    assert g.status()["issues"] == [{"target": "thermal_zone0.type", "reason": "invalid_data"}]
