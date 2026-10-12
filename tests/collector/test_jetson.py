import pytest

from collector import metadata
from collector import jetson
from tests.collector.test_common import fake_hwmon


@pytest.mark.parametrize("status, expected", [("pmode:0002", "MAXN_SUPER"), ("pmode:0000", "15W"), ("pmode:0009", "9")])
def test_power_mode(status, expected):
    conf = "< POWER_MODEL ID=0 NAME=15W >\n< POWER_MODEL ID=2 NAME=MAXN_SUPER >"
    assert jetson.power_mode(status, conf) == expected


def test_tach_fans(tmp_path):
    """Only chips with the non-standard rpm file count; the pwmfan half is common hwmon's job."""
    root = fake_hwmon(tmp_path, [{"name": "pwmfan", "pwm1": 76}, {"name": "pwm_tach", "rpm": 1636}])
    assert jetson.tach_fans(root) == [{"name": "pwm_tach", "rpm": 1636, "percent": None}]


def test_gpu(tmp_path):
    assert jetson.gpu(str(tmp_path)) is None  # no gpu.0/load: not reported
    for path, value in [("devices/platform/gpu.0/load", 500), ("class/devfreq/17000000.gpu/cur_freq", 306000000),
                        ("class/devfreq/17000000.gpu/max_freq", 1020000000)]:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(str(value))
    assert jetson.gpu(str(tmp_path)) == {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000}


@pytest.mark.parametrize("text, expected", [
    ("# R36 (release), REVISION: 5.2, GCID: 46426093, BOARD: generic, EABI: aarch64, DATE: Thu Jul 16", "R36.5.2"),
    ("# R35 (release), REVISION: 4.1, GCID: 33958178, BOARD: t186ref", "R35.4.1"),
    ("garbage", None),
    (None, None),
])
def test_l4t(text, expected):
    assert jetson.l4t(text) == expected


def make(root, files):
    for path, value in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(str(value))


@pytest.mark.parametrize("files, expected", [
    # Orin R35 names the GPU devfreq device 17000000.ga10b; load sits in its device directory
    ({"class/devfreq/17000000.ga10b/cur_freq": 306000000, "class/devfreq/17000000.ga10b/max_freq": 1300500000,
      "class/devfreq/17000000.ga10b/device/load": 250},
     {"usage": 25.0, "freq": 306000000, "max_freq": 1300500000}),
    # Xavier: 17000000.gv11b, load through /sys/devices/gpu.0
    ({"class/devfreq/17000000.gv11b/cur_freq": 114750000, "class/devfreq/17000000.gv11b/max_freq": 1377000000,
      "devices/gpu.0/load": 500},
     {"usage": 50.0, "freq": 114750000, "max_freq": 1377000000}),
    # load readable but no GPU devfreq device: clock unknown (None), not 0 MHz
    ({"devices/platform/gpu.0/load": 500}, {"usage": 50.0, "freq": None, "max_freq": None}),
    # devfreq device without readable clock files
    ({"class/devfreq/17000000.gpu/device/load": 0}, {"usage": 0.0, "freq": None, "max_freq": None}),
])
def test_gpu_across_bsps(tmp_path, files, expected):
    make(tmp_path, files)
    assert jetson.gpu(str(tmp_path)) == expected


# what each Jetson part could not read is noted in its group; one part failing does not stop the others

import errno

from collector import sysfs
from tests.collector.test_common import fail_reads


def test_gpu_load_fallback_hides_failed_candidates(tmp_path):
    """The devfreq device's load is unreadable but gpu.0's is: the value counts, the failed candidate does not."""
    make(tmp_path, {"class/devfreq/17000000.gpu/device/load": "garbage", "class/devfreq/17000000.gpu/cur_freq": 306000000,
                    "class/devfreq/17000000.gpu/max_freq": 1020000000, "devices/platform/gpu.0/load": 500})
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) == {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000}
    assert g.status() == {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0}


def test_gpu_clock_failure_keeps_usage(tmp_path, monkeypatch):
    make(tmp_path, {"class/devfreq/17000000.gpu/device/load": 250, "class/devfreq/17000000.gpu/cur_freq": 1,
                    "class/devfreq/17000000.gpu/max_freq": 1020000000})
    fail_reads(monkeypatch, {str(tmp_path / "class/devfreq/17000000.gpu/cur_freq"): errno.EIO})
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) == {"usage": 25.0, "freq": None, "max_freq": 1020000000}
    assert g.status()["state"] == "partial" and g.status()["issues"] == [{"target": "cur_freq", "reason": "io_error"}]


def test_gpu_without_load(tmp_path):
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) is None  # nothing there: gpu null, unavailable, not an error
    assert (g.status()["state"], g.status()["reason"]) == ("unavailable", "not_exposed")
    make(tmp_path, {"devices/platform/gpu.0/load": ""})
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) is None
    assert g.status()["state"] == "error" and g.status()["issues"] == [{"target": "load", "reason": "invalid_data"}]


def extend_with(monkeypatch, tmp_path, status=None, release=None, conf="< POWER_MODEL ID=2 NAME=MAXN_SUPER >"):
    """jetson.extend() with the nvpmodel status and L4T release files faked and a fixed GPU."""
    import io
    real = open
    files = {"/var/lib/nvpmodel/status": status, "/etc/nvpmodel.conf": conf,
             sysfs.host_path("/etc/nv_tegra_release"): release}

    def fake(path, *args, **kw):
        if path in files:
            if files[path] is None:
                raise FileNotFoundError(path)
            if isinstance(files[path], bytes):  # as open() would fail on bytes that are not UTF-8
                files[path].decode()
            return io.StringIO(files[path])
        return real(path, *args, **kw)
    monkeypatch.setattr(sysfs, "open", fake, raising=False)

    def fake_gpu(group):
        return {"usage": group.got(50.0), "freq": None, "max_freq": None}
    monkeypatch.setattr(jetson, "gpu", lambda group=None: fake_gpu(group))
    monkeypatch.setattr(jetson, "tach_fans", lambda group=None: [])
    stats, g = {"fans": [], "system": {}}, metadata.groups("gpu", "fans", "power_mode", "board_info")
    jetson.extend(stats, g)
    return stats, {n: x.status() for n, x in g.items()}


def test_extend_power_mode_and_l4t(monkeypatch, tmp_path):
    stats, st = extend_with(monkeypatch, tmp_path, "pmode:0002\n", "# R36 (release), REVISION: 5.2, GCID: 1")
    assert stats == {"fans": [], "system": {"l4t": "R36.5.2"}, "power_mode": "MAXN_SUPER",
                     "gpu": {"usage": 50.0, "freq": None, "max_freq": None}}
    assert st["power_mode"]["state"] == st["board_info"]["state"] == st["gpu"]["state"] == "ok"


def test_extend_bad_power_mode_and_l4t_do_not_stop_the_rest(monkeypatch, tmp_path):
    stats, st = extend_with(monkeypatch, tmp_path, "garbage", "not a release line")
    assert stats["power_mode"] is None and "l4t" not in stats["system"] and stats["gpu"]["usage"] == 50.0
    assert st["power_mode"]["issues"] == [{"target": "status", "reason": "invalid_data"}]
    assert st["board_info"]["issues"] == [{"target": "l4t", "reason": "invalid_data"}]
    assert st["gpu"]["state"] == "ok"


def test_extend_without_nvpmodel(monkeypatch, tmp_path):
    """No nvpmodel mount (e.g. a container started without deploy/docker/compose.jetson.yaml): absent, not an error."""
    stats, st = extend_with(monkeypatch, tmp_path, None, None)
    assert stats["power_mode"] is None and st["power_mode"]["state"] == st["board_info"]["state"] == "unavailable"


def test_tach_fan_unreadable_rpm(tmp_path):
    root = fake_hwmon(tmp_path, [{"name": "pwm_tach", "rpm": "x"}, {"name": "other_tach", "rpm": 0}])
    g = metadata.Group("fans")
    assert jetson.tach_fans(root, g) == [{"name": "pwm_tach", "rpm": None, "percent": None},
                                         {"name": "other_tach", "rpm": 0, "percent": None}]
    assert g.status()["state"] == "partial"


def test_gpu_undecodable_candidate_falls_back(tmp_path):
    make(tmp_path, {"devices/platform/gpu.0/load": 500})
    (tmp_path / "class/devfreq/17000000.gpu/device").mkdir(parents=True)
    (tmp_path / "class/devfreq/17000000.gpu/device/load").write_bytes(b"\xff\n")
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g)["usage"] == 50.0
    assert g.status()["issues"] == []  # the failed candidate is not the result


def test_gpu_unlistable_devfreq_without_fallback(tmp_path, monkeypatch):
    from tests.collector.test_common import deny_listing
    (tmp_path / "class/devfreq/17000000.gpu").mkdir(parents=True)
    deny_listing(monkeypatch, tmp_path / "class/devfreq", errno.EACCES)
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) is None
    assert (g.status()["state"], g.status()["reason"]) == ("error", "permission_denied")  # not "not_exposed"


# PR #24 re-review: a load found through another path does not explain missing clocks

GPU_FILES = {"class/devfreq/17000000.gpu/cur_freq": 306000000, "class/devfreq/17000000.gpu/max_freq": 1020000000,
             "devices/platform/gpu.0/load": 500}


def test_gpu_matrix_a_failed_load_candidate_hidden(tmp_path):
    make(tmp_path, {**GPU_FILES, "class/devfreq/17000000.gpu/device/load": "bad"})
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) == {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000}
    assert g.status() == {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0}


@pytest.mark.parametrize("code, reason", [(errno.EACCES, "permission_denied"), (errno.EIO, "io_error")])
def test_gpu_matrix_b_unlistable_devfreq_with_load(tmp_path, monkeypatch, code, reason):
    from tests.collector.test_common import deny_listing
    make(tmp_path, GPU_FILES)
    deny_listing(monkeypatch, tmp_path / "class/devfreq", code)
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) == {"usage": 50.0, "freq": None, "max_freq": None}
    assert g.status() == {"state": "partial", "reason": "some_unreadable", "issues_truncated": 0,
                          "issues": [{"target": "devfreq", "reason": reason}]}
    monkeypatch.undo()  # listing works again
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) == {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000}
    assert g.status()["state"] == "ok"


def test_gpu_matrix_c_no_devfreq_is_not_a_warning(tmp_path):
    make(tmp_path, {"devices/platform/gpu.0/load": 500})
    g = metadata.Group("gpu")
    assert jetson.gpu(str(tmp_path), g) == {"usage": 50.0, "freq": None, "max_freq": None}
    assert g.status()["state"] == "ok"


def test_extend_undecodable_nvpmodel_conf_keeps_the_mode_id(monkeypatch, tmp_path):
    stats, st = extend_with(monkeypatch, tmp_path, "pmode:0002\n", None, conf=b"\xff\n")
    assert stats["power_mode"] == "2" and stats["gpu"]["usage"] == 50.0  # the id instead of the name
    assert st["power_mode"]["state"] == "partial"
    assert st["power_mode"]["issues"] == [{"target": "conf", "reason": "invalid_data"}]
