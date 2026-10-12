from pathlib import Path

import pytest

from collector import BOARDS
from platmon import BOARD_TEMPERATURE, DEFAULTS, levels, load_config

SHIPPED_INI = str(Path(__file__).parents[2] / "platmon.ini")


def enabled(cfg):
    return {name for name in ("http",) if cfg[name].getboolean("enabled")}


def test_defaults():
    cfg = load_config()
    assert enabled(cfg) == {"http"}
    assert cfg["http"].getint("port") == 9797 and cfg["http"].getboolean("web")
    assert cfg["core"].getfloat("interval") == 1.0
    assert all(cfg[name].getboolean("enabled") for name in
               ("network", "disk_io", "storage", "wifi", "pressure", "probe", "history"))


def test_shipped_ini_matches_defaults():
    shipped, builtin = load_config(SHIPPED_INI), load_config()
    for section, options in DEFAULTS.items():
        for key, default in options.items():
            getter = {bool: "getboolean", int: "getint", float: "getfloat", str: "get"}[type(default)]
            assert getattr(shipped[section], getter)(key) == getattr(builtin[section], getter)(key)


def test_explicit_disabled_features_override_defaults(tmp_path):
    features = ("network", "disk_io", "storage", "wifi", "pressure", "probe", "history")
    ini = tmp_path / "disabled.ini"
    ini.write_text("".join(f"[{name}]\nenabled = no\n" for name in features))
    cfg = load_config(str(ini))
    assert all(not cfg[name].getboolean("enabled") for name in features)


def test_file_overrides_defaults(tmp_path):
    ini = tmp_path / "p.ini"
    ini.write_text("[http]\nport = 9000\nweb = no\n")
    cfg = load_config(str(ini))
    assert cfg["http"].getint("port") == 9000 and not cfg["http"].getboolean("web")
    assert cfg["http"]["bind"] == "0.0.0.0"  # untouched keys keep their default
    assert enabled(cfg) == {"http"}


def test_frontends_flag_enables_exactly_those(tmp_path):
    ini = tmp_path / "p.ini"
    ini.write_text("[http]\nenabled = no\n")
    assert enabled(load_config(str(ini), ["http"])) == {"http"}


@pytest.mark.parametrize("ini, frontends, error", [
    ("[htpp]\nport = 1\n", None, "unknown config section"),  # typos must not be silently ignored
    ("[http]\nprot = 9000\n", None, r"\[http\] unknown key\(s\): prot"),
    ("", ["web"], "unknown frontend"),
    ("", ["terminal"], "unknown frontend"),
    # configs from before the terminal view moved to the platmon command say what to do
    ("[terminal]\nenabled = no\n", None, r"\[terminal\] is no longer supported: .*platmon. command"),
    # bad values end in one clear error, not a traceback from deep inside a frontend
    ("[http]\nport = abc\n", None, r"port = 'abc' is not a valid int"),
    ("[http]\nenabled = maybe\n", None, "is not a valid bool"),
    ("[http]\nport = 70000\n", None, "out of range"),
    ("[core]\ninterval = 0\n", None, "out of range"),  # 0 would make the sampler spin
    # loads fine but would start nothing: refused here, so install.sh refuses before replacing anything
    ("[http]\nenabled = no\n", None, r"nothing to run: every output is disabled; set enabled = yes in \[http\]"),
    ("", [], "nothing to run"),  # --frontends with no name
    ("[core]\ninterval = -1\n", None, "out of range"),
    ("port = 1\n", None, "cannot parse config"),  # no section header
    ("[http]\nbind = 10%\n", ["http"], None),  # % is literal, not interpolation
    # web page levels: two numbers, warning below critical, within the scale
    ("[thresholds]\ncpu = 80\n", None, r"\[thresholds\] cpu = '80': give two numbers"),
    ("[thresholds]\ncpu = 80, 90, 95\n", None, "give two numbers"),
    ("[thresholds]\nmemory = high, 90\n", None, "give two numbers"),
    ("[thresholds]\ngpu = 90, 80\n", None, r"need 0 < warning < critical <= 100"),
    ("[thresholds]\nfilesystem = 80, 101\n", None, "<= 100"),
    ("[thresholds]\ncpu = nan, 90\n", None, "need 0 < warning"),
    ("[thresholds]\ntemperature = 0, 90\n", None, r"<= 150"),
    ("[thresholds]\ntemperature = 85, 151\n", None, r"<= 150"),
    ("[thresholds]\nswap = 80, 90\n", None, r"\[thresholds\] unknown key\(s\): swap"),
    ("[thresholds]\ntemperature = 70.5, 82\n", None, None),
])
def test_rejects_bad_config(tmp_path, ini, frontends, error):
    path = tmp_path / "p.ini"
    path.write_text(ini)
    if error is None:
        load_config(str(path), frontends)
        return
    with pytest.raises(ValueError, match=error):
        load_config(str(path), frontends)


def test_levels(tmp_path):
    """Defaults: percent levels, and no temperature (the page picks the board's); the file overrides them."""
    assert levels(load_config()) == {"cpu": [80, 90], "gpu": [80, 90], "memory": [80, 90], "filesystem": [80, 90],
                                     "temperature": None}
    ini = tmp_path / "p.ini"
    ini.write_text("[thresholds]\nmemory = 70, 85\ntemperature = 70.5, 82\n")
    got = levels(load_config(str(ini)))
    assert got["memory"] == [70, 85] and got["temperature"] == [70.5, 82] and got["cpu"] == [80, 90]


@pytest.mark.parametrize("platform, expected", [("Jetson Orin", [85, 95]), ("Raspberry Pi", [75, 80]),
                                                ("Jetson Xavier", [80, 90]), ("PC", [80, 90]), ("Linux", [80, 90])])
def test_board_temperature_when_not_configured(tmp_path, platform, expected):
    """The server resolves the board default, so clients get one final level; a configured one wins."""
    assert levels(load_config(), platform)["temperature"] == expected
    ini = tmp_path / "p.ini"
    ini.write_text("[thresholds]\ntemperature = 60, 70\n")
    assert levels(load_config(str(ini)), platform)["temperature"] == [60, 70]
    assert set(BOARD_TEMPERATURE) <= {name for _, name, _ in BOARDS}  # the names the collector detects


def test_missing_file():
    with pytest.raises(ValueError, match="cannot read"):
        load_config("/nonexistent/platmon.ini")


def test_sigterm_exits_promptly(tmp_path):
    """docker/systemd stop sends SIGTERM; platmon must exit right away with status 0."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as s:  # a free port for the http frontend
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ini = tmp_path / "p.ini"
    ini.write_text(f"[http]\nbind = 127.0.0.1\nport = {port}\n")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = subprocess.Popen([sys.executable, os.path.join(root, "platmon.py"), str(ini)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert "platmon: platform" in p.stdout.readline()  # started, startup line logged
    time.sleep(0.3)
    p.send_signal(signal.SIGTERM)
    t0 = time.monotonic()
    assert p.wait(timeout=5) == 0
    assert time.monotonic() - t0 < 2
