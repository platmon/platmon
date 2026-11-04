"""frontends/ui-spec.json, the design tokens the frontends share: every adapter agrees with it, so the
terminal and the web page cannot drift apart (views, levels, state colors), and the brand roles agree with
the approved brand colors."""
import html
import json
import re
from pathlib import Path

from frontends import cli

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "frontends/ui-spec.json").read_text(encoding="utf-8"))
WEB = ROOT / "frontends/web"


def css_tokens():
    """styles.css custom properties: (light, dark), dark from the prefers-color-scheme block."""
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    dark_at = css.index("@media (prefers-color-scheme: dark)")
    tokens = lambda text: dict(re.findall(r"--([\w-]+):\s*([^;]+);", text))
    return tokens(css[:dark_at]), tokens(css[dark_at:css.index("}", css.index("{", dark_at) + 1) + 1])


def test_cli_copy_matches_the_spec():
    assert cli.UI_SPEC == {key: value for key, value in SPEC.items() if key != "about"}


def test_views_are_the_web_tabs_and_the_terminal_views():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    tabs = re.findall(r'<button id="tab-(\w+)" role="tab"[^>]*>(.*?)</button>', page)
    labels = [(v["id"], v["labels"][0]) for v in SPEC["views"]]
    assert [(name, html.unescape(re.sub(r"<[^>]+>", "", text))) for name, text in tabs] == labels
    assert list(cli.VIEWS) == [v["id"] for v in SPEC["views"]]
    assert {name: list(texts) for name, texts in cli.VIEW_LABELS.items()} == {v["id"]: v["labels"] for v in SPEC["views"]}


def test_level_defaults_are_the_servers_and_every_frontends():
    levels = SPEC["levels"]
    config = (ROOT / "native/src/config.rs").read_text(encoding="utf-8")
    server = {name: [float(x) for x in value.split(",")] if value else None
              for name, value in re.findall(r'\("(\w+)", "([^"]*)", Kind::Str\)',
                                            config[config.index('("thresholds"'):config.index("];", config.index('("thresholds"'))])}
    generic = [float(x) for x in re.search(r"_ => \[([^\]]+)\]", config).group(1).split(",")]
    assert {**server, "temperature": generic} == levels["defaults"]  # temperature unset: the board's, generic here
    assert re.findall(r'\("(\w+)", \(-?[\d.]+, -?[\d.]+, "[^"]*"\)\)', config) == levels["low_is_bad"]

    app = (WEB / "app.js").read_text(encoding="utf-8")
    web = re.search(r"const LIMITS = \{(.*?)\};", app, re.S).group(1)
    assert {name: [int(warn), int(crit)] for name, warn, crit in re.findall(r"(\w+): \[(-?\d+), (-?\d+)\]", web)} \
        == levels["defaults"]
    assert re.search(r"LIMITS\.temperature \|\|= \[(-?\d+), (-?\d+)\]", app).groups() == \
        tuple(str(x) for x in levels["defaults"]["temperature"])

    # the terminal leaves temperature to the server (its board's level) and falls back to the generic one
    assert {**cli.LIMITS, "temperature": cli.temp_limits()} == {k: tuple(v) for k, v in levels["defaults"].items()}
    assert list(cli.LOW) == levels["low_is_bad"]


def test_roles_are_the_web_tokens_and_the_terminal_codes():
    light, dark = css_tokens()
    for role, value in SPEC["roles"].items():
        assert (light[role], dark[role]) == (value["light"], value["dark"]), role
        if "ansi" in value:
            assert cli.Style.CODES[role] == value["ansi"], role
    assert set(SPEC["levels"]["names"]) <= {role for role, value in SPEC["roles"].items() if "ansi" in value}


def test_states_color_the_status_the_same_way():
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    web = dict(re.findall(r'#status\[data-state="(\w+)"\] \{ color: var\(--(\w+)-text\)', css))
    assert web == SPEC["states"]
    assert re.search(r"color = UI_SPEC\[\"states\"\]\[state\]", Path(cli.__file__).read_text(encoding="utf-8"))


def test_brand_roles_are_the_approved_brand_colors():
    brand = json.loads((WEB / "assets/brand/brand-colors.json").read_text(encoding="utf-8"))["colors"]
    for theme in ("light", "dark"):
        assert {role: SPEC["roles"][role][theme] for role in ("ok", "fg", "bg", "muted")} == \
            {role: brand[theme][key].lower() for role, key in
             (("ok", "symbol"), ("fg", "wordmark"), ("bg", "background"), ("muted", "description"))}


def test_units_follow_a_space_everywhere():
    """A reading and its unit are two words in every frontend: 91.4 %, 51.2 °C, as 918 MHz and -70 dBm."""
    read = lambda path: (ROOT / path).read_text(encoding="utf-8")
    assert not re.findall(r".{20}\}(?:%|°C)", read("frontends/cli.py"))  # f"{v}%"
    assert not re.findall(r".{20}(?:\+ '(?:%|°C)'|\}(?:%(?![\";)])|°C))", read("frontends/web/app.js"))  # not CSS lengths
    assert not re.findall(r".{20}\{\}(?:%|°C)", read("native/src/render.rs"))  # format!("{}%")
