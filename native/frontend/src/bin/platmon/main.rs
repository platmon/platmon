//! The platmon command: live view of a platmon service, this device's by default. Ported from
//! frontends/cli.py (removed once this was packaged in its place; in the git history), keeping its options,
//! exit codes, messages and screens (the versioning contract); where this file names cli.py, it is that file.
//!
//!   platmon [host[:port]] [interval_sec] [--once | --view VIEW] [--version]   (default localhost:9797, 1 s)
//!
//! On an interactive Unix terminal the live view is screen.rs (term.rs draws it); --once, pipes and
//! Windows print the plain screen, render.rs, which the server's /text answers with too.
//!
//! Test hook, not for users: `platmon --screen-script` reads one JSON request per stdin line
//! ({"stats", "obs", "width", "height", "view", "offset", "status": {"state", "age", "error", "addr",
//! "interval", "traffic"}, "color", "utf8", "levels"}) and answers each with one line,
//! {"lines": screen(), "scroll": [offset, page], "attention": [[name, value, level], ...]} ({"keys", "view",
//! "offset", "page"}: {"view", "offset"} after those keys), so tests/frontends/test_cli_screen.py can check
//! frames without a terminal and test_ui_spec.py the attention list against the web page's.
mod net;
mod screen;
#[cfg(unix)]
mod term;

// ../../../shared: the JSON parser and render(), the plain screen the server's /text answers with
use platmon_shared::{json, render};

use json::Value;
use net::Error;
use std::collections::HashMap;
use std::io::{BufRead, Write};
use std::time::{Duration, Instant};

const PORT: u16 = 9797;

/// The application version: the repository's VERSION file, read at build time, as platmon-server's.
pub fn version() -> &'static str {
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../VERSION")).trim()
}

const USAGE: &str = "usage: platmon [-h] [--once]
               [--view {overview,resources,network,storage,thermal,power,system}]
               [--version]
               [host] [interval]
";

/// argparse's help, as cli.py prints it at 80 columns.
const HELP: &str = "
Live view of a platmon service (this device by default).

positional arguments:
  host                  host[:port], default localhost:9797
  interval              seconds between updates, default 1

options:
  -h, --help            show this help message and exit
  --once                print one snapshot without clearing the screen, then
                        exit
  --view {overview,resources,network,storage,thermal,power,system}
                        the live view's first view (default overview); switch
                        with 1-7, Tab or the arrow keys. Interactive terminals
                        only
  --version             show program's version number and exit

The service runs in the background: systemctl status platmon checks it (from a
checkout: deploy/systemd/install.sh or deploy/docker/start.sh). In the live
view 1-7, Tab or the arrow keys switch views and q quits. NO_COLOR=1 turns
colors off; PLATMON_ASCII=1 draws with ASCII only.
";

/// Text for stdout or stderr as Python's text streams write it: on Windows "\r\n" line ends, and in the
/// code page when it is not UTF-8 (a pipe or a file), a character it lacks as "?".
fn encoded(text: &str, utf8: bool) -> Vec<u8> {
    #[cfg(windows)]
    {
        let text = text.replace('\n', "\r\n");
        if utf8 { text.into_bytes() } else { win::to_code_page(&text) }
    }
    #[cfg(not(windows))]
    {
        let _ = utf8; // the text stays UTF-8 (stdout_utf8())
        text.as_bytes().to_vec()
    }
}

/// print(): the text and a line end, flushed; a closed pipe ends the command (cli.py: BrokenPipeError).
fn print(text: &str, utf8: bool) {
    let mut o = std::io::stdout().lock();
    if o.write_all(&encoded(&format!("{}\n", text), utf8)).and_then(|_| o.flush()).is_err() {
        std::process::exit(1);
    }
}

fn fail(code: i32, text: &str) -> ! {
    let _ = std::io::stderr().write_all(&encoded(&format!("{}\n", text), true));
    std::process::exit(code);
}

fn usage_error(message: &str) -> ! {
    fail(2, &format!("{}platmon: error: {}", USAGE, message))
}

struct Args {
    host: String,
    interval: f64,
    once: bool,
    view: Option<String>,
}

/// Python's float() of a command-line string: surrounding blanks and digit-separating underscores allowed.
fn py_float(text: &str) -> Option<f64> {
    let t = text.trim();
    let b = t.as_bytes();
    if b.iter().enumerate().any(|(i, c)| *c == b'_' && !(i > 0 && b[i - 1].is_ascii_digit() && b.get(i + 1).is_some_and(u8::is_ascii_digit))) {
        return None;
    }
    t.replace('_', "").parse().ok()
}

/// argparse's reading of cli.py's options: abbreviated long options, errors in the order met. The
/// positionals as Python 3.13's argparse takes them, on either side of an option (`platmon HOST --once 2`);
/// 3.12.3, Ubuntu 24.04's, left that 2 unrecognized.
fn parse_args(args: &[String]) -> Args {
    const LONG: [&str; 4] = ["--help", "--once", "--view", "--version"];
    let views = screen::views();
    let negative = |a: &str| a.len() > 1 && a.as_bytes()[0] == b'-' && {
        let r = &a[1..];
        r.bytes().all(|c| c.is_ascii_digit()) || r.split_once('.').is_some_and(|(i, f)| i.bytes().all(|c| c.is_ascii_digit()) && !f.is_empty() && f.bytes().all(|c| c.is_ascii_digit()))
    };
    // Each argument: Some((option, explicit value)) for an option (option "" when unknown), None for a positional.
    let mut kinds: Vec<Option<(&str, Option<String>)>> = Vec::new();
    let mut rest = false;
    for a in args {
        if rest || a == "--" || !a.starts_with('-') || a == "-" {
            rest |= a == "--";
            kinds.push(None);
            continue;
        }
        let (name, value) = match a.split_once('=') { Some((n, v)) if a.starts_with("--") => (n, Some(v.to_string())), _ => (a.as_str(), None) };
        if a == "-h" || name == "-h" {
            kinds.push(Some(("--help", value)));
            continue;
        }
        if let Some(v) = a.strip_prefix("-h") { // -hx: -h with an explicit value
            kinds.push(Some(("--help", Some(v.to_string()))));
            continue;
        }
        let matches: Vec<&str> = if a.starts_with("--") {
            LONG.iter().copied().filter(|o| *o == name).chain(LONG.iter().copied().filter(|o| o.starts_with(name) && *o != name)).collect()
        } else { vec![] };
        let exact = matches.first().filter(|o| **o == name);
        match (exact, matches.len()) {
            (Some(o), _) => kinds.push(Some((o, value))),
            (None, 1) => kinds.push(Some((matches[0], value))),
            (None, 0) if negative(a) || a.contains(' ') => kinds.push(None),
            (None, 0) => kinds.push(Some(("", None))),
            _ => usage_error(&format!("ambiguous option: {} could match {}", a, matches.join(", "))),
        }
    }
    let mut out = Args { host: "localhost".into(), interval: 1.0, once: false, view: None };
    let (mut extras, mut positionals, mut marker, mut i) = (Vec::new(), 0, false, 0);
    while i < args.len() {
        let Some((option, value)) = &kinds[i] else {
            let a = &args[i];
            i += 1;
            if a == "--" && !marker { // the first "--" only marks the end of the options
                marker = true;
            } else if positionals == 0 {
                out.host = a.clone();
                positionals = 1;
            } else if positionals == 1 {
                out.interval = py_float(a).unwrap_or_else(|| usage_error(&format!("argument interval: invalid float value: '{}'", a)));
                positionals = 2;
            } else {
                extras.push(a.clone());
            }
            continue;
        };
        let ignored = |name: &str, v: &str| usage_error(&format!("argument {}: ignored explicit argument '{}'", name, v));
        match *option {
            "--help" => {
                if let Some(v) = value { ignored("-h/--help", v) }
                print(&format!("{}{}", USAGE, HELP).trim_end_matches('\n').to_string(), true);
                std::process::exit(0);
            }
            "--version" => {
                if let Some(v) = value { ignored("--version", v) }
                print(&format!("platmon {}", version()), true);
                std::process::exit(0);
            }
            "--once" => {
                if let Some(v) = value { ignored("--once", v) }
                out.once = true;
            }
            "--view" => {
                let v = match value {
                    Some(v) => v.clone(),
                    None if i + 1 < args.len() && kinds[i + 1].is_none() && args[i + 1] != "--" => { i += 1; args[i].clone() }
                    None => usage_error("argument --view: expected one argument"),
                };
                if !views.contains(&v) {
                    let choices: Vec<String> = views.iter().map(|v| format!("'{}'", v)).collect();
                    usage_error(&format!("argument --view: invalid choice: '{}' (choose from {})", v, choices.join(", ")));
                }
                out.view = Some(v);
            }
            _ => extras.push(args[i].clone()),
        }
        i += 1;
    }
    if !extras.is_empty() {
        usage_error(&format!("unrecognized arguments: {}", extras.join(" ")));
    }
    if !(out.interval.is_finite() && out.interval > 0.0) { // nan and inf would never sleep right
        usage_error("interval must be a number greater than 0");
    }
    if out.once && out.view.is_some() { // --once prints every section, so there is no view to choose
        usage_error("--view selects a view of the live screen; --once prints every section");
    }
    out
}

/// Whether stdout takes UTF-8 (cli.py: sys.stdout.encoding). Unix: the locale's character set, UTF-8
/// for the C and POSIX locales as Python coerces them; the text stays UTF-8 either way, only the glyphs
/// change. Windows: a console always (Python writes it as Unicode); a pipe or a file in the code page.
fn stdout_utf8() -> bool {
    #[cfg(unix)]
    {
        let v = ["LC_ALL", "LC_CTYPE", "LANG"].iter().filter_map(|k| std::env::var(k).ok()).find(|v| !v.is_empty()).unwrap_or_default();
        if v.is_empty() || v == "C" || v == "POSIX" {
            return true;
        }
        let codeset = v.split_once('.').map_or("", |(_, c)| c.split('@').next().unwrap_or(""));
        codeset.to_ascii_lowercase().replace('-', "") == "utf8"
    }
    #[cfg(windows)]
    {
        win::stdout_utf8()
    }
}

fn stdout_isatty() -> bool {
    #[cfg(unix)]
    { term::stdout_isatty() }
    #[cfg(windows)]
    { std::io::IsTerminal::is_terminal(&std::io::stdout()) }
}

#[cfg(windows)]
mod win {
    use windows_sys::Win32::Globalization::{GetACP, WideCharToMultiByte, CP_ACP, WC_NO_BEST_FIT_CHARS};

    pub fn stdout_utf8() -> bool {
        // SAFETY: GetACP takes nothing and returns the ANSI code page.
        std::io::IsTerminal::is_terminal(&std::io::stdout()) || unsafe { GetACP() } == 65001
    }

    /// The text in the ANSI code page, a character it lacks as its default character ("?").
    pub fn to_code_page(text: &str) -> Vec<u8> {
        let wide: Vec<u16> = text.encode_utf16().collect();
        if wide.is_empty() {
            return vec![];
        }
        // SAFETY: lengths given explicitly; the second call writes at most n bytes into a buffer of n.
        unsafe {
            let n = WideCharToMultiByte(CP_ACP, WC_NO_BEST_FIT_CHARS, wide.as_ptr(), wide.len() as i32, std::ptr::null_mut(), 0,
                                        std::ptr::null(), std::ptr::null_mut());
            let mut buf = vec![0u8; n.max(0) as usize];
            WideCharToMultiByte(CP_ACP, WC_NO_BEST_FIT_CHARS, wide.as_ptr(), wide.len() as i32, buf.as_mut_ptr(), n, std::ptr::null(),
                                std::ptr::null_mut());
            buf
        }
    }

    unsafe extern "system" fn ctrl_c(_: u32) -> windows_sys::core::BOOL {
        std::process::exit(0)
    }

    /// Ctrl+C: quiet, exit 0, as cli.py's KeyboardInterrupt.
    pub fn exit_on_interrupt() {
        // SAFETY: registers a handler routine with the console.
        unsafe { windows_sys::Win32::System::Console::SetConsoleCtrlHandler(Some(ctrl_c), 1) };
    }
}

fn exit_on_interrupt() {
    #[cfg(unix)]
    term::exit_on_interrupt();
    #[cfg(windows)]
    win::exit_on_interrupt();
}

/// aged(): an /api/observations answer as of seconds after it arrived: each observation's age grows by
/// that much, and one past its group's stale_after_ms is stale.
fn aged(body: &Value, seconds: f64) -> Value {
    let Some(Value::Obj(groups)) = body.get("groups") else { return body.clone() };
    let mut out = body.clone();
    let mut new = Value::obj();
    for (name, g) in groups {
        let mut g = g.clone();
        if let Some(o) = g.get("observation").filter(|o| matches!(o, Value::Obj(_))).cloned() {
            if let Some(age) = o.get("data_age_ms").and_then(|a| render::num(a).ok()) {
                let age = age + 1000.0 * seconds;
                let stale = render::truthy(o.get("stale")) || g.get("stale_after_ms").and_then(|t| render::num(t).ok()).is_some_and(|t| age > t);
                let state = if stale { Value::from("stale") } else { g.get("state").cloned().unwrap_or_default() };
                let mut o = o;
                o.set("data_age_ms", Value::Float(age));
                o.set("stale", Value::Bool(stale));
                g.set("state", state);
                g.set("observation", o);
            }
        }
        new.set(name, g);
    }
    out.set("groups", new);
    out
}

/// /api/observations at most every 10 s (it changes on its own, slower, cadence); none for servers
/// without it, or when it cannot be read: the main view goes on.
struct Observations {
    at: Option<Instant>,
    body: Option<Value>,
    supported: bool,
}

impl Observations {
    fn get(&mut self, addr: &str) -> Option<Value> {
        let now = Instant::now();
        if self.supported && self.at.map_or(true, |at| now - at >= Duration::from_secs(10)) {
            self.at = Some(now);
            self.body = match net::get_json(addr, "/api/observations") {
                Ok(v) => Some(v),
                Err(Error::Http(code, _)) => { self.supported = code != 404; None } // an older server: do not ask again
                Err(_) => None,
            };
        }
        let body = self.body.as_ref().filter(|b| render::truthy(Some(b)))?;
        Some(aged(body, (now - self.at?).as_secs_f64()))
    }
}

/// (the /api/stats answer, the observations or None).
fn fetch(addr: &str, obs: &mut Observations) -> Result<(Value, Option<Value>), Error> {
    let stats = net::get_json(addr, "/api/stats")?;
    Ok((stats, obs.get(addr)))
}

/// render() for this stdout; an answer it cannot draw ends the command, as cli.py's exception does.
fn plain_screen(addr: &str, stats: &Value, obs: Option<&Value>, utf8: bool) -> String {
    let text = render::render(stats, obs).unwrap_or_else(|_| fail(1, &format!("platmon: cannot draw the answer of {}: not the shape of /api/stats", addr)));
    if utf8 { text } else { text.replace('█', "#") }
}

fn explain(addr: &str, e: &Error) -> String {
    if let Error::Fatal(line) = e {
        fail(1, line);
    }
    net::explain(addr, e, cfg!(windows))
}

/// data_age(): seconds since the data was read: the server's data_age_ms plus the time since it arrived.
#[cfg(unix)]
fn data_age(s: &Value, got: Instant, now: Instant) -> f64 {
    let age = s.get("sample").and_then(|x| x.get("data_age_ms")).and_then(|a| render::num(a).ok()).filter(|a| *a >= 0.0);
    age.map_or(0.0, |a| a / 1000.0) + now.saturating_duration_since(got).as_secs_f64()
}

/// The live view on an interactive terminal: one request per interval, nothing in between; keys switch
/// the view without asking the service for anything more.
#[cfg(unix)]
fn live(addr: &str, interval: f64, obs_cache: &mut Observations, first: (Value, Option<Value>), view: String, lim: &screen::Limits) {
    let st = screen::make_style(std::env::var("NO_COLOR").ok().as_deref(), std::env::var("PLATMON_ASCII").ok().as_deref(), stdout_utf8());
    let keep = (10.0 / interval).round_ties_even().max(1.0) as usize;
    use std::collections::VecDeque;
    let mut traffic: HashMap<String, VecDeque<f64>> = HashMap::new(); // about 10 s of rx + tx per interface
    let remember = |s: &Value, traffic: &mut HashMap<String, VecDeque<f64>>| {
        for i in s.get("network").and_then(|n| n.get("interfaces")).and_then(|i| if let Value::Arr(a) = i { Some(a) } else { None }).into_iter().flatten() {
            if let Some(r) = i.get("rates").filter(|r| render::truthy(Some(r))) {
                let q = traffic.entry(render::s(&i["name"])).or_default();
                if q.len() == keep { q.pop_front(); }
                q.push_back(render::num(&r["rx_bytes_per_s"]).unwrap_or(0.0) + render::num(&r["tx_bytes_per_s"]).unwrap_or(0.0));
            }
        }
    };
    let ((mut s, mut obs), mut got, mut error) = (first, Instant::now(), None::<String>);
    let (mut view, mut offset) = (view, 0i64);
    let mut page: i64;
    remember(&s, &mut traffic);
    let mut term = term::Terminal::enter().unwrap_or_else(|e| fail(1, &format!("platmon: {}", e)));
    let step = Duration::try_from_secs_f64(interval).unwrap_or(Duration::from_secs(1 << 31));
    let mut deadline = Instant::now() + step;
    loop {
        let (cols, rows) = term::size();
        let state = if error.is_some() { "offline" } else if !render::collection_note(&s).is_empty() { "partial" } else { "live" };
        let status = screen::Status { state: state.into(), age: data_age(&s, got, Instant::now()), error: error.clone(), addr: addr.into(), interval,
                                      traffic: traffic.iter().map(|(k, v)| (k.clone(), v.iter().sum::<f64>() / v.len() as f64)).collect() };
        // cols - 1: never wraps the last column
        let (lines, scroll) = screen::screen(&s, obs.as_ref(), cols.saturating_sub(1), rows, &st, &status, &view, offset, lim);
        term.draw(lines);
        (offset, page) = scroll; // what was drawn, so scrolling starts from what is on screen
        match term.wait(deadline.saturating_duration_since(Instant::now()).as_secs_f64()) {
            term::Event::Resize => continue,
            term::Event::Keys(keys) => {
                if keys.to_lowercase().contains('q') {
                    return;
                }
                let moved = screen::next_view(&keys, &view);
                offset = if moved != view { 0 } else { screen::next_offset(&keys, offset, page) };
                view = moved;
                continue;
            }
            term::Event::Tick => {}
        }
        match fetch(addr, obs_cache) {
            Ok((stats, o)) => {
                (s, obs, got, error) = (stats, o, Instant::now(), None);
                remember(&s, &mut traffic);
            }
            Err(Error::Fatal(line)) => { // what cli.py does not catch: the terminal restored first, as its with block does
                drop(term);
                fail(1, &line);
            }
            Err(e) => error = Some(explain(addr, &e)), // lost while watching: keep the last data, keep retrying
        }
        deadline += step;
        if deadline < Instant::now() { // a slow answer: start counting again, never catch up in bursts
            deadline = Instant::now() + step;
        }
    }
}

/// The hidden --screen-script (see the top of this file).
fn screen_script() {
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let line = line.unwrap_or_default();
        if line.trim().is_empty() { continue }
        let r = json::parse(&line).unwrap_or_else(|e| fail(2, &format!("platmon --screen-script: {}", e)));
        let num = |v: &Value| render::num(v).unwrap_or(0.0);
        let mut out = Value::obj();
        if let Some(keys) = r.get("keys") { // {"keys", "view", "offset", "page"}: {"view", "offset"} after them
            let keys = render::s(keys);
            out.set("view", Value::Str(screen::next_view(&keys, &render::s(&r["view"]))));
            out.set("offset", Value::Int(screen::next_offset(&keys, num(&r["offset"]) as i64, num(&r["page"]) as i64)));
            print(&out.to_py(), true);
            continue;
        }
        let st = screen::Style::new(r["color"] != Value::Bool(false), r["utf8"] != Value::Bool(false));
        let mut lim = screen::Limits::new();
        lim.use_levels(r.get("levels"));
        let q = &r["status"];
        let traffic = match q.get("traffic") { Some(Value::Obj(t)) => t.iter().map(|(k, v)| (k.clone(), num(v))).collect(), _ => HashMap::new() };
        let status = screen::Status { state: render::s(&q["state"]), age: num(&q["age"]), error: q.get("error").filter(|e| !e.is_null()).map(render::s),
                                      addr: render::s(&q["addr"]), interval: num(&q["interval"]), traffic };
        let obs = r.get("obs").filter(|o| !o.is_null());
        let view = r.get("view").map_or("overview".into(), render::s);
        let (lines, (o, p)) = screen::screen(&r["stats"], obs, num(&r["width"]) as usize, num(&r["height"]) as usize, &st, &status, &view,
                                             num(&r["offset"]) as i64, &lim);
        out.set("lines", Value::Arr(lines.into_iter().map(Value::Str).collect()));
        out.set("scroll", Value::Arr(vec![Value::Int(o), Value::Int(p)]));
        let alerts = screen::attention(&r["stats"], obs, &lim).into_iter()
            .map(|(name, value, lv, _)| Value::Arr(vec![Value::Str(name), Value::Str(value), Value::from(lv)])).collect();
        out.set("attention", Value::Arr(alerts));
        print(&out.to_py(), true);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--screen-script"] {
        return screen_script();
    }
    let a = parse_args(&args);
    let addr = if a.host.contains(':') { a.host.clone() } else { format!("{}:{}", a.host, PORT) };
    let mut obs = Observations { at: None, body: None, supported: true };
    let utf8 = stdout_utf8();
    // not running / unreachable: say so once instead of retrying forever
    let first = fetch(&addr, &mut obs).unwrap_or_else(|e| fail(1, &explain(&addr, &e)));
    if a.once {
        return print(&plain_screen(&addr, &first.0, first.1.as_ref(), utf8), utf8);
    }
    exit_on_interrupt();
    #[cfg(unix)]
    if term::interactive() {
        let mut lim = screen::Limits::new();
        if let Ok(status) = net::get_json(&addr, "/api/status") { // the server's [thresholds]; read once
            lim.use_levels(status.get("levels"));
        }
        return live(&addr, a.interval, &mut obs, first, a.view.unwrap_or_else(|| "overview".into()), &lim);
    }
    // not a terminal (a pipe, a log, Windows): the plain screen. A console redraws it in place; a pipe or a
    // log gets no escapes, a blank line between snapshots.
    let clear = if stdout_isatty() { "\x1b[H\x1b[J" } else { "" };
    let (mut lead, mut out) = (clear, plain_screen(&addr, &first.0, first.1.as_ref(), utf8));
    let step = Duration::try_from_secs_f64(a.interval).unwrap_or(Duration::from_secs(1 << 31));
    loop {
        print(&format!("{}{}", lead, out), utf8);
        lead = if clear.is_empty() { "\n" } else { clear };
        std::thread::sleep(step);
        out = match fetch(&addr, &mut obs) {
            Ok((stats, o)) => plain_screen(&addr, &stats, o.as_ref(), utf8),
            Err(e) => format!("{}\n(retrying every {}s, Ctrl+C to quit)", explain(&addr, &e), screen::g(a.interval)),
        };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_as_python_reads_them() {
        assert_eq!(py_float(" 2 "), Some(2.0));
        assert_eq!(py_float("1_0"), Some(10.0));
        assert_eq!(py_float("1__0"), None);
        assert!(py_float("nan").unwrap().is_nan() && py_float("inf") == Some(f64::INFINITY));
        assert_eq!(py_float("x"), None);
    }

    #[test]
    fn version_is_the_version_file() {
        assert!(version().split('.').count() == 3);
    }

    #[test]
    fn aged_grows_ages_and_crosses_the_stale_threshold() {
        let body = json::parse(r#"{"groups": {"wifi": {"state": "ok", "stale_after_ms": 15000, "observation": {"data_age_ms": 14000, "stale": false}}}}"#).unwrap();
        let now = aged(&body, 2.0);
        assert_eq!(now["groups"]["wifi"]["observation"]["stale"], Value::Bool(true));
        assert_eq!(now["groups"]["wifi"]["state"], Value::from("stale"));
        assert_eq!(body["groups"]["wifi"]["observation"]["stale"], Value::Bool(false));
    }

    #[cfg(unix)]
    #[test]
    fn data_age_is_the_servers_plus_time_since() {
        let got = Instant::now();
        let at = |ms: u64| got + Duration::from_millis(ms);
        assert_eq!(data_age(&json::parse(r#"{"sample": {"data_age_ms": 1500}}"#).unwrap(), got, at(2000)), 3.5);
        assert_eq!(data_age(&Value::obj(), got, at(2000)), 2.0); // an older server: the time since its answer
        assert_eq!(data_age(&json::parse(r#"{"sample": {"data_age_ms": null}}"#).unwrap(), got, at(500)), 0.5);
    }
}
