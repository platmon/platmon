"""collector/npu.py: the accel class read per driver, the intel_vpu share measured between two
collections, and what an accelerator whose driver publishes nothing still says. Fake trees, fake clock."""
import errno

import pytest

from collector import sysfs
from collector.npu import Npu
from test_common import fail_reads
from test_gpu import Clock


def fake_accel(root, devices):
    """devices: {"accel0": {"driver": "intel_vpu", "npu_busy_time_us": 10, ...}}; the attributes sit in
    the device directory, as the kernel puts them."""
    for name, files in devices.items():
        device = root / "class" / "accel" / name / "device"
        device.mkdir(parents=True, exist_ok=True)
        driver = files.get("driver")
        (device / "uevent").write_text((f"DRIVER={driver}\n" if driver else "") + "PCI_ID=8086:7D1D\n")
        for attr, value in files.items():
            if attr != "driver":
                (device / attr).write_text(str(value))
    return str(root)


def vpu(root, busy=0, freq=0, top=1400):
    return fake_accel(root, {"accel0": {"driver": "intel_vpu", "npu_busy_time_us": busy,
                                        "npu_current_frequency_mhz": freq, "npu_max_frequency_mhz": top}})


def test_the_first_reading_has_no_share_and_the_next_one_has(tmp_path):
    """The busy time is a counter, so a share needs two readings; the clocks are read either way. An NPU
    the firmware has never run reads 0 µs, which is an idle NPU, not a missing one."""
    c = Clock()
    n = Npu(clock=c, root=vpu(tmp_path))
    assert n.sample(sysfs.Group("npu")) == {"usage": None, "freq": 0, "max_freq": 1400 * 10**6,
                                            "driver": "intel_vpu", "provider": "npu_busy_time"}
    c.advance(1)
    assert n.sample(sysfs.Group("npu"))["usage"] == 0.0  # the counter did not move: idle, measured
    (tmp_path / "class/accel/accel0/device/npu_busy_time_us").write_text("250000")  # 250 ms of work
    (tmp_path / "class/accel/accel0/device/npu_current_frequency_mhz").write_text("1300")
    c.advance(1)
    entry = n.sample(sysfs.Group("npu"))
    assert entry["usage"] == 25.0 and entry["freq"] == 1300 * 10**6


@pytest.mark.parametrize("worked_us, usage", [(0, 0.0), (1_000_000, 100.0), (500_000, 50.0),
                                              (1_200_000, 100.0)])  # more work than the window: clamped
def test_the_share_is_the_time_the_npu_worked(tmp_path, worked_us, usage):
    c = Clock()
    n = Npu(clock=c, root=vpu(tmp_path, busy=5000))
    n.sample(sysfs.Group("npu"))
    (tmp_path / "class/accel/accel0/device/npu_busy_time_us").write_text(str(5000 + worked_us))
    c.advance(1)
    assert n.sample(sysfs.Group("npu"))["usage"] == usage


def test_a_counter_reset_or_a_long_break_is_not_averaged_over(tmp_path):
    c = Clock()
    n = Npu(clock=c, root=vpu(tmp_path, busy=900_000))
    n.sample(sysfs.Group("npu"))
    (tmp_path / "class/accel/accel0/device/npu_busy_time_us").write_text("10")  # the device was reset
    c.advance(1)
    assert n.sample(sysfs.Group("npu"))["usage"] is None
    (tmp_path / "class/accel/accel0/device/npu_busy_time_us").write_text("500010")
    c.advance(6)  # longer than max_gap
    assert n.sample(sysfs.Group("npu"))["usage"] is None
    (tmp_path / "class/accel/accel0/device/npu_busy_time_us").write_text("600010")
    c.advance(1)
    assert n.sample(sysfs.Group("npu"))["usage"] == 10.0  # measuring again from the last reading


def test_an_accelerator_this_module_cannot_read_is_still_named(tmp_path):
    group = sysfs.Group("npu")
    root = fake_accel(tmp_path, {"accel0": {"driver": "amdxdna"}})
    assert Npu(clock=Clock(), root=root).sample(group) == {
        "usage": None, "freq": None, "max_freq": None, "driver": "amdxdna", "provider": None}
    assert group.status() == {"state": "unavailable", "reason": "not_exposed", "issues": [], "issues_truncated": 0}


def test_a_host_without_an_accelerator_is_not_detected(tmp_path):
    group = sysfs.Group("npu")
    assert Npu(clock=Clock(), root=str(tmp_path)).sample(group) is None  # no accel class at all
    assert group.status() == {"state": "unavailable", "reason": "not_detected", "issues": [], "issues_truncated": 0}


def test_an_unreadable_attribute_is_a_failure_and_keeps_the_rest(tmp_path, monkeypatch):
    root = vpu(tmp_path, freq=950)
    fail_reads(monkeypatch, {f"{root}/class/accel/accel0/device/npu_busy_time_us": errno.EACCES})
    group = sysfs.Group("npu")
    entry = Npu(clock=Clock(), root=root).sample(group)
    assert entry["usage"] is None and entry["freq"] == 950 * 10**6
    assert group.status()["state"] == "partial"
    assert group.status()["issues"] == [{"target": "npu_busy_time", "reason": "permission_denied"}]


def test_each_value_carries_where_it_was_read(tmp_path):
    c = Clock()
    group = sysfs.Group("npu", sysfs.Trace(c))
    Npu(clock=c, root=vpu(tmp_path)).sample(group)
    assert set(group.sensors) == {"usage", "freq", "max_freq"}
    for key, unit in (("usage", "percent"), ("freq", "hertz"), ("max_freq", "hertz")):
        record = group.sensors[key]
        assert record["provider"] == "accel" and record["unit"] == unit
        assert record["identity_basis"] == "resolved_path" and record["id"].startswith("src1:")
        assert record["span"] is not None


def test_the_collector_reports_it_as_its_own_group(tmp_path, monkeypatch):
    import collector
    from test_common import without_host_accelerators
    monkeypatch.setattr(collector, "BOARD", None)  # a plain Linux host, whatever board runs the tests
    without_host_accelerators(monkeypatch, tmp_path)  # and its own devices stay out
    stats = collector.collect(npu=Npu(clock=Clock(), root=vpu(tmp_path, busy=1)))
    assert stats["npu"]["driver"] == "intel_vpu" and stats["npu"]["max_freq"] == 1400 * 10**6
    assert stats["collectors"]["npu"]["state"] == "ok"
    assert stats["collectors"]["gpu"]["reason"] == "not_detected"  # read separately, from its own class
