//! The platmon command as a user runs it, against a stand-in server on a local port: what
//! tests/frontends/test_cli.py checked of frontends/cli.py, kept when cli.py was removed. render() itself is
//! tested in shared/src/render.rs, the live screen by tests/frontends/test_cli_screen.py.
use platmon_shared::{json::parse, render::render};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Command, Output, Stdio};

const FULL: &str = r#"{"platform": "Jetson Orin", "model": "Test Board", "uptime": 90061, "power_mode": "MAXN_SUPER",
    "system": {"os": "Ubuntu 22.04.5 LTS", "kernel": "5.15.199-tegra", "arch": "aarch64", "hostname": "orin", "l4t": "R36.5.2"},
    "cpu": [{"id": 0, "usage": 12.5, "freq": 1728000000}, {"id": 2, "usage": 0.0, "freq": 729600000}],
    "gpu": {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000},
    "memory": {"total": 8589934592, "used": 2147483648, "swap_total": 4294967296, "swap_used": 0},
    "disk": {"total": 107374182400, "used": 26843545600},
    "temperature": {"cpu": 51.2}, "power": {"VDD_IN": 4.82},
    "fans": [{"name": "pwmfan", "rpm": null, "percent": 31}, {"name": "pwm_tach", "rpm": 1741, "percent": null}]}"#;

fn answer(mut c: TcpStream, stats: Option<&str>) {
    let mut request = Vec::new();
    let mut buf = [0u8; 1024];
    while !request.windows(4).any(|w| w == b"\r\n\r\n") {
        match c.read(&mut buf) {
            Ok(0) | Err(_) => return,
            Ok(n) => request.extend_from_slice(&buf[..n]),
        }
    }
    let path = String::from_utf8_lossy(&request).split(' ').nth(1).unwrap_or("").to_string();
    let (status, body) = match (path.as_str(), stats) {
        ("/api/stats", Some(s)) => ("200 OK", s.to_string()),
        ("/api/stats", None) => ("503 Service Unavailable", r#"{"error": "no current data", "code": "no_current_data"}"#.to_string()),
        _ => ("404 Not Found", String::new()), // an older server for /api/observations
    };
    let _ = write!(c, "HTTP/1.0 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}", status, body.len(), body);
}

/// A server that answers /api/stats with stats (None: 503, no current data) and nothing else (404); its
/// host:port.
fn serve(stats: Option<&'static str>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    std::thread::spawn(move || {
        for c in listener.incoming().flatten() {
            std::thread::spawn(move || answer(c, stats));
        }
    });
    addr
}

fn platmon(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_platmon")).args(args).output().unwrap()
}

fn text(b: &[u8]) -> String {
    String::from_utf8_lossy(b).replace("\r\n", "\n")
}

fn screen(stats: &str) -> String {
    render(&parse(stats).unwrap(), None).unwrap()
}

#[test]
fn once_prints_one_snapshot() {
    let r = platmon(&["--once", &serve(Some(FULL))]);
    assert!(r.status.success(), "{}", text(&r.stderr));
    assert_eq!(text(&r.stdout), screen(FULL) + "\n"); // no screen clearing
}

/// A stdout that does not take UTF-8: the bars are "#". On Unix the rest stays UTF-8 (the locale only
/// changes the glyphs); on Windows a pipe is in the code page, a character it lacks "?".
#[test]
fn once_without_utf8_does_not_crash() {
    const BOARD: &str = r#"{"platform": "Jetson Orin", "model": "Board अ", "uptime": 90061, "power_mode": null, "cpu": [],
        "gpu": null, "memory": {"total": 8589934592, "used": 2147483648, "swap_total": 0, "swap_used": 0},
        "disk": {"total": 107374182400, "used": 26843545600}, "temperature": {"cpu": 51.2}, "power": {}, "fans": []}"#;
    let mut command = Command::new(env!("CARGO_BIN_EXE_platmon"));
    command.args(["--once", &serve(Some(BOARD))]);
    #[cfg(unix)]
    command.env("LC_ALL", "en_US.ISO-8859-1");
    let r = command.output().unwrap();
    assert!(r.status.success(), "{}", text(&r.stderr));
    #[cfg(unix)]
    assert_eq!(text(&r.stdout), screen(BOARD).replace('█', "#") + "\n");
    #[cfg(windows)]
    {
        let out = &r.stdout;
        assert!(out.windows(9).any(|w| w == b"Board ?\r\n") && out.windows(11).any(|w| w == b"RAM   #####"), "{:?}", out);
    }
}

#[test]
fn pipe_gets_snapshots_without_escapes() {
    // repeated snapshots to a pipe or a log: plain text, a blank line between them, no screen clearing
    let mut child = Command::new(env!("CARGO_BIN_EXE_platmon")).args([&serve(Some(FULL)), "0.05"]).stdout(Stdio::piped()).spawn().unwrap();
    let one = screen(FULL);
    let mut out = BufReader::new(child.stdout.take().unwrap());
    let mut got = String::new();
    for _ in 0..2 * one.split('\n').count() + 1 {
        out.read_line(&mut got).unwrap();
    }
    let _ = child.kill();
    let _ = child.wait();
    assert_eq!(got.replace("\r\n", "\n"), format!("{one}\n\n{one}\n"));
}

#[test]
fn view_only_selects_a_live_view() {
    // --view picks the live screen's first view; --once prints every section, so the two do not mix
    let bad = platmon(&["--once", "--view", "network"]);
    assert_eq!(bad.status.code(), Some(2));
    assert!(text(&bad.stderr).contains("--view selects a view of the live screen"));
    let unknown = platmon(&["--view", "cpu"]);
    assert!(unknown.status.code() == Some(2) && text(&unknown.stderr).contains("invalid choice"));
    assert!(text(&platmon(&["--help"]).stdout).contains("1-7")); // the keys are documented where the options are
}

#[test]
fn not_running_explains_and_exits() {
    let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port(); // nothing listens there now
    let r = platmon(&[&format!("localhost:{port}")]); // without --once too: fail once, no endless retry loop
    assert_eq!(r.status.code(), Some(1));
    let err = text(&r.stderr);
    let lines: Vec<&str> = err.lines().collect();
    assert!(lines[0].contains(&format!("platmon is not running on localhost:{port}")), "{err}");
    if cfg!(windows) { // the service control manager's and the event log's
        assert!(lines[1].starts_with("  check the service: sc.exe query"), "{err}");
    } else {
        assert!(lines[1].starts_with("  check the service: systemctl status"), "{err}"); // what a package install has
        assert!(err.contains("deploy/docker/start.sh")); // and a checkout's way still
    }
}

#[test]
fn no_current_data_is_explained() {
    let r = platmon(&["--once", &serve(None)]); // service up, but no snapshot: 503
    let err = text(&r.stderr);
    assert!(r.status.code() == Some(1) && err.contains("answered 503") && err.contains("no current data"), "{err}");
}

#[test]
fn bad_interval_is_a_usage_error() {
    for interval in ["0", "-1", "nan", "inf"] {
        let r = platmon(&["localhost", interval]); // refused before connecting or sleeping
        assert!(r.status.code() == Some(2) && text(&r.stderr).contains("interval must be a number greater than 0"), "{interval}");
    }
}

#[test]
fn version_is_the_version_file() {
    let version = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../VERSION")).trim();
    assert_eq!(text(&platmon(&["--version"]).stdout), format!("platmon {version}\n"));
}
