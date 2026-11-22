//! platmon-tray: a platmon service's state as a tray icon, this device's by default (docs/guides/interfaces.md,
//! Tray). Read-only: it asks the HTTP API, never controls the service.
//!
//!   platmon-tray [host[:port]] [--autostart | --no-autostart] [--version] [--help]   (default localhost:9797)
//!
//! Every 10 s /api/status and /api/stats, every 60 s /api/observations; the levels from /api/status, the
//! attention list and the readings from the command's code (../../screen.rs). Not reachable when the service
//! does not answer or is stale or starting; critical or warning from attention(), warning also when degraded.
//! A click opens the web page. Linux: a StatusNotifierItem over D-Bus (sni.rs, dbus.rs); Windows: a
//! notification-area icon (win.rs).
//!
//! Exit codes: 0 --help, --version, --autostart and --no-autostart done; 1 a failure (no session bus, the bus
//! lost, the window or the autostart entry not written); 2 a usage error; 3 (Linux) no StatusNotifierWatcher on
//! the session bus, so nothing could show the icon.
#![cfg_attr(windows, windows_subsystem = "windows")]

#[cfg(unix)]
mod dbus;
#[cfg(unix)]
mod sni;
#[cfg(windows)]
mod win;

use platmon_frontend::json::Value;
use platmon_frontend::net::{self, Error};
use platmon_frontend::{render, screen, version};
use std::time::{Duration, Instant};

const PORT: u16 = 9797;
const STATS_EVERY: Duration = Duration::from_secs(10);
const OBSERVATIONS_EVERY: Duration = Duration::from_secs(60);
/// What Windows's tooltip holds: 128 UTF-16 units with the terminator.
pub const TIP_UNITS: usize = 127;

const USAGE: &str = "usage: platmon-tray [-h] [--version] [--autostart | --no-autostart] [host]";
const HELP: &str = "
A platmon service's state as a tray icon: ok, warning, critical or not reachable,
with the readings or what needs attention as its tooltip; a click opens the web
page. Read-only.

positional arguments:
  host            host[:port], default localhost:9797

options:
  -h, --help      show this help message and exit
  --version       show program's version number and exit
  --autostart     start the tray at login (for this user, with this host), then
                  exit
  --no-autostart  remove that login entry, then exit

Linux: shown by desktops with StatusNotifierItem support (KDE, XFCE, Cinnamon;
GNOME with the AppIndicator extension). man platmon-tray has more.";

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Level {
    Ok,
    Warning,
    Critical,
    Down,
}

impl Level {
    fn word(self) -> &'static str {
        match self {
            Level::Ok => "ok",
            Level::Warning => "warning",
            Level::Critical => "critical",
            Level::Down => "not reachable",
        }
    }
}

/// What the icon shows: its level, a title (the host and the state) and the tooltip's text.
#[derive(Clone, Debug, PartialEq)]
pub struct Shown {
    pub level: Level,
    pub title: String,
    pub text: String,
}

impl Shown {
    /// Title and text as one tooltip of at most units UTF-16 units.
    #[cfg_attr(not(any(windows, test)), allow(dead_code))]
    pub fn tip(&self, units: usize) -> String {
        fit_utf16(&format!("{}\n{}", self.title, self.text), units)
    }
}

/// text cut to at most units UTF-16 units, whole characters only, an ellipsis where it was cut.
#[cfg_attr(not(any(windows, test)), allow(dead_code))]
fn fit_utf16(text: &str, units: usize) -> String {
    if text.encode_utf16().count() <= units {
        return text.to_string();
    }
    let (mut out, mut n) = (String::new(), 1); // 1: the ellipsis
    for c in text.chars() {
        n += c.len_utf16();
        if n > units { break }
        out.push(c);
    }
    out + "…"
}

fn down(addr: &str, why: String) -> Shown {
    Shown { level: Level::Down, title: format!("{} — {}", addr, Level::Down.word()), text: why }
}

/// Before the first answer.
pub fn waiting(addr: &str) -> Shown {
    down(addr, "waiting for the first answer".into())
}

/// CPU 23% · RAM 41% · 52°C: the mean CPU, the memory and the hottest temperature, as the readings have them.
fn readings(stats: &Value, lim: &screen::Limits) -> String {
    let parts: Vec<String> = screen::metrics(stats, lim).into_iter().filter_map(|m| {
        let v = m.value?;
        match (m.label.as_str(), m.unit) {
            ("CPU" | "RAM", "%") => Some(format!("{} {:.0}%", m.label, v)),
            (_, "°C") => Some(format!("{:.0}°C", v)),
            _ => None,
        }
    }).collect();
    if parts.is_empty() { "no readings".into() } else { parts.join(" · ") }
}

/// The icon for an answering service: its state ("ready", "degraded"), its levels, a snapshot and the
/// observations (or none).
fn assess(addr: &str, state: &str, levels: Option<&Value>, stats: &Value, obs: Option<&Value>) -> Shown {
    let mut lim = screen::Limits::new();
    lim.use_levels(levels);
    let alerts = screen::attention(stats, obs, &lim);
    let level = if alerts.iter().any(|a| a.2 == "crit") { Level::Critical }
                else if !alerts.is_empty() || state == "degraded" { Level::Warning }
                else { Level::Ok };
    let name = match stats.get("system").and_then(|s| s.get("hostname")) {
        Some(Value::Str(h)) if !h.is_empty() => h.clone(),
        _ => addr.to_string(),
    };
    let text = if level == Level::Ok {
        readings(stats, &lim)
    } else if alerts.is_empty() {
        let note = render::collection_note(stats);
        if note.is_empty() { "Collection degraded: see the service log".into() } else { note }
    } else {
        let mut lines: Vec<String> = alerts.iter().take(3).map(|(name, value, _, _)| format!("{} {}", name, value)).collect();
        if alerts.len() > 3 {
            lines.push(format!("and {} more", alerts.len() - 3));
        }
        lines.join("\n")
    };
    Shown { level, title: format!("{} — {}", name, level.word()), text }
}

/// /api/observations at most every 60 s; never again after a 404 (a server without it).
struct Observations {
    at: Option<Instant>,
    body: Option<Value>,
    supported: bool,
}

impl Observations {
    fn get(&mut self, addr: &str) -> Option<&Value> {
        // ponytail: ages are the server's as of the last fetch (up to 60 s old); the command ages them (aged())
        if self.supported && self.at.is_none_or(|at| at.elapsed() >= OBSERVATIONS_EVERY) {
            self.at = Some(Instant::now());
            self.body = match net::get_json(addr, "/api/observations") {
                Ok(v) => Some(v),
                Err(Error::Http(code, _)) => { self.supported = code != 404; None }
                Err(_) => None,
            };
        }
        self.body.as_ref()
    }
}

fn why(addr: &str, e: &Error) -> String {
    let text = net::explain(addr, e, cfg!(windows));
    text.lines().next().unwrap_or("").to_string()
}

/// One round: /api/status, then /api/stats and the observations when the service has current data.
fn poll(addr: &str, obs: &mut Observations) -> Shown {
    let status = match net::get_json(addr, "/api/status") {
        Ok(v) => v,
        Err(e) => return down(addr, why(addr, &e)),
    };
    let state = render::s(&status["state"]);
    if state == "stale" || state == "starting" {
        return down(addr, format!("platmon at {} is {}: no current data", addr, state));
    }
    let stats = match net::get_json(addr, "/api/stats") {
        Ok(v) => v,
        Err(e) => return down(addr, why(addr, &e)),
    };
    assess(addr, &state, status.get("levels"), &stats, obs.get(addr))
}

/// Polls forever, calling changed with each new state (the first one included).
pub fn watch(addr: &str, mut changed: impl FnMut(Shown)) -> ! {
    let mut obs = Observations { at: None, body: None, supported: true };
    let mut last: Option<Shown> = None;
    loop {
        let shown = poll(addr, &mut obs);
        if last.as_ref() != Some(&shown) {
            changed(shown.clone());
            last = Some(shown);
        }
        std::thread::sleep(STATS_EVERY);
    }
}

pub fn url(addr: &str) -> String {
    format!("http://{}/", addr)
}

/// Opens the web page in the default browser; a failure is only reported.
pub fn open(url: &str) {
    #[cfg(windows)]
    win::open(url);
    #[cfg(unix)]
    match std::process::Command::new("xdg-open").arg(url).stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).spawn() {
        Ok(mut child) => { std::thread::spawn(move || child.wait()); } // reaped, never a zombie
        Err(e) => eprintln!("platmon-tray: cannot run xdg-open {} ({})", url, e),
    }
}

/// Writes to stdout or stderr; on Windows the tray has no console of its own, so it borrows its parent's.
fn say(text: &str, error: bool) {
    #[cfg(windows)]
    win::attach_console();
    if error { eprintln!("{}", text) } else { println!("{}", text) }
}

fn usage_error(message: &str) -> ! {
    say(&format!("{}\nplatmon-tray: error: {}", USAGE, message), true);
    std::process::exit(2)
}

struct Args {
    host: Option<String>,
    autostart: Option<bool>,
}

fn parse_args(args: &[String]) -> Args {
    let mut out = Args { host: None, autostart: None };
    for a in args {
        match a.as_str() {
            "-h" | "--help" => {
                say(&format!("{}\n{}", USAGE, HELP), false);
                std::process::exit(0);
            }
            "--version" => {
                say(&format!("platmon-tray {}", version()), false);
                std::process::exit(0);
            }
            "--autostart" | "--no-autostart" => {
                let on = a == "--autostart";
                if out.autostart.is_some_and(|o| o != on) {
                    usage_error("--autostart and --no-autostart exclude each other");
                }
                out.autostart = Some(on);
            }
            _ if a.starts_with('-') => usage_error(&format!("unrecognized arguments: {}", a)),
            _ if out.host.is_some() => usage_error(&format!("unrecognized arguments: {}", a)),
            _ => out.host = Some(a.clone()),
        }
    }
    out
}

/// A host for the autostart entry's command line: what a host name, an address and a port are made of, so
/// that it needs no quoting there.
fn plain_host(host: &str) -> bool {
    !host.is_empty() && host.bytes().all(|c| c.is_ascii_alphanumeric() || b".-_:[]%".contains(&c))
}

/// The login entry's command: this executable and the host when one was given.
fn command_line(exe: &str, host: Option<&str>, quote: impl Fn(&str) -> String) -> String {
    let mut line = quote(exe);
    if let Some(h) = host {
        line = format!("{} {}", line, h);
    }
    line
}

/// The XDG autostart file (freedesktop's Desktop Application Autostart Specification).
#[cfg(unix)]
fn autostart_path() -> Result<std::path::PathBuf, String> {
    let env = |k: &str| std::env::var_os(k).filter(|v| !v.is_empty()).map(std::path::PathBuf::from);
    let config = env("XDG_CONFIG_HOME").filter(|p| p.is_absolute()).or_else(|| env("HOME").map(|h| h.join(".config")))
        .ok_or("neither XDG_CONFIG_HOME nor HOME is set")?;
    Ok(config.join("autostart/platmon-tray.desktop"))
}

/// A desktop entry's Exec argument: quoted when it has more than plain path characters, the quoting's
/// backslashes then escaped once more as the string type asks.
#[cfg(unix)]
fn exec_quote(arg: &str) -> String {
    if !arg.is_empty() && arg.bytes().all(|c| c.is_ascii_alphanumeric() || b"/._+-".contains(&c)) {
        return arg.to_string();
    }
    let mut q = String::from("\"");
    for c in arg.chars() {
        if matches!(c, '"' | '`' | '$' | '\\') { q.push('\\') }
        q.push(c);
    }
    (q + "\"").replace('\\', "\\\\")
}

#[cfg(unix)]
fn desktop_entry(exec: &str) -> String {
    format!("[Desktop Entry]\nType=Application\nName=platmon tray\nComment=The platmon service's state in the tray\nExec={}\n\
             Icon=utilities-system-monitor\nTerminal=false\nX-GNOME-Autostart-enabled=true\n", exec)
}

/// --autostart and --no-autostart: (what was done) or why not.
fn autostart(on: bool, host: Option<&str>) -> Result<String, String> {
    let exe = std::env::current_exe().map_err(|e| format!("cannot find this program's path ({})", e))?;
    let exe = exe.to_str().ok_or("this program's path is not UTF-8")?;
    #[cfg(unix)]
    {
        let path = autostart_path()?;
        if on {
            let exec = command_line(exe, host, exec_quote);
            std::fs::create_dir_all(path.parent().unwrap()).and_then(|_| std::fs::write(&path, desktop_entry(&exec)))
                .map_err(|e| format!("cannot write {} ({})", path.display(), e))?;
            Ok(format!("wrote {}: platmon-tray starts at your next login", path.display()))
        } else {
            match std::fs::remove_file(&path) {
                Ok(()) => Ok(format!("removed {}", path.display())),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(format!("{} does not exist; nothing to remove", path.display())),
                Err(e) => Err(format!("cannot remove {} ({})", path.display(), e)),
            }
        }
    }
    #[cfg(windows)]
    {
        win::autostart(on.then(|| command_line(exe, host, |e| format!("\"{}\"", e))).as_deref())
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let a = parse_args(&args);
    if let Some(h) = a.host.as_deref().filter(|h| !plain_host(h)) {
        usage_error(&format!("argument host: not a host name, an address or host:port: '{}'", h));
    }
    if let Some(on) = a.autostart {
        match autostart(on, a.host.as_deref()) {
            Ok(done) => return say(&format!("platmon-tray: {}", done), false),
            Err(e) => { say(&format!("platmon-tray: {}", e), true); std::process::exit(1) }
        }
    }
    let host = a.host.unwrap_or_else(|| "localhost".into());
    let addr = if host.contains(':') { host } else { format!("{}:{}", host, PORT) };
    #[cfg(unix)]
    sni::run(&addr);
    #[cfg(windows)]
    win::run(&addr);
}

#[cfg(test)]
mod tests {
    use super::*;
    use platmon_frontend::json::parse;

    const STATS: &str = r#"{"system": {"hostname": "orin"}, "cpu": [{"usage": 20.0}, {"usage": 26.4}],
        "memory": {"total": 1000, "used": 410}, "disk": {"total": 100, "used": 10}, "temperature": {"cpu": 51.6, "gpu": 48.0}}"#;

    #[test]
    fn ok_shows_the_readings() {
        let s = assess("localhost:9797", "ready", None, &parse(STATS).unwrap(), None);
        assert_eq!(s, Shown { level: Level::Ok, title: "orin — ok".into(), text: "CPU 23% · RAM 41% · 52°C".into() });
    }

    #[test]
    fn levels_come_from_the_server_and_critical_goes_first() {
        let stats = parse(STATS).unwrap();
        // the server's levels: memory warns at 40, cpu is critical at 20
        let levels = parse(r#"{"memory": [40, 95], "cpu": [10, 20], "temperature": null}"#).unwrap();
        let s = assess("h:1", "ready", Some(&levels), &stats, None);
        assert_eq!(s.level, Level::Critical);
        assert_eq!(s.title, "orin — critical");
        assert_eq!(s.text, "CPU 23.2 %\nRAM 41.0 %");
        let s = assess("h:1", "ready", Some(&parse(r#"{"memory": [40, 95]}"#).unwrap()), &stats, None);
        assert_eq!((s.level, s.text.as_str()), (Level::Warning, "RAM 41.0 %"));
    }

    #[test]
    fn degraded_is_a_warning_and_more_than_three_are_counted() {
        let stats = parse(r#"{"cpu": [], "memory": {}, "disk": {}, "collectors": {"gpu": {"state": "error"}}}"#).unwrap();
        let s = assess("h:1", "degraded", None, &stats, None);
        assert_eq!(s, Shown { level: Level::Warning, title: "h:1 — warning".into(), text: "Collection: gpu error".into() });
        let hot = parse(r#"{"cpu": [], "memory": {}, "disk": {}, "temperature": {"a": 91, "b": 92, "c": 85, "d": 95}}"#).unwrap();
        let s = assess("h:1", "ready", None, &hot, None);
        assert_eq!(s.level, Level::Critical);
        assert_eq!(s.text, "d 95.0 °C\nb 92.0 °C\na 91.0 °C\nand 1 more");
    }

    #[test]
    fn observations_feed_the_attention_list() {
        let obs = parse(r#"{"groups": {"battery": {"observation": {"stale": false, "data_age_ms": 1},
            "data": {"batteries": [{"name": "BAT0", "percent": 8}]}}}}"#).unwrap();
        let s = assess("h:1", "ready", None, &parse(STATS).unwrap(), Some(&obs));
        assert_eq!((s.level, s.text.as_str()), (Level::Critical, "BAT0 8 %"));
    }

    #[test]
    fn tooltips_fit_windows() {
        assert_eq!(fit_utf16("abc", 3), "abc");
        assert_eq!(fit_utf16("abcd", 3), "ab…");
        assert_eq!(fit_utf16("a😀b", 3), "a…"); // the emoji's two units do not fit with the ellipsis
        let s = Shown { level: Level::Ok, title: "t".repeat(60), text: "°".repeat(100) };
        let tip = s.tip(TIP_UNITS);
        assert_eq!(tip.encode_utf16().count(), TIP_UNITS);
        assert!(tip.ends_with('…') && tip.starts_with(&format!("{}\n", "t".repeat(60))));
    }

    #[test]
    fn hosts_and_autostart_lines() {
        assert!(plain_host("orin.local:9797") && plain_host("[fe80::1%eth0]:9797"));
        assert!(!plain_host("a b") && !plain_host("") && !plain_host("x;y"));
        assert_eq!(command_line("/usr/bin/platmon-tray", Some("orin:9797"), |e| e.to_string()), "/usr/bin/platmon-tray orin:9797");
        assert_eq!(command_line(r"C:\p\platmon-tray.exe", None, |e| format!("\"{}\"", e)), r#""C:\p\platmon-tray.exe""#);
    }

    #[cfg(unix)]
    #[test]
    fn desktop_entries() {
        assert_eq!(exec_quote("/usr/bin/platmon-tray"), "/usr/bin/platmon-tray");
        assert_eq!(exec_quote("/opt/my tray/$x"), r#""/opt/my tray/\\$x""#);
        let e = desktop_entry("/usr/bin/platmon-tray");
        assert!(e.starts_with("[Desktop Entry]\nType=Application\n") && e.contains("\nExec=/usr/bin/platmon-tray\n"));
    }

    #[test]
    fn arguments() {
        let a = parse_args(&["orin".into(), "--autostart".into()]);
        assert_eq!((a.host.as_deref(), a.autostart), (Some("orin"), Some(true)));
        assert_eq!(parse_args(&[]).autostart, None);
    }
}
