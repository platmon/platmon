//! The live terminal screen, frontends/cli.py's screen() ported (cli.py is in the git history), and what it
//! draws with: the same data, wording and levels as the web page, one view at a time. Pure: lines of text
//! from a snapshot, a width and a height; the terminal itself is term.rs's. Where cli.py would raise on a
//! malformed answer (a key missing, None formatted as a number), this draws what it can instead (the plain
//! screen, render.rs, still refuses).
use crate::json::{parse, Value};
use crate::render::{collection_note, cpu_note, f, group, num, pad_right, per_s, rate_reason, s, truthy};
use std::collections::HashMap;
use std::sync::OnceLock;

/// Copy of frontends/ui-spec.json without "about" (the design tokens shared with the web page), as cli.py
/// keeps one: the build has only native/ and VERSION (build.sh, the Dockerfile), so the file itself cannot
/// be included. tests/frontends/test_ui_spec.py checks this copy against the file.
const UI_SPEC: &str = r##"
{
  "views": [
    {"id": "overview", "labels": ["Overview", "Over", "Ovr", "Ov"]},
    {"id": "resources", "labels": ["Resources", "Res", "Res", "Re"]},
    {"id": "network", "labels": ["Network", "Net", "Net", "Ne"]},
    {"id": "storage", "labels": ["Storage", "Store", "Sto", "St"]},
    {"id": "thermal", "labels": ["Temperature", "Temp", "Tmp", "Tm"]},
    {"id": "power", "labels": ["Power & battery", "Power", "Pwr", "Pw"]},
    {"id": "system", "labels": ["System", "Sys", "Sys", "Sy"]}
  ],
  "metrics": [
    {"id": "cpu", "labels": ["CPU", "CPU"], "unit": "%"},
    {"id": "memory", "labels": ["RAM", "RAM"], "unit": "%"},
    {"id": "gpu", "labels": ["GPU", "GPU"], "unit": "%"},
    {"id": "filesystem", "labels": ["Root filesystem", "DISK"], "unit": "%"},
    {"id": "temperature", "labels": ["Temperature", "TEMP"], "unit": "°C"}
  ],
  "columns": {
    "rx": ["↓ Receive", "↓ RX"], "tx": ["↑ Send", "↑ TX"],
    "packets": ["Packets/s rx · tx", "PKT/S rx · tx"], "errors": ["Errors · drops", "ERR · DROP"],
    "read": ["Read", "READ"], "write": ["Write", "WRITE"], "ops": ["Ops/s r · w", "OPS/S r · w"], "io_time": ["I/O time", "I/O TIME"]
  },
  "formats": {"per_s_decimals": 0, "size_units": ["G", "M", "K"]},
  "system_fields": ["os", "kernel", "arch", "hostname", "l4t"],
  "levels": {
    "names": ["ok", "warn", "crit"],
    "low_is_bad": ["battery", "wifi"],
    "defaults": {"cpu": [80, 90], "gpu": [80, 90], "memory": [80, 90], "filesystem": [80, 90],
                 "temperature": [80, 90], "battery": [20, 10], "wifi": [-70, -80]}
  },
  "scales": {"wifi_dbm": [-90, -30]},
  "states": {"live": "ok", "partial": "warn", "offline": "crit"},
  "roles": {
    "ok": {"light": "#2f7d5b", "dark": "#4fb286", "ansi": "32"},
    "warn": {"light": "#d9a21b", "dark": "#e3b341", "ansi": "33"},
    "crit": {"light": "#c62828", "dark": "#f47067", "ansi": "31"},
    "muted": {"light": "#6b7280", "dark": "#9aa1ab", "ansi": "90"},
    "fg": {"light": "#1d2127", "dark": "#e6e8eb"},
    "bg": {"light": "#f6f7f9", "dark": "#121417"}
  }
}
"##;

fn spec() -> &'static Value {
    static SPEC: OnceLock<Value> = OnceLock::new();
    SPEC.get_or_init(|| parse(UI_SPEC).expect("UI_SPEC is JSON"))
}

fn strs(v: &Value) -> Vec<String> {
    match v { Value::Arr(a) => a.iter().map(s).collect(), _ => vec![] }
}

fn pair(v: &Value) -> Option<(f64, f64)> {
    match v { Value::Arr(a) if a.len() == 2 => Some((num(&a[0]).ok()?, num(&a[1]).ok()?)), _ => None }
}

/// VIEWS: the views' ids in the tab order.
pub fn views() -> Vec<String> {
    match &spec()["views"] { Value::Arr(a) => a.iter().map(|v| s(&v["id"])).collect(), _ => vec![] }
}

fn view_labels(view: &str) -> Vec<String> {
    match &spec()["views"] { Value::Arr(a) => a.iter().find(|v| s(&v["id"]) == view).map_or(vec![], |v| strs(&v["labels"])), _ => vec![] }
}

/// LABEL: the Overview readings' short labels.
fn label(id: &str) -> String {
    match &spec()["metrics"] { Value::Arr(a) => a.iter().find(|m| s(&m["id"]) == id).map_or(String::new(), |m| short(&m["labels"])), _ => String::new() }
}

fn short(labels: &Value) -> String {
    labels.get_index(1).map_or(String::new(), s)
}

/// COL: the table columns' short labels.
fn col(key: &str) -> String {
    short(&spec()["columns"][key])
}

fn low(name: &str) -> bool {
    strs(&spec()["levels"]["low_is_bad"]).iter().any(|n| n == name)
}

/// LIMITS: the shared defaults, temperature left to the server (temp_limits() falls back to the generic one).
#[derive(Clone)]
pub struct Limits(pub Vec<(String, Option<(f64, f64)>)>);

impl Limits {
    pub fn new() -> Limits {
        let Value::Obj(d) = &spec()["levels"]["defaults"] else { return Limits(vec![]) };
        Limits(d.iter().map(|(k, v)| (k.clone(), if k == "temperature" { None } else { pair(v) })).collect())
    }

    pub fn get(&self, name: &str) -> Option<(f64, f64)> {
        self.0.iter().find(|(k, _)| k == name).and_then(|(_, v)| *v)
    }

    /// use_levels(): the server's levels ({name: [warning, critical]}, temperature may be null); anything
    /// else in them, or a server without them, keeps the defaults.
    pub fn use_levels(&mut self, levels: Option<&Value>) {
        let Some(Value::Obj(items)) = levels else { return };
        for (name, value) in items {
            let Some(slot) = self.0.iter_mut().find(|(k, _)| k == name) else { continue };
            if value.is_null() && name == "temperature" {
                slot.1 = None;
            } else if let Value::Arr(a) = value {
                let nums: Vec<f64> = a.iter().filter_map(|v| num(v).ok()).collect();
                if a.len() == 2 && nums.len() == 2 && if low(name) { nums[0] > nums[1] } else { nums[0] < nums[1] } {
                    slot.1 = Some((nums[0], nums[1]));
                }
            }
        }
    }

    /// temp_limits(): the server's temperature level; the web page's generic default without one.
    fn temp(&self) -> Option<(f64, f64)> {
        self.get("temperature").or_else(|| pair(&spec()["levels"]["defaults"]["temperature"]))
    }
}

const MAX_ROWS: usize = 12;
const WIDE: usize = 100;
const NARROW: usize = 60;
pub const BOTTOM: i64 = 1 << 30;

/// Python's format(x, "g"): six significant digits, trailing zeros dropped.
pub fn g(x: f64) -> String {
    if x == 0.0 { return "0".into() }
    if !x.is_finite() { return f(x, 0) }
    let e = format!("{:.5e}", x);
    let (_, exp) = e.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let trim = |t: String| if t.contains('.') { t.trim_end_matches('0').trim_end_matches('.').to_string() } else { t };
    if (-4..6).contains(&exp) {
        trim(format!("{:.*}", (5 - exp) as usize, x))
    } else {
        let (m, _) = e.split_once('e').unwrap();
        format!("{}e{}{:02}", trim(m.to_string()), if exp < 0 { '-' } else { '+' }, exp.abs())
    }
}

/// t right-aligned in w columns, as Python's format(t, ">w") (pad_right is render.rs's).
fn pad_l(t: &str, w: usize) -> String {
    let n = t.chars().count();
    if n >= w { t.to_string() } else { " ".repeat(w - n) + t }
}

fn chars(t: &str, n: usize) -> String {
    t.chars().take(n).collect()
}

fn isnum(v: &Value) -> bool {
    num(v).is_ok()
}

fn n(v: &Value) -> f64 {
    num(v).unwrap_or(0.0)
}

fn t(v: Option<&Value>) -> bool {
    truthy(v)
}

/// The ANSI escapes' spans: ESC [ [0-9;?]* [A-Za-z], cli.py's ANSI.
fn escape_at(b: &[u8], i: usize) -> Option<usize> {
    if b.get(i) != Some(&0x1b) || b.get(i + 1) != Some(&b'[') { return None }
    let mut j = i + 2;
    while j < b.len() && (b[j].is_ascii_digit() || b[j] == b';' || b[j] == b'?') { j += 1 }
    (j < b.len() && b[j].is_ascii_alphabetic()).then_some(j + 1)
}

pub fn strip(t: &str) -> String {
    let (b, mut out, mut i, mut start) = (t.as_bytes(), String::new(), 0, 0);
    while i < b.len() {
        if let Some(end) = escape_at(b, i) {
            out.push_str(&t[start..i]);
            i = end;
            start = end;
        } else {
            i += 1;
        }
    }
    out + &t[start..]
}

pub fn vlen(t: &str) -> usize {
    strip(t).chars().count()
}

fn pad(text: &str, width: usize, right: bool) -> String {
    let gap = " ".repeat(width.saturating_sub(vlen(text)));
    if right { gap + text } else { text.to_string() + &gap }
}

/// line cut to width visible columns; escapes are kept, colors reset after a cut.
pub fn fit(line: &str, width: usize) -> String {
    if vlen(line) <= width { return line.to_string() }
    let (b, mut out, mut shown, mut i, mut start) = (line.as_bytes(), String::new(), 0, 0, 0);
    let text = |part: &str, out: &mut String, shown: &mut usize| {
        if *shown < width {
            let cut = chars(part, width - *shown);
            *shown += cut.chars().count();
            out.push_str(&cut);
        }
    };
    while i < b.len() {
        if let Some(end) = escape_at(b, i) {
            text(&line[start..i], &mut out, &mut shown);
            out.push_str(&line[i..end]);
            i = end;
            start = end;
        } else {
            i += 1;
        }
    }
    text(&line[start..], &mut out, &mut shown);
    out + if line.contains('\x1b') { "\x1b[0m" } else { "" }
}

/// Basic ANSI colors (when color is on) and the glyphs: block characters, or ASCII without UTF-8.
pub struct Style {
    pub color: bool,
    full: &'static str, track: &'static str, tick: &'static str, rule: &'static str, dot: &'static str,
    cross: &'static str, up: &'static str, larr: &'static str, rarr: &'static str, uarr: &'static str,
    darr: &'static str, ell: &'static str, ge: &'static str,
}

impl Style {
    pub fn new(color: bool, utf8: bool) -> Style {
        let g = if utf8 { ["█", "░", "╎", "─", "●", "✕", "▲", "←", "→", "↑", "↓", "…", "≥"] }
                else { ["#", "-", "|", "-", "*", "x", "!", "<", ">", "^", "v", "...", ">="] };
        Style { color, full: g[0], track: g[1], tick: g[2], rule: g[3], dot: g[4], cross: g[5], up: g[6], larr: g[7],
                rarr: g[8], uarr: g[9], darr: g[10], ell: g[11], ge: g[12] }
    }

    fn code(name: &str) -> Option<String> {
        match name {
            "dim" => Some("2".into()), "bold" => Some("1".into()), "under" => Some("4".into()), "crit_bg" => Some("30;41".into()),
            role => spec()["roles"].get(role).and_then(|r| r.get("ansi")).map(s),
        }
    }

    /// st(text, *names): names that are "" are none (cli.py's None).
    fn p(&self, text: &str, names: &[&str]) -> String {
        let codes: Vec<String> = names.iter().filter(|n| !n.is_empty()).filter_map(|n| Style::code(n)).collect();
        if self.color && !codes.is_empty() && !text.is_empty() { format!("\x1b[{}m{}\x1b[0m", codes.join(";"), text) } else { text.to_string() }
    }
}

/// make_style(): NO_COLOR: no colors; ASCII glyphs without UTF-8, or with PLATMON_ASCII=1.
#[cfg(unix)] // the live view's, which Windows does not have
pub fn make_style(no_color: Option<&str>, ascii: Option<&str>, utf8: bool) -> Style {
    Style::new(no_color.map_or(true, str::is_empty), utf8 && ascii != Some("1"))
}

fn clip(st: &Style, line: &str, width: usize) -> String {
    if vlen(line) <= width { line.to_string() } else { fit(line, width.saturating_sub(st.ell.chars().count())) + &st.p(st.ell, &["muted"]) }
}

fn level(v: f64, limits: Option<(f64, f64)>) -> &'static str {
    match limits { None => "ok", Some((w, c)) => if v >= c { "crit" } else if v >= w { "warn" } else { "ok" } }
}

fn low_level(v: f64, limits: Option<(f64, f64)>) -> &'static str {
    match limits { None => "ok", Some((w, c)) => if v <= c { "crit" } else if v <= w { "warn" } else { "ok" } }
}

fn level_of(lim: &Limits, name: &str, v: f64) -> &'static str {
    if low(name) { low_level(v, lim.get(name)) } else { level(v, lim.get(name)) }
}

/// Text color for a level: ok text stays the terminal's own color ("").
fn tone(lv: &str) -> &str {
    if lv == "ok" { "" } else { lv }
}

fn or<'a>(a: &'a str, b: &'a str) -> &'a str {
    if a.is_empty() { b } else { a }
}

fn reading(st: &Style, v: f64, unit: &str, lv: &str) -> String {
    st.p(&format!("{:5.1} {}", v, pad_right(unit, 2)), &["bold", tone(lv)])
}

fn spread(left: &str, right: &str, width: usize) -> String {
    let gap = (width as i64 - vlen(left) as i64 - vlen(right) as i64).max(1) as usize;
    format!("{}{}{}", left, " ".repeat(gap), right)
}

fn meter(st: &Style, pct: f64, width: usize, limits: Option<(f64, f64)>, ticks: bool, lv: Option<&str>) -> String {
    let pct = pct.min(100.0).max(0.0);
    let filled = (width as f64 * pct / 100.0).round_ties_even() as usize;
    let marks: Vec<i64> = match limits {
        Some((a, b)) if ticks => [a, b].iter().map(|t| (width as i64 - 1).min((width as f64 * t / 100.0).trunc() as i64)).collect(),
        _ => vec![],
    };
    let track: String = (filled..width).map(|i| if marks.contains(&(i as i64)) { st.tick } else { st.track }).collect();
    let lv = lv.unwrap_or_else(|| if limits.is_some() { level(pct, limits) } else { "ok" });
    st.p(&st.full.repeat(filled), &[or(tone(lv), "muted")]) + &st.p(&track, &["muted", "dim"])
}

/// per_sec(): packets or operations per second as the web page writes them, half up.
fn per_sec(v: f64) -> String {
    let d = n(&spec()["formats"]["per_s_decimals"]) as i32;
    let p = 10f64.powi(d);
    f((v * p + 0.5).floor() / p, d as usize)
}

/// size(): bytes in the unit that fits them, as the web page writes them: 45.1G, 14M, 512K.
fn size(b: f64) -> String {
    for (unit, power) in strs(&spec()["formats"]["size_units"]).iter().zip([30, 20, 10]) {
        let scale = 2f64.powi(power);
        if b >= scale {
            return format!("{}{}", f(b / scale, if power == 30 { 1 } else { 0 }), unit);
        }
    }
    if b != 0.0 { format!("{}B", f(b, 0)) } else { "0".into() }
}

fn uptime_text(seconds: &Value) -> String {
    let up = n(seconds).trunc() as i64;
    format!("{}d {:02}:{:02}", up.div_euclid(86400), up.rem_euclid(86400) / 3600, up.rem_euclid(3600) / 60)
}

fn mhz(hz: Option<&Value>) -> String {
    if t(hz) { format!("{} MHz", f(n(hz.unwrap()) / 1e6, 0)) } else { String::new() }
}

/// Python's x[k] or {}: the member when it is a dict.
fn dict<'a>(v: Option<&'a Value>) -> &'a Value {
    static EMPTY: Value = Value::Obj(Vec::new());
    match v { Some(d @ Value::Obj(_)) => d, _ => &EMPTY }
}

fn list(v: Option<&Value>) -> &[Value] {
    match v { Some(Value::Arr(a)) => a, _ => &[] }
}

fn items(v: Option<&Value>) -> &[(String, Value)] {
    match v { Some(Value::Obj(o)) => o, _ => &[] }
}

fn observed_text(o: Option<&Value>) -> String {
    match o.and_then(|o| o.get("data_age_ms")) {
        Some(age) if isnum(age) => format!("observed {} s ago{}", f(n(age) / 1000.0, 0),
                                           if t(o.unwrap().get("stale")) { " (not current)" } else { "" }),
        _ => String::new(),
    }
}

fn stale_text(o: Option<&Value>) -> String {
    if o.is_some_and(|o| t(Some(o)) && t(o.get("stale"))) { observed_text(o) } else { String::new() }
}

/// Python's x + y for two counters: ints stay ints.
fn add(a: &Value, b: &Value) -> String {
    match (a, b) {
        (Value::Int(x), Value::Int(y)) => (x + y).to_string(),
        _ => s(&Value::Float(n(a) + n(b))),
    }
}

/// textwrap.wrap(text, width) with its defaults: words split at whitespace and after the hyphens of
/// hyphenated words, lines filled greedily, a word longer than a line broken.
// ponytail: em-dash ("--") chunks are not split as textwrap does; nothing on the screen has one
fn wrap(text: &str, width: usize) -> Vec<String> {
    let c: Vec<char> = text.chars().map(|c| if c.is_whitespace() { ' ' } else { c }).collect();
    let letter = |i: usize| c.get(i).is_some_and(|c| c.is_alphabetic() || *c == '_');
    let mut chunks: Vec<String> = Vec::new();
    let mut i = 0;
    while i < c.len() {
        let start = i;
        if c[i] == ' ' {
            while i < c.len() && c[i] == ' ' { i += 1 }
        } else {
            while i < c.len() && c[i] != ' ' {
                i += 1;
                let h = i - 1;
                if c[h] == '-' && h > start && (h >= 2 && letter(h - 1) && letter(h - 2) || h >= 3 && letter(h - 1) && c[h - 2] == '-' && letter(h - 3))
                    && letter(h + 1) && (letter(h + 2) || c.get(h + 2) == Some(&'-') && letter(h + 3)) {
                    break;
                }
            }
        }
        chunks.push(c[start..i].iter().collect());
    }
    chunks.reverse();
    let len = |x: &String| x.chars().count();
    let mut lines: Vec<String> = Vec::new();
    while !chunks.is_empty() {
        let (mut cur, mut cur_len): (Vec<String>, usize) = (vec![], 0);
        if chunks.last().unwrap().trim().is_empty() && !lines.is_empty() {
            chunks.pop();
        }
        while let Some(last) = chunks.last() {
            if cur_len + len(last) <= width { cur_len += len(last); cur.push(chunks.pop().unwrap()) } else { break }
        }
        if chunks.last().is_some_and(|l| len(l) > width) {
            let space = if width < 1 { 1 } else { width - cur_len };
            let chunk: Vec<char> = chunks.last().unwrap().chars().collect();
            let mut end = space;
            if chunk.len() > space {
                if let Some(h) = chunk[..space].iter().rposition(|x| *x == '-') {
                    if h > 0 && chunk[..h].iter().any(|x| *x != '-') { end = h + 1 }
                }
            }
            cur.push(chunk[..end].iter().collect());
            *chunks.last_mut().unwrap() = chunk[end..].iter().collect();
            if chunks.last().unwrap().is_empty() { chunks.pop(); }
        }
        if cur.last().is_some_and(|l| l.trim().is_empty()) {
            cur.pop();
        }
        if !cur.is_empty() {
            lines.push(cur.concat());
        }
    }
    lines
}

pub struct Metric {
    pub label: String,
    pub value: Option<f64>,
    pub unit: &'static str,
    limits: Option<(f64, f64)>,
    detail: String,
}

fn hottest_first(st: &Value) -> Vec<(String, f64)> {
    let mut temps: Vec<(String, f64)> = items(st.get("temperature")).iter().filter(|(_, v)| isnum(v)).map(|(k, v)| (k.clone(), n(v))).collect();
    temps.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    temps
}

/// The web Overview's readings.
pub fn metrics(st: &Value, lim: &Limits) -> Vec<Metric> {
    let cpus: Vec<f64> = list(st.get("cpu")).iter().filter(|c| c.get("usage").is_some_and(isnum)).map(|c| n(&c["usage"])).collect();
    let (m, d, gpu, npu) = (&st["memory"], &st["disk"], st.get("gpu"), st.get("npu"));
    let pct = |used: &Value, total: &Value| t(Some(total)).then(|| 100.0 * n(used) / n(total));
    let usage = |x: &Value| x.get("usage").filter(|u| isnum(u)).map(n);
    let mut out = vec![
        Metric { label: label("cpu"), value: (!cpus.is_empty()).then(|| cpus.iter().sum::<f64>() / cpus.len() as f64), unit: "%",
                 limits: lim.get("cpu"), detail: format!("mean of {} cores", cpus.len()) },
        Metric { label: label("memory"), value: pct(&m["used"], &m["total"]), unit: "%", limits: lim.get("memory"),
                 detail: format!("{} / {}", size(n(&m["used"])), size(n(&m["total"]))) }];
    if t(gpu) {
        let g = gpu.unwrap();
        out.push(Metric { label: label("gpu"), value: usage(g), unit: "%", limits: lim.get("gpu"), detail: mhz(g.get("freq")) });
    }
    if t(npu) {
        let x = npu.unwrap();
        out.push(Metric { label: "NPU".into(), value: usage(x), unit: "%", limits: None, detail: mhz(x.get("freq")) });
    }
    out.push(Metric { label: label("filesystem"), value: pct(&d["used"], &d["total"]), unit: "%", limits: lim.get("filesystem"),
                      detail: format!("{} / {}", size(n(&d["used"])), size(n(&d["total"]))) });
    let temps = hottest_first(st);
    if let Some((name, v)) = temps.first() {
        out.push(Metric { label: label("temperature"), value: Some(*v), unit: "°C", limits: lim.temp(),
                          detail: format!("{} · hottest of {}", name, temps.len()) });
    }
    out
}

fn fs_pct(fs: &Value) -> Option<f64> {
    t(fs.get("total_bytes")).then(|| 100.0 * n(&fs["used_bytes"]) / n(&fs["total_bytes"]))
}

/// (data, observation) of a current (not stale) observation group.
fn current<'a>(obs: Option<&'a Value>, name: &str) -> Option<&'a Value> {
    group(obs, name).filter(|(_, o)| t(Some(o)) && !t(o.get("stale"))).map(|(d, _)| d)
}

/// attention(s, obs, views=True): (name, value, level, view) of the readings at their warning level or
/// worse, critical first.
pub fn attention(st: &Value, obs: Option<&Value>, lim: &Limits) -> Vec<(String, String, &'static str, &'static str)> {
    let disk = label("filesystem");
    let mut out = Vec::new();
    for m in metrics(st, lim) {
        let Some(v) = m.value else { continue };
        if m.label == label("temperature") { continue }
        let view = if m.label == disk { "storage" } else { "resources" };
        out.push((if m.label == disk { "/".to_string() } else { m.label.clone() }, format!("{} {}", f(v, 1), m.unit), level(v, m.limits), view));
    }
    for (k, v) in hottest_first(st) {
        out.push((k, format!("{} °C", f(v, 1)), level(v, lim.temp()), "thermal"));
    }
    for fs in current(obs, "storage").map_or(&[][..], |d| list(d.get("filesystems").filter(|v| t(Some(v))))) {
        let mps = list(fs.get("mount_points"));
        if let Some(pct) = fs_pct(fs) {
            if !mps.iter().any(|p| matches!(p.get("path"), Some(Value::Str(x)) if x == "/")) {
                out.push((s(&mps.first().unwrap_or(&Value::Null)["path"]), format!("{} %", f(pct, 0)), level(pct, lim.get("filesystem")), "storage"));
            }
        }
    }
    for b in current(obs, "battery").map_or(&[][..], |d| list(d.get("batteries").filter(|v| t(Some(v))))) {
        if b.get("present") != Some(&Value::Bool(false)) && b.get("percent").is_some_and(isnum) {
            out.push((s(&b["name"]), format!("{} %", s(&b["percent"])), level_of(lim, "battery", n(&b["percent"])), "power"));
        }
    }
    for i in current(obs, "wifi").map_or(&[][..], |d| list(d.get("interfaces").filter(|v| t(Some(v))))) {
        if i.get("connected") == Some(&Value::Bool(true)) && i.get("signal_dbm").is_some_and(isnum) {
            out.push((s(&i["name"]), format!("{} dBm", s(&i["signal_dbm"])), level_of(lim, "wifi", n(&i["signal_dbm"])), "network"));
        }
    }
    out.retain(|a| a.2 != "ok");
    out.sort_by_key(|a| a.2 != "crit");
    out
}

fn busiest_first<'a>(interfaces: &'a [Value], traffic: &HashMap<String, f64>) -> Vec<&'a Value> {
    let busy = |i: &Value| -> f64 {
        let r = i.get("rates");
        if !t(r) { return 1.0 }
        let r = r.unwrap();
        -(traffic.get(&s(&i["name"])).copied().unwrap_or_else(|| n(&r["rx_bytes_per_s"]) + n(&r["tx_bytes_per_s"])))
    };
    let mut out: Vec<&Value> = interfaces.iter().collect();
    out.sort_by(|a, b| busy(a).partial_cmp(&busy(b)).unwrap_or(std::cmp::Ordering::Equal));
    out
}

/// next_view(): 1-7 directly, Tab or the right arrow the next, Shift-Tab or the left arrow the previous.
pub fn next_view(keys: &str, view: &str) -> String {
    let all = views();
    let index = all.iter().position(|v| v == view).unwrap_or(0);
    if !keys.contains('\x1b') {
        for (number, name) in all.iter().enumerate() {
            if keys.contains(&(number + 1).to_string()) { return name.clone() }
        }
    }
    let (b, mut finals, mut i) = (keys.as_bytes(), Vec::new(), 0);
    while i < b.len() {
        match escape_at(b, i) { Some(end) => { finals.push(b[end - 1]); i = end } None => i += 1 }
    }
    if finals.contains(&b'Z') || finals.contains(&b'D') {
        return all[(index + all.len() - 1) % all.len()].clone();
    }
    if finals.contains(&b'C') || keys.contains('\t') {
        return all[(index + 1) % all.len()].clone();
    }
    view.to_string()
}

/// next_offset(): the up and down arrows a line, PgUp and PgDn a page, Home and End its two ends.
pub fn next_offset(keys: &str, offset: i64, page: i64) -> i64 {
    let (b, mut offset, mut i) = (keys.as_bytes(), offset, 0);
    while i < b.len() {
        if b[i] != 0x1b { i += 1; continue }
        let (params, key, end) = if b.get(i + 1) == Some(&b'[') {
            let mut j = i + 2;
            while j < b.len() && (b[j].is_ascii_digit() || b[j] == b';' || b[j] == b'?') { j += 1 }
            match b.get(j) { Some(&k) if k.is_ascii_alphabetic() || k == b'~' => (&keys[i + 2..j], k, j + 1), _ => { i += 1; continue } }
        } else if b.get(i + 1) == Some(&b'O') && b.get(i + 2).is_some_and(u8::is_ascii_alphabetic) {
            ("", b[i + 2], i + 3)
        } else {
            i += 1;
            continue;
        };
        match (key, params) {
            (b'A', _) => offset -= 1,
            (b'B', _) => offset += 1,
            (b'~', "5") => offset -= page,
            (b'~', "6") => offset += page,
            (b'H' | b'h', _) | (b'~', "1") => offset = 0,
            (b'F' | b'f', _) | (b'~', "4") => offset = BOTTOM,
            _ => {}
        }
        i = end;
    }
    offset.max(0)
}

pub fn tabbar(st: &Style, width: usize, view: &str, marks: &[(&'static str, &'static str)]) -> String {
    let all = views();
    let mark = |name: &str| marks.iter().find(|(v, _)| *v == name).map(|(_, lv)| *lv);
    for (size, gap) in ["   ", "   ", " ", " "].iter().enumerate() {
        let cells: Vec<String> = all.iter().enumerate().map(|(i, name)| {
            let text = format!("{}{} {}", if mark(name).is_some() { st.up } else { "" }, i + 1, view_labels(name)[size]);
            if name == view {
                st.p(&if st.color { text } else { format!("[{}]", text) }, &["ok", "bold", "under"])
            } else if let Some(lv) = mark(name) {
                st.p(&text, &[lv, "bold"])
            } else {
                st.p(&text, &["muted"])
            }
        }).collect();
        let line = format!("  {}", cells.join(gap));
        if vlen(&line) <= width { return line }
    }
    let index = all.iter().position(|v| v == view).unwrap_or(0);
    st.p(&format!("  {}/{} {} {}/{}", index + 1, all.len(), view_labels(view)[2], st.larr, st.rarr), &["ok", "bold"])
}

fn core_cell(st: &Style, c: &Value, clock: bool, lim: &Limits) -> String {
    let usage = n(&c["usage"]);
    let lv = tone(level(usage, lim.get("cpu")));
    let bold = if lv.is_empty() { "" } else { "bold" };
    let freq = if t(c.get("freq")) { pad_l(&f(n(&c["freq"]) / 1e6, 0), 4) } else { "    ".into() };
    st.p(&format!("{} ", pad_l(&s(&c["id"]), 4)), &[or(lv, "muted"), bold]) + &meter(st, usage, 8, lim.get("cpu"), false, None)
        + &st.p(&format!(" {:5.1} %", usage), &[lv, bold]) + &if clock { st.p(&format!(" {}", freq), &["muted"]) } else { String::new() }
}

fn detail_widths(width: usize, items: &[&Value], columns: &[usize]) -> Vec<usize> {
    let names = 4 + items.iter().map(|i| s(&i["name"]).chars().count()).chain([6]).max().unwrap() + 2;
    let mut columns = columns.to_vec();
    while columns.len() > 1 && names + columns.iter().sum::<usize>() > width {
        columns.pop();
    }
    let first = (width as i64 - columns.iter().sum::<usize>() as i64).min(names.max(28) as i64).max(8) as usize;
    [vec![first], columns].concat()
}

fn joined(width: usize, parts: &[String]) -> String {
    let mut line = String::new();
    for part in parts {
        if vlen(&line) + vlen(part) > width { break }
        line += part;
    }
    line
}

/// status: state (live, partial, offline), age (seconds), error (explain() text or None), addr, interval,
/// traffic ({interface: recent rx + tx}) for the Overview's busiest interfaces.
pub struct Status {
    pub state: String,
    pub age: f64,
    pub error: Option<String>,
    pub addr: String,
    pub interval: f64,
    pub traffic: HashMap<String, f64>,
}

struct View<'a> {
    s: &'a Value,
    obs: Option<&'a Value>,
    width: usize,
    st: &'a Style,
    status: &'a Status,
    lim: &'a Limits,
    wide: bool,
    bar: usize,
}

impl View<'_> {
    fn title(&self, name: &str, right: &str) -> String {
        let left = self.st.p(&format!("  {}", name), &["muted", "bold"]);
        if right.is_empty() || name.chars().count() + right.chars().count() + 5 > self.width { return left }
        spread(&left, &self.st.p(&format!("{} ", right), &["muted"]), self.width)
    }

    fn row(&self, widths: &[usize], cells: &[String], tones: &[&str]) -> String {
        cells.iter().zip(widths).zip(tones).enumerate().map(|(i, ((c, w), tn))| {
            let names: Vec<&str> = tn.split_whitespace().collect();
            self.st.p(&pad(&chars(c, *w), *w, i > 0), &names)
        }).collect()
    }

    fn no_rate(&self, widths: &[usize], name: &str, item: &Value) -> String {
        self.st.p(&(pad(&format!("    {}", name), widths[0], false) + &rate_reason(item.get("reason").unwrap_or(&Value::Null))), &["muted"])
    }

    fn net_row(&self, widths: &[usize], i: &Value) -> String {
        let r = i.get("rates");
        if !t(r) { return self.no_rate(widths, &s(&i["name"]), i) }
        let r = r.unwrap();
        let get = |side: &str, k: &str| dict(i.get(side)).get(k).cloned().unwrap_or(Value::Int(0));
        let (err, drop) = (add(&get("rx", "errors"), &get("tx", "errors")), add(&get("rx", "dropped"), &get("tx", "dropped")));
        let idle = !t(r.get("rx_bytes_per_s")) && !t(r.get("tx_bytes_per_s"));
        let bad = n(&get("rx", "errors")) + n(&get("tx", "errors")) != 0.0 || n(&get("rx", "dropped")) + n(&get("tx", "dropped")) != 0.0;
        self.row(widths, &[format!("    {}", s(&i["name"])), per_s(n(&r["rx_bytes_per_s"])), per_s(n(&r["tx_bytes_per_s"])),
                           format!("{} · {}", per_sec(n(&r["rx_packets_per_s"])), per_sec(n(&r["tx_packets_per_s"]))), format!("{} · {}", err, drop)],
                 &if idle { ["muted"; 5] } else { ["", "", "", "muted", if bad { "warn" } else { "muted" }] })
    }

    fn disk_rows(&self, widths: &[usize], disks: &[Value]) -> Vec<String> {
        let mut out = Vec::new();
        for d in disks.iter().take(MAX_ROWS) {
            let r = d.get("rates");
            if !t(r) {
                out.push(self.no_rate(widths, &s(&d["name"]), d));
                continue;
            }
            let r = r.unwrap();
            let idle = !t(r.get("read_bytes_per_s")) && !t(r.get("write_bytes_per_s"));
            out.push(self.row(widths, &[format!("    {}", s(&d["name"])), per_s(n(&r["read_bytes_per_s"])), per_s(n(&r["write_bytes_per_s"])),
                                        format!("{} · {}", per_sec(n(&r["reads_per_s"])), per_sec(n(&r["writes_per_s"]))),
                                        format!("{} %", f(100.0 * n(&r["io_time_ratio"]), 1))],
                              &if idle { ["muted"; 5] } else { ["", "", "", "muted", ""] }));
        }
        if disks.len() > MAX_ROWS {
            out.push(self.st.p(&format!("    +{} more disks (all in /api/stats)", disks.len() - MAX_ROWS), &["muted"]));
        }
        out
    }

    #[allow(clippy::too_many_arguments)]
    fn meter_row(&self, label: &str, pct: Option<f64>, limits: Option<(f64, f64)>, detail: &str, label_w: usize, indent: usize, unit: &str) -> String {
        let head = self.st.p(&format!("{}{}", " ".repeat(indent), pad_right(label, label_w)), &["muted"]);
        let Some(pct) = pct else { return head + &self.st.p("unavailable", &["muted"]) };
        let lv = if limits.is_some() { level(pct, limits) } else { "ok" };
        clip(self.st, &(head + &reading(self.st, pct, unit, lv) + " " + &meter(self.st, pct, self.bar, limits, true, None)
                        + &if detail.is_empty() { String::new() } else { self.st.p(&format!("  {}", detail), &["muted"]) }), self.width)
    }

    fn meter4(&self, label: &str, pct: Option<f64>, limits: Option<(f64, f64)>, detail: &str) -> String {
        self.meter_row(label, pct, limits, detail, 6, 4, "%")
    }

    fn empty(&self, text: &str) -> Vec<String> {
        vec![self.st.p(&format!("  {}", text), &["muted"])]
    }
}

fn usage_of(x: &Value) -> Option<f64> {
    x.get("usage").filter(|u| !u.is_null()).map(n)
}

fn body_overview(v: &View) -> Vec<String> {
    let (st, width) = (v.st, v.width);
    let mut body = Vec::new();
    let cpus: Vec<&Value> = list(v.s.get("cpu")).iter().filter(|c| c.get("usage").is_some_and(isnum)).collect();
    for m in metrics(v.s, v.lim) {
        if m.label == "NPU" { continue }
        let detail = if m.label == label("cpu") && !cpus.is_empty() {
            let mut busy = cpus[0];
            for c in &cpus[1..] {
                if n(&c["usage"]) > n(&busy["usage"]) { busy = c }
            }
            let lv = tone(level(n(&busy["usage"]), m.limits));
            let more = cpus.iter().filter(|c| !std::ptr::eq(**c, busy) && level(n(&c["usage"]), m.limits) != "ok").count();
            st.p(&format!("max CPU{} {} %", s(&busy["id"]), f(n(&busy["usage"]), 1)), &[or(lv, "muted"), if lv.is_empty() { "" } else { "bold" }])
                + &if more > 0 { st.p(&format!("  +{} {}{} %", more, st.ge, g(m.limits.map_or(0.0, |l| l.0))), &["muted"]) } else { String::new() }
        } else {
            st.p(&m.detail, &["muted"])
        };
        body.push(match m.value {
            None => v.meter_row(&m.label, None, m.limits, "", 6, 2, m.unit),
            Some(value) => clip(st, &(st.p(&format!("  {}", pad_right(&m.label, 6)), &["muted"]) + &reading(st, value, m.unit, level(value, m.limits))
                                      + " " + &meter(st, value, v.bar, m.limits, true, None) + "  " + &detail), width),
        });
    }
    let note = cpu_note(v.s).unwrap_or_default();
    if !note.is_empty() {
        body.push(st.p(&format!("  {}", note), &["muted"]));
    }
    body.push(String::new());

    let net = dict(v.s.get("network"));
    let ifaces: Vec<&Value> = busiest_first(list(net.get("interfaces")), &v.status.traffic).into_iter()
        .filter(|i| !matches!(i.get("name"), Some(Value::Str(x)) if x == "lo")).collect();
    if !ifaces.is_empty() {
        let block = |i: &Value| -> String {
            let r = i.get("rates");
            if !t(r) { return st.p(&format!("{} {}", s(&i["name"]), rate_reason(i.get("reason").unwrap_or(&Value::Null))), &["muted"]) }
            let r = r.unwrap();
            let idle = !t(r.get("rx_bytes_per_s")) && !t(r.get("tx_bytes_per_s"));
            let text = format!("{} ", s(&i["name"])) + &st.p(st.darr, &["muted"]) + &pad(&per_s(n(&r["rx_bytes_per_s"])), 11, true)
                + "  " + &st.p(st.uarr, &["muted"]) + &pad(&per_s(n(&r["tx_bytes_per_s"])), 11, true);
            if idle { st.p(&strip(&text), &["muted", "dim"]) } else { text }
        };
        let (mut line, mut shown) = (st.p("  NET   ", &["muted"]), 0);
        for i in &ifaces {
            let rest = ifaces.len() - shown - 1;
            let candidate = line.clone() + if shown > 0 { "    " } else { "" } + &block(i);
            if shown > 0 && vlen(&candidate) + if rest > 0 { format!("   +{}", rest).len() } else { 0 } > width { break }
            line = candidate;
            shown += 1;
        }
        if shown < ifaces.len() {
            line += &st.p(&format!("   +{}", ifaces.len() - shown), &["muted"]);
        }
        body.push(clip(st, &line, width));
    }
    body
}

fn body_resources(v: &View) -> Vec<String> {
    let (s_, st) = (v.s, v.st);
    let mut body = Vec::new();
    let cpus = list(s_.get("cpu"));
    if !cpus.is_empty() {
        let mut clocks: Vec<&Value> = Vec::new();
        for c in cpus {
            let fq = c.get("freq").unwrap_or(&Value::Null);
            if !clocks.iter().any(|x| x == &fq || isnum(x) && isnum(fq) && n(x) == n(fq)) { clocks.push(fq) }
        }
        let same = clocks.len() == 1 && !clocks[0].is_null() && !(isnum(clocks[0]) && n(clocks[0]) == 0.0);
        body.push(v.title("CPU CORES", &if same { format!("{} measured · all at {}", cpus.len(), mhz(Some(clocks[0]))) }
                                         else { format!("%  ·  MHz  ·  {} measured", cpus.len()) }));
        let cells: Vec<String> = cpus.iter().map(|c| core_cell(st, c, !same, v.lim)).collect();
        let cols = ((v.width as i64 + 3).div_euclid(if same { 23 } else { 28 })).max(1) as usize;
        body.extend(cells.chunks(cols).map(|row| row.join("   ")));
    }
    let note = cpu_note(s_).unwrap_or_default();
    if !note.is_empty() {
        body.push(st.p(&format!("  {}", note), &["muted"]));
    }
    let (gpu, npu) = (s_.get("gpu"), s_.get("npu"));
    if t(gpu) || t(npu) {
        body.extend([String::new(), v.title("ACCELERATORS", "")]);
        if t(gpu) {
            let g = gpu.unwrap();
            let detail = if t(g.get("freq")) && t(g.get("max_freq")) { format!("{} / {}", f(n(&g["freq"]) / 1e6, 0), mhz(g.get("max_freq"))) }
                         else { mhz(g.get("freq")) };
            body.push(v.meter4("GPU", usage_of(g), v.lim.get("gpu"), &detail));
        }
        if t(npu) {
            let x = npu.unwrap();
            body.push(v.meter4("NPU", usage_of(x), None, &mhz(x.get("freq"))));
        }
    }
    let m = &s_["memory"];
    body.extend([String::new(), v.title("MEMORY", "")]);
    let (used, total) = (n(&m["used"]), n(&m["total"]));
    body.push(v.meter4("RAM", t(m.get("total")).then(|| 100.0 * used / total), v.lim.get("memory"),
                       &format!("{} / {} · {} free", size(used), size(total), size(total - used))));
    if t(m.get("swap_total")) {
        let (su, stt) = (n(&m["swap_used"]), n(&m["swap_total"]));
        body.push(v.meter4("SWAP", Some(100.0 * su / stt), v.lim.get("memory"), &format!("{} / {}", size(su), size(stt))));
    }
    let res = items(dict(s_.get("pressure")).get("resources"));
    if !res.is_empty() {
        body.extend([String::new(), v.title("RESOURCE PRESSURE", "some / full · avg10, time stalled")]);
        for (name, r) in res {
            let full = if t(r.get("full")) { format!(" / {}", f(n(&r["full"]["avg10"]), 1)) } else { String::new() };
            body.push(st.p(&format!("    {}", pad_right(name, 10)), &["muted"]) + &format!("{}{} %", f(n(&r["some"]["avg10"]), 1), full));
        }
    }
    body
}

fn wifi_signal(i: &Value) -> (String, Vec<String>) {
    let val = |k: &str| i.get(k).filter(|v| !v.is_null());
    let signal = if let Some(d) = val("signal_dbm") { format!("{} dBm", s(d)) }
                 else if let Some(r) = val("signal_raw") { format!("signal {} (unit not reported)", s(r)) } else { "signal n/a".into() };
    let extra = [val("link_quality").map(|q| format!("link quality {}", s(q))), val("noise_dbm").map(|x| format!("noise {} dBm", s(x)))];
    (signal, extra.into_iter().flatten().collect())
}

fn target_text(tg: &Value) -> String {
    let address = s(&tg["address"]);
    if address.contains(':') { format!("[{}]:{}", address, s(&tg["port"])) } else { format!("{}:{}", address, s(&tg["port"])) }
}

fn body_network(v: &View) -> Vec<String> {
    let st = v.st;
    let net = dict(v.s.get("network"));
    let mut ifaces: Vec<&Value> = list(net.get("interfaces")).iter().collect();
    ifaces.sort_by_key(|i| matches!(i.get("name"), Some(Value::Str(x)) if x == "lo"));
    let (wifi, probe) = (group(v.obs, "wifi"), group(v.obs, "probe"));
    let wlans = wifi.map_or(&[][..], |(d, _)| list(d.get("interfaces")));
    let targets = probe.map_or(&[][..], |(d, _)| list(d.get("targets")));
    if ifaces.is_empty() && wlans.is_empty() && targets.is_empty() {
        return v.empty("No interface, Wi-Fi or TCP connect readings · they appear here when collected");
    }
    let mut body = Vec::new();
    if !ifaces.is_empty() {
        let shown = &ifaces[..ifaces.len().min(MAX_ROWS)];
        let widths = detail_widths(v.width, shown, &[13, 13, 17, 12]);
        body.push(v.row(&widths, &["  INTERFACES".into(), col("rx"), col("tx"), col("packets"), col("errors")],
                        &["muted bold", "muted", "muted", "muted", "muted"]));
        body.extend(shown.iter().map(|i| v.net_row(&widths, i)));
        if ifaces.len() > shown.len() {
            body.push(st.p(&format!("    +{} more interfaces (all in /api/stats)", ifaces.len() - shown.len()), &["muted"]));
        }
        body.push(st.p(&pad("this netns · err/drop = totals", v.width.min(widths.iter().sum::<usize>().max(32)), true), &["muted"]));
    }
    if !wlans.is_empty() {
        body.extend([String::new(), v.title("WI-FI", &stale_text(wifi.map(|(_, o)| o)))]);
        let names = wlans.iter().map(|i| s(&i["name"]).chars().count()).max().unwrap_or(0);
        for i in wlans {
            let name = format!("    {}  ", pad_right(&s(&i["name"]), names));
            if i.get("connected") != Some(&Value::Bool(true)) {
                let state = if i.get("connected") == Some(&Value::Bool(false)) { "not connected".to_string() }
                            else { i.get("connection_state").map_or("unknown".into(), s) };
                body.push(st.p(&(name + &state), &["muted"]));
                continue;
            }
            let (signal, extra) = wifi_signal(i);
            let extra: Vec<String> = extra.iter().map(|e| st.p(&format!("   {}", e), &["muted"])).collect();
            let dbm = i.get("signal_dbm").filter(|d| !d.is_null());
            let Some(dbm) = dbm.map(n) else {
                body.push(joined(v.width, &[vec![st.p(&name, &["muted"]), signal], extra].concat()));
                continue;
            };
            let lv = level_of(v.lim, "wifi", dbm);
            let (weak, strong) = pair(&spec()["scales"]["wifi_dbm"]).unwrap();
            body.push(joined(v.width, &[vec![st.p(&name, &["muted"]), st.p(&pad(&signal, 9, false), &[tone(lv), "bold"]),
                                             " ".to_string() + &meter(st, 100.0 * (dbm - weak) / (strong - weak), if v.wide { 20 } else { 11 }, None, false, Some(lv))],
                                        extra].concat()));
        }
    }
    if !targets.is_empty() {
        body.extend([String::new(), v.title("TCP CONNECT", &stale_text(probe.map(|(_, o)| o)))]);
        let where_w = targets.iter().map(|tg| target_text(tg).chars().count()).max().unwrap() + 2;
        for tg in targets {
            let ms = tg.get("connect_ms").filter(|m| !m.is_null());
            let now = ms.map_or_else(|| s(tg.get("reason").unwrap_or(&Value::Null)), |m| format!("{} ms", f(n(m), 1)));
            body.push(joined(v.width, &[st.p(&pad(&format!("    {}", target_text(tg)), 4 + where_w, false), &["muted"]), pad(&now, 10, true),
                                        st.p(&format!("   failed {}/{} recent", s(&tg["failures"]), s(&tg["attempts"])),
                                             &[if t(tg.get("failures")) { "warn" } else { "muted" }])]));
        }
    }
    body
}

fn body_storage(v: &View) -> Vec<String> {
    let st = v.st;
    let storage = group(v.obs, "storage");
    let disks = list(dict(v.s.get("disk_io")).get("disks"));
    let data = storage.map(|(d, _)| d);
    let fss = data.map_or(&[][..], |d| list(d.get("filesystems")));
    let parts = data.map_or(&[][..], |d| list(d.get("partitions")));
    if fss.is_empty() && parts.is_empty() && disks.is_empty() {
        return v.empty("No filesystem or disk I/O readings · they appear here when collected");
    }
    let mut body = Vec::new();
    let first = |fs: &Value| list(fs.get("mount_points")).first().cloned().unwrap_or_default();
    if !fss.is_empty() {
        body.push(v.title("FILESYSTEMS", &stale_text(storage.map(|(_, o)| o))));
        let path_w = fss.iter().map(|fs| s(&first(fs)["path"]).chars().count()).max().unwrap() + 2;
        let caps: Vec<usize> = fss.iter().filter(|fs| !fs["total_bytes"].is_null())
            .map(|fs| format!("{} / {}", size(n(&fs["used_bytes"])), size(n(&fs["total_bytes"]))).chars().count()).collect();
        let cap_w = caps.into_iter().max().unwrap_or(0);
        let bw = (if v.wide { 20 } else { 11 }).min((v.width as i64 - 20 - path_w as i64).max(6) as usize);
        for fs in fss {
            let m = list(fs.get("mount_points"));
            let m0 = first(fs);
            let where_ = pad(&s(&m0["path"]), path_w, false) + if t(m0.get("read_only")) { "(ro) " } else { "" };
            let more = if m.len() > 1 { st.p(&format!("+{} ", m.len() - 1), &["muted"]) } else { String::new() };
            let dev_name = if t(fs.get("device")) { s(&fs["device"]) } else { format!("{}:{}", s(&fs["major"]), s(&fs["minor"])) };
            let dev = st.p(&format!("   {} {}", s(&fs["fstype"]), dev_name), &["muted"]);
            let Some(pct) = fs_pct(fs) else {
                body.push(joined(v.width, &[st.p(&format!("    {}", where_), &["muted"]), more, st.p("capacity unknown", &["muted"]), dev]));
                continue;
            };
            let lim = v.lim.get("filesystem");
            body.push(joined(v.width, &[st.p(&format!("    {}", where_), &["muted"]), more, meter(st, pct, bw, lim, true, None),
                                        st.p(&pad(&format!("{} %", f(pct, 0)), 7, true), &[tone(level(pct, lim))]),
                                        format!("   {}", pad(&format!("{} / {}", size(n(&fs["used_bytes"])), size(n(&fs["total_bytes"]))), cap_w, false)),
                                        st.p(&format!("   {} free", size(n(&fs["available_bytes"]))), &["muted"]), dev]));
        }
    }
    if !parts.is_empty() {
        let mut by_disk: Vec<(String, Vec<&Value>)> = Vec::new();
        for p in parts {
            let d = s(&p["disk"]);
            match by_disk.iter_mut().find(|(k, _)| *k == d) { Some(x) => x.1.push(p), None => by_disk.push((d, vec![p])) }
        }
        body.extend([String::new(), v.title("PARTITIONS", "")]);
        for (disk, items) in by_disk {
            let mounted = items.iter().filter(|p| t(p.get("mount_points"))).count();
            body.push(st.p(&format!("    {}", pad_right(&disk, 12)), &["muted"])
                      + &format!("{} partition{}, {} mounted", items.len(), if items.len() != 1 { "s" } else { "" }, mounted));
        }
    }
    if !disks.is_empty() {
        let all: Vec<&Value> = disks.iter().collect();
        let widths = detail_widths(v.width, &all, &[13, 13, 17, 12]);
        body.extend([String::new(), v.row(&widths, &["  DISK I/O".into(), col("read"), col("write"), col("ops"), col("io_time")],
                                          &["muted bold", "muted", "muted", "muted", "muted"])]);
        body.extend(v.disk_rows(&widths, disks));
        body.push(st.p(&pad("I/O time = share of window with I/O in flight", v.width.min(widths.iter().sum::<usize>().max(46)), true), &["muted"]));
    }
    body
}

fn body_thermal(v: &View) -> Vec<String> {
    let st = v.st;
    let temps: Vec<(&String, f64)> = items(v.s.get("temperature")).iter().filter(|(_, x)| isnum(x)).map(|(k, x)| (k, n(x))).collect();
    let limits = v.lim.temp();
    if temps.is_empty() {
        return v.empty("No temperature readings · this board reports none");
    }
    let mut body = vec![v.title("TEMPERATURE", &limits.map_or("0-100 °C".into(), |(w, c)| format!("0-100 °C · warning {} · critical {}", g(w), g(c))))];
    let name_w = temps.iter().map(|(k, _)| k.chars().count()).max().unwrap() + 2;
    let bw = 20.min((v.width as i64 - 13 - name_w as i64).max(6) as usize);
    for (k, x) in temps {
        let lv = tone(level(x, limits));
        body.push(clip(st, &(st.p(&format!("    {}", pad_right(k, name_w)), &[or(lv, "muted"), if lv.is_empty() { "" } else { "bold" }])
                             + &reading(st, x, "°C", level(x, limits)) + " " + &meter(st, x, bw, limits, true, None)), v.width));
    }
    body
}

fn battery_readings(b: &Value) -> Vec<String> {
    let val = |k: &str| b.get(k).filter(|v| !v.is_null());
    let either = |a: &str, c: &str| b.get(a).filter(|v| t(Some(v))).or_else(|| b.get(c).filter(|v| t(Some(v))));
    let (full, design) = (either("charge_full_uah", "energy_full_uwh"), either("charge_full_design_uah", "energy_full_design_uwh"));
    let parts = [b.get("status").filter(|v| t(Some(v))).map(s),
                 val("voltage_uv").map(|x| format!("{} V", f(n(x) / 1e6, 2))),
                 val("current_ua").map(|x| format!("{:+.2} A", n(x) / 1e6)),
                 val("power_uw").map(|x| format!("{} W", f(n(x) / 1e6, 2))),
                 val("cycle_count").map(|x| format!("{} cycles", s(x))),
                 val("temp_c").map(|x| format!("{} °C", f(n(x), 1))),
                 full.zip(design).map(|(a, d)| format!("{} % of design", f(n(a) * 100.0 / n(d), 0)))];
    parts.into_iter().flatten().filter(|p| !p.is_empty()).collect()
}

fn body_power(v: &View) -> Vec<String> {
    let st = v.st;
    let power = items(v.s.get("power"));
    let fans = list(v.s.get("fans"));
    let battery = group(v.obs, "battery");
    let cells = battery.map_or(&[][..], |(d, _)| list(d.get("batteries")));
    let supplies = battery.map_or(&[][..], |(d, _)| list(d.get("external")));
    if power.is_empty() && fans.is_empty() && cells.is_empty() && supplies.is_empty() {
        return v.empty("No power rail, fan or battery readings · this board reports none");
    }
    let mode = if t(v.s.get("power_mode")) { format!("mode {}", s(&v.s["power_mode"])) } else { String::new() };
    let mut body = Vec::new();
    if !power.is_empty() {
        body.push(v.title("POWER RAILS", &mode));
        let name_w = power.iter().map(|(k, _)| k.chars().count()).chain(["sum of rails".len()]).max().unwrap() + 2;
        for (k, p) in power {
            body.push(st.p(&format!("    {}", pad_right(k, name_w)), &["muted"]) + &format!("{:6.2} W", n(p)));
        }
        if power.len() > 1 {
            body.push(st.p(&format!("    {}", st.rule.repeat(name_w + 8)), &["muted"]));
            body.push(st.p(&format!("    {}", pad_right("sum of rails", name_w)), &["muted"])
                      + &st.p(&format!("{:6.2} W", power.iter().map(|(_, p)| n(p)).sum::<f64>()), &["bold"]));
        }
    }
    if !fans.is_empty() {
        if !body.is_empty() { body.push(String::new()) }
        body.push(v.title("FANS", if power.is_empty() { &mode } else { "" }));
        let name_w = fans.iter().map(|fan| s(&fan["name"]).chars().count()).max().unwrap() + 2;
        for fan in fans {
            let rpm = fan.get("rpm").filter(|r| !r.is_null());
            let head = st.p(&format!("    {}", pad_right(&s(&fan["name"]), name_w)), &["muted"]);
            match fan.get("percent").filter(|p| !p.is_null()) {
                Some(pct) => body.push(clip(st, &(head + &st.p(&format!("{:3.0} %  ", n(pct)), &["bold"]) + &meter(st, n(pct), v.bar, None, true, None)
                                                  + &rpm.map_or(String::new(), |r| st.p(&format!("  {} rpm", s(r)), &["muted"]))), v.width)),
                None => body.push(head + &rpm.map_or("n/a".into(), |r| format!("{} rpm", s(r)))),
            }
        }
    }
    if !cells.is_empty() || !supplies.is_empty() {
        if !body.is_empty() { body.push(String::new()) }
        body.push(v.title("BATTERY", &stale_text(battery.map(|(_, o)| o))));
        let name_w = cells.iter().map(|b| s(&b["name"]).chars().count()).chain([0]).max().unwrap() + 2;
        for b in cells {
            let name = st.p(&format!("    {}", pad_right(&s(&b["name"]), name_w)), &["muted"]);
            if b.get("present") == Some(&Value::Bool(false)) {
                body.push(name + "not present");
                continue;
            }
            let pct = b.get("percent").filter(|p| !p.is_null());
            let charge = match pct { Some(p) => format!("{} %", s(p)), None => b.get("capacity_level").filter(|c| t(Some(c))).map_or("charge n/a".into(), s) };
            let detail = battery_readings(b).join(" · ");
            let lv = pct.map_or("ok", |p| level_of(v.lim, "battery", n(p)));
            let head = vec![name, st.p(&pad(&charge, 7, false), &[or(tone(lv), "bold"), if tone(lv).is_empty() { "" } else { "bold" }])];
            let bw = (if v.wide { 14 } else { 8 }).min((v.width as i64 - 10 - name_w as i64 - vlen(&charge) as i64 - vlen(&detail) as i64).max(6) as usize);
            let bar: Vec<String> = pct.map(|p| "  ".to_string() + &meter(st, n(p), bw, v.lim.get("battery"), true, Some(lv))).into_iter().collect();
            let inline = vlen(&head.concat()) + vlen(&detail) + 2 <= v.width;
            let shown_detail = if !detail.is_empty() && inline { vec![st.p(&detail, &["muted"])] } else { vec![] };
            body.push(joined(v.width, &[head, shown_detail, bar].concat()));
            if !detail.is_empty() && !inline {
                for part in wrap(&detail, 12.max(v.width as i64 - 6) as usize) {
                    body.push(st.p(&format!("      {}", part), &["muted"]));
                }
            }
        }
        if !supplies.is_empty() {
            let sup_w = supplies.iter().map(|e| s(&e["name"]).chars().count()).max().unwrap() + 2;
            for e in supplies {
                let online = e.get("online").unwrap_or(&Value::Null);
                let state = if t(Some(online)) { "on" } else if *online == Value::Bool(false) { "off" } else { "n/a" };
                body.push(joined(v.width, &[st.p(&format!("    {}", pad_right(&s(&e["name"]), sup_w)), &["muted"]), state.into(),
                                            st.p(&format!("   {} power", s(&e["type"])), &["muted"])]));
            }
        }
    }
    body
}

const SYSTEM_ROWS: [(&str, &str); 5] = [("os", "OS"), ("kernel", "Kernel"), ("arch", "Architecture"), ("hostname", "Hostname"), ("l4t", "L4T")];

fn body_system(v: &View) -> Vec<String> {
    let st = v.st;
    let system = items(v.s.get("system"));
    let get = |k: &str| system.iter().find(|(x, _)| x == k).map(|(_, v)| v);
    let mut rows: Vec<(String, String)> = vec![("Model".into(), s(&v.s["model"])), ("Platform".into(), s(&v.s["platform"])),
                                               ("Uptime".into(), uptime_text(&v.s["uptime"]))];
    rows.extend(SYSTEM_ROWS.iter().filter(|(k, _)| t(get(k))).map(|(k, label)| (label.to_string(), s(get(k).unwrap()))));
    rows.extend(system.iter().filter(|(k, x)| t(Some(x)) && !SYSTEM_ROWS.iter().any(|(r, _)| r == k)).map(|(k, x)| (k.clone(), s(x))));
    let own = [("Version".to_string(), crate::version().to_string()),
               ("Service".to_string(), format!("{} · every {} s", v.status.addr, g(v.status.interval)))];
    let label_w = rows.iter().chain(own.iter()).map(|(l, _)| l.chars().count()).max().unwrap() + 3;
    let line = |(l, x): &(String, String)| st.p(&format!("  {}", pad_right(l, label_w)), &["muted"]) + x;
    rows.iter().map(line).chain([String::new(), v.title("PLATMON", "")]).chain(own.iter().map(line)).collect()
}

fn status_slot(st: &Style, width: usize, status: &Status, alerts: &[(String, String, &str, &str)], note: &str) -> Vec<String> {
    if let Some(error) = &status.error {
        let mut parts = error.split('\n');
        let first = parts.next().unwrap_or("");
        let lead = format!("  {} NOT CURRENT  ", st.cross);
        let lead_n = lead.chars().count();
        let indent = " ".repeat(lead_n);
        let room = 16.max(width as i64 - lead_n as i64) as usize;
        let mut said = wrap(first, room);
        if said.is_empty() { said.push(String::new()) }
        let mut lines = vec![st.p(&pad(&(lead + &said[0]), width, false), &["crit_bg"])];
        lines.extend(said[1..].iter().map(|l| st.p(&pad(&(indent.clone() + l), width, false), &["crit_bg"])));
        let advice: Vec<String> = [format!("retrying every {} s", g(status.interval))].into_iter().chain(parts.map(|r| r.trim().to_string())).collect();
        lines.extend(wrap(&advice.join(" · "), room).iter().map(|l| st.p(&(indent.clone() + l), &["crit"])));
        while lines.len() < 2 { lines.push(String::new()) }
        return lines;
    }
    let mut lines = Vec::new();
    if !note.is_empty() {
        let why = note.strip_prefix("Collection: ").unwrap_or(note);
        lines.push(clip(st, &(st.p(&format!("  {} PARTIAL  ", st.up), &["warn", "bold"]) + &st.p(why, &["warn"])), width));
    }
    if let Some(top) = alerts.first() {
        let lead = st.p(&format!("  {} NEEDS ATTENTION   ", st.up), &[tone(top.2), "bold"]);
        let chips: Vec<String> = alerts.iter().map(|(name, value, lv, _)| if *lv == "crit" { st.p(&format!(" {} {} ", name, value), &["crit_bg"]) }
                                                                           else { st.p(&format!("{} {}", name, value), &["warn", "bold"]) }).collect();
        let (mut line, mut shown) = (lead, 0);
        for chip in &chips {
            let rest = chips.len() - shown - 1;
            if shown > 0 && vlen(&format!("{}  {}", line, chip)) + if rest > 0 { format!("  +{}", rest).len() } else { 0 } > width { break }
            line = line + if shown > 0 { "  " } else { "" } + chip;
            shown += 1;
        }
        if shown < chips.len() {
            line += &st.p(&format!("  +{}", chips.len() - shown), &["muted"]);
        }
        lines.push(clip(st, &line, width));
    }
    lines.resize(2, String::new());
    lines
}

/// screen(): the live view as lines of at most width columns and at most height lines: the header, the
/// tabs, the status slot, the view's body and the footer. Also returns the clamped offset and the page
/// size (cli.py's status["scroll"]), so the caller scrolls by what was drawn.
#[allow(clippy::too_many_arguments)]
pub fn screen(s_: &Value, obs: Option<&Value>, width: usize, height: usize, st: &Style, status: &Status, view: &str, offset: i64,
              lim: &Limits) -> (Vec<String>, (i64, i64)) {
    let (wide, narrow) = (width >= WIDE, width < NARROW);
    let sys_ = dict(s_.get("system"));
    let state = status.state.as_str();
    let color = s(&spec()["states"][state]);
    let stale = state == "offline" || status.age > 2.0 * status.interval;
    let hostname = sys_.get("hostname");
    let name = if wide || !t(hostname) { s(&s_["model"]) } else { s(hostname.unwrap()) };
    let meta: Vec<Value> = [if wide { hostname.cloned().unwrap_or_default() } else { s_["platform"].clone() }, s_["power_mode"].clone(),
                            Value::Str(format!("up {}", uptime_text(&s_["uptime"])))].into_iter().filter(|x| t(Some(x))).collect();
    let alerts = attention(s_, obs, lim);
    let mut marks: Vec<(&'static str, &'static str)> = Vec::new();
    for (_, _, lv, view) in &alerts {
        if !marks.iter().any(|(v, _)| v == view) { marks.push((view, lv)) }
    }
    let head = vec![
        spread(&(st.p(" p_", &["ok", "bold"]) + &if narrow { "  ".to_string() } else { st.p(" platmon", &["bold"]) + "   " } + &st.p(&name, &["bold"])),
               &(st.p(&format!("{} {}", st.dot, state.to_uppercase()), &[&color])
                 + &if stale { st.p(&format!("  data {} s old", f(status.age, 0)), &["warn"]) } else { String::new() } + " "), width),
        st.p(&(" ".repeat(if narrow { 4 } else { 13 }) + &meta.iter().map(s).collect::<Vec<_>>().join(" · ")), &["muted"]),
        tabbar(st, width, view, &marks)];
    let slot = status_slot(st, width, status, &alerts, &collection_note(s_));
    let v = View { s: s_, obs, width, st, status, lim, wide, bar: if wide { 20 } else if width >= NARROW { 10 } else { 8 } };
    let mut body = match view {
        "resources" => body_resources(&v), "network" => body_network(&v), "storage" => body_storage(&v),
        "thermal" => body_thermal(&v), "power" => body_power(&v), "system" => body_system(&v), _ => body_overview(&v),
    };
    while body.last().is_some_and(String::is_empty) {
        body.pop();
    }
    if status.error.is_some() {
        body = body.iter().map(|line| st.p(&strip(line), &["dim"])).collect();
    }
    let host = status.addr.rsplit_once(':').map_or(status.addr.as_str(), |(h, _)| h).trim_matches(|c| c == '[' || c == ']');
    let where_ = if ["localhost", "127.0.0.1", "::1"].contains(&host) { String::new() } else { format!("{} · ", status.addr) };
    let keys = st.p("  ", &[]) + &st.p("q", &["bold"]) + &st.p(" quit", &["muted"])
        + &if width >= 36 { st.p(" · ", &["muted"]) + &st.p("1-7", &["bold"]) + &st.p(" view", &["muted"]) } else { String::new() }
        + &if width >= 80 { st.p(" · ", &["muted"]) + &st.p(&format!("{}/{}", st.uarr, st.darr), &["bold"]) + &st.p(" scroll", &["muted"]) } else { String::new() };
    let interval = g(status.interval);
    let foot = vec![st.p(&st.rule.repeat(width), &["muted"]),
                    spread(&keys, &st.p(&if wide { format!("{}every {} s ", where_, interval) } else { format!("{}{} s ", where_, interval) }, &["muted"]), width)];
    let room = (height as i64 - head.len() as i64 - slot.len() as i64 - foot.len() as i64).max(1);
    let mut scroll = (0, (room - 1).max(1));
    if body.len() as i64 > room {
        let page = room - 1;
        let last = body.len() as i64 - page;
        let offset = offset.min(last).max(0);
        let (above, below) = (offset, last - offset);
        scroll = (offset, page.max(1));
        let shown: Vec<String> = [if above > 0 { format!("{} {} above", st.uarr, above) } else { String::new() },
                                  if below > 0 { format!("{} {} below", st.darr, below) } else { String::new() }]
            .into_iter().filter(|x| !x.is_empty()).collect();
        let mut kept: Vec<String> = body[offset as usize..(offset + page) as usize].to_vec();
        kept.push(joined(width, &[st.p(&format!("  {}", shown.join(" · ")), &["muted"]),
                                  st.p(&format!("  ·  {}{} PgUp/PgDn Home/End scroll", st.uarr, st.darr), &["muted"])]));
        body = kept;
    }
    let lines: Vec<String> = head.into_iter().chain(slot).chain(body).chain(foot).take(height.max(1)).map(|l| fit(&l, width)).collect();
    (lines, scroll)
}

/// The traffic-free Status of a snapshot (tests).
#[cfg(test)]
pub fn live(addr: &str) -> Status {
    Status { state: "live".into(), age: 0.4, error: None, addr: addr.into(), interval: 1.0, traffic: HashMap::new() }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn g_formats_like_python() {
        for (x, want) in [(1.0, "1"), (0.5, "0.5"), (80.0, "80"), (85.5, "85.5"), (1e6, "1e+06"), (0.0001, "0.0001"), (1e-5, "1e-05"),
                          (123456.0, "123456"), (2.25, "2.25"), (0.1 + 0.2, "0.3")] {
            assert_eq!(g(x), want);
        }
    }

    #[test]
    fn wrap_breaks_like_textwrap() {
        assert_eq!(wrap("cannot reach platmon at my-host:9797 (timed out)", 20), ["cannot reach platmon", "at my-host:9797", "(timed out)"]);
        assert_eq!(wrap("aaaa-bbbb-cccc", 6), ["aaaa-", "bbbb-", "cccc"]);
        assert_eq!(wrap("abcdefghij", 4), ["abcd", "efgh", "ij"]);
        assert_eq!(wrap("  lead   and  more ", 8), ["  lead", "and", "more"]);
        assert!(wrap("   ", 5).is_empty());
    }

    #[test]
    fn fit_keeps_escapes_and_resets() {
        let st = Style::new(true, true);
        let cut = fit(&(st.p("abcdef", &["warn"]) + "ghij"), 4);
        assert_eq!(vlen(&cut), 4);
        assert!(cut.ends_with("\x1b[0m"));
    }

    #[test]
    fn keys() {
        assert_eq!(next_view("3", "overview"), "network");
        assert_eq!(next_view("\x1b[1;2C", "network"), "storage");
        assert_eq!(next_view("\x1b[Z", "resources"), "overview");
        assert_eq!(next_offset("\x1b[6~", 2, 10), 12);
        assert_eq!(next_offset("\x1bOF", 0, 10), BOTTOM);
        assert_eq!(next_offset("\x1b[A", 0, 10), 0);
    }

    #[test]
    fn every_line_fits() {
        let s_ = parse(r#"{"model": "M", "platform": "P", "uptime": 90061, "power_mode": null, "cpu": [{"id": 0, "usage": 95.0, "freq": null}],
                          "gpu": null, "memory": {"total": 100, "used": 50, "swap_total": 0, "swap_used": 0}, "disk": {"total": 10, "used": 9},
                          "temperature": {"cpu": 51.0}, "power": {}, "fans": []}"#).unwrap();
        for view in views() {
            for (w, h) in [(119, 40), (60, 20), (20, 6), (10, 1)] {
                let (lines, _) = screen(&s_, None, w, h, &Style::new(true, true), &live("localhost:9797"), &view, 0, &Limits::new());
                assert!(lines.len() <= h && lines.iter().all(|l| vlen(l) <= w), "{} {}x{}", view, w, h);
            }
        }
    }

    // What tests/frontends/test_cli_screen.py checked of cli.py's attention(), use_levels(), make_style(),
    // tabbar() and busiest_first(), kept when cli.py was removed; that file still checks the screens, through
    // the binary's --screen-script.

    const FULL: &str = r#"{"platform": "Jetson Orin", "model": "Test Board", "uptime": 90061, "power_mode": "MAXN_SUPER",
        "system": {"os": "Ubuntu 22.04.5 LTS", "kernel": "5.15.199-tegra", "arch": "aarch64", "hostname": "orin", "l4t": "R36.5.2"},
        "cpu": [{"id": 0, "usage": 12.5, "freq": 1728000000}, {"id": 7, "usage": 91.4, "freq": 1728000000}],
        "gpu": {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000},
        "memory": {"total": 8589934592, "used": 2147483648, "swap_total": 4294967296, "swap_used": 0},
        "disk": {"total": 107374182400, "used": 26843545600},
        "temperature": {"cpu": 51.2, "tj": 86.2}, "power": {"VDD_IN": 4.82},
        "fans": [{"name": "pwmfan", "rpm": null, "percent": 31}, {"name": "pwm_tach", "rpm": 1741, "percent": null}]}"#;

    /// test_cli_screen.py's HOT: CPU7 at 91.4 %, tj at 86.2 °C.
    fn hot() -> Value {
        parse(FULL).unwrap()
    }

    fn found(a: Vec<(String, String, &'static str, &'static str)>) -> Vec<(String, String, &'static str)> {
        a.into_iter().map(|(name, value, lv, _)| (name, value, lv)).collect()
    }

    fn one(name: &str, value: &str, lv: &'static str) -> (String, String, &'static str) {
        (name.into(), value.into(), lv)
    }

    #[test]
    fn attention_follows_the_web_levels() {
        // the CPU mean, 52 %, is fine; tj at 86.2 °C is a warning by the generic level (85/95), not critical
        assert_eq!(found(attention(&hot(), None, &Limits::new())), [one("tj", "86.2 °C", "warn")]);
    }

    #[test]
    fn server_levels_replace_the_defaults() {
        // /api/status "levels" ([thresholds]) as the web page uses them; bad or missing ones keep the defaults
        let mut lim = Limits::new();
        lim.use_levels(Some(&parse(r#"{"temperature": [80, 90], "memory": [10, 20], "cpu": [90, 80], "gpu": "x", "swap": [1, 2]}"#).unwrap()));
        assert_eq!((lim.get("memory"), lim.get("cpu"), lim.get("gpu")), (Some((10.0, 20.0)), Some((80.0, 90.0)), Some((80.0, 90.0))));
        assert!(!lim.0.iter().any(|(k, _)| k == "swap"));
        assert_eq!(found(attention(&hot(), None, &lim)), [one("RAM", "25.0 %", "crit"), one("tj", "86.2 °C", "warn")]);
        lim.use_levels(Some(&parse(r#"{"temperature": null}"#).unwrap()));
        assert_eq!(found(attention(&hot(), None, &lim))[1], one("tj", "86.2 °C", "warn")); // null: the generic level again
        lim.use_levels(None); // an older server
        assert_eq!(lim.get("memory"), Some((10.0, 20.0)));
    }

    #[test]
    fn low_levels() {
        // battery charge and Wi-Fi signal: at or below a level, the opposite of the other readings; only from a
        // current observation, and never from an empty bay or a disconnected interface
        let calm = parse(r#"{"model": "M", "platform": "P", "uptime": 1, "power_mode": null, "cpu": [], "gpu": null,
                             "memory": {"total": 100, "used": 1, "swap_total": 0, "swap_used": 0}, "disk": {"total": 10, "used": 1},
                             "temperature": {}, "power": {}, "fans": []}"#).unwrap();
        let obs = |percent: i64, dbm: i64, stale: bool| parse(&format!(r#"{{"groups": {{
            "battery": {{"observation": {{"data_age_ms": 4200, "stale": {stale}}}, "data": {{"batteries": [
                {{"name": "BAT1", "present": true, "percent": {percent}}}, {{"name": "BAT2", "present": false, "percent": 3}}], "external": []}}}},
            "wifi": {{"observation": {{"data_age_ms": 4200, "stale": {stale}}}, "data": {{"interfaces": [
                {{"name": "wlan0", "connected": true, "signal_dbm": {dbm}}}, {{"name": "wlan1", "connected": false, "signal_dbm": -99}}]}}}}}}}}"#)).unwrap();
        let lim = Limits::new();
        assert!(attention(&calm, Some(&obs(89, -47, false)), &lim).is_empty());
        assert_eq!(found(attention(&calm, Some(&obs(20, -70, false)), &lim)), [one("BAT1", "20 %", "warn"), one("wlan0", "-70 dBm", "warn")]);
        assert_eq!(found(attention(&calm, Some(&obs(10, -81, false)), &lim)), [one("BAT1", "10 %", "crit"), one("wlan0", "-81 dBm", "crit")]);
        assert!(attention(&calm, Some(&obs(10, -81, true)), &lim).is_empty()); // not a current reading
        // the server's levels: low ones arrive warning above critical; the wrong order keeps the default
        let mut lim = Limits::new();
        let wifi = lim.get("wifi");
        lim.use_levels(Some(&parse(r#"{"battery": [30, 15], "wifi": [-80, -70]}"#).unwrap()));
        assert_eq!((lim.get("battery"), lim.get("wifi")), (Some((30.0, 15.0)), wifi));
        assert_eq!(wifi, Some((-70.0, -80.0)));
    }

    #[cfg(unix)]
    #[test]
    fn ascii_on_request() {
        assert_eq!(make_style(None, None, true).full, "█");
        assert_eq!(make_style(None, Some("1"), true).full, "#");
        assert_eq!(make_style(None, None, false).full, "#");
        assert!(!make_style(Some("1"), None, true).color);
    }

    #[test]
    fn the_tab_bar_names_every_view_and_marks_the_active_one() {
        let st = Style::new(false, true);
        let n = views().len();
        let bar = tabbar(&st, 119, "network", &[]);
        assert!(bar.contains("[3 Network]") && bar.contains("1 Overview") && bar.contains(&format!("{n} System")));
        assert!(bar.contains("5 Temperature") && bar.contains("6 Power & battery")); // the two the thermal view was split into
        let narrow = tabbar(&st, 44, "network", &[]); // shorter labels, still every view
        assert!(narrow.contains("[3 Ne]") && vlen(&narrow) <= 44 && (1..=n).all(|i| narrow.contains(&i.to_string())));
        let tiny = tabbar(&st, 24, "system", &[]); // no room for every label: the position instead
        assert!(tiny.trim().starts_with(&format!("{n}/{n} Sys")) && vlen(&tiny) <= 24, "{tiny}");
    }

    #[test]
    fn numbers_read_the_same_on_both() {
        // the web page's results (tests/frontends/test_ui_spec.py NUMBERS holds the same lists)
        let sizes = [0.0, 1023.0, 1024.0, 512.0 * 1024.0, 14.3 * 1048576.0, 102.4 * 1048576.0, 1073741824.0, 45.1 * 1073741824.0,
                     57.0 * 1073741824.0];
        assert_eq!(sizes.map(size), ["0", "1023B", "1K", "512K", "14M", "102M", "1.0G", "45.1G", "57.0G"]);
        assert_eq!([0.0, 0.4, 0.5, 1.5, 2.5, 912.4, 301.0].map(per_sec), ["0", "0", "1", "2", "3", "912", "301"]);
    }

    #[test]
    fn busiest_interfaces_first() {
        let rate = |name: &str, rx: f64, tx: f64| parse(&format!(r#"{{"name": "{name}", "rates": {{"rx_bytes_per_s": {rx}, "tx_bytes_per_s": {tx},
            "rx_packets_per_s": 0, "tx_packets_per_s": 0}}}}"#)).unwrap();
        let items = [rate("br0", 0.0, 0.0), parse(r#"{"name": "wlan1", "rates": null, "reason": "warmup"}"#).unwrap(),
                     rate("eth0", 10.0, 5.0), rate("can0", 0.0, 0.0)];
        let order: Vec<String> = busiest_first(&items, &HashMap::new()).into_iter().map(|i| s(&i["name"])).collect();
        assert_eq!(order, ["eth0", "br0", "can0", "wlan1"]);
    }
}
