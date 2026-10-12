"""collector/wifi.py: /proc/net/wireless rows (format of net/wireless/wext-proc.c), the observation and the
CLI lines. Fake file contents; the last test reads the host's file where there is one."""
import os
from collector import metadata
from types import SimpleNamespace

import pytest

import platmon
from collector import sysfs
from collector.wifi import Wifi, parse
from frontends.cli import wifi_lines
from tests.collector.test_network_integration import ini_file

HEAD = ("Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n")
ORIN = "wlP1p1s0: 0000   60.  -50.  -256        0      0      0      0      0        0\n"   # as read on the boards
PI = " wlan0: 0000   46.  -64.  -256        0      0      0    312      0        0\n"
DOWN = " wlan1: 0000    0     0     0        0      0      0      0      0        0\n"      # listed, not associated
RAW = "  eth9: 0000   70.   70.    0        0      0      0      0      0        0\n"      # a driver without dBm
NOISE = " wlan2: 0000   50.  -55.  -95.       0      0      0      0      0        0\n"


def observe(source):
    def read(path):
        if isinstance(source, Exception):
            raise source
        return source
    g = metadata.Group("wifi")
    return Wifi(read=read, netns=lambda: SimpleNamespace(st_dev=1, st_ino=2),
                indexes=lambda: [(3, "wlan0")], link=lambda name, index: True)(g), g.status()


def test_connected_rows_from_the_boards():
    rows, bad = parse(HEAD + ORIN + PI)
    assert bad == [] and rows == [
        {"name": "wlP1p1s0", "connected": None, "connection_state": "unknown", "signal_dbm": -50, "signal_raw": None, "link_quality": 60, "noise_dbm": None},
        {"name": "wlan0", "connected": None, "connection_state": "unknown", "signal_dbm": -64, "signal_raw": None, "link_quality": 46, "noise_dbm": None}]


def test_unupdated_signal_does_not_determine_connection():
    assert parse(HEAD + DOWN)[0] == [{"name": "wlan1", "connected": None, "connection_state": "unknown", "signal_dbm": None, "signal_raw": None,
                                      "link_quality": None, "noise_dbm": None}]  # never a last value, never 0


def test_a_value_without_dbm_is_not_called_dbm():
    row = parse(HEAD + RAW)[0][0]
    assert (row["signal_dbm"], row["signal_raw"]) == (None, 70)


def test_noise_when_the_driver_gives_it():
    assert parse(HEAD + NOISE)[0][0]["noise_dbm"] == -95 and parse(HEAD + ORIN)[0][0]["noise_dbm"] is None  # -256: n/a


def test_bad_rows_and_files():
    rows, bad = parse(HEAD + ORIN + "garbage\n" + "a/b: 0000 1. 2. 3. 0 0 0 0 0 0\n")
    assert [r["name"] for r in rows] == ["wlP1p1s0"] and [t for t, _ in bad] == ["line4", "line5"]
    with pytest.raises(ValueError):
        parse("not the header\n")


@pytest.mark.parametrize("source, state, reason", [
    (HEAD + PI, "ok", None), (HEAD, "unavailable", "not_detected"), (FileNotFoundError(2, "x"), "unavailable", "not_exposed"),
    (PermissionError(13, "x"), "error", "permission_denied"), ("junk", "error", "invalid_data")])
def test_observation_states(source, state, reason):
    out, status = observe(source)
    assert (status["state"], status["reason"]) == (state, reason)
    assert out["provider"] == "proc_net_wireless" and out["scope"]["kind"] == "process_network_namespace"


def test_wifi_is_on_by_default_with_a_5_s_candidate_interval(tmp_path):
    cfg = platmon.load_config()
    assert cfg["wifi"].getboolean("enabled") and cfg["wifi"].getfloat("interval") == 5.0
    with pytest.raises(ValueError, match="out of range"):
        platmon.load_config(ini_file(tmp_path, "[wifi]\ninterval = 0.5\n"))


def test_cli_lines():
    def obs(interfaces, stale=False):
        return {"groups": {"wifi": {"observation": {"data_age_ms": 3000, "stale": stale}, "data": {"interfaces": interfaces}}}}
    rows = parse(HEAD + PI + DOWN + RAW)[0]
    for item in rows:
        item["connected"] = item["name"] != "wlan1"
        item["connection_state"] = "connected" if item["connected"] else "disconnected"
    assert wifi_lines(obs(rows)) == ["WIFI  observed 3 s ago", "  wlan0  -64 dBm  link quality 46",
                                     "  wlan1  not connected", "  eth9  signal 70 (unit not reported)  link quality 70"]
    assert wifi_lines(obs(rows, stale=True))[0] == "WIFI  observed 3 s ago (not current)"
    assert wifi_lines(obs([])) == [] and wifi_lines(None) == [] and wifi_lines({"groups": {"wifi": {"data": None}}}) == []


def test_real_host_file():
    group = metadata.Group("wifi")
    out = Wifi()(group)
    status = group.status()
    assert status["state"] in ("ok", "unavailable", "partial")
    for i in out["interfaces"]:
        assert i["signal_dbm"] is None or i["signal_dbm"] < 0


def test_observation_exports_known_interface_identity():
    from collector.network import namespace_id
    out, _ = observe(HEAD + PI)
    assert out["scope"]["id"] == namespace_id(SimpleNamespace(st_dev=1, st_ino=2))
    assert out["interfaces"][0]["ifindex"] == 3


def test_unavailable_identity_is_not_invented():
    def denied():
        raise PermissionError(13, "denied")
    g = metadata.Group("wifi")
    out = Wifi(read=lambda path: HEAD + PI, netns=denied, indexes=lambda: [])(g)
    assert out["scope"]["id"] is None and out["interfaces"][0]["ifindex"] is None
    assert g.status()["state"] == "partial"


def test_signal_not_updated_does_not_claim_disconnection():
    out, _ = observe(HEAD + PI.replace('-64.', '-64 '))
    assert out["interfaces"][0]["connected"] is True
    assert out["interfaces"][0]["signal_dbm"] is None


@pytest.mark.parametrize("result,state,connected", [
    (True, "connected", True), (False, "disconnected", False), (None, "unknown", None),
    (PermissionError(13, "private path"), "permission_denied", None),
    (FileNotFoundError(2, "private path"), "unsupported", None),
    (OSError(5, "private path"), "unavailable", None)])
def test_connection_states_are_not_inferred_from_signal(result, state, connected):
    def link(name, index):
        if isinstance(result, Exception):
            raise result
        return result
    g = metadata.Group("wifi")
    out = Wifi(read=lambda path: HEAD + PI, netns=lambda: SimpleNamespace(st_dev=1, st_ino=2),
               indexes=lambda: [(3, "wlan0")], link=link)(g)
    item = out["interfaces"][0]
    assert item["connection_state"] == state and item["connected"] is connected
    assert item["signal_dbm"] == (-64 if connected is True else None)
    assert "private path" not in str(out) + str(g.status())


def test_carrier_and_index_from_fake_sysfs(tmp_path):
    from collector.wifi import link_connected
    d = tmp_path / "wlan0"
    d.mkdir()
    (d / "ifindex").write_text("3\n")
    (d / "carrier").write_text("1\n")
    assert link_connected("wlan0", 3, root=str(tmp_path)) is True
    (d / "carrier").write_text("0\n")
    assert link_connected("wlan0", 3, root=str(tmp_path)) is False
    assert link_connected("wlan0", 4, root=str(tmp_path)) is None
    (d / "carrier").write_text("2\n")
    with pytest.raises(ValueError):
        link_connected("wlan0", 3, root=str(tmp_path))


def test_interface_replaced_during_carrier_read_is_unknown():
    from collector.wifi import link_connected
    values = iter(["3", "1", "4"])
    assert link_connected("wlan0", 3, read=lambda path: next(values)) is None


def test_raw_zero_and_invalid_signal_range():
    rows, bad = parse(HEAD + RAW.replace('70.   70.', '70.    0.'))
    assert not bad and rows[0]["signal_raw"] == 0 and rows[0]["signal_dbm"] is None
    assert parse(HEAD + PI.replace('-64.', '-300.'))[1] == [("wlan0", "invalid_data")]


@pytest.mark.parametrize("flags,after,expected", [("0x0", "3", False), ("0x1000", "3", False), ("0x0", "4", None)])
def test_down_carrier_is_disconnected_without_issue(flags, after, expected):
    import errno
    from collector.wifi import link_connected
    indexes = iter(["3", after])
    def read(path):
        if path.endswith("/ifindex"):
            return next(indexes)
        if path.endswith("/flags"):
            return flags
        raise OSError(errno.EINVAL, "interface down")
    g = metadata.Group("wifi")
    out = Wifi(read=lambda p: HEAD + PI, netns=lambda: SimpleNamespace(st_dev=1, st_ino=2),
               indexes=lambda: [(3, "wlan0")],
               link=lambda name, index: link_connected(name, index, read=read))(g)
    item = out["interfaces"][0]
    assert item["connected"] is expected
    assert item["connection_state"] == ("disconnected" if expected is False else "unknown")
    assert item["signal_dbm"] is None
    assert g.status()["state"] == "ok" and not g.status()["issues"]


@pytest.mark.parametrize("error,flags", [(22, "0x1"), (5, "0x0"), (13, "0x0")])
def test_carrier_errors_are_not_all_treated_as_down(error, flags):
    from collector.wifi import link_connected
    def read(path):
        if path.endswith("/ifindex"):
            return "3"
        if path.endswith("/flags"):
            return flags
        raise OSError(error, "failed")
    with pytest.raises(OSError) as caught:
        link_connected("wlan0", 3, read=read)
    assert caught.value.errno == error
