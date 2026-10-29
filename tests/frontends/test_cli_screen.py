"""The live terminal view (screen()): fits the terminal, levels like the web page, plain without color."""
import re
from collections import deque

import pytest

from frontends import cli
from frontends.cli import (ANSI, BOTTOM, LIMITS, VIEWS, Style, attention, busiest_first, data_age, fit,
                           make_style, next_offset, next_view, render, screen, tabbar, use_levels, vlen)
from tests.frontends.test_cli import FULL

HOT = dict(FULL, cpu=[{"id": 0, "usage": 12.5, "freq": 1728000000}, {"id": 7, "usage": 91.4, "freq": 1728000000}],
           temperature={"cpu": 51.2, "tj": 86.2})
LIVE = {"state": "live", "age": 0.4, "error": None, "addr": "localhost:9797", "interval": 1.0}


def plain(lines):
    return [ANSI.sub("", line) for line in lines]


# the last four are shorter than the header, a line and the footer together
@pytest.mark.parametrize("width,height", [(119, 40), (79, 24), (60, 20), (40, 12), (20, 6), (80, 5), (12, 3), (10, 1)])
def test_fits_the_terminal(width, height):
    lines = screen(HOT, None, width, height, Style(), LIVE, {})
    assert len(lines) <= height
    assert all(vlen(line) <= width for line in lines)


def test_no_color_means_no_escapes():
    lines = screen(HOT, None, 119, 40, Style(color=False), LIVE, {})
    assert not any("\033" in line for line in lines)


def test_ascii_without_utf8():
    text = "\n".join(screen(HOT, None, 119, 40, Style(color=False, utf8=False), LIVE, {"CPU": deque([10, None, 90])}))
    assert not re.search(r"[^\x00-\x7f°·↓↑]", text)  # labels and units only; bars, rules and sparks are ASCII


def test_attention_follows_the_web_levels():
    # CPU mean 52 % is fine; tj 86.2 °C is a warning on Jetson Orin (85/95), not critical
    assert attention(HOT, None) == [("tj", "86.2 °C", "warn")]
    assert "NEEDS ATTENTION" in "\n".join(plain(screen(HOT, None, 119, 40, Style(), LIVE, {})))
    assert "NEEDS ATTENTION" not in "\n".join(plain(screen(FULL, None, 119, 40, Style(), LIVE, {})))


def test_offline_keeps_the_last_data():
    down = dict(LIVE, state="offline", age=47, error="cannot reach platmon at x:9797 (timed out)\n  check that its service runs")
    out = plain(screen(FULL, None, 119, 40, Style(), down, {}))
    assert "OFFLINE" in out[0] and "data 47 s old" in out[0]
    assert any("NOT CURRENT" in line for line in out)
    assert any(line.lstrip().startswith("RAM") for line in out)  # still there, dimmed


def test_too_short_says_what_was_left_out_and_how_to_reach_it():
    out = plain(screen(HOT, None, 119, 14, Style(), LIVE, {}))
    assert len(out) == 14
    assert "6 below" in out[-3] and "PgUp/PgDn" in out[-3]  # what is out of sight, and the keys for it


def test_fit_keeps_escapes_and_resets():
    st = Style()
    cut = fit(st("abcdef", "warn") + "ghij", 4)
    assert vlen(cut) == 4 and cut.endswith("\033[0m")


def test_server_levels_replace_the_defaults(monkeypatch):
    """/api/status "levels" ([thresholds]) as the web page uses them; bad or missing ones keep the defaults."""
    monkeypatch.setattr(cli, "LIMITS", dict(LIMITS))
    use_levels({"temperature": [80, 90], "memory": [10, 20], "cpu": [90, 80], "gpu": "x", "swap": [1, 2]})
    assert cli.LIMITS["memory"] == (10, 20) and cli.LIMITS["cpu"] == (80, 90) and cli.LIMITS["gpu"] == (80, 90)
    assert "swap" not in cli.LIMITS
    assert attention(HOT, None) == [("RAM", "25.0 %", "crit"), ("tj", "86.2 °C", "warn")]  # 80/90 replaces Orin's 85/95
    use_levels({"temperature": None})
    assert attention(HOT, None)[1] == ("tj", "86.2 °C", "warn")  # null: the board default again (85/95)
    use_levels(None)  # an older server
    assert cli.LIMITS["memory"] == (10, 20)


def test_low_levels_attention_and_colors(monkeypatch):
    """Battery charge and Wi-Fi signal: at or below a level, the opposite of the other readings; only from a
    current observation, and never from an empty bay or a disconnected interface."""
    import copy
    monkeypatch.setattr(cli, "LIMITS", dict(LIMITS))
    obs = copy.deepcopy(OBSERVED)
    bat, wlan = obs["groups"]["battery"]["data"]["batteries"][0], obs["groups"]["wifi"]["data"]["interfaces"][0]
    obs["groups"]["battery"]["data"]["batteries"].append(dict(bat, name="BAT2", present=False, percent=3))
    obs["groups"]["wifi"]["data"]["interfaces"].append(dict(wlan, name="wlan1", connected=False, signal_dbm=-99))
    assert attention(FULL, obs) == []  # 89 % and -47 dBm are fine
    bat["percent"], wlan["signal_dbm"] = 20, -70
    assert attention(FULL, obs) == [("BAT1", "20 %", "warn"), ("wlan0", "-70 dBm", "warn")]
    bat["percent"], wlan["signal_dbm"] = 10, -81
    assert attention(FULL, obs) == [("BAT1", "10 %", "crit"), ("wlan0", "-81 dBm", "crit")]
    power = "\n".join(screen(RICH, obs, 119, 90, Style(), LIVE, {}, "power"))
    network = "\n".join(screen(RICH, obs, 119, 90, Style(), LIVE, {}, "network"))
    crit = r"\033\[31(;1)?m"  # the critical color, bold or not
    assert re.search(crit + "10 %", power) and re.search(crit + "-81 dBm", network)
    assert "NEEDS ATTENTION" in "\n".join(plain(screen(FULL, obs, 119, 40, Style(), LIVE, {})))
    obs["groups"]["battery"]["observation"]["stale"] = True
    obs["groups"]["wifi"]["observation"]["stale"] = True
    assert attention(FULL, obs) == []  # not a current reading
    # the server's levels: low ones arrive warning above critical; the wrong order keeps the default
    use_levels({"battery": [30, 15], "wifi": [-80, -70]})
    assert cli.LIMITS["battery"] == (30, 15) and cli.LIMITS["wifi"] == (-70, -80)


def test_fallback_board_levels_match_the_server():
    """The command's copy for older servers is the server's table (native/src/config.rs, levels)."""
    import re
    from pathlib import Path
    source = (Path(__file__).parents[2] / "native/src/config.rs").read_text(encoding="utf-8")
    table = re.search(r'let pair = match p \{ (.*?), _ =>', source).group(1)
    server = {name: (float(a), float(b)) for name, a, b in re.findall(r'"([^"]+)" => \[([0-9.]+), ([0-9.]+)\]', table)}
    assert cli.TEMP_LIMITS == server and len(server) == 2


def test_data_age_is_the_servers_plus_time_since():
    assert data_age({"sample": {"data_age_ms": 1500}}, got=10.0, now=12.0) == 3.5
    assert data_age({}, got=10.0, now=12.0) == 2.0  # an older server: the time since its answer
    assert data_age({"sample": {"data_age_ms": None}}, got=10.0, now=10.5) == 0.5


def test_busiest_interfaces_first():
    rate = lambda name, rx, tx=0: {"name": name, "rx": {"errors": 0, "dropped": 0}, "tx": {"errors": 0, "dropped": 0},
                                   "rates": {"rx_bytes_per_s": rx, "tx_bytes_per_s": tx,
                                             "rx_packets_per_s": 0, "tx_packets_per_s": 0}}
    items = [rate("br0", 0), {"name": "wlan1", "rates": None, "reason": "warmup"}, rate("eth0", 10, 5), rate("can0", 0)]
    assert [i["name"] for i in busiest_first(items)] == ["eth0", "br0", "can0", "wlan1"]
    narrow = plain(screen(dict(FULL, network={"interfaces": items}), None, 70, 40, Style(), LIVE, {}))
    assert any(line.strip().startswith("eth0") for line in narrow)  # two rows on a narrow screen: the busy one shown


def test_ascii_on_request():
    assert make_style({}, "utf-8").full == "█"
    assert make_style({"PLATMON_ASCII": "1"}, "utf-8").full == "#"
    assert make_style({}, "ascii").full == "#" and make_style({"NO_COLOR": "1"}, "UTF-8").color is False


def test_render_is_unchanged_by_the_live_view():
    """--once, pipes and /text keep the plain screen."""
    assert "\033" not in render(HOT)


# ---------------------------------------------------------------------------------------------------------
# The views (the web page's tabs): Overview is the whole screen as before, the detail views show their kind
# in full, so a narrow terminal reaches every reading by switching views.

RATES = {"rx_bytes_per_s": 2048.0, "tx_bytes_per_s": 1024.0, "rx_packets_per_s": 8.0, "tx_packets_per_s": 4.0}
IO_RATES = {"read_bytes_per_s": 4096.0, "write_bytes_per_s": 512.0, "reads_per_s": 3.0, "writes_per_s": 1.0,
            "io_time_ratio": 0.25}
RICH = dict(HOT, npu={"usage": 11.0, "freq": 614400000},
            gpu={"usage": 73.0, "freq": 918000000, "max_freq": 1300000000},
            network={"interfaces": [{"name": "eth0", "rates": RATES, "rx": {"errors": 0, "dropped": 0},
                                     "tx": {"errors": 1, "dropped": 2}}]},
            disk_io={"disks": [{"name": "nvme0n1", "rates": IO_RATES, "in_flight": 0}]},
            pressure={"resources": {"cpu": {"some": {"avg10": 12.4}},
                                    "io": {"some": {"avg10": 3.1}, "full": {"avg10": 0.8}}}})
G = 2**30
OBSERVED = {"groups": {
    "storage": {"observation": {"data_age_ms": 4200, "stale": False}, "data": {
        "filesystems": [{"mount_points": [{"path": "/", "read_only": False}], "device": "/dev/nvme0n1p1",
                         "fstype": "ext4", "major": 259, "minor": 1, "used_bytes": 40 * G,
                         "total_bytes": 100 * G, "available_bytes": 55 * G}],
        "partitions": [{"disk": "nvme0n1", "mount_points": ["/"]}]}},
    "wifi": {"observation": {"data_age_ms": 4200, "stale": False}, "data": {"interfaces": [
        {"name": "wlan0", "connected": True, "signal_dbm": -47, "signal_raw": None, "link_quality": 62,
         "noise_dbm": -92}]}},
    "probe": {"observation": {"data_age_ms": 4200, "stale": False}, "data": {"targets": [
        {"address": "192.168.1.1", "port": 53, "connect_ms": 1.8, "failures": 0, "attempts": 10,
         "reason": None}]}},
    "battery": {"observation": {"data_age_ms": 4200, "stale": False}, "data": {
        "batteries": [{"name": "BAT1", "present": True, "percent": 89, "status": "Not charging",
                       "capacity_level": "Normal", "technology": "Li-ion", "health": None,
                       "charge_now_uah": 3026000, "charge_full_uah": 3400000,
                       "charge_full_design_uah": 4000000, "energy_now_uwh": None, "energy_full_uwh": None,
                       "energy_full_design_uwh": None, "current_ua": -1200000, "power_uw": None,
                       "voltage_uv": 16436000, "cycle_count": 237}],
        # a driver name long enough to crowd a narrow terminal, as a USB-PD source really is
        "external": [{"name": "ADP1", "type": "Mains", "online": True},
                     {"name": "ucsi-source-psy-USBC000:001", "type": "USB", "online": False}]}}}}


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("width,height", [(119, 40), (79, 24), (60, 20), (44, 22), (40, 12), (20, 6), (12, 3)])
def test_every_view_fits_the_terminal(view, width, height):
    lines = screen(RICH, OBSERVED, width, height, Style(), LIVE, {}, view)
    assert len(lines) <= height
    assert all(vlen(line) <= width for line in lines)


@pytest.mark.parametrize("view", VIEWS)
def test_every_view_is_ascii_without_utf8(view):
    text = "\n".join(screen(RICH, OBSERVED, 119, 40, Style(color=False, utf8=False), LIVE, {}, view))
    assert not re.search(r"[^\x00-\x7f°·↓↑]", text)  # labels and units only; bars, rules and arrows are ASCII


@pytest.mark.parametrize("width", [119, 79, 60, 44])
def test_the_battery_keeps_every_reading_at_any_width(width):
    """Power & battery carries it, as the web page's Battery card does. A narrow terminal gives up the
    bar and then wraps the readings onto their own lines; it never drops one. The height is generous on
    purpose: what a view does when it is taller than the terminal is scrolling, tested separately."""
    lines = plain(screen(RICH, OBSERVED, width, 90, Style(), LIVE, {}, "power"))
    start = next(n for n, line in enumerate(lines) if "BATTERY" in line)
    # wrapping splits a reading across lines, so the section is compared as one run of words
    section = re.sub(r"\s+", " ", " ".join(lines[start:]))
    assert "observed 4 s ago" in section and "BAT1" in section and "89 %" in section
    for reading in ("Not charging", "16.44 V", "-1.20 A", "237 cycles", "85 % of design"):
        assert reading in section, f"{reading} lost at {width} columns"
    assert "ADP1 on" in section and "ucsi-source-psy-USBC000:001 off" in section  # no supply cut off


def test_the_battery_section_is_absent_without_the_observation():
    """A host with no supply, or a server without the endpoint: the section is simply not there."""
    text = "\n".join(plain(screen(RICH, {"groups": {}}, 119, 40, Style(), LIVE, {}, "power")))
    assert "BATTERY" not in text and "POWER & FANS" in text


def test_the_tab_bar_names_every_view_and_marks_the_active_one():
    bar = tabbar(Style(color=False), 119, "network")
    assert "[3 Network]" in bar and "1 Overview" in bar and f"{len(VIEWS)} System" in bar
    assert "5 Temperature" in bar and "6 Power & battery" in bar  # the two the thermal view was split into
    narrow = tabbar(Style(color=False), 44, "network")  # shorter labels, still every view
    assert "[3 Ne]" in narrow and vlen(narrow) <= 44
    assert all(str(n) in narrow for n in range(1, len(VIEWS) + 1))
    tiny = tabbar(Style(color=False), 24, "system")  # no room for seven labels: the position instead
    assert tiny.strip().startswith(f"{len(VIEWS)}/{len(VIEWS)} Sys") and vlen(tiny) <= 24


def test_keys_switch_views():
    assert next_view("3", "overview") == "network"
    assert next_view("\t", "overview") == "resources"
    assert next_view("\033[C", "system") == "overview"  # right wraps round
    assert next_view("\033[D", "overview") == "system"  # left wraps back
    assert next_view("\033[Z", "resources") == "overview"  # Shift-Tab
    assert next_view("x", "network") == "network" and next_view("9", "network") == "network"
    assert next_view("\033[1;2C", "network") == "storage"  # a modified arrow moves; its digits are not a number


def test_the_active_view_is_the_one_drawn():
    system = plain(screen(RICH, OBSERVED, 79, 24, Style(), LIVE, {}, "system"))
    assert any("Kernel" in line for line in system) and not any("NEEDS ATTENTION" in line for line in system)
    overview = plain(screen(RICH, OBSERVED, 79, 24, Style(), LIVE, {}, "overview"))
    assert any("NEEDS ATTENTION" in line for line in overview) and not any("Kernel" in line for line in overview)


def test_detail_views_keep_what_the_narrow_overview_drops():
    """At 79 columns the Overview has no room for disk I/O, power, Wi-Fi or TCP; its views do."""
    overview = "\n".join(plain(screen(RICH, OBSERVED, 79, 40, Style(), LIVE, {}, "overview")))
    assert "I/O TIME" not in overview and "VDD_IN" not in overview and "wlan0" not in overview
    storage = "\n".join(plain(screen(RICH, OBSERVED, 79, 40, Style(), LIVE, {}, "storage")))
    assert "I/O TIME" in storage and "nvme0n1" in storage and "1 partition, 1 mounted" in storage
    thermal = "\n".join(plain(screen(RICH, OBSERVED, 79, 40, Style(), LIVE, {}, "thermal")))
    assert "tj" in thermal and "VDD_IN" not in thermal  # temperatures alone
    power = "\n".join(plain(screen(RICH, OBSERVED, 79, 40, Style(), LIVE, {}, "power")))
    assert "VDD_IN" in power and "fan pwmfan" in power and "BATTERY" in power
    network = "\n".join(plain(screen(RICH, OBSERVED, 79, 40, Style(), LIVE, {}, "network")))
    assert "wlan0" in network and "-47 dBm" in network and "192.168.1.1:53" in network


def test_resources_shows_the_swap_the_live_overview_never_had():
    text = "\n".join(plain(screen(RICH, OBSERVED, 79, 40, Style(), LIVE, {}, "resources")))
    assert "SWAP" in text and "4.0G" in text  # FULL has 4 GiB of swap, none of it used
    assert "max 1300 MHz" in text and "NPU" in text and "RESOURCE PRESSURE" in text
    assert "CPU0" not in text and " 0 " in text  # the core grid, as the Overview numbers them


def test_system_lists_the_boards_own_fields():
    board = dict(RICH, system=dict(RICH["system"], firmware="1.2"))
    text = "\n".join(plain(screen(board, None, 79, 24, Style(), LIVE, {}, "system")))
    assert "Architecture   aarch64" in text and "L4T            R36.5.2" in text
    assert "firmware       1.2" in text  # an unknown key keeps its own name, as the web page adds it
    assert "Power mode" not in text  # it stays in Power & battery, as on the web page


@pytest.mark.parametrize("view,text", [
    ("network", "No interface, Wi-Fi or TCP connect readings"),
    ("storage", "No filesystem or disk I/O readings"),
    ("thermal", "No temperature readings"),
    ("power", "No power rail, fan or battery readings")])
def test_a_view_with_nothing_to_show_says_so(view, text):
    bare = dict(FULL, temperature={}, power={}, fans=[], network=None, disk_io=None)
    lines = plain(screen(bare, None, 79, 24, Style(), LIVE, {}, view))
    assert any(text in line for line in lines)


@pytest.mark.parametrize("width", [119, 79, 60, 44, 30])
def test_a_long_name_never_cuts_a_value_off_a_table(width):
    """An interface like enx80691aa55882 used to push the rate columns past the right edge, so the last
    one ended mid-number ("0 B"). The name column holds the longest name; where it cannot, the name is
    what gets cut, and a value column drops whole before that."""
    items = [dict(RICH["network"]["interfaces"][0], name=name) for name in ("lo", "enx80691aa55882")]
    lines = plain(screen(dict(RICH, network={"interfaces": items}), OBSERVED, width, 30, Style(), LIVE,
                         {}, "network"))
    rows = [line for line in lines if line.strip().startswith(("lo", "enx"))]
    assert len(rows) == 2
    values = ["2.0 KiB/s", "1.0 KiB/s", "8 · 4", "0 · 3"]  # rx, tx, packets, errors · dropped of RATES
    for row in rows:
        assert vlen(row) <= width
        assert any(value in row for value in values), row  # at least the first value column survives
        for value in values:  # and each one is either there in full or dropped, never cut
            for cut in (value[:-1], value[:-2]):
                assert cut not in row or value in row, (row, cut)


def test_a_view_longer_than_the_terminal_scrolls():
    """A board with 38 sensors could not be read past the seventeenth: the view ended in "+N more lines ·
    enlarge the terminal" and no key moved it."""
    many = dict(HOT, temperature={f"sensor{i:02}": 40.0 + i for i in range(38)})
    offset, page, seen = 0, 1, set()
    for keys in (None, "\033[B", "\033[6~", "\033[F", "\033[1~"):
        if keys is not None:
            offset = next_offset(keys, offset, page)
        status = dict(LIVE)
        lines = plain(screen(many, None, 79, 18, Style(), status, {}, "thermal", offset))
        offset, page = status["scroll"]
        assert len(lines) <= 18 and all(vlen(line) <= 79 for line in lines)
        seen.add(offset)
        body = "\n".join(lines)
        if offset:
            assert f"{offset} above" in body
    assert seen == {0, 1, 13, 27}  # top, a line, a page, the end, back to the top
    # the end shows the last sensors, which "+N more lines" never reached
    status = dict(LIVE)
    end = "\n".join(plain(screen(many, None, 79, 18, Style(), status, {}, "thermal", BOTTOM)))
    assert "sensor00" in end and "27 above" in end


def test_scroll_keys_and_clamping():
    assert next_offset("\033[B", 0, 10) == 1 and next_offset("\033[A", 5, 10) == 4
    assert next_offset("\033[A", 0, 10) == 0  # never above the top
    assert next_offset("\033[6~", 2, 10) == 12 and next_offset("\033[5~", 12, 10) == 2
    assert next_offset("\033[1~", 9, 10) == 0 and next_offset("\033OF", 0, 10) == BOTTOM
    assert next_offset("x", 4, 10) == 4 and next_offset("3", 4, 10) == 4  # a view key does not scroll
    # a view that fits is never scrolled, and switching views starts at the top again
    status = dict(LIVE)
    screen(HOT, None, 119, 40, Style(), status, {}, "system", 7)
    assert status["scroll"][0] == 0


def test_the_overview_detail_takes_the_width_that_is_free():
    """"x86_pkg_temp · hottest of 38" was cut to "x86_pkg_temp · h" at 16 columns while 22 columns of an
    80-column terminal went unused. The column grows to what the details need, and no further, so a
    board with short ones keeps the layout it had."""
    many = dict(HOT, temperature={**{f"coretemp Core {i}": 40.0 + i for i in range(37)}, "x86_pkg_temp": 88.0})
    history = {"CPU": [10] * 24, "TEMP": [88] * 24}
    for width in (119, 100, 78, 70):
        row = [line for line in plain(screen(many, None, width, 40, Style(), LIVE, history, "overview"))
               if line.lstrip().startswith("TEMP")][0]
        assert "x86_pkg_temp · hottest of 38" in row, (width, row)
        assert vlen(row) <= width
    # a short detail leaves the column where it was: the spark still starts at the same place
    short = plain(screen(HOT, None, 119, 40, Style(), LIVE, history, "overview"))
    cpu = [line for line in short if line.lstrip().startswith("CPU ")][0]
    assert cpu.index("mean of") == 2 + 6 + 7 + 1 + 20 + 2  # label, value, bar, gap: where it always was
    assert cpu.rstrip().endswith("▁" * 24) and vlen(cpu) <= 119


def test_the_offline_banner_wraps_instead_of_losing_the_advice():
    """A cut banner loses the errno, the retry interval or the name of the script to run — the whole
    point of it. It needs 111 columns, so on anything narrower it wraps."""
    error = cli.explain("localhost:9797", type("E", (OSError,), {"reason": "[Errno 111] refused"})())
    down = dict(LIVE, state="offline", age=47, error=error)
    for width in (119, 79, 60, 44):
        lines = plain(screen(FULL, None, width, 24, Style(), down, {}, "system"))
        assert all(vlen(line) <= width for line in lines)
        banner = " ".join(line.strip() for line in lines if "NOT CURRENT" in line
                          or line.startswith(" " * 17))
        for word in ("[Errno 111] refused", "retrying every 1 s", "deploy/systemd/install.sh",
                     "deploy/docker/start.sh"):
            assert word in " ".join(banner.split()), (width, word, banner)


def test_offline_dims_every_view():
    down = dict(LIVE, state="offline", age=47, error="cannot reach platmon at x:9797 (timed out)")
    lines = screen(RICH, OBSERVED, 119, 40, Style(), down, {}, "power")
    assert any("NOT CURRENT" in ANSI.sub("", line) for line in lines)
    assert any("VDD_IN" in ANSI.sub("", line) for line in lines)  # still there, dimmed
