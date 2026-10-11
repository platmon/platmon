"""Sensor source ids, read spans and the read-based data age (sensor_meta, sample.data_age_basis).
Fake sysfs trees with real symlinks and a fake elapsed clock; nothing on the host is read for sensors."""
import functools
import io
import json
import os
import subprocess
import sys

import pytest

import collector
from collector import common, jetson, sysfs
from collector.common import CpuCounters
from collector.sampler import REQUIRED_READS, Collected, Sampler, sensor_values
from test_common import Proc, idle_busy
from test_sampler import Clock, fake


class Tick:
    """Elapsed clock that moves 1 µs on every call, so each read gets its own start and end."""

    def __init__(self):
        self.ns = 10**12

    def __call__(self):
        self.ns += 1000
        return self.ns


def chip(sysroot, device, n, files):
    """A hwmon chip at <sysroot>/<device>/hwmon/hwmon<n>, linked from class/hwmon like the kernel does."""
    d = sysroot / device / "hwmon" / f"hwmon{n}"
    d.mkdir(parents=True)
    for name, value in files.items():
        (d / name).write_text(str(value))
    link = sysroot / "class" / "hwmon" / f"hwmon{n}"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(d, link.parent))
    return d


def zone(sysroot, n, type_, temp):
    d = sysroot / "devices" / "virtual" / "thermal" / f"thermal_zone{n}"
    d.mkdir(parents=True)
    (d / "type").write_text(type_)
    (d / "temp").write_text(str(temp))
    link = sysroot / "class" / "thermal" / f"thermal_zone{n}"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(d, link.parent))


def traced(*names, clock=None):
    return sysfs.groups(*names, trace=sysfs.Trace(clock or Tick()))


def hwmon(sysroot, clock=None):
    g = traced("temperature", "power", "fans", clock=clock)
    out = common.hwmon_sensors(root=str(sysroot / "class" / "hwmon"), groups=g)
    return out, g


CHIP = {"name": "ina", "temp1_input": 40000, "temp1_label": "Board", "power1_input": 2000000, "power1_label": "VIN",
        "in2_label": "VDD", "in2_input": 5000, "curr2_input": 1000, "fan1_input": 900, "pwm1": 128}


# ---------- source identity ----------

def test_descriptor_id_is_fixed():
    """sha256 of a sorted, compact JSON descriptor: the same in any process and hash seed."""
    expected = "src1:dc1e592a9185fe57929e05c59a5759c4bb2cdd10e04a85525241df68d9a80bd0"
    src = sysfs.source("hwmon", "celsius", "device_channel", device="devices/platform/i2c/1-0040", attr="temp1_input")
    assert src == {"id": expected, "provider": "hwmon", "identity_basis": "device_channel", "unit": "celsius"}
    code = ("from collector import sysfs; print(sysfs.source('hwmon', 'celsius', 'device_channel', "
            "device='devices/platform/i2c/1-0040', attr='temp1_input')['id'])")
    for seed in ("0", "12345"):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             env=dict(os.environ, PYTHONHASHSEED=seed), cwd=os.path.dirname(os.path.dirname(__file__)))
        assert out.stdout.strip() == expected
    assert sysfs.source("hwmon", "celsius", "unresolved") == {
        "id": None, "provider": "hwmon", "identity_basis": "unresolved", "unit": "celsius"}


def test_label_change_keeps_id_and_channels_differ(tmp_path):
    d = chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    (_, power, _), g = hwmon(tmp_path)
    first = {k: v["id"] for k, v in g["temperature"].sensors.items()}
    assert list(first) == ["ina Board"]
    (d / "temp1_label").write_text("Renamed")
    _, g2 = hwmon(tmp_path)
    assert {k: v["id"] for k, v in g2["temperature"].sensors.items()} == {"ina Renamed": first["ina Board"]}
    ids = [g["power"].sensors["VIN"]["id"], g["power"].sensors["VDD"]["id"], first["ina Board"]]
    fan = [v["id"] for v in g["fans"].sensors.values()]
    assert len(set(ids + fan)) == 5  # temp, direct power, computed power, rpm, pwm: all different
    assert g["power"].sensors["VIN"]["identity_basis"] == "device_channel"


def test_same_name_on_two_devices_differs(tmp_path):
    chip(tmp_path, "devices/platform/i2c/1-0040", 1, {"name": "nvme", "temp1_input": 40000, "temp1_label": "Composite"})
    chip(tmp_path, "devices/platform/i2c/1-0041", 2, {"name": "nvme", "temp1_input": 41000, "temp1_label": "Composite"})
    (temps, _, _), g = hwmon(tmp_path)
    assert list(temps) == ["nvme Composite", "nvme Composite (hwmon2)"]
    a, b = (g["temperature"].sensors[k]["id"] for k in temps)
    assert a != b


def test_renumbered_hwmon_keeps_device_id(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"  # also: different access prefixes give the same ids
    chip(a, "devices/platform/i2c/1-0040", 3, CHIP)
    chip(b, "devices/platform/i2c/1-0040", 7, CHIP)  # same device, enumerated as hwmon7 this time
    _, ga = hwmon(a)
    _, gb = hwmon(b)
    for name in ("temperature", "power"):
        assert {k: v["id"] for k, v in ga[name].sensors.items()} == {k: v["id"] for k, v in gb[name].sensors.items()}


def test_two_hwmon_nodes_on_one_device_are_not_merged(tmp_path):
    chip(tmp_path, "devices/platform/i2c/1-0040", 1, {"name": "x", "temp1_input": 40000})
    chip(tmp_path, "devices/platform/i2c/1-0040", 2, {"name": "y", "temp1_input": 41000})
    (temps, _, _), g = hwmon(tmp_path)
    infos = [g["temperature"].sensors[k] for k in temps]
    assert [i["identity_basis"] for i in infos] == ["resolved_path", "resolved_path"]  # the device alone is ambiguous
    assert infos[0]["id"] != infos[1]["id"]


def test_virtual_hwmon_and_thermal_zone_are_path_based(tmp_path):
    chip(tmp_path, "devices/virtual", 0, {"name": "acpitz", "temp1_input": 30000})
    zone(tmp_path, 0, "cpu-thermal", 51000)
    _, g = hwmon(tmp_path)
    assert g["temperature"].sensors["acpitz temp1"]["identity_basis"] == "resolved_path"  # not "devices/virtual"
    tg = sysfs.Group("temperature", sysfs.Trace(Tick()))
    temps, *_ = common.thermal_zones(root=str(tmp_path / "class" / "thermal"), group=tg)
    assert temps == {"cpu": 51.0}
    assert tg.sensors["cpu"]["provider"] == "thermal" and tg.sensors["cpu"]["identity_basis"] == "resolved_path"


def test_class_alias_and_device_path_resolve_alike(tmp_path):
    d = chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    via_class = sysfs.canonical(str(tmp_path / "class" / "hwmon" / "hwmon3" / "temp1_input"), str(tmp_path))
    assert via_class == sysfs.canonical(str(d / "temp1_input"), str(tmp_path)) == \
        "devices/platform/i2c/1-0040/hwmon/hwmon3/temp1_input"
    assert sysfs.canonical(str(tmp_path / "missing"), str(tmp_path)) is None
    assert sysfs.canonical("/etc/hostname", str(tmp_path)) is None  # outside the sysfs root


def resolution_cases(root):
    """Paths whose resolution differs in kind: aliases, a symlinked root, links inside the path, outside
    the root, missing, dangling, a loop, a file in place of a directory."""
    d = chip(root, "devices/platform/i2c/1-0040", 3, CHIP)
    (root / "devices/virtual").mkdir(parents=True)
    (root / "devices/virtual/alias").symlink_to(root / "devices/platform")  # a link in the middle of a path
    (root / "class/dangling").symlink_to(root / "nowhere")
    (root / "class/loop").symlink_to(root / "class/loop")
    (root / "file").write_text("x")
    outside = root.parent / f"{root.name}-outside"
    outside.mkdir()
    (root / "class/out").symlink_to(outside)
    linked_root = root.parent / f"{root.name}-link"
    linked_root.symlink_to(root)
    paths = [str(root / "class/hwmon/hwmon3"), str(root / "class/hwmon/hwmon3/temp1_input"), str(d),
             str(root / "devices/virtual/alias/i2c/1-0040/hwmon/hwmon3/name"), str(root), str(root / "."),
             str(root / "class/../class/hwmon/hwmon3"), str(root / "missing"), str(root / "class/dangling"),
             str(root / "class/loop"), str(root / "file/below"), str(root / "class/out"), "/etc/hostname"]
    return paths, [str(root), str(linked_root)]


def test_canonical_agrees_with_the_portable_way(tmp_path):
    paths, roots = resolution_cases(tmp_path)
    for root in roots:
        for path in paths:
            assert sysfs.canonical(path, root) == sysfs._canonical_portable(path, root), (path, root)
    assert sysfs.canonical(paths[3], roots[1]) == "devices/platform/i2c/1-0040/hwmon/hwmon3/name"


def test_canonical_without_kernel_resolution(tmp_path, monkeypatch):
    """No O_PATH (not Linux), or /proc not mounted: the same answers, the portable way."""
    paths, roots = resolution_cases(tmp_path)
    expected = [sysfs.canonical(p, r) for r in roots for p in paths]
    monkeypatch.setattr(sysfs, "_O_PATH", None)
    assert [sysfs.canonical(p, r) for r in roots for p in paths] == expected

    def no_proc(path):  # /proc/self/fd cannot be read (os.readlink itself stays: realpath needs it)
        raise FileNotFoundError(f"/proc/self/fd for {path}")
    monkeypatch.setattr(sysfs, "_O_PATH", getattr(os, "O_PATH", 0))
    monkeypatch.setattr(sysfs, "_kernel_path", no_proc)
    assert [sysfs.canonical(p, r) for r in roots for p in paths] == expected


def between_resolutions(monkeypatch, change):
    """Runs change() after the first _kernel_path call of each canonical(), i.e. between its two
    resolutions."""
    real = sysfs._kernel_path
    calls = []

    def hooked(p):
        out = real(p)
        calls.append(p)
        if len(calls) % 2 == 1:
            change()
        return out
    monkeypatch.setattr(sysfs, "_kernel_path", hooked)
    return calls


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="kernel resolution is Linux-only")
def test_a_node_removed_while_resolving_is_not_reported(tmp_path, monkeypatch):
    """The path is the last thing resolved, so a node gone by then gives None, as the portable way's
    final existence check does; never the path it had a moment earlier."""
    import shutil
    d = chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    link = tmp_path / "class/hwmon/hwmon3"
    calls = between_resolutions(monkeypatch, lambda: shutil.rmtree(d))
    assert sysfs.canonical(str(link), str(tmp_path)) is None
    assert calls == [str(tmp_path)]  # the root resolved first; the path, resolved last, was gone
    assert sysfs._canonical_portable(str(link), str(tmp_path)) is None


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="kernel resolution is Linux-only")
def test_a_link_retargeted_while_resolving_gives_the_new_target(tmp_path, monkeypatch):
    chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    other = tmp_path / "devices/platform/i2c/1-0041/hwmon/hwmon3"
    other.mkdir(parents=True)
    link = tmp_path / "class/hwmon/hwmon3"

    def retarget():
        link.unlink()
        link.symlink_to(os.path.relpath(other, link.parent))
    between_resolutions(monkeypatch, retarget)
    assert sysfs.canonical(str(link), str(tmp_path)) == "devices/platform/i2c/1-0041/hwmon/hwmon3"


def test_a_retargeted_link_resolves_to_its_new_device(tmp_path):
    """Same name, new target: the next resolution follows it; nothing from before is reused."""
    chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    first = sysfs.chip_identities([(str(tmp_path / "class/hwmon/hwmon3"), "ina")], str(tmp_path))
    other = tmp_path / "devices/platform/i2c/1-0041/hwmon/hwmon3"
    other.mkdir(parents=True)
    link = tmp_path / "class/hwmon/hwmon3"
    link.unlink()
    link.symlink_to(os.path.relpath(other, link.parent))
    second = sysfs.chip_identities([(str(link), "ina")], str(tmp_path))
    assert first[str(link)] == ("device_channel", {"device": "devices/platform/i2c/1-0040"})
    assert second[str(link)] == ("device_channel", {"device": "devices/platform/i2c/1-0041"})


def test_an_unresolved_path_recovers(tmp_path):
    link = tmp_path / "class/hwmon/hwmon3"
    link.parent.mkdir(parents=True)
    target = tmp_path / "devices/platform/i2c/1-0040/hwmon/hwmon3"
    link.symlink_to(os.path.relpath(target, link.parent))  # dangling for now
    assert sysfs.chip_identities([(str(link), "ina")], str(tmp_path))[str(link)] == ("unresolved", {})
    target.mkdir(parents=True)
    assert sysfs.chip_identities([(str(link), "ina")], str(tmp_path))[str(link)][0] == "device_channel"


def test_a_second_node_on_the_device_is_seen_when_it_comes_and_goes(tmp_path):
    """The shared-device decision is made from the current listing every time."""
    import shutil
    a = chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    chips = [(str(tmp_path / "class/hwmon/hwmon3"), "ina")]
    assert sysfs.chip_identities(chips, str(tmp_path))[chips[0][0]][0] == "device_channel"
    chip(tmp_path, "devices/platform/i2c/1-0040", 4, CHIP)
    both = chips + [(str(tmp_path / "class/hwmon/hwmon4"), "ina")]
    assert {v[0] for v in sysfs.chip_identities(both, str(tmp_path)).values()} == {"resolved_path"}
    shutil.rmtree(a.parent / "hwmon4")
    (tmp_path / "class/hwmon/hwmon4").unlink()
    assert sysfs.chip_identities(chips, str(tmp_path))[chips[0][0]][0] == "device_channel"


def test_unresolved_source_keeps_the_value(tmp_path, monkeypatch):
    chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    monkeypatch.setattr(sysfs, "canonical", lambda path, root: None)
    (temps, power, fans), g = hwmon(tmp_path)
    assert temps == {"ina Board": 40.0} and power == {"VIN": 2.0, "VDD": 5.0} and fans[0]["rpm"] == 900
    assert {v["identity_basis"] for v in g["temperature"].sensors.values()} == {"unresolved"}
    assert g["temperature"].sensors["ina Board"]["id"] is None and g["temperature"].sensors["ina Board"]["span"]
    assert g["temperature"].status()["state"] == "ok"  # no id is not a read error


def test_gpu_sources_follow_the_file_used(tmp_path):
    def gpu_tree(root, files):
        for path, value in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(str(value))

    gpu_tree(tmp_path / "a", {"class/devfreq/17000000.gpu/device/load": 500, "class/devfreq/17000000.gpu/cur_freq": 1})
    gpu_tree(tmp_path / "b", {"devices/platform/gpu.0/load": 500})  # another BSP: another source
    ga, gb = sysfs.Group("gpu", sysfs.Trace(Tick())), sysfs.Group("gpu", sysfs.Trace(Tick()))
    jetson.gpu(str(tmp_path / "a"), ga)
    jetson.gpu(str(tmp_path / "b"), gb)
    assert ga.sensors["usage"]["id"] != gb.sensors["usage"]["id"]
    assert ga.sensors["usage"]["id"] != ga.sensors["freq"]["id"]
    # gpu.0 linked to the devfreq device: the same file, so the same source
    (tmp_path / "a/devices/platform").mkdir(parents=True)
    (tmp_path / "a/devices/platform/gpu.0").symlink_to(tmp_path / "a/class/devfreq/17000000.gpu/device")
    assert sysfs.canonical(str(tmp_path / "a/devices/platform/gpu.0/load"), str(tmp_path / "a")) == \
        sysfs.canonical(str(tmp_path / "a/class/devfreq/17000000.gpu/device/load"), str(tmp_path / "a"))


# ---------- read spans ----------

class Script:
    """Elapsed clock with scripted values per read, in calling order."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


def test_label_read_later_does_not_refresh_the_number(tmp_path):
    chip(tmp_path, "devices/platform/i2c/1-0040", 3, {"name": "c", "temp1_input": 0, "temp1_label": "Zero"})
    tick = Tick()
    (temps, _, _), g = hwmon(tmp_path, tick)
    span = g["temperature"].sensors["c Zero"]["span"]
    assert temps == {"c Zero": 0.0} and span is not None  # a measured 0 has its read too
    assert span[1] < tick.ns - 1000  # the label was read after it; its read is not this span


def test_computed_and_fan_spans(tmp_path):
    chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    _, g = hwmon(tmp_path)
    vdd = g["power"].sensors["VDD"]["span"]
    vin = g["power"].sensors["VIN"]["span"]
    assert vdd[1] - vdd[0] > vin[1] - vin[0]  # from the current read's start to the voltage read's end
    rpm, pwm = (v["span"] for v in g["fans"].sensors.values())
    assert rpm[1] < pwm[0]  # two reads, two spans


def test_gpu_spans_skip_the_failed_candidate(tmp_path):
    for path, value in {"class/devfreq/17000000.gpu/device/load": "bad", "devices/platform/gpu.0/load": 500,
                        "class/devfreq/17000000.gpu/cur_freq": 1, "class/devfreq/17000000.gpu/max_freq": 2}.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(str(value))
    tick = Tick()
    g = sysfs.Group("gpu", sysfs.Trace(tick))
    jetson.gpu(str(tmp_path), g)
    usage, freq, max_freq = (g.sensors[k]["span"] for k in ("usage", "freq", "max_freq"))
    assert usage[0] > 10**12 + 1000  # the failed candidate's read started at +1000 ns; this one after it
    assert usage[1] < freq[0] < freq[1] < max_freq[0]


def test_opens_are_the_same_with_records(tmp_path, monkeypatch):
    """Recording adds no reads: the same files are opened with and without a trace."""
    chip(tmp_path, "devices/platform/i2c/1-0040", 3, CHIP)
    opened, real = [], open
    monkeypatch.setattr(sysfs, "open", lambda p, *a, **k: opened.append(p) or real(p, *a, **k), raising=False)
    common.hwmon_sensors(root=str(tmp_path / "class" / "hwmon"))
    plain, opened[:] = list(opened), []
    hwmon(tmp_path)
    assert opened == plain


# ---------- the recorded collection ----------

def resolve(doc, ptr):
    """Strict RFC 6901: only ~0 and ~1 escapes, array indexes 0 or [1-9][0-9]* within range."""
    import re
    assert ptr.startswith("/"), ptr
    for token in ptr.split("/")[1:]:
        assert not re.search(r"~(?![01])", token), ptr
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(doc, list):
            assert re.fullmatch(r"0|[1-9][0-9]*", token) and int(token) < len(doc), ptr
            doc = doc[int(token)]
        else:
            doc = doc[token]
    return doc


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """collector.collect_recorded() on a fake tree: CPUs 0, 2 and 5, a labelled chip and a thermal zone."""
    d = chip(tmp_path, "devices/platform/i2c/1-0040", 3, dict(CHIP, temp1_label="a/b~c 보드 온도"))
    chip(tmp_path, "devices/platform/i2c/1-0041", 4, {"name": "fan2", "fan1_input": 0})
    zone(tmp_path, 0, "cpu-thermal", 50000)
    monkeypatch.setattr(collector, "BOARD", None)
    monkeypatch.setattr(common, "hwmon_sensors", functools.partial(common.hwmon_sensors, root=str(tmp_path / "class/hwmon")))
    monkeypatch.setattr(common, "thermal_zones", functools.partial(common.thermal_zones, root=str(tmp_path / "class/thermal")))
    real = open

    def fake(path, *a, **k):
        if str(path).startswith("/sys/devices/system/cpu/"):
            if "cpu2/" in path:
                raise OSError(5, "EIO")
            return io.StringIO("1200000\n")
        return real(path, *a, **k)
    monkeypatch.setattr(sysfs, "open", fake, raising=False)
    tick = Tick()
    p = Proc(monkeypatch)
    cpu = CpuCounters(tick)
    p.readings += [{n: idle_busy(0, 0) for n in (0, 2, 5)}, {n: idle_busy(10, 10) for n in (0, 2, 5)}]
    collector.collect_recorded(cpu, None, tick)  # warmup
    return collector.collect_recorded(cpu, None, tick), tick, p, d


def test_pointers_resolve_in_the_output(recorded):
    result, tick, p, _ = recorded
    stats = result.stats
    assert [c["id"] for c in stats["cpu"]] == [0, 2, 5]
    for ptr, info in result.sensors.items():
        value = resolve(stats, ptr)  # every pointer exists in this snapshot
        assert (info["span"] is None) == (value is None), ptr  # a null has no read, a value has one
    assert set(result.sensors) >= {"/temperature/ina a~1b~0c 보드 온도", "/temperature/cpu", "/power/VIN", "/power/VDD",
                                   "/fans/0/rpm", "/fans/0/percent", "/fans/1/rpm", "/cpu/1/freq"}
    assert resolve(stats, "/cpu/1/freq") is None  # CPU 2 is the list's second row; its clock failed
    assert resolve(stats, "/fans/1/rpm") == 0 and result.sensors["/fans/1/rpm"]["span"]
    assert "/fans/1/percent" in result.sensors and resolve(stats, "/fans/1/percent") is None  # no pwm: null, no read
    for ptr, _, _, value in sensor_values(stats):  # every value that needs a record has one with a read
        if value is not None:
            assert result.sensors[ptr]["span"], ptr
    assert p.reads == 2  # one /proc/stat reading per collection, as before


def test_required_reads_are_named_and_timed(recorded):
    result, tick, *_ = recorded
    assert set(result.required) == {"cpu", "memory", "disk", "uptime"} and all(result.required.values())
    assert result.clock is tick


def test_label_fallback_pointer(recorded):
    result, tick, p, d = recorded
    (d / "temp1_label").write_bytes(b"\xff\n")  # C1: unreadable label, the channel name is used
    p.readings.append({n: idle_busy(20, 20) for n in (0, 2, 5)})
    again = collector.collect_recorded(CpuCounters(tick), None, tick)
    assert "/temperature/ina temp1" in again.sensors and resolve(again.stats, "/temperature/ina temp1") == 40.0
    assert again.sensors["/temperature/ina temp1"]["id"] == result.sensors["/temperature/ina a~1b~0c 보드 온도"]["id"]


def test_direct_collect_has_no_records():
    s = collector.collect()
    assert "sensor_meta" not in s and not isinstance(s, Collected)


# ---------- data age in the Sampler ----------

MS = 10**6
BASE = {"id": "src1:x", "provider": "hwmon", "identity_basis": "device_channel", "unit": "celsius"}


REQS = [(100, 110), (120, 130), (140, 150), (160, 170)]  # cpu, memory, disk, uptime


def collected(c, required, sensors=(), values=None, clock=None, extra=None):
    """A Collected whose spans are ms offsets from the collection start (which takes 300 ms). required: spans
    for cpu, memory, disk, uptime in that order (fewer: the rest missing); sensors: {name: span} for
    /temperature/<name> with value 1.0 in the stats; values: more temperatures without a record; extra:
    records at pointers that are not in the stats."""
    def collect():
        start = c.ns
        at = lambda s: None if s is None else (start + s[0] * MS, start + s[1] * MS)  # noqa: E731
        c.advance(0.3)
        temps = {**{n: 1.0 for n in dict(sensors)}, **(values or {})}
        meta = {f"/temperature/{n}": {**BASE, "span": at(s)} for n, s in dict(sensors).items()}
        meta.update({p: {**BASE, "span": at(s)} for p, s in (extra or {}).items()})
        req = dict(zip(REQUIRED_READS, map(at, required)))
        return Collected({"temperature": temps}, meta, req, clock)
    return collect


def recorded_sampler(c, required, **kw):
    clock = lambda: c.ns  # noqa: E731 - the one clock both use
    s = Sampler(None, clock=(clock, {"source": "fake"}), wall=lambda: c.wall)
    s.collect = collected(c, required, clock=clock, **kw)
    return s


def test_data_age_numbers():
    """Collection 0-300 ms, current reads from 100 ms, answer at 500 ms."""
    c = Clock()
    s = recorded_sampler(c, [(100, 110), (150, 160), (200, 210), (290, 300)], sensors={"x": (150, 160)})
    s._attempt()
    c.advance(0.2)
    stats, status = s.read()
    sample = stats["sample"]
    assert (sample["duration_ms"], sample["age_ms"], sample["cycle_age_ms"], sample["data_age_ms"]) == (300, 200, 500, 400)
    assert sample["data_age_basis"] == "oldest_current_read_start"
    assert stats["sensor_meta"] == {"/temperature/x": {**BASE, "read": {"started_offset_ms": 150.0, "completed_offset_ms": 160.0}}}
    assert status["sample"]["data_age_basis"] == "oldest_current_read_start" and status["sample"]["cycle_age_ms"] == 500
    # the same timings from a plain dict callable: from the collection start
    c2 = Clock()

    def plain():
        c2.advance(0.3)
        return {"v": 1}
    p = fake(plain, c2)
    p._attempt()
    c2.advance(0.2)
    sample = p.read()[0]["sample"]
    assert (sample["data_age_ms"], sample["data_age_basis"]) == (500, "cycle_start_upper_bound")


def test_a_sensor_read_before_the_required_ones_counts():
    """Sensor T read at 20-21 ms, required reads from 100 ms, answer at 500 ms: 480 ms, from T's read."""
    c = Clock()
    s = recorded_sampler(c, [(100, 110), (150, 160), (200, 210), (250, 260)], sensors={"T": (20, 21)})
    s._attempt()
    c.advance(0.2)
    stats = s.read()[0]
    assert stats["sample"]["data_age_ms"] == 480.0 and stats["sample"]["data_age_basis"] == "oldest_current_read_start"
    assert stats["sensor_meta"]["/temperature/T"]["read"]["started_offset_ms"] == 20.0


def test_stale_boundary_on_read_basis():
    c = Clock()
    s = recorded_sampler(c, REQS)
    s._attempt()
    c.advance(4.8)  # collection took 0.3 s, the read started at 0.1 s: now exactly stale_after ago
    assert s.read()[0]["sample"]["data_age_ms"] == 5000.0
    c.ns += 1
    stats, status = s.read()
    assert stats is None and status["state"] == "stale" and status["sample"]["data_age_basis"] == "oldest_current_read_start"


@pytest.mark.parametrize("kw, problem", [
    ({"sensors": {"x": (100, 110)}, "values": {"y": 2.0}}, "a value without its record"),
    ({"sensors": {"x": (-10, 5)}}, "a value without a valid read"),     # before the collection
    ({"sensors": {"x": (400, 410)}}, "a value without a valid read"),   # after it ended
    ({"sensors": {"x": (160, 150)}}, "a value without a valid read"),   # reversed
    ({"sensors": {"x": (100, 110)}, "extra": {"/temperature/gone": (20, 21)}}, "a record for a value that is not there"),
    ({"sensors": {"x": (100, 110)}, "extra": {"/memory/total": (20, 21)}}, "a record for a value that is not there"),
])
def test_records_that_do_not_match_fall_back(kw, problem, capsys):
    c = Clock()
    s = recorded_sampler(c, REQS, **kw)
    s._attempt()
    stats = s.read()[0]
    sample, meta = stats["sample"], stats.get("sensor_meta", {})
    assert sample["data_age_basis"] == "cycle_start_upper_bound" and sample["data_age_ms"] == sample["cycle_age_ms"]
    assert "/temperature/gone" not in meta and "/memory/total" not in meta  # only sensor values, only there
    assert stats["temperature"]["x"] == 1.0  # the values are published as collected
    assert problem in capsys.readouterr().err
    s._attempt()
    assert capsys.readouterr().err == ""  # the same problem is logged once


@pytest.mark.parametrize("required", [[None] * 4, REQS[:3] + [None], REQS[:3], []])
def test_missing_required_reads_fall_back(required):
    c = Clock()
    s = recorded_sampler(c, required, sensors={"x": (150, 160)})
    s._attempt()
    assert s.read()[0]["sample"]["data_age_basis"] == "cycle_start_upper_bound"


def recorded_with(c, make):
    """A Sampler whose collect() returns make(start, clock) on its own clock; the collection takes 300 ms."""
    clock = lambda: c.ns  # noqa: E731

    def collect():
        start = c.ns
        c.advance(0.3)
        return make(start, clock)
    return Sampler(collect, clock=(clock, {"source": "fake"}), wall=lambda: c.wall)


def at(start, a, b):
    return (start + a * MS, start + b * MS)


def good_required(start):
    return {name: at(start, *span) for name, span in zip(REQUIRED_READS, REQS)}


@pytest.mark.parametrize("name", REQUIRED_READS)
def test_a_deleted_required_read_falls_back(name):
    """A: the key itself missing, not just None."""
    c = Clock()

    def make(start, clock):
        required = good_required(start)
        del required[name]
        return Collected({"temperature": {}}, {}, required, clock)
    s = recorded_with(c, make)
    s._attempt()
    assert s.read()[0]["sample"]["data_age_basis"] == "cycle_start_upper_bound"


def test_other_names_cannot_stand_in_for_required_reads():
    """B: {"r0": valid span} is not the four required reads."""
    c = Clock()
    s = recorded_with(c, lambda start, clock: Collected({"temperature": {}}, {}, {"r0": at(start, 100, 110)}, clock))
    s._attempt()
    assert s.read()[0]["sample"]["data_age_basis"] == "cycle_start_upper_bound"


@pytest.mark.parametrize("with_record, age, basis", [
    (False, 500.0, "cycle_start_upper_bound"),   # C/D: T is in the stats, its record is not: from the cycle start
    (True, 480.0, "oldest_current_read_start"),  # E: everything there; T read at 20 ms
])
def test_values_are_checked_against_the_stats_not_a_list(with_record, age, basis):
    """Cycle 0-300 ms, required reads from 100 ms, answer at 500 ms, temperature T = 42.0."""
    c = Clock()

    def make(start, clock):
        sensors = {"/temperature/T": {**BASE, "span": at(start, 20, 21)}} if with_record else {}
        return Collected({"temperature": {"T": 42.0}}, sensors, good_required(start), clock)
    s = recorded_with(c, make)
    s._attempt()
    c.advance(0.2)
    stats = s.read()[0]
    assert (stats["sample"]["data_age_ms"], stats["sample"]["data_age_basis"]) == (age, basis)
    assert stats["temperature"] == {"T": 42.0}


@pytest.mark.parametrize("ptr, ok", [
    ("/fans/-1/rpm", False), ("/fans/01/rpm", False), ("/fans/+0/rpm", False), ("/fans/ 0/rpm", False),
    ("/fans/2/rpm", False),                          # out of range
    ("/temperature/a~2b", False),                    # ~2 is not an escape
    ("/temperature/a~02b", True),                    # the key "a~2b", escaped right
    ("/fans/1/rpm", True), ("/temperature/x~1y z 온도", True), ("/temperature/01", True), ("/temperature/-1", True),
])
def test_only_rfc6901_pointers_to_sensor_values_are_published(ptr, ok):
    """Records must sit at the pointer sensor_values writes; anything else is not published and falls back.
    Dictionary keys like "01" or "-1" are fine; only list indexes follow the index rule."""
    c = Clock()
    stats = {"fans": [{"name": "a", "rpm": 1, "percent": None}, {"name": "b", "rpm": 2, "percent": None}],
             "temperature": {"a~2b": 1.0, "x/y z 온도": 2.0, "01": 3.0, "-1": 4.0}}

    def make(start, clock):
        good = {p: {**BASE, "span": at(start, 20, 21)} for p, _, _, v in sensor_values(stats) if v is not None}
        records = dict(good)
        records[ptr] = {**BASE, "span": at(start, 30, 31)}
        return Collected(stats, records, good_required(start), clock)
    s = recorded_with(c, make)
    s._attempt()
    out = s.read()[0]
    assert (ptr in out["sensor_meta"]) == ok
    assert out["sample"]["data_age_basis"] == ("oldest_current_read_start" if ok else "cycle_start_upper_bound")
    for published in out["sensor_meta"]:
        resolve(out, published)  # strict RFC 6901


@pytest.mark.parametrize("sensors, required", [(None, "ok"), ({"/temperature/T": None}, "ok"), ({5: {}}, "ok"),
                                               ("x", "ok"), ({}, None), ({}, "x"), ({}, {"cpu": "not a span"})])
def test_malformed_records_keep_the_values(sensors, required):
    """Records of the wrong shape: the snapshot is published with its values, from the cycle start."""
    c = Clock()
    clock = lambda: c.ns  # noqa: E731

    def collect():
        start = c.ns
        c.advance(0.3)
        req = good_required(start) if required == "ok" else required
        return Collected({"temperature": {"T": 43.0}}, sensors, req, clock)
    s = Sampler(collect, clock=(clock, {"source": "fake"}), wall=lambda: c.wall)
    s._attempt()  # also the very first attempt
    stats, status = s.read()
    assert stats["temperature"] == {"T": 43.0} and stats["sample"]["sequence"] == 1
    assert stats["sample"]["data_age_basis"] == "cycle_start_upper_bound"
    assert status["last_attempt"]["state"] == "ok" and status["last_attempt"]["consecutive_failures"] == 0


def test_writer_survives_bad_records_and_recovers():
    """The real writer thread: good, malformed, good records; values, sequence and last_attempt follow."""
    import threading
    import time
    c = Clock()
    clock = lambda: c.ns  # noqa: E731
    n = [0]
    published = threading.Event()

    def collect():
        n[0] += 1
        start = c.ns
        c.advance(0.01)
        span = (start + 1000, start + 2000)
        required = dict.fromkeys(REQUIRED_READS, span)
        if n[0] == 2:  # malformed records
            return Collected({"temperature": {"T": float(n[0])}}, {"/temperature/T": None}, required, clock)
        if n[0] >= 3:
            published.set()
        return Collected({"temperature": {"T": float(n[0])}}, {"/temperature/T": {**BASE, "span": span}}, required, clock)
    s = Sampler(collect, interval=0.01, clock=(clock, {"source": "fake"}), wall=lambda: c.wall).start()
    try:
        assert published.wait(5)
        deadline = time.monotonic() + 5
        while s.read()[0]["sample"]["sequence"] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        stats, status = s.read()
        assert s._thread.is_alive()
        assert stats["temperature"]["T"] == float(stats["sample"]["sequence"]) and stats["sample"]["sequence"] >= 3
        assert stats["sample"]["data_age_basis"] == "oldest_current_read_start"  # good records again
        assert status["last_attempt"]["state"] == "ok"
    finally:
        s.stop()
        s._thread.join(5)


def test_a_slow_log_write_does_not_block_status_or_stop(monkeypatch):
    """The fallback is logged outside the state lock: read() and stop() answer while the write hangs."""
    import threading
    c = Clock()
    clock = lambda: c.ns  # noqa: E731
    entered, gate = threading.Event(), threading.Event()

    class SlowErr:
        def write(self, text):
            entered.set()
            gate.wait(5)
            return len(text)

        def flush(self):
            pass
    s = Sampler(collected(c, [None] * 4, clock=clock), clock=(clock, {"source": "fake"}), wall=lambda: c.wall)
    monkeypatch.setattr(sys, "stderr", SlowErr())
    writer = threading.Thread(target=s._attempt)
    writer.start()
    try:
        assert entered.wait(5)
        done = []
        reader = threading.Thread(target=lambda: done.append(s.read()) or s.stop() or done.append("stopped"))
        reader.start()
        reader.join(2)
        assert done[1:] == ["stopped"] and done[0][1]["state"] in ("starting", "stale")
    finally:
        gate.set()
        writer.join(5)
        reader.join(5)
    assert s.read()[0]["sample"]["sequence"] == 1  # published once the log line was written


def test_records_on_another_clock_are_not_used():
    c = Clock()
    s = Sampler(None, clock=(lambda: c.ns, {"source": "fake"}), wall=lambda: c.wall)
    s.collect = collected(c, REQS, sensors={"x": (100, 110)}, clock=lambda: c.ns)  # another function
    s._attempt()
    stats = s.read()[0]
    assert stats["sample"]["data_age_basis"] == "cycle_start_upper_bound"
    assert stats["sensor_meta"]["/temperature/x"]["read"] is None


def test_too_slow_still_counts_the_whole_collection():
    c = Clock()
    s = recorded_sampler(c, REQS)
    inner = s.collect

    def slow():
        result = inner()
        c.advance(5.8)
        return result
    s.collect = slow
    s._attempt()
    assert s.read()[1]["last_attempt"]["reason"] == "collection_too_slow"  # recent reads; still dropped


def test_published_records_stay_and_plain_dicts_cannot_fake_them():
    c = Clock()
    s = recorded_sampler(c, REQS, sensors={"x": (100, 110)})
    s._attempt()
    first = s.read()[0]
    first["sensor_meta"]["/temperature/x"]["read"] = None  # changing a returned copy
    c.advance(0.5)
    again = s.read()[0]
    assert again["sensor_meta"]["/temperature/x"]["read"] == {"started_offset_ms": 100.0, "completed_offset_ms": 110.0}
    assert again["sample"]["sequence"] == 1
    assert s.latest() == {"temperature": {"x": 1.0}}  # no Sampler metadata
    s.collect = lambda: (_ for _ in ()).throw(OSError("memory"))  # a failed collection: last good kept as it was
    s._attempt()
    assert s.read()[0]["sensor_meta"] == again["sensor_meta"]
    p = fake(lambda: {"v": 2, "sensor_meta": {"/v": {"read": {"started_offset_ms": 0}}}}, Clock())
    p._attempt()
    stats = p.read()[0]
    assert "sensor_meta" not in stats and stats["sample"]["data_age_basis"] == "cycle_start_upper_bound"


@pytest.mark.parametrize("step", [-3600, 3600])
def test_wall_clock_steps_do_not_move_read_offsets(step):
    c = Clock()
    s = recorded_sampler(c, REQS, sensors={"x": (100, 110)})
    s._attempt()
    c.advance(0.2, wall=step)
    stats = s.read()[0]
    assert stats["sample"]["data_age_ms"] == 400.0
    assert stats["sensor_meta"]["/temperature/x"]["read"] == {"started_offset_ms": 100.0, "completed_offset_ms": 110.0}


def test_service_end_to_end_reports_no_paths(recorded):
    """The service's own path: collect_recorded through the Sampler; no paths or raw ns in the API."""
    result, tick, p, _ = recorded
    c = Clock()
    s = Sampler(None, clock=(tick, {"source": "fake"}), wall=lambda: c.wall)
    cpu = CpuCounters(tick)
    p.readings += [{n: idle_busy(30, 30) for n in (0, 2, 5)}, {n: idle_busy(40, 40) for n in (0, 2, 5)}]
    s.collect = functools.partial(collector.collect_recorded, cpu, None, tick)
    s._attempt()
    s._attempt()
    stats = s.read()[0]
    assert stats["sample"]["data_age_basis"] == "oldest_current_read_start"
    assert stats["sample"]["data_age_ms"] <= stats["sample"]["cycle_age_ms"]
    text = json.dumps(stats["sensor_meta"])
    assert "/sys" not in text and "tmp" not in text and str(10**12)[:6] not in text
    for ptr in stats["sensor_meta"]:
        resolve(stats, ptr)


@pytest.mark.parametrize("device, basis", [
    ("devices/virtual", "resolved_path"),                                   # devices/virtual/hwmon/hwmonN
    ("devices/virtual/thermal/thermal_zone0", "resolved_path"),
    ("devices/virtual/platform/fan_controller", "resolved_path"),
    ("devices/platform/i2c/1-0040", "device_channel"),
    ("devices/platform/virtual_fan", "device_channel"),                     # a real device named "virtual..."
])
def test_virtual_devices_are_path_based(tmp_path, device, basis):
    chip(tmp_path, device, 3, {"name": "x", "temp1_input": 40000})
    _, g = hwmon(tmp_path)
    assert g["temperature"].sensors["x temp1"]["identity_basis"] == basis
