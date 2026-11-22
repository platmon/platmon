"""frontends/ui-spec.json, the design tokens the frontends share: every adapter agrees with it, so the
terminal and the web page cannot drift apart (views, levels, state colors), and the brand roles agree with
the approved brand colors. The terminal is the platmon command (native/frontend/src/screen.rs),
which reads its copy of the spec (UI_SPEC) for all of these; its source is checked here, and its attention
list through the binary when native/build.sh has built it."""
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "frontends/ui-spec.json").read_text(encoding="utf-8"))
WEB = ROOT / "frontends/web"
# the terminal's code, not its tests
TERMINAL = (ROOT / "native/frontend/src/screen.rs").read_text(encoding="utf-8").split("#[cfg(test)]")[0]
COMMAND = Path(os.environ.get("PLATMON_COMMAND", ROOT / "native/target/x86_64-unknown-linux-musl/release/platmon"))
G = 2**30
FULL = {
    "platform": "Jetson Orin", "model": "Test Board", "uptime": 90061, "power_mode": "MAXN_SUPER",
    "system": {"os": "Ubuntu 22.04.5 LTS", "kernel": "5.15.199-tegra", "arch": "aarch64", "hostname": "orin",
               "l4t": "R36.5.2"},
    "cpu": [{"id": 0, "usage": 12.5, "freq": 1728000000}, {"id": 2, "usage": 0.0, "freq": 729600000}],
    "gpu": {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000},
    "memory": {"total": 8 * G, "used": 2 * G, "swap_total": 4 * G, "swap_used": 0},
    "disk": {"total": 100 * G, "used": 25 * G},
    "temperature": {"cpu": 51.2},
    "power": {"VDD_IN": 4.82},
    "fans": [{"name": "pwmfan", "rpm": None, "percent": 31}, {"name": "pwm_tach", "rpm": 1741, "percent": None}],
}


def css_tokens():
    """styles.css custom properties: (light, dark), dark from the prefers-color-scheme block."""
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    dark_at = css.index("@media (prefers-color-scheme: dark)")
    tokens = lambda text: dict(re.findall(r"--([\w-]+):\s*([^;]+);", text))
    return tokens(css[:dark_at]), tokens(css[dark_at:css.index("}", css.index("{", dark_at) + 1) + 1])


def test_rust_command_copy_matches_the_spec():
    """The Rust platmon command's copy (native/frontend/src/screen.rs UI_SPEC: its build has only
    native/ and VERSION, so the file cannot be included)."""
    source = (ROOT / "native/frontend/src/screen.rs").read_text(encoding="utf-8")
    copy = re.search(r'const UI_SPEC: &str = r##"(.*?)"##;', source, re.S).group(1)
    assert json.loads(copy) == {key: value for key, value in SPEC.items() if key != "about"}


def test_views_are_the_web_tabs_and_the_terminal_views():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    tabs = re.findall(r'<button id="tab-(\w+)" role="tab"[^>]*>(.*?)</button>', page)
    labels = [(v["id"], v["labels"][0]) for v in SPEC["views"]]
    # the long label, and on a phone's bottom bar the spec's short one, as the terminal's narrow tab bar
    text = lambda part: html.unescape(re.sub(r"<[^>]+>", "", part))
    assert [(name, text(re.sub(r'<span class="narrow">.*?</span>', "", label))) for name, label in tabs] == labels
    assert [text(re.search(r'<span class="narrow">(.*?)</span>', label).group(1)) for _, label in tabs] == \
        [v["labels"][1] for v in SPEC["views"]]
    assert re.search(r'spec\(\)\["views"\]', TERMINAL)  # the terminal's views and labels are its copy's


def test_system_fields_are_the_web_rows_and_the_terminal_labels():
    """The system entries the frontends know by name; any other one is shown as "key value" on both."""
    fields = SPEC["system_fields"]
    rows = re.findall(r'id="system-(\w+)-row"', (WEB / "index.html").read_text(encoding="utf-8"))
    # model, platform and uptime are not system entries; version and service are the page's own (the terminal's System view too)
    assert rows == ["model", "platform", "uptime"] + fields + ["version", "service"]
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert json.loads(re.search(r"const SYSTEM_FIELDS = (\[.*?\]);", app).group(1).replace("'", '"')) == fields
    rows = re.search(r"const SYSTEM_ROWS: .*? = \[(.*?)\];", TERMINAL).group(1)
    assert re.findall(r'\("(\w+)", "', rows) == fields


def test_bar_scales_are_the_web_pages_and_the_terminals():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert json.loads(re.search(r"const WIFI_DBM = (\[.*?\]);", app).group(1)) == SPEC["scales"]["wifi_dbm"]
    assert 'spec()["scales"]["wifi_dbm"]' in TERMINAL


def test_level_defaults_are_the_servers_and_every_frontends():
    levels = SPEC["levels"]
    config = (ROOT / "native/src/config.rs").read_text(encoding="utf-8")
    server = {name: [float(x) for x in value.split(",")] if value else None
              for name, value in re.findall(r'\("(\w+)", "([^"]*)", Kind::Str\)',
                                            config[config.index('("thresholds"'):config.index("];", config.index('("thresholds"'))])}
    levels_fn = config[config.index("fn levels("):config.index("\n}", config.index("fn levels("))]  # the board table
    generic = [float(x) for x in re.search(r"_ => \[([^\]]+)\]", levels_fn).group(1).split(",")]
    assert {**server, "temperature": generic} == levels["defaults"]  # temperature unset: the board's, generic here
    assert re.findall(r'\("(\w+)", \(-?[\d.]+, -?[\d.]+, "[^"]*"\)\)', config) == levels["low_is_bad"]

    app = (WEB / "app.js").read_text(encoding="utf-8")
    web = re.search(r"const LIMITS = \{(.*?)\};", app, re.S).group(1)
    assert {name: [int(warn), int(crit)] for name, warn, crit in re.findall(r"(\w+): \[(-?\d+), (-?\d+)\]", web)} \
        == levels["defaults"]  # also what a null temperature from /api/status leaves (applyStatus takes lists only)

    # the terminal leaves temperature to the server (its board's level) and falls back to its copy's generic one
    assert 'spec()["levels"]["defaults"]' in TERMINAL and 'spec()["levels"]["low_is_bad"]' in TERMINAL


def test_low_is_bad_readings_get_their_level_from_the_spec():
    """A low-is-bad reading's level comes from one helper per frontend, which follows the spec's list: a
    reading added to low_is_bad turns low on both, and no call picks the low or the high rule by hand."""
    low = SPEC["levels"]["low_is_bad"]
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert json.loads(re.search(r"const LOW_IS_BAD = (\[.*?\]);", app).group(1).replace("'", '"')) == low
    assert not re.findall(r".{20}\blowLevel\(", app)  # only levelOf picks lowLevel
    # only level_of picks low_level (its definition and that one call; the levels are screen.rs's low_levels test)
    assert len(re.findall(r"\blow_level\(", TERMINAL)) == 2 and "if low(name) { low_level(" in TERMINAL
    # what else reads a low-is-bad reading's limits only draws its bar (the ticks), colored by the level given
    names = "|".join(low)
    for line in re.findall(rf".*LIMITS\.(?:{names})\b.*", app) + re.findall(rf'.*lim\.get\("(?:{names})"\).*', TERMINAL):
        assert re.search(r"\b(?:bar|meter)\(.*\blv\b", line), line


def test_roles_are_the_web_tokens_and_the_terminal_codes():
    light, dark = css_tokens()
    for role, value in SPEC["roles"].items():
        assert (light[role], dark[role]) == (value["light"], value["dark"]), role
    assert set(SPEC["levels"]["names"]) <= {role for role, value in SPEC["roles"].items() if "ansi" in value}


def test_states_color_the_status_the_same_way():
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    web = dict(re.findall(r'#status\[data-state="(\w+)"\] \{ color: var\(--(\w+)-text\)', css))
    assert web == SPEC["states"]
    assert 'let color = s(&spec()["states"][state]);' in TERMINAL
    assert 'role => spec()["roles"].get(role).and_then(|r| r.get("ansi"))' in TERMINAL  # the roles' ANSI codes


def test_brand_roles_are_the_approved_brand_colors():
    brand = json.loads((WEB / "assets/brand/brand-colors.json").read_text(encoding="utf-8"))["colors"]
    for theme in ("light", "dark"):
        assert {role: SPEC["roles"][role][theme] for role in ("ok", "fg", "bg", "muted")} == \
            {role: brand[theme][key].lower() for role, key in
             (("ok", "symbol"), ("fg", "wordmark"), ("bg", "background"), ("muted", "description"))}


def test_units_follow_a_space_everywhere():
    """A reading and its unit are two words in every frontend: 91.4 %, 51.2 °C, as 918 MHz and -70 dBm."""
    read = lambda path: (ROOT / path).read_text(encoding="utf-8")
    assert not re.findall(r".{20}(?:\+ '(?:%|°C)'|\}(?:%(?![\";)])|°C))", read("frontends/web/app.js"))  # not CSS lengths
    for rust in ("native/shared/src/render.rs", "native/frontend/src/screen.rs"):
        assert not re.findall(r".{20}\{\}(?:%|°C)", read(rust)), rust  # format!("{}%")


def observation(data, stale=False):
    return {"state": "ok", "stale_after_ms": 180000, "observation": {"id": 1, "data_age_ms": 1000, "stale": stale},
            "data": data}


def filesystems(*used_pct):
    return {"filesystems": [{"device": f"sd{n}", "fs_type": "ext4", "total_bytes": 100, "used_bytes": pct,
                             "available_bytes": 100 - pct, "mount_points": [{"path": f"/data{n}"}]}
                            for n, pct in enumerate(used_pct)]}


HOT = dict(FULL, cpu=[{"id": 0, "usage": 95.0, "freq": None}, {"id": 1, "usage": 89.0, "freq": None}],
           gpu={"usage": 81.0, "freq": None}, memory=dict(FULL["memory"], used=7 * 2**30),
           disk={"total": 100 * 2**30, "used": 50 * 2**30}, temperature={"cpu": 83.5, "gpu": 91.0, "soc": 40.0})
OBSERVED = {"schema_version": 2, "groups": {
    "storage": observation(filesystems(85, 95, 10)),
    "battery": observation({"batteries": [{"name": "BAT0", "present": True, "percent": 9},
                                          {"name": "BAT1", "present": False, "percent": 5}]}),
    "wifi": observation({"interfaces": [{"name": "wlan0", "connected": True, "signal_dbm": -75},
                                        {"name": "wlan1", "connected": False, "signal_dbm": -90}]})}}
STALE = {"schema_version": 2, "groups": {name: observation(g["data"], stale=True) for name, g in OBSERVED["groups"].items()}}


@pytest.mark.parametrize("stats, obs", [(HOT, OBSERVED), (HOT, STALE), (FULL, OBSERVED), (FULL, None)],
                         ids=["hot", "stale-observations", "observations-only", "nothing"])
def test_needs_attention_is_the_same_on_both(stats, obs, tmp_path):
    """The same snapshot raises the same entries, in the same order, on the web page and the terminal (the
    binary's --screen-script answers with its attention list)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    if not COMMAND.is_file():
        pytest.skip(f"needs the platmon command ({COMMAND}, native/build.sh)")
    levels = {"cpu": [80, 90], "gpu": [80, 90], "memory": [80, 90], "filesystem": [80, 90],
              "temperature": [85, 95], "battery": [20, 10], "wifi": [-70, -80]}
    case = tmp_path / "case.json"
    case.write_text(json.dumps({"stats": stats, "observations": obs, "levels": levels}), encoding="utf-8")
    web = json.loads(subprocess.run([node, str(ROOT / "tests/frontends/web_attention_harness.js"), str(WEB / "app.js"),
                                     str(case)], check=True, capture_output=True, text=True, encoding="utf-8").stdout)
    request = {"stats": stats, "obs": obs, "width": 119, "height": 40, "levels": levels,
               "status": {"state": "live", "age": 0, "error": None, "addr": "localhost:9797", "interval": 1}}
    answer = subprocess.run([str(COMMAND), "--screen-script"], input=json.dumps(request) + "\n", check=True,
                            capture_output=True, text=True, encoding="utf-8").stdout
    terminal = json.loads(answer)["attention"]
    assert [[html.unescape(name), value, lv] for name, value, lv in web] == terminal


def test_the_narrow_tab_bar_has_a_column_per_view():
    """On a phone the tab list is a fixed bottom bar: one column per view, or the last view wraps onto a
    second row under the bar (a 6-column grid held 7 views)."""
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    narrow = css[css.index("@media (max-width: 639px)"):]
    tabs = narrow[narrow.index(".tabs {"):narrow.index("}", narrow.index(".tabs {"))]
    assert int(re.search(r"grid-template-columns: repeat\((\d+),", tabs).group(1)) == len(SPEC["views"])


def test_every_scrolling_box_names_its_scrollbar_colors():
    """A box that scrolls draws its bar in theme colors: left to the browser, desktop Firefox drew the
    Network table's bar white in the dark theme."""
    css = re.sub(r"/\*.*?\*/", "", (WEB / "styles.css").read_text(encoding="utf-8"), flags=re.S)
    rules = [([s.strip() for s in sel.split(",")], body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]
    scrolls = {s for sels, body in rules if re.search(r"overflow(?:-[xy])?: (?:auto|scroll)", body) for s in sels}
    colored = {s for sels, body in rules if re.search(r"scrollbar-color: var\(--\w+\)", body) for s in sels}
    assert scrolls and scrolls <= colored, scrolls - colored


def js_object(app, name):
    """A constant object literal of app.js (keys bare, strings in single quotes) as Python data."""
    text = re.search(rf"const {name} = (\{{.*?\}});\n", app, re.S).group(1)
    return json.loads(re.sub(r"([{,]\s*)(\w+):", r'\1"\2":', text.replace("'", '"')))


def test_metric_and_column_labels_are_the_spec_on_both():
    """The Overview readings and the table columns are named once: the web page takes the long label, the
    terminal the short one, and neither writes a label of its own."""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert js_object(app, "METRICS") == {m["id"]: m["labels"] for m in SPEC["metrics"]}
    assert js_object(app, "COLUMNS") == SPEC["columns"]
    assert js_object(app, "FORMATS") == SPEC["formats"]
    for long, _ in SPEC["columns"].values():
        assert app.count(f"'{long}'") == 1, long  # in COLUMNS only
    body = TERMINAL[TERMINAL.index('"##;'):]  # after the UI_SPEC copy
    for _, short in SPEC["columns"].values():
        assert f'"{short}"' not in body, short
    assert 'spec()["metrics"]' in TERMINAL and 'spec()["columns"]' in TERMINAL  # the Overview's labels are the copy's


# what the terminal writes for these (screen.rs's numbers_read_the_same_on_both test holds the same lists)
NUMBERS = {
    "size": ([0, 1023, 1024, 512 * 1024, 14.3 * 2**20, 102.4 * 2**20, 2**30, 45.1 * 2**30, 57 * 2**30],
             ["0", "1023B", "1K", "512K", "14M", "102M", "1.0G", "45.1G", "57.0G"]),
    "per_sec": ([0, 0.4, 0.5, 1.5, 2.5, 912.4, 301.0], ["0", "0", "1", "2", "3", "912", "301"])}


@pytest.mark.parametrize("fn", sorted(NUMBERS))
def test_numbers_read_the_same_on_both(fn):
    """Sizes in the unit that fits them and per-second counts rounded the same way: 14M on both, never
    0.0G, and 0.5 packets/s is 1 on both (Python formats half to even, JavaScript half up)."""
    values, terminal = NUMBERS[fn]
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    js = (re.search(r"const FORMATS = .*?;\n", app).group(0) + re.search(r"const size = .*?\n\};\n", app, re.S).group(0)
          + "const per_sec = v => v.toFixed(FORMATS.per_s_decimals);\n"
          + f"console.log(JSON.stringify({json.dumps(values)}.map({fn})));")
    web = json.loads(subprocess.run([node, "-e", js], check=True, capture_output=True, text=True).stdout)
    assert web == terminal
    assert "toFixed(FORMATS.per_s_decimals)" in app
