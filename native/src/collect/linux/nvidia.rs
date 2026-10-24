//! collector/gpu.py's nvidia-smi fallback: found once like shutil.which (and WSL's location), queried with
//! one bounded, read-only run per collection, its CSV read like csv.reader(skipinitialspace=True,
//! strict=True) of Python 3.11 and later (3.10 refuses a NUL, these read it as a character).
use std::io::Read;
use std::os::unix::io::AsRawFd;
use std::os::unix::process::ExitStatusExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::config::py_float;
use crate::json::{round_to, Value};
use crate::metadata::{clock, source, Group, Slot};
use crate::pystr::py_space;
use super::sysfs;

const FIELDS: [&str; 3] = ["utilization.gpu", "clocks.current.graphics", "clocks.max.graphics"];
const TIMEOUT: Duration = Duration::from_millis(500);
const WSL: &str = "/usr/lib/wsl/lib/nvidia-smi";
const FIELD_LIMIT: usize = 131072; // csv.field_size_limit()

/// shutil.which("nvidia-smi"), else WSL's copy (not in a systemd service's PATH).
pub fn find() -> Option<String> {
    let path = std::env::var("PATH").unwrap_or_else(|_| "/bin:/usr/bin".into()); // CS_PATH without PATH
    let usable = |p: &str| {
        let Ok(c) = std::ffi::CString::new(p) else { return false };
        std::fs::metadata(p).map_or(false, |m| !m.is_dir()) && unsafe { libc::access(c.as_ptr(), libc::X_OK) } == 0
    };
    if !path.is_empty() {
        let mut seen: Vec<&str> = Vec::new();
        for dir in path.split(':') {
            if seen.contains(&dir) {
                continue;
            }
            seen.push(dir);
            let name = match dir {
                "" => "nvidia-smi".to_string(),
                d if d.ends_with('/') => format!("{}nvidia-smi", d),
                d => format!("{}/nvidia-smi", d),
            };
            if usable(&name) {
                return Some(name);
            }
        }
    }
    let wsl = std::fs::metadata(WSL).map_or(false, |m| m.is_file());
    (wsl && usable(WSL)).then(|| WSL.to_string())
}

enum Failure {
    Timeout,
    Os(std::io::Error),
}

/// subprocess.run(args, capture_output=True, timeout=0.5): the exit code (negative: the signal), stdout
/// and stderr; past the timeout the child is killed and reaped.
fn expired(child: &mut std::process::Child) -> Result<(i32, Vec<u8>, Vec<u8>), Failure> {
    let _ = child.kill();
    let _ = child.wait();
    Err(Failure::Timeout)
}

fn run(path: &str, args: &[&str], timeout: Duration) -> Result<(i32, Vec<u8>, Vec<u8>), Failure> {
    let deadline = Instant::now() + timeout;
    let mut child = Command::new(path).args(args).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(Failure::Os)?;
    let mut pipes = [child.stdout.take().map(|p| Box::new(p) as Box<dyn ReadFd>), child.stderr.take().map(|p| Box::new(p) as Box<dyn ReadFd>)];
    let mut bufs = [Vec::new(), Vec::new()];
    while pipes.iter().any(Option::is_some) {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return expired(&mut child);
        }
        let mut fds: Vec<libc::pollfd> = pipes.iter().flatten().map(|p| libc::pollfd { fd: p.fd(), events: libc::POLLIN, revents: 0 }).collect();
        let n = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, left.as_millis().max(1) as libc::c_int) };
        if n < 0 {
            let e = std::io::Error::last_os_error();
            if e.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            let _ = child.kill();
            let _ = child.wait();
            return Err(Failure::Os(e));
        }
        let mut k = 0;
        for i in 0..2 {
            let Some(p) = pipes[i].as_mut() else { continue };
            let ready = fds[k].revents != 0;
            k += 1;
            if !ready {
                continue;
            }
            let mut chunk = [0u8; 65536];
            match p.read(&mut chunk) {
                Ok(0) => pipes[i] = None,
                Ok(n) => bufs[i].extend_from_slice(&chunk[..n]),
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                Err(_) => pipes[i] = None,
            }
        }
    }
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let code = status.code().unwrap_or_else(|| -status.signal().unwrap_or(0));
                let [out, err] = bufs;
                return Ok((code, out, err));
            }
            Ok(None) if Instant::now() >= deadline => return expired(&mut child),
            Ok(None) => std::thread::sleep(Duration::from_millis(1)),
            Err(e) => return Err(Failure::Os(e)),
        }
    }
}

trait ReadFd: Read {
    fn fd(&self) -> i32;
}

impl<T: Read + AsRawFd> ReadFd for T {
    fn fd(&self) -> i32 { self.as_raw_fd() }
}

/// str.splitlines() of text whose \r and \r\n are already \n (universal newlines).
fn splitlines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    for (i, c) in text.char_indices() {
        if matches!(c, '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            out.push(&text[start..i]);
            start = i + c.len_utf8();
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// list(csv.reader(lines, skipinitialspace=True, strict=True)); Err on what raises csv.Error.
fn csv_rows(lines: &[&str]) -> Result<Vec<Vec<String>>, ()> {
    #[derive(PartialEq)]
    enum State { StartRecord, StartField, InField, InQuoted, QuoteInQuoted }
    struct Field { text: String, chars: usize }
    impl Field {
        fn push(&mut self, c: char) -> Result<(), ()> {
            if self.chars >= FIELD_LIMIT { return Err(()) } // field larger than field limit
            self.chars += 1;
            self.text.push(c);
            Ok(())
        }
        fn take(&mut self) -> String { self.chars = 0; std::mem::take(&mut self.text) }
    }
    let (mut rows, mut row, mut field) = (Vec::new(), Vec::new(), Field { text: String::new(), chars: 0 });
    let mut state = State::StartRecord;
    for line in lines {
        for c in line.chars().map(Some).chain([None]) { // None: the end of the line
            match state {
                State::StartRecord | State::StartField => {
                    if state == State::StartRecord && c.is_none() {
                        break; // an empty line: an empty row
                    }
                    state = State::StartField;
                    match c {
                        None => { row.push(field.take()); state = State::StartRecord; }
                        Some('"') => state = State::InQuoted,
                        Some(' ') => {}
                        Some(',') => row.push(field.take()),
                        Some(c) => { field.push(c)?; state = State::InField; }
                    }
                }
                State::InField => match c {
                    None => { row.push(field.take()); state = State::StartRecord; }
                    Some(',') => { row.push(field.take()); state = State::StartField; }
                    Some(c) => field.push(c)?,
                },
                State::InQuoted => match c {
                    None => {} // the field goes on in the next line
                    Some('"') => state = State::QuoteInQuoted,
                    Some(c) => field.push(c)?,
                },
                State::QuoteInQuoted => match c {
                    Some('"') => { field.push('"')?; state = State::InQuoted; }
                    Some(',') => { row.push(field.take()); state = State::StartField; }
                    None => { row.push(field.take()); state = State::StartRecord; }
                    Some(_) => return Err(()), // ',' expected after '"'
                },
            }
        }
        if state == State::StartRecord {
            rows.push(std::mem::take(&mut row));
        }
    }
    if state != State::StartRecord {
        return Err(()); // unexpected end of data (inside quotes)
    }
    Ok(rows)
}

fn uuid_ok(text: &str) -> bool {
    text.strip_prefix("GPU-").map_or(false, |r| !r.is_empty() && r.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-'))
}

/// Gpu._nvidia: the "gpu" entry of the GPU with the smallest UUID, or None with the reason noted. A failed
/// query never reuses an old value; unavailable fields stay null.
pub fn sample(path: &str, g: &mut Group) -> Option<Value> {
    let start = g.trace.then(clock);
    let query = format!("--query-gpu=uuid,{}", FIELDS.join(","));
    let (code, out, err) = match run(path, &[&query, "--format=csv,noheader,nounits"], TIMEOUT) {
        Ok(r) => r,
        Err(Failure::Timeout) => { g.note("nvidia-smi", "io_error", Some("query timed out after 0.5 s".into())); return None; }
        Err(Failure::Os(e)) => { g.note("nvidia-smi", sysfs::reason_of(&e), Some(e.to_string())); return None; }
    };
    let (Ok(out), Ok(_)) = (String::from_utf8(out), std::str::from_utf8(&err)) else {
        g.note("nvidia-smi", "invalid_data", Some("query is not UTF-8".into()));
        return None;
    };
    if code != 0 {
        g.note("nvidia-smi", "io_error", Some(format!("query exit code {}", code)));
        return None;
    }
    let span = start.map(|s| (s, clock()));
    let out = out.replace("\r\n", "\n").replace('\r', "\n");
    let rows = match csv_rows(&splitlines(&out)) {
        Ok(rows) if rows.is_empty() => { g.note("nvidia-smi", "not_detected", None); return None; }
        Ok(rows) if rows.iter().all(|r| r.len() == 4 && uuid_ok(r[0].trim_matches(py_space))) => rows,
        _ => { g.note("nvidia-smi", "invalid_data", Some("malformed query response".into())); return None; }
    };
    let row = rows.iter().min_by(|a, b| a[0].trim_matches(py_space).cmp(b[0].trim_matches(py_space))).unwrap();
    let uuid = row[0].trim_matches(py_space);
    let mut entry = Value::obj();
    entry.set("driver", "nvidia".into());
    entry.set("provider", "nvidia-smi".into());
    for ((key, attr), text) in ["usage", "freq", "max_freq"].into_iter().zip(FIELDS).zip(&row[1..]) {
        let mut value = None;
        if matches!(text.trim_matches(py_space), "N/A" | "[N/A]" | "[Not Supported]" | "Not Supported") {
            g.note(attr, "not_exposed", None);
        } else {
            match py_float(text) {
                Some(v) if v.is_finite() && v >= 0.0 && (key != "usage" || v <= 100.0) && (key == "usage" || hertz(v).is_some()) => value = Some(v),
                _ => g.note(attr, "invalid_data", None),
            }
        }
        entry.set(key, match value {
            None => Value::Null,
            Some(v) if key == "usage" => g.got(Value::Float(round_to(v, 1))),
            Some(v) => g.got(hertz(v).unwrap()),
        });
        let src = source("nvidia-smi", if key == "usage" { "percent" } else { "hertz" }, "device_channel",
                         vec![("device", uuid.into()), ("channel", attr.into())]);
        g.sensor(Slot::Key(key.into()), Some(src), if value.is_some() { span } else { None });
    }
    Some(entry)
}

/// round(MHz × 10**6) as Python's int, exact beyond i64 too; None from 2**128 Hz (over 3.4 × 10**32 MHz) on,
/// where Python still gives an int: refused here as invalid_data (README, Support scope), never saturated.
fn hertz(mhz: f64) -> Option<Value> {
    let x = (mhz * 1e6).round_ties_even();
    if !(x < 340282366920938463463374607431768211456.0) { // 2**128, exact as a double; also inf and nan
        return None;
    }
    Some(if x < 9223372036854775808.0 { Value::Int(x as i64) } else { Value::Big(x as u128) })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rows(lines: &[&str]) -> Option<Vec<Vec<String>>> { csv_rows(lines).ok() }

    #[test]
    fn csv_reads_like_python() {
        // the cases of python3 -c 'csv.reader(lines, skipinitialspace=True, strict=True)' on 3.10, 3.13, 3.14
        let v = |x: &[&str]| Some(vec![x.iter().map(|s| s.to_string()).collect::<Vec<_>>()]);
        assert_eq!(rows(&["GPU-a, 1, 2, 3"]), v(&["GPU-a", "1", "2", "3"]));
        assert_eq!(rows(&["GPU-a,  ,2,3"]), v(&["GPU-a", "", "2", "3"]));
        assert_eq!(rows(&["GPU-a, \"1", "2\", 3, 4"]), v(&["GPU-a", "12", "3", "4"]));
        assert_eq!(rows(&["GPU-a, \"1\" ,2,3"]), None);
        assert_eq!(rows(&["GPU-a,\"1\"x,2,3"]), None);
        assert_eq!(rows(&["GPU-a,1\0,2,3"]), v(&["GPU-a", "1\0", "2", "3"]));
        assert_eq!(rows(&[""]), Some(vec![vec![]]));
        assert_eq!(rows(&["GPU-a, a\"b, 2, 3"]), v(&["GPU-a", "a\"b", "2", "3"]));
        assert_eq!(rows(&["\"GPU-a\",1,2,3 "]), v(&["GPU-a", "1", "2", "3 "]));
        assert_eq!(rows(&["GPU-a, \"x", "y"]), None);
        assert_eq!(rows(&["GPU-a,1,2,3,"]), v(&["GPU-a", "1", "2", "3", ""]));
        assert_eq!(rows(&["GPU-a, \" 1\",2,3"]), v(&["GPU-a", " 1", "2", "3"]));
        assert_eq!(rows(&["a\"\"b", "\"a\"\"b\""]), Some(vec![vec!["a\"\"b".into()], vec!["a\"b".into()]]));
        assert_eq!(rows(&[&"x".repeat(FIELD_LIMIT)]).map(|r| r[0][0].len()), Some(FIELD_LIMIT));
        assert_eq!(rows(&[&"x".repeat(FIELD_LIMIT + 1)]), None);
    }

    #[test]
    fn clocks_in_hertz_like_python_or_refused() {
        // python3 -c 'print(round(1410 * 10**6), round(5e-7 * 10**6), round(9223372036854.775807 * 10**6), round(1e30 * 10**6), round(3.4e32 * 10**6))'
        assert_eq!(hertz(1410.0), Some(Value::Int(1_410_000_000)));
        assert_eq!(hertz(0.0000005), Some(Value::Int(0))); // 0.5 Hz: ties to even
        assert_eq!(hertz(9223372036854.775807), Some(Value::Big(9223372036854775808)));
        assert_eq!(hertz(1e30), Some(Value::Big(1000000000000000042420637374017961984)));
        assert_eq!(hertz(3.4e32), Some(Value::Big(339999999999999996123846586046231871488)));
        assert_eq!(hertz(340282366920938.463463374607431768211456e18), None); // 2**128 Hz
        assert_eq!(hertz(1e40), None);
        assert_eq!(hertz(f64::MAX), None); // × 10**6 is inf
    }

    #[test]
    fn a_clock_beyond_2_128_hz_is_null_and_invalid() {
        let dir = std::env::temp_dir().join(format!("platmon-nvidia-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let fake = dir.join("nvidia-smi");
        // written by a child: a write descriptor of this process could be inherited by another test's fork
        // until its exec, and executing the script then fails with ETXTBSY
        let script = "#!/bin/sh\necho 'GPU-review, 10, 1e40, 1980'\n";
        let made = Command::new("/bin/sh").args(["-c", "printf '%s' \"$1\" > \"$2\" && chmod 755 \"$2\"", "sh", script])
            .arg(&fake).status().unwrap();
        assert!(made.success());
        let mut g = Group::new("gpu", true);
        let entry = sample(fake.to_str().unwrap(), &mut g).unwrap();
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(entry.get("usage"), Some(&Value::Float(10.0)));
        assert_eq!(entry.get("freq"), Some(&Value::Null));
        assert_eq!(entry.get("max_freq"), Some(&Value::Int(1_980_000_000)));
        assert_eq!(g.issues, [("clocks.current.graphics".to_string(), "invalid_data")]);
        assert_eq!(g.values, 2);
    }

    #[test]
    fn lines_split_like_python() {
        assert_eq!(splitlines("a\nb\x0bc\u{2028}d\n"), ["a", "b", "c", "d"]);
        assert_eq!(splitlines("\n"), [""]);
        assert!(splitlines("").is_empty());
    }

    #[test]
    fn a_slow_query_is_killed_and_reaped() {
        let t = Instant::now();
        assert!(matches!(run("/bin/sleep", &["5"], Duration::from_millis(200)), Err(Failure::Timeout)));
        assert!(t.elapsed() < Duration::from_secs(2));
        match run("/bin/sh", &["-c", "printf 'a\\n'; echo b >&2; exit 3"], TIMEOUT) {
            Ok((3, out, err)) => assert_eq!((out.as_slice(), err.as_slice()), (&b"a\n"[..], &b"b\n"[..])),
            _ => panic!("sh"),
        }
        assert!(matches!(run("/nonexistent/nvidia-smi", &[], TIMEOUT), Err(Failure::Os(e)) if e.raw_os_error() == Some(libc::ENOENT)));
    }
}
