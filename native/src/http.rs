//! frontends/server.py: the live HTTP API. Requests only read the latest publication (runtime::Sampler,
//! slow::Observations) and never collect. HTTP/1.0, one request per connection, at most 32 connections
//! (excess ones are closed before a thread is started for them), 10 s to send the request line and
//! headers (per read, as Python's socket timeout), 10 s without progress per response write.
//! Contract: docs/reference/api.md.
//!
//! Step 3A: /api/stats, /api/status, /api/observations and the web page. /api/history, /api/samples and
//! /api/observation-samples answer as the Python server does with history and replay off; /text (the
//! platmon command's screen) is not ported and is a 404.
use crate::json::Value;
use crate::metadata;
use crate::runtime::Sampler;
use crate::slow::Observations;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream};
use std::os::fd::FromRawFd;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

const LIVE: [&str; 7] = ["/api/stats", "/api/status", "/api/observations", "/api/history", "/api/samples",
                         "/api/observation-samples", "/text"];
const WEB: [(&str, &str, &str); 7] = [
    ("/app.js", "app.js", "text/javascript; charset=utf-8"),
    ("/styles.css", "styles.css", "text/css; charset=utf-8"),
    ("/assets/brand/platmon-logo-light.svg", "assets/brand/platmon-logo-light.svg", "image/svg+xml"),
    ("/assets/brand/platmon-logo-dark.svg", "assets/brand/platmon-logo-dark.svg", "image/svg+xml"),
    ("/assets/brand/platmon-icon-light.svg", "assets/brand/platmon-icon-light.svg", "image/svg+xml"),
    ("/assets/brand/platmon-icon-dark.svg", "assets/brand/platmon-icon-dark.svg", "image/svg+xml"),
    ("/assets/brand/favicon.svg", "assets/brand/favicon.svg", "image/svg+xml"),
];
const MAX_LINE: usize = 65536; // http.client._MAXLINE, and the request line's limit
const MAX_HEADERS: usize = 100;
const WRITE_CHUNK: usize = 64 * 1024;

pub struct Server {
    pub sampler: Arc<Sampler>,
    pub observations: Arc<Observations>,
    /// the directory of the web page (frontends/web), or None for web = no
    pub web: Option<String>,
    pub version: String,
    /// [thresholds] as /api/status and the page get them
    pub levels: Value,
    pub request_timeout: Duration,
    pub write_timeout: Duration,
    pub max_connections: usize,
    /// how long /api/stats waits for the first snapshot
    pub first_wait: Duration,
    active: AtomicUsize,
}

/// An IPv4 listening socket as Python's HTTPServer makes it: SO_REUSEADDR, a backlog of 5.
pub fn bind(addr: &str, port: u16) -> std::io::Result<TcpListener> {
    let ip: std::net::Ipv4Addr = addr.parse().map_err(|_| std::io::Error::other(format!("not an IPv4 address: {}", addr)))?;
    unsafe {
        let fd = libc::socket(libc::AF_INET, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0);
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let listener = TcpListener::from_raw_fd(fd); // closes fd on the error paths below
        let one: libc::c_int = 1;
        let mut sa: libc::sockaddr_in = std::mem::zeroed();
        sa.sin_family = libc::AF_INET as libc::sa_family_t;
        sa.sin_port = port.to_be();
        sa.sin_addr.s_addr = u32::from(ip).to_be();
        if libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_REUSEADDR, (&one as *const libc::c_int).cast(),
                            std::mem::size_of::<libc::c_int>() as libc::socklen_t) != 0
            || libc::bind(fd, (&sa as *const libc::sockaddr_in).cast(), std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t) != 0
            || libc::listen(fd, 5) != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(listener)
    }
}

/// RFC 7231 date of now, as the Date header.
fn http_date() -> String {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs()) as i64;
    let (days, rem) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    // civil_from_days (H. Hinnant)
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    const WD: [&str; 7] = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"];
    const MON: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    format!("{}, {:02} {} {} {:02}:{:02}:{:02} GMT", WD[days.rem_euclid(7) as usize], d, MON[m as usize - 1], y,
            rem / 3600, rem % 3600 / 60, rem % 60)
}

fn phrase(code: u16) -> (&'static str, &'static str) {
    match code {
        200 => ("OK", "Request fulfilled, document follows"),
        400 => ("Bad Request", "Bad request syntax or unsupported method"),
        404 => ("Not Found", "Nothing matches the given URI"),
        414 => ("Request-URI Too Long", "URI is too long."),
        431 => ("Request Header Fields Too Large", "The server refused this request because the request header fields are too large."),
        501 => ("Not Implemented", "Server does not support this operation"),
        503 => ("Service Unavailable", "The server cannot process the request due to a high load"),
        505 => ("HTTP Version Not Supported", "Cannot fulfill request."),
        _ => ("", ""),
    }
}

fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

/// Python's str.split() on a request line decoded as Latin-1.
fn words(line: &str) -> Vec<&str> {
    line.split(|c: char| c.is_ascii_whitespace() || matches!(c, '\x0b' | '\x1c'..='\x1f' | '\u{85}' | '\u{a0}'))
        .filter(|w| !w.is_empty()).collect()
}

struct Exchange<'a> {
    stream: &'a TcpStream,
    /// HTTP/0.9: the answer is the body alone. As in Python, a request counts as that until its version is
    /// read, so errors about the request line itself have no status line or headers either.
    bare: bool,
}

impl Exchange<'_> {
    fn head(&mut self, code: u16, message: &str, headers: &[(&str, String)]) -> std::io::Result<()> {
        if self.bare {
            return Ok(());
        }
        let mut h = format!("HTTP/1.0 {} {}\r\nServer: platmon-native\r\nDate: {}\r\n", code, message, http_date());
        for (k, v) in headers {
            h.push_str(&format!("{}: {}\r\n", k, v));
        }
        h.push_str("\r\n");
        self.stream.write_all(h.as_bytes())
    }

    /// The body in chunks: each write has its own timeout, so a long answer may take longer overall
    /// while a reader that stopped is still dropped.
    fn body(&mut self, body: &[u8]) -> std::io::Result<()> {
        let mut pending = body;
        while !pending.is_empty() {
            let sent = (&*self.stream).write(&pending[..pending.len().min(WRITE_CHUNK)])?;
            if sent == 0 {
                return Err(std::io::ErrorKind::WriteZero.into());
            }
            pending = &pending[sent..];
        }
        Ok(())
    }

    fn reply(&mut self, path: &str, code: u16, body: &[u8], ctype: &str) -> std::io::Result<()> {
        let mut headers = vec![("Content-Type", ctype.to_string()), ("Content-Length", body.len().to_string())];
        if LIVE.contains(&path) { // a cached answer would show old data as current
            headers.push(("Cache-Control", "no-store".into()));
        }
        self.head(code, phrase(code).0, &headers)?;
        self.body(body)
    }

    /// BaseHTTPRequestHandler.send_error: the message is also the status line's reason.
    fn error(&mut self, code: u16, message: Option<&str>) -> std::io::Result<()> {
        let (short, explain) = phrase(code);
        let message = message.unwrap_or(short);
        let body = format!("<!DOCTYPE HTML>\n<html lang=\"en\">\n    <head>\n        <meta charset=\"utf-8\">\n        \
                            <title>Error response</title>\n    </head>\n    <body>\n        <h1>Error response</h1>\n        \
                            <p>Error code: {code}</p>\n        <p>Message: {}.</p>\n        \
                            <p>Error code explanation: {code} - {}.</p>\n    </body>\n</html>\n",
                           html_escape(message), html_escape(explain));
        self.head(code, message, &[("Connection", "close".into()), ("Content-Type", "text/html;charset=utf-8".into()),
                                   ("Content-Length", body.len().to_string())])?;
        self.body(body.as_bytes())
    }
}

/// One line of at most limit bytes (more is read but not kept: its length tells it is too long).
fn read_line(r: &mut impl BufRead, limit: usize) -> std::io::Result<Vec<u8>> {
    let mut line = Vec::new();
    r.by_ref().take(limit as u64 + 1).read_until(b'\n', &mut line)?;
    Ok(line)
}

impl Server {
    pub fn new(sampler: Arc<Sampler>, observations: Arc<Observations>, web: Option<String>, version: String, levels: Value) -> Server {
        Server { sampler, observations, web, version, levels, request_timeout: Duration::from_secs(10),
                 write_timeout: Duration::from_secs(10), max_connections: 32, first_wait: Duration::from_secs(5),
                 active: AtomicUsize::new(0) }
    }

    /// Accepts connections forever: one thread each, up to max_connections; an excess one is closed at once.
    pub fn serve(self: Arc<Self>, listener: TcpListener) {
        for conn in listener.incoming() {
            let Ok(stream) = conn else { continue };
            if self.active.fetch_add(1, Ordering::SeqCst) >= self.max_connections {
                self.active.fetch_sub(1, Ordering::SeqCst);
                let _ = stream.shutdown(Shutdown::Write);
                continue;
            }
            let s = self.clone();
            let started = std::thread::Builder::new().name("http".into()).stack_size(256 * 1024).spawn(move || {
                let _ = s.handle(&stream); // a stalled or disconnected peer: the connection just closes
                let _ = stream.shutdown(Shutdown::Write);
                s.active.fetch_sub(1, Ordering::SeqCst);
            });
            if started.is_err() {
                self.active.fetch_sub(1, Ordering::SeqCst);
            }
        }
    }

    fn handle(&self, stream: &TcpStream) -> std::io::Result<()> {
        stream.set_read_timeout(Some(self.request_timeout))?;
        stream.set_write_timeout(Some(self.write_timeout))?;
        let mut r = BufReader::new(stream);
        let mut x = Exchange { stream, bare: true };
        let raw = read_line(&mut r, MAX_LINE)?;
        if raw.len() > MAX_LINE {
            x.bare = false; // Python's request_version is "" there, not HTTP/0.9
            return x.error(414, None);
        }
        let line: String = raw.iter().map(|&b| b as char).collect(); // Latin-1
        let w = words(line.trim_end_matches(['\r', '\n']));
        if w.is_empty() {
            return Ok(()); // nothing asked: closed without an answer
        }
        if w.len() >= 3 {
            let version = w[w.len() - 1];
            let numbers: Option<Vec<u64>> = version.strip_prefix("HTTP/").and_then(|v| {
                let parts: Vec<&str> = v.split('.').collect();
                (parts.len() == 2 && parts.iter().all(|p| !p.is_empty() && p.len() <= 10 && p.bytes().all(|b| b.is_ascii_digit())))
                    .then(|| parts.iter().map(|p| p.parse().unwrap()).collect())
            });
            match numbers {
                None => return x.error(400, Some(&format!("Bad request version ({})", py_repr(version)))),
                Some(n) if n[0] >= 2 => return x.error(505, Some(&format!("Invalid HTTP version ({})", &version[5..]))),
                _ => x.bare = false,
            }
        }
        if !(2..=3).contains(&w.len()) {
            return x.error(400, Some(&format!("Bad request syntax ({})", py_repr(line.trim_end_matches(['\r', '\n'])))));
        }
        let (command, mut path) = (w[0], w[1].to_string());
        if w.len() == 2 && command != "GET" {
            return x.error(400, Some(&format!("Bad HTTP/0.9 request type ({})", py_repr(command))));
        }
        if path.starts_with("//") {
            path = format!("/{}", path.trim_start_matches('/'));
        }
        let mut headers = 0;
        loop {
            let h = read_line(&mut r, MAX_LINE)?;
            if h.len() > MAX_LINE {
                return x.error(431, Some("Line too long"));
            }
            if h.is_empty() || h == b"\r\n" || h == b"\n" {
                break;
            }
            headers += 1;
            if headers > MAX_HEADERS {
                return x.error(431, Some("Too many headers"));
            }
        }
        if command != "GET" {
            return x.error(501, Some(&format!("Unsupported method ({})", py_repr(command))));
        }
        self.get(&mut x, &path)
    }

    fn get(&self, x: &mut Exchange, full: &str) -> std::io::Result<()> {
        let path = full.split('?').next().unwrap_or("");
        if path == "/api/samples" || path == "/api/observation-samples" { // replay is not in step 3A: as when it is off
            return x.reply(path, 404, br#"{"code":"replay_unavailable"}"#, "application/json");
        }
        match full {
            "/api/status" => { // never waits for the first snapshot; 200 even when not ready
                let (_, mut status) = self.sampler.view(metadata::clock);
                status.set("levels", self.levels.clone());
                x.reply(full, 200, status.to_py().as_bytes(), "application/json")
            }
            "/api/observations" => x.reply(full, 200, self.observations.view(metadata::clock).as_bytes(), "application/json"),
            "/api/stats" => {
                self.sampler.wait_ready(self.first_wait);
                match self.sampler.view(metadata::clock) {
                    (Some(stats), _) => x.reply(full, 200, stats.as_bytes(), "application/json"),
                    (None, status) => { // the last good sample's identity and age, never its values
                        let mut body = Value::obj();
                        body.set("error", "no current data (not collected yet, or collection keeps failing)".into());
                        for k in ["schema_version", "code", "instance_id", "sample"] {
                            body.set(k, if k == "code" { "no_current_data".into() } else { status.get(k).cloned().unwrap_or_default() });
                        }
                        x.reply(full, 503, body.to_py().as_bytes(), "application/json")
                    }
                }
            }
            "/" | "/index.html" if self.web.is_some() => {
                let page = std::fs::read_to_string(format!("{}/index.html", self.web.as_ref().unwrap()))?;
                let page = page.replace("@VERSION@", &self.version).replace("@LEVELS@", &self.levels.to_py());
                x.reply(full, 200, page.as_bytes(), "text/html; charset=utf-8")
            }
            _ => match WEB.iter().find(|(p, _, _)| *p == full).filter(|_| self.web.is_some()) {
                Some((_, file, ctype)) => {
                    let body = std::fs::read(format!("{}/{}", self.web.as_ref().unwrap(), file))?;
                    x.reply(full, 200, &body, ctype)
                }
                None => x.error(404, None),
            },
        }
    }
}

/// repr() of a str: in single quotes unless it holds one and no double quote.
fn py_repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => { out.push('\\'); out.push(c); }
            c if (c as u32) < 0x20 || (0x7f..0xa1).contains(&(c as u32)) || c == '\u{ad}' => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json;
    use crate::runtime::Collection;
    use std::sync::atomic::AtomicBool;

    static STOP: AtomicBool = AtomicBool::new(false);

    /// A server on a free port with a Sampler collecting every 50 ms (each collection counted).
    fn server(tune: impl FnOnce(&mut Server)) -> (std::net::SocketAddr, Arc<AtomicUsize>) {
        let sampler = Arc::new(Sampler::new(0.05, None, "test".into(), "boottime"));
        let calls = Arc::new(AtomicUsize::new(0));
        let (s, c) = (sampler.clone(), calls.clone());
        std::thread::spawn(move || s.run(move || -> Collection {
            c.fetch_add(1, Ordering::SeqCst);
            let mut v = Value::obj();
            v.set("stats", json::parse(r#"{"cpu": []}"#).unwrap());
            Ok(v)
        }, &STOP));
        let obs = Arc::new(Observations { groups: vec![], instance_id: "test".into(), clock_source: "boottime" });
        let mut srv = Server::new(sampler, obs, None, "0".into(), Value::obj());
        tune(&mut srv);
        let listener = bind("127.0.0.1", 0).unwrap();
        let addr = listener.local_addr().unwrap();
        let srv = Arc::new(srv);
        std::thread::spawn(move || srv.serve(listener));
        (addr, calls)
    }

    fn get(addr: std::net::SocketAddr, request: &str) -> String {
        let mut s = TcpStream::connect(addr).unwrap();
        s.write_all(request.as_bytes()).unwrap();
        let mut out = String::new();
        let _ = s.read_to_string(&mut out);
        out
    }

    #[test]
    fn concurrent_reads_never_collect() {
        let (addr, calls) = server(|_| {});
        std::thread::sleep(Duration::from_millis(100));
        let t0 = std::time::Instant::now();
        let before = calls.load(Ordering::SeqCst);
        let clients: Vec<_> = (0..16).map(|_| std::thread::spawn(move || {
            let mut last = 0;
            for _ in 0..25 {
                let r = get(addr, "GET /api/stats HTTP/1.1\r\nHost: x\r\n\r\n");
                assert!(r.starts_with("HTTP/1.0 200 OK\r\n") && r.contains("\r\nCache-Control: no-store\r\n"), "{}", r);
                let body = json::parse(r.split("\r\n\r\n").nth(1).unwrap()).unwrap();
                let Some(Value::Int(seq)) = body.get("sample").and_then(|s| s.get("sequence")) else { panic!() };
                assert!(*seq >= last); // never back in time
                last = *seq;
            }
        })).collect();
        for c in clients {
            c.join().unwrap();
        }
        let rounds = t0.elapsed().as_secs_f64() / 0.05;
        let collected = (calls.load(Ordering::SeqCst) - before) as f64;
        assert!(collected <= rounds + 2.0, "{} collections in {} intervals for 400 requests", collected, rounds);
    }

    #[test]
    fn excess_connections_are_closed() {
        let (addr, _) = server(|s| s.max_connections = 4);
        let held: Vec<TcpStream> = (0..4).map(|_| TcpStream::connect(addr).unwrap()).collect();
        std::thread::sleep(Duration::from_millis(100));
        let mut extra = TcpStream::connect(addr).unwrap();
        extra.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut buf = [0u8; 16];
        assert_eq!(extra.read(&mut buf).unwrap(), 0); // closed without an answer
        drop(held);
        std::thread::sleep(Duration::from_millis(100));
        assert!(get(addr, "GET /api/status HTTP/1.0\r\n\r\n").starts_with("HTTP/1.0 200 OK"));
    }

    #[test]
    fn a_silent_client_is_dropped() {
        let (addr, _) = server(|s| s.request_timeout = Duration::from_millis(200));
        let mut s = TcpStream::connect(addr).unwrap();
        s.write_all(b"GET /api/sta").unwrap(); // part of a request line, then nothing
        s.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let t0 = std::time::Instant::now();
        let mut out = Vec::new();
        assert_eq!(s.read_to_end(&mut out).unwrap(), 0);
        assert!(t0.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn answers_like_the_python_server() {
        let (addr, _) = server(|_| {});
        let status = |r: &str| r.lines().next().unwrap_or("").to_string();
        assert_eq!(status(&get(addr, "GET /nothing HTTP/1.0\r\n\r\n")), "HTTP/1.0 404 Not Found");
        assert_eq!(status(&get(addr, "GET /api/stats?x HTTP/1.0\r\n\r\n")), "HTTP/1.0 404 Not Found"); // whole path
        assert_eq!(status(&get(addr, "POST /api/stats HTTP/1.0\r\n\r\n")), "HTTP/1.0 501 Unsupported method ('POST')");
        assert_eq!(status(&get(addr, "get / HTTP/1.0\r\n\r\n")), "HTTP/1.0 501 Unsupported method ('get')");
        // errors about the request line before its version is read: the page alone, as for HTTP/0.9
        assert!(get(addr, "GET /api/stats HTTP/2.0\r\n\r\n").starts_with("<!DOCTYPE HTML>"));
        assert!(get(addr, "GET / HTTP/x\r\n\r\n").contains("<p>Message: Bad request version ('HTTP/x').</p>"));
        assert!(get(addr, "A B C D\r\n\r\n").contains("Error code: 400"));
        assert_eq!(status(&get(addr, &format!("GET /{} HTTP/1.0\r\n\r\n", "a".repeat(70000)))), "HTTP/1.0 414 Request-URI Too Long");
        assert_eq!(status(&get(addr, &format!("GET / HTTP/1.0\r\n{}\r\n", "A: b\r\n".repeat(101)))),
                   "HTTP/1.0 431 Too many headers");
        let r = get(addr, "GET /api/samples?after=1 HTTP/1.0\r\n\r\n");
        assert!(r.starts_with("HTTP/1.0 404 Not Found\r\n") && r.contains("no-store") && r.ends_with(r#"{"code":"replay_unavailable"}"#));
        assert_eq!(get(addr, "GET //api/status HTTP/1.0\r\n\r\n").lines().next(), Some("HTTP/1.0 200 OK"));
        assert!(get(addr, "GET /api/status\r\n\r\n").starts_with("{\"schema_version\": 1")); // HTTP/0.9: the body alone
        assert_eq!(get(addr, "\r\n"), ""); // an empty request line: no answer
    }

    #[test]
    fn no_snapshot_is_503() {
        let sampler = Arc::new(Sampler::new(1.0, None, "x".into(), "boottime"));
        let obs = Arc::new(Observations { groups: vec![], instance_id: "x".into(), clock_source: "boottime" });
        let mut srv = Server::new(sampler, obs, None, "0".into(), Value::obj());
        srv.first_wait = Duration::from_millis(50);
        let listener = bind("127.0.0.1", 0).unwrap();
        let addr = listener.local_addr().unwrap();
        std::thread::spawn(move || Arc::new(srv).serve(listener));
        let r = get(addr, "GET /api/stats HTTP/1.0\r\n\r\n");
        assert!(r.starts_with("HTTP/1.0 503 Service Unavailable\r\n"), "{}", r);
        assert!(r.ends_with(r#"{"error": "no current data (not collected yet, or collection keeps failing)", "schema_version": 1, "code": "no_current_data", "instance_id": "x", "sample": null}"#));
    }
}
