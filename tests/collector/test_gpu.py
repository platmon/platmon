"""collector/gpu.py: the DRM class read per driver, the i915 share measured between two collections,
and what a card whose driver publishes nothing still says. Fake trees and a fake clock; no host data."""
import errno
import subprocess

import pytest

from collector import metadata
from collector import sysfs
from collector.gpu import Gpu
from tests.collector.test_common import fail_reads

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
    assert g.sample(metadata.Group("gpu")) == {"usage": None, "freq": 300 * 10**6, "max_freq": 1500 * 10**6,
                                            "driver": "i915", "provider": "rc6_residency"}
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text("1400")  # 400 ms idle of 1000 ms
    c.advance(1)
    assert g.sample(metadata.Group("gpu"))["usage"] == 60.0


@pytest.mark.parametrize("idled_ms, usage", [(1000, 0.0), (0, 100.0), (500, 50.0), (999, 0.1),
                                             (1200, 0.0)])  # more idle than the window: clamped, never negative
def test_the_share_is_the_time_the_gpu_was_not_idle(tmp_path, idled_ms, usage):
    c = Clock()
    g = Gpu(clock=c, root=intel(tmp_path, rc6=5000))
    g.sample(metadata.Group("gpu"))
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text(str(5000 + idled_ms))
    c.advance(1)
    assert g.sample(metadata.Group("gpu"))["usage"] == usage


def test_a_counter_reset_or_a_long_break_is_not_averaged_over(tmp_path):
    """A counter that went backwards and a gap longer than max_gap both start again, as the CPU does."""
    c = Clock()
    g = Gpu(clock=c, root=intel(tmp_path, rc6=5000), max_gap=5.0)
    g.sample(metadata.Group("gpu"))
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text("10")  # the device was reset
    c.advance(1)
    assert g.sample(metadata.Group("gpu"))["usage"] is None
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text("3010")
    c.advance(6)  # longer than max_gap
    assert g.sample(metadata.Group("gpu"))["usage"] is None
    (tmp_path / "class/drm/card0/gt/gt0/rc6_residency_ms").write_text("3510")
    c.advance(1)
    assert g.sample(metadata.Group("gpu"))["usage"] == 50.0  # measuring again from the last reading


def test_amdgpu_publishes_the_share_itself(tmp_path):
    root = fake_drm(tmp_path, {"card0": {"driver": "amdgpu", "device/gpu_busy_percent": 37}})
    group = metadata.Group("gpu")
    assert Gpu(clock=Clock(), root=root).sample(group) == {
        "usage": 37.0, "freq": None, "max_freq": None, "driver": "amdgpu", "provider": "gpu_busy_percent"}
    assert group.status()["state"] == "ok"


def test_a_card_this_module_cannot_read_is_still_named(tmp_path):
    """A driver without a reader here: the card is reported with its name and no numbers, so a host with a
    GPU platmon cannot measure does not look like a host without one."""
    group = metadata.Group("gpu")
    root = fake_drm(tmp_path, {"card0": {"driver": "nouveau"}})
    assert Gpu(clock=Clock(), root=root).sample(group) == {
        "usage": None, "freq": None, "max_freq": None, "driver": "nouveau", "provider": None}
    assert group.status() == {"state": "unavailable", "reason": "not_exposed", "issues": [], "issues_truncated": 0}


def test_no_card_at_all_is_not_detected(tmp_path):
    group = metadata.Group("gpu")
    (tmp_path / "class" / "drm").mkdir(parents=True)
    assert Gpu(clock=Clock(), root=str(tmp_path)).sample(group) is None
    assert group.status()["reason"] == "not_detected"
    missing = metadata.Group("gpu")  # no DRM class at all (a kernel without it): nothing there, no failure
    assert Gpu(clock=Clock(), root=str(tmp_path / "gone")).sample(missing) is None
    assert missing.status() == {"state": "unavailable", "reason": "not_detected", "issues": [], "issues_truncated": 0}


def test_connectors_are_not_cards_and_the_first_readable_card_wins(tmp_path):
    """card1-DP-1 is a connector of card1, not a card; between two cards the first one with a reader is
    taken, so an unknown driver on card0 does not hide a readable card1."""
    root = fake_drm(tmp_path, {"card0": {"driver": "nouveau"},
                               "card1": {"driver": "i915", "gt/gt0/rc6_residency_ms": 1000,
                                         "gt_act_freq_mhz": 700, "gt_max_freq_mhz": 1800},
                               "card1-DP-1": {"driver": "i915"}})
    entry = Gpu(clock=Clock(), root=root).sample(metadata.Group("gpu"))
    assert entry["driver"] == "i915" and entry["freq"] == 700 * 10**6


def test_an_unreadable_attribute_is_a_failure_and_keeps_the_rest(tmp_path, monkeypatch):
    root = intel(tmp_path)
    fail_reads(monkeypatch, {f"{root}/class/drm/card0/gt/gt0/rc6_residency_ms": errno.EACCES})
    group = metadata.Group("gpu")
    entry = Gpu(clock=Clock(), root=root).sample(group)
    assert entry["usage"] is None and entry["freq"] == 300 * 10**6
    assert group.status()["state"] == "partial"  # the clocks were read
    assert group.status()["issues"] == [{"target": "rc6_residency", "reason": "permission_denied"}]


def test_each_value_carries_where_it_was_read(tmp_path):
    """With a trace the Sampler expects a record per value, keyed as the Jetson GPU keys them."""
    c = Clock()
    trace = metadata.Trace(c)
    group = metadata.Group("gpu", trace)
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
    from collector import core as collector
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


def nvidia_query(monkeypatch, output="GPU-test, 12.5, 210, 3105\n", error=None, returncode=0):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if error:
            raise error
        return subprocess.CompletedProcess(args, returncode, output, "driver error")

    monkeypatch.setattr("collector.gpu.subprocess.run", run)
    return calls


@pytest.mark.parametrize("cards", [{}, {"card0": {}}, {"card0": {"driver": "nvidia"}}])
def test_nvidia_with_no_drm_or_an_unreadable_card(tmp_path, monkeypatch, cards):
    calls = nvidia_query(monkeypatch)
    group = metadata.Group("gpu", metadata.Trace(Clock()))
    gpu = Gpu(root=fake_drm(tmp_path, cards), nvidia_smi="/usr/lib/wsl/lib/nvidia-smi")
    assert gpu.sample(group) == {"usage": 12.5, "freq": 210000000, "max_freq": 3105000000,
                                 "driver": "nvidia", "provider": "nvidia-smi"}
    assert group.status()["state"] == "ok"
    assert calls[0][0] == ["/usr/lib/wsl/lib/nvidia-smi",
                          "--query-gpu=uuid,utilization.gpu,clocks.current.graphics,clocks.max.graphics",
                          "--format=csv,noheader,nounits"]
    assert calls[0][1]["timeout"] == 0.5
    assert calls[0][1].get("shell", False) is False
    assert set(group.sensors) == {"usage", "freq", "max_freq"}
    assert all(s["provider"] == "nvidia-smi" and s["identity_basis"] == "device_channel"
               and s["span"] is not None for s in group.sensors.values())
    assert len({s["id"] for s in group.sensors.values()}) == 3


def test_nvidia_does_not_replace_readable_drm(tmp_path, monkeypatch):
    calls = nvidia_query(monkeypatch)
    gpu = Gpu(root=intel(tmp_path), nvidia_smi="nvidia-smi")
    assert gpu.sample(metadata.Group("gpu"))["driver"] == "i915"
    assert calls == []


@pytest.mark.parametrize("output, state, usage", [
    ("GPU-test, 0, 0, [N/A]\n", "ok", 0.0),
    ("GPU-test, N/A, [Not Supported], N/A\n", "unavailable", None),
    ("GPU-test, nan, 210, 3105\n", "partial", None),
    ("GPU-test, inf, 210, 3105\n", "partial", None),
    ("GPU-test, 101, 210, 3105\n", "partial", None),
    ("GPU-test, -1, 210, 3105\n", "partial", None),
    ("GPU-test, bad, 210, 3105\n", "partial", None),
    ("GPU-test, 0, 1e308, 3105\n", "partial", 0.0),
])
def test_nvidia_unavailable_and_invalid_fields(tmp_path, monkeypatch, output, state, usage):
    nvidia_query(monkeypatch, output)
    group = metadata.Group("gpu")
    entry = Gpu(root=str(tmp_path), nvidia_smi="nvidia-smi").sample(group)
    assert entry["usage"] == usage
    assert group.status()["state"] == state


@pytest.mark.parametrize("output, reason", [("", "not_detected"),
    ("broken response", "invalid_data"), ("GPU-test, 0, 210\n", "invalid_data"),
    ("N/A, 0, 210, 3105\n", "invalid_data")])
def test_nvidia_bad_or_empty_response(tmp_path, monkeypatch, output, reason):
    nvidia_query(monkeypatch, output)
    group = metadata.Group("gpu")
    assert Gpu(root=str(tmp_path), nvidia_smi="nvidia-smi").sample(group) is None
    assert group.status()["reason"] == reason


@pytest.mark.parametrize("error, returncode, reason", [
    (subprocess.TimeoutExpired("nvidia-smi", 0.5), 0, "io_error"),
    (PermissionError(errno.EACCES, "denied"), 0, "permission_denied"),
    (FileNotFoundError(errno.ENOENT, "gone"), 0, "io_error"),
    (None, 9, "io_error"),
])
def test_nvidia_failed_query_does_not_reuse_values(tmp_path, monkeypatch, error, returncode, reason):
    gpu = Gpu(root=str(tmp_path), nvidia_smi="nvidia-smi")
    nvidia_query(monkeypatch)
    assert gpu.sample(metadata.Group("gpu"))["usage"] == 12.5
    nvidia_query(monkeypatch, error=error, returncode=returncode)
    group = metadata.Group("gpu")
    assert gpu.sample(group) is None
    assert group.status()["reason"] == reason


def test_nvidia_selection_and_identity_survive_query_order(tmp_path, monkeypatch):
    gpu = Gpu(root=str(tmp_path), nvidia_smi="nvidia-smi")
    rows = ["GPU-b, 90, 100, 1000", "GPU-a, 10, 200, 2000"]
    identities = []
    for output in ("\n".join(rows), "\n".join(reversed(rows))):
        nvidia_query(monkeypatch, output)
        group = metadata.Group("gpu", metadata.Trace(Clock()))
        assert gpu.sample(group)["usage"] == 10.0
        identities.append(group.sensors["usage"]["id"])
    assert identities[0] == identities[1]


def test_nvidia_wsl_discovery_without_path(monkeypatch):
    monkeypatch.setattr("collector.gpu.shutil.which", lambda name: None)
    monkeypatch.setattr("collector.gpu.os.path.isfile", lambda path: path == "/usr/lib/wsl/lib/nvidia-smi")
    monkeypatch.setattr("collector.gpu.os.access", lambda path, mode: True)
    assert Gpu()._nvidia_smi == "/usr/lib/wsl/lib/nvidia-smi"
