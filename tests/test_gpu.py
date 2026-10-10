"""collector/gpu.py: the DRM class read per driver, the i915 share measured between two collections,
and what a card whose driver publishes nothing still says. Fake trees and a fake clock; no host data."""
import errno

import pytest

from collector import sysfs
from collector.gpu import Gpu
from test_common import fail_reads

S = 10**9


class Clock:
    def __init__(self):
        self.ns = 10**12

    def __call__(self):
        return self.ns

    def advance(self, seconds):
        self.ns += round(seconds * 1e9)


def fake_drm(root, cards):
    """cards: {"card0": {"driver": "i915", "gt_act_freq_mhz": 300, "gt/gt0/rc6_residency_ms": 1000}}.
    A card without a driver gets a uevent without the line, as a card of a driver that sets none would."""
    for name, files in cards.items():
        card = root / "class" / "drm" / name
        (card / "device").mkdir(parents=True, exist_ok=True)
        driver = files.get("driver")
        (card / "device" / "uevent").write_text(
            (f"DRIVER={driver}\n" if driver else "") + "PCI_CLASS=30000\nPCI_ID=8086:7D55\n")
        for rel, value in files.items():
            if rel == "driver":
                continue
            path = card / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(value))
    return str(root)


def intel(root, rc6=1000, act=300, top=1500, name="card0"):
    return fake_drm(root, {name: {"driver": "i915", "gt/gt0/rc6_residency_ms": rc6,
                                  "gt_act_freq_mhz": act, "gt_max_freq_mhz": top}})


def test_the_first_reading_has_no_share_and_the_next_one_has(tmp_path):
    """RC6 is idle time, so a share needs two readings; the clocks are read either way."""
    c = Clock()
    g = Gpu(clock=c, root=intel(tmp_path))
    assert g.sample(sysfs.Group("gpu")) == {"usage": None, "freq": 300 * 10**6, "max_freq": 1500 * 10**6,
                                            "driver": "i915", "provider": "rc6_residency"}
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text("1400")  # 400 ms idle of 1000 ms
    c.advance(1)
    assert g.sample(sysfs.Group("gpu"))["usage"] == 60.0


@pytest.mark.parametrize("idled_ms, usage", [(1000, 0.0), (0, 100.0), (500, 50.0), (999, 0.1),
                                             (1200, 0.0)])  # more idle than the window: clamped, never negative
def test_the_share_is_the_time_the_gpu_was_not_idle(tmp_path, idled_ms, usage):
    c = Clock()
    g = Gpu(clock=c, root=intel(tmp_path, rc6=5000))
    g.sample(sysfs.Group("gpu"))
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text(str(5000 + idled_ms))
    c.advance(1)
    assert g.sample(sysfs.Group("gpu"))["usage"] == usage


def test_a_counter_reset_or_a_long_break_is_not_averaged_over(tmp_path):
    """A counter that went backwards and a gap longer than max_gap both start again, as the CPU does."""
    c = Clock()
    g = Gpu(clock=c, root=intel(tmp_path, rc6=5000), max_gap=5.0)
    g.sample(sysfs.Group("gpu"))
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text("10")  # the device was reset
    c.advance(1)
    assert g.sample(sysfs.Group("gpu"))["usage"] is None
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text("3010")
    c.advance(6)  # longer than max_gap
    assert g.sample(sysfs.Group("gpu"))["usage"] is None
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text("3510")
    c.advance(1)
    assert g.sample(sysfs.Group("gpu"))["usage"] == 50.0  # measuring again from the last reading


def test_amdgpu_publishes_the_share_itself(tmp_path):
    root = fake_drm(tmp_path, {"card0": {"driver": "amdgpu", "device/gpu_busy_percent": 37}})
    group = sysfs.Group("gpu")
    assert Gpu(clock=Clock(), root=root).sample(group) == {
        "usage": 37.0, "freq": None, "max_freq": None, "driver": "amdgpu", "provider": "gpu_busy_percent"}
    assert group.status()["state"] == "ok"


def test_a_card_this_module_cannot_read_is_still_named(tmp_path):
    """A driver without a reader here: the card is reported with its name and no numbers, so a host with a
    GPU platmon cannot measure does not look like a host without one."""
    group = sysfs.Group("gpu")
    root = fake_drm(tmp_path, {"card0": {"driver": "nouveau"}})
    assert Gpu(clock=Clock(), root=root).sample(group) == {
        "usage": None, "freq": None, "max_freq": None, "driver": "nouveau", "provider": None}
    assert group.status() == {"state": "unavailable", "reason": "not_exposed", "issues": [], "issues_truncated": 0}


def test_no_card_at_all_is_not_detected(tmp_path):
    group = sysfs.Group("gpu")
    (tmp_path / "class" / "drm").mkdir(parents=True)
    assert Gpu(clock=Clock(), root=str(tmp_path)).sample(group) is None
    assert group.status()["reason"] == "not_detected"
    missing = sysfs.Group("gpu")  # no DRM class at all (a kernel without it): nothing there, no failure
    assert Gpu(clock=Clock(), root=str(tmp_path / "gone")).sample(missing) is None
    assert missing.status() == {"state": "unavailable", "reason": "not_detected", "issues": [], "issues_truncated": 0}


def test_connectors_are_not_cards_and_the_first_readable_card_wins(tmp_path):
    """card1-DP-1 is a connector of card1, not a card; between two cards the first one with a reader is
    taken, so an unknown driver on card0 does not hide a readable card1."""
    root = fake_drm(tmp_path, {"card0": {"driver": "nouveau"},
                               "card1": {"driver": "i915", "gt/gt0/rc6_residency_ms": 1000,
                                         "gt_act_freq_mhz": 700, "gt_max_freq_mhz": 1800},
                               "card1-DP-1": {"driver": "i915"}})
    entry = Gpu(clock=Clock(), root=root).sample(sysfs.Group("gpu"))
    assert entry["driver"] == "i915" and entry["freq"] == 700 * 10**6


def test_an_unreadable_attribute_is_a_failure_and_keeps_the_rest(tmp_path, monkeypatch):
    root = intel(tmp_path)
    fail_reads(monkeypatch, {f"{root}/class/drm/card0/gt/gt0/rc6_residency_ms": errno.EACCES})
    group = sysfs.Group("gpu")
    entry = Gpu(clock=Clock(), root=root).sample(group)
    assert entry["usage"] is None and entry["freq"] == 300 * 10**6
    assert group.status()["state"] == "partial"  # the clocks were read
    assert group.status()["issues"] == [{"target": "rc6_residency", "reason": "permission_denied"}]


def test_each_value_carries_where_it_was_read(tmp_path):
    """With a trace the Sampler expects a record per value, keyed as the Jetson GPU keys them."""
    c = Clock()
    trace = sysfs.Trace(c)
    group = sysfs.Group("gpu", trace)
    g = Gpu(clock=c, root=intel(tmp_path))
    g.sample(group)
    assert set(group.sensors) == {"usage", "freq", "max_freq"}
    for key, unit in (("usage", "percent"), ("freq", "hertz"), ("max_freq", "hertz")):
        record = group.sensors[key]
        assert record["provider"] == "drm" and record["unit"] == unit
        assert record["identity_basis"] == "resolved_path" and record["id"].startswith("src1:")
        assert record["span"] is not None
    assert group.sensors["usage"]["id"] != group.sensors["freq"]["id"]


def test_the_collector_uses_it_and_a_board_source_comes_first(tmp_path, monkeypatch):
    """collect() fills "gpu" from the DRM class on any host; where a board module answers, that wins."""
    import collector
    monkeypatch.setattr(collector, "BOARD", None)
    stats = collector.collect(gpu=Gpu(clock=Clock(), root=intel(tmp_path)))
    assert stats["gpu"]["driver"] == "i915" and stats["gpu"]["max_freq"] == 1500 * 10**6
    assert stats["collectors"]["gpu"]["state"] == "ok"

    class Board:  # a board module that answers: the DRM reader is not asked
        @staticmethod
        def extend(stats, groups):
            stats["gpu"] = groups["gpu"].got({"usage": 12.0, "freq": None, "max_freq": None})
            stats["power_mode"] = None
    monkeypatch.setattr(collector, "BOARD", Board)
    stats = collector.collect(gpu=Gpu(clock=Clock(), root=intel(tmp_path)))
    assert stats["gpu"] == {"usage": 12.0, "freq": None, "max_freq": None}
