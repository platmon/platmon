//! The requests cli.py made with urllib: GET on a TcpStream, 5 s for each connect, send and receive, the
//! answer read to the end (the server answers HTTP/1.0 and closes). No proxy. Failures are worded as
//! Python words them, since explain() quotes them: "[Errno 111] Connection refused", "timed out",
//! "[Errno -2] Name or service not known", "[WinError 10061] ...".
use crate::json::{parse, Value};
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::time::Duration;

const TIMEOUT: Duration = Duration::from_secs(5);

pub enum Error {
    /// An answer other than 2xx: its code and reason phrase (urllib's HTTPError).
    Http(u16, String),
    /// Anything else cli.py catches (OSError, ValueError): str() of what it caught.
    Failed(String),
    /// What cli.py does not catch (it ends with a traceback): the traceback's last line.
    Fatal(String),
}

/// Python's repr() of a str, enough for host names.
fn repr(t: &str) -> String {
    let mut out = String::from("'");
    for c in t.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\'' => out.push_str("\\'"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out + "'"
}

/// http.client's host and port of the URL's netloc: port 80 without one, the brackets of an IPv6
/// address taken off.
fn host_port(netloc: &str) -> Result<(String, u16), Error> {
    if let Some(c) = netloc.chars().find(|c| (*c as u32) <= 0x20 || *c as u32 == 0x7f) {
        return Err(Error::Fatal(format!("http.client.InvalidURL: URL can't contain control characters. {} (found at least {})",
                                        repr(netloc), repr(&c.to_string()))));
    }
    let (mut host, mut port) = (netloc, 80);
    if let Some(i) = netloc.rfind(':').filter(|i| netloc.rfind(']').map_or(true, |j| *i > j)) {
        let text = &netloc[i + 1..];
        if !text.is_empty() {
            let p: i64 = text.trim().parse().map_err(|_| Error::Fatal(format!("http.client.InvalidURL: nonnumeric port: '{}'", text)))?;
            port = u16::try_from(p).map_err(|_| Error::Fatal("OverflowError: getsockaddrarg: port must be 0-65535.".into()))?;
        }
        host = &netloc[..i];
    }
    if host.len() >= 2 && host.starts_with('[') && host.ends_with(']') {
        host = &host[1..host.len() - 1];
    }
    Ok((host.to_string(), port))
}

/// str() of an OSError as Python writes it.
pub fn reason(e: &io::Error) -> String {
    if matches!(e.kind(), io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock) {
        return "timed out".into(); // socket.timeout
    }
    match e.raw_os_error() {
        Some(code) => os_text(code),
        None => e.to_string(),
    }
}

fn os_message(code: i32) -> String {
    let text = io::Error::from_raw_os_error(code).to_string();
    text.strip_suffix(&format!(" (os error {})", code)).unwrap_or(&text).to_string()
}

#[cfg(unix)]
fn os_text(code: i32) -> String {
    // The static musl build words a few errors otherwise than glibc, whose Python the packages' hosts run.
    #[cfg(all(target_os = "linux", target_env = "musl"))]
    let glibc = match code { 99 => Some("Cannot assign requested address"), 101 => Some("Network is unreachable"),
                             103 => Some("Software caused connection abort"), 110 => Some("Connection timed out"),
                             113 => Some("No route to host"), _ => None };
    #[cfg(not(all(target_os = "linux", target_env = "musl")))]
    let glibc: Option<&str> = None;
    format!("[Errno {}] {}", code, glibc.map_or_else(|| os_message(code), str::to_string))
}

#[cfg(windows)]
fn os_text(code: i32) -> String {
    format!("[WinError {}] {}", code, os_message(code).trim_end_matches(|c: char| c == '.' || c.is_whitespace()))
}

/// socket.gaierror's text for a name that does not resolve: std's error does not keep getaddrinfo's code,
/// so the name is asked once more for it.
#[cfg(unix)]
fn lookup_failure(host: &str, e: &io::Error) -> String {
    let Ok(name) = std::ffi::CString::new(host) else { return e.to_string() };
    // SAFETY: plain getaddrinfo with a NUL-terminated name and zeroed hints; the result is freed.
    let code = unsafe {
        let mut hints: libc::addrinfo = std::mem::zeroed();
        hints.ai_socktype = libc::SOCK_STREAM;
        let mut res = std::ptr::null_mut();
        let code = libc::getaddrinfo(name.as_ptr(), std::ptr::null(), &hints, &mut res);
        if code == 0 { libc::freeaddrinfo(res) }
        code
    };
    if code == 0 {
        return e.to_string();
    }
    if code == libc::EAI_SYSTEM {
        return reason(&io::Error::last_os_error());
    }
    #[cfg(target_os = "linux")] // glibc's words, as above
    let glibc = match code { -2 => Some("Name or service not known"), -3 => Some("Temporary failure in name resolution"),
                             -4 => Some("Non-recoverable failure in name resolution"), -5 => Some("No address associated with hostname"), _ => None };
    #[cfg(not(target_os = "linux"))]
    let glibc: Option<&str> = None;
    // SAFETY: gai_strerror returns a static NUL-terminated string.
    let text = glibc.map_or_else(|| unsafe { std::ffi::CStr::from_ptr(libc::gai_strerror(code)) }.to_string_lossy().into_owned(), str::to_string);
    format!("[Errno {}] {}", code, text)
}

#[cfg(windows)]
fn lookup_failure(_host: &str, e: &io::Error) -> String {
    format!("[Errno {}] getaddrinfo failed", e.raw_os_error().unwrap_or(0))
}

fn exchange(mut s: TcpStream, netloc: &str, path: &str) -> io::Result<Vec<u8>> {
    s.set_read_timeout(Some(TIMEOUT))?;
    s.set_write_timeout(Some(TIMEOUT))?;
    s.write_all(format!("GET {} HTTP/1.0\r\nHost: {}\r\n\r\n", path, netloc).as_bytes())?;
    let (mut data, mut buf) = (Vec::new(), [0u8; 16384]);
    loop {
        let n = match s.read(&mut buf) {
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        };
        if n == 0 { return Ok(data) }
        data.extend_from_slice(&buf[..n]);
        if let Some(end) = data.windows(4).position(|w| w == b"\r\n\r\n") { // done once Content-Length bytes are in
            let head = String::from_utf8_lossy(&data[..end]).to_ascii_lowercase();
            let length = head.lines().find_map(|l| l.strip_prefix("content-length:").and_then(|v| v.trim().parse::<usize>().ok()));
            if length.is_some_and(|len| data.len() >= end + 4 + len) {
                return Ok(data);
            }
        }
    }
}

/// GET path from netloc (host[:port]): the body of a 2xx answer.
pub fn get(netloc: &str, path: &str) -> Result<Vec<u8>, Error> {
    let netloc = netloc.split(['/', '?', '#']).next().unwrap_or("");
    let (host, port) = host_port(netloc)?;
    let addrs: Vec<SocketAddr> = (host.as_str(), port).to_socket_addrs().map_err(|e| Error::Failed(lookup_failure(&host, &e)))?.collect();
    let mut last = io::Error::other("getaddrinfo returns an empty list");
    for a in addrs { // each address in turn, the last failure reported, as socket.create_connection
        match TcpStream::connect_timeout(&a, TIMEOUT) {
            Ok(s) => return answer(exchange(s, netloc, path).map_err(|e| Error::Failed(reason(&e)))?),
            Err(e) => last = e,
        }
    }
    Err(Error::Failed(reason(&last)))
}

fn answer(data: Vec<u8>) -> Result<Vec<u8>, Error> {
    if data.is_empty() {
        return Err(Error::Failed("Remote end closed connection without response".into()));
    }
    let end = data.windows(4).position(|w| w == b"\r\n\r\n").map_or(data.len(), |e| e + 4);
    let head = String::from_utf8_lossy(&data[..end]);
    let line = head.lines().next().unwrap_or("").trim_end();
    let mut parts = line.splitn(3, ' ');
    let (version, code) = (parts.next().unwrap_or(""), parts.next().and_then(|c| c.parse::<u16>().ok()));
    let Some(code) = code.filter(|c| version.starts_with("HTTP/") && (100..1000).contains(c)) else {
        return Err(Error::Fatal(format!("http.client.BadStatusLine: {}", line)));
    };
    if !(200..300).contains(&code) {
        return Err(Error::Http(code, parts.next().unwrap_or("").trim().to_string()));
    }
    Ok(data[end..].to_vec())
}

/// get() and the body as JSON (json.load: a ValueError when it is not).
pub fn get_json(netloc: &str, path: &str) -> Result<Value, Error> {
    let body = get(netloc, path)?;
    let text = std::str::from_utf8(&body).map_err(|e| Error::Failed(e.to_string()))?;
    parse(text).map_err(Error::Failed)
}

/// explain(): why the service could not be read, and what to do about it; on Windows the service control
/// manager's and the event log's commands in place of systemd's.
pub fn explain(addr: &str, e: &Error, windows: bool) -> String {
    let reason = match e {
        Error::Http(code, why) => {
            let hint = if *code == 503 { "\n  it has no current data yet, or collection keeps failing; check the service log" } else { "" };
            return format!("platmon at {} answered {} {}{}", addr, code, why, hint);
        }
        Error::Failed(r) | Error::Fatal(r) => r,
    };
    let host = addr.rsplit_once(':').map_or(addr, |(h, _)| h);
    if host == "localhost" || host == "127.0.0.1" {
        if windows {
            return format!("platmon is not running on {} ({})\n  check the service: sc.exe query platmon    (why it stopped: Event Viewer, \
                            Windows Logs > Application, source platmon)", addr, reason);
        }
        return format!("platmon is not running on {} ({})\n  check the service: systemctl status platmon    (log: journalctl -u platmon)\n  \
                        from a checkout: deploy/systemd/start.sh or deploy/docker/start.sh", addr, reason);
    }
    format!("cannot reach platmon at {} ({})\n  check that its service runs there and the port is reachable (the device's [http] bind, a firewall)",
            addr, reason)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_and_port_as_http_client_reads_them() {
        let ok = |n: &str| host_port(n).ok().unwrap();
        assert_eq!(ok("localhost:9797"), ("localhost".into(), 9797));
        assert_eq!(ok("[::1]:9797"), ("::1".into(), 9797));
        assert_eq!(ok("[::1]"), ("::1".into(), 80));
        assert_eq!(ok("localhost:"), ("localhost".into(), 80));
        assert!(matches!(host_port("localhost:abc"), Err(Error::Fatal(m)) if m == "http.client.InvalidURL: nonnumeric port: 'abc'"));
        assert!(matches!(host_port("a b:1"), Err(Error::Fatal(m)) if m.ends_with("'a b:1' (found at least ' ')")));
    }

    #[test]
    fn explain_names_the_service_manager() {
        let e = Error::Failed("[WinError 10061] refused".into());
        let win = explain("localhost:9797", &e, true);
        assert!(win.contains("sc.exe query platmon") && win.contains("source platmon") && !win.contains("systemctl"));
        assert!(explain("localhost:9797", &e, false).contains("systemctl status platmon"));
        assert!(explain("10.0.0.2:9797", &e, false).starts_with("cannot reach platmon at 10.0.0.2:9797 ([WinError 10061] refused)"));
        assert_eq!(explain("h:1", &Error::Http(404, "Not Found".into()), false), "platmon at h:1 answered 404 Not Found");
    }

    #[test]
    fn answers() {
        assert_eq!(answer(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}".to_vec()).ok().unwrap(), b"{}");
        assert!(matches!(answer(b"HTTP/1.0 503 Service Unavailable\r\n\r\nx".to_vec()), Err(Error::Http(503, r)) if r == "Service Unavailable"));
        assert!(matches!(answer(vec![]), Err(Error::Failed(_))));
    }
}
