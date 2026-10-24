//! The passive groups: collector/network.py, disk_io.py and pressure.py. Each is read once per collection,
//! after the required data, and gives its output and the (start, end) of its read.
use crate::json::{round_to, Value};
use crate::metadata::{clock, Group, Span};
use crate::pystr::{py_printable, py_space, py_split, py_strip};
use super::sysfs::{self, ReadError};
use std::io;

const MAX_COUNTER: u128 = u64::MAX as u128;

/// counters.counter: an unsigned 64-bit counter in ASCII digits; None otherwise (sign, decimals, other digits).
fn counter(text: &str) -> Option<u64> {
    if text.is_empty() || !text.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    // more digits than u128 holds is beyond the limit too
    text.parse::<u128>().ok().filter(|v| *v <= MAX_COUNTER).map(|v| v as u64)
}

/// round(delta * 1e9 / window_ns, 3) with Python's int to float conversions.
fn rate(delta: u128, window_ns: i64) -> f64 {
    round_to(delta as f64 * 1e9 / window_ns as f64, 3)
}

fn window_ms(window: Option<i64>, reason: Option<&str>) -> Value {
    match (window, reason) {
        (Some(w), None | Some("gap") | Some("counter_regressed")) => round_to(w as f64 / 1e6, 3).into(),
        _ => Value::Null,
    }
}

fn reason_value(reason: Option<&'static str>) -> Value {
    reason.map_or(Value::Null, Into::into)
}

/// The read's (start, end): the end only when the file was read and decoded, as in Python.
fn timed_read(path: &str, read: fn(&str) -> Result<String, ReadError>) -> (i64, Result<(String, i64), ReadError>) {
    let start = clock();
    (start, read(path).map(|t| (t, clock())))
}

// --- network ------------------------------------------------------------------------------------------

const NET_DEV: &str = "/proc/self/net/dev";
const NETNS: &str = "/proc/self/ns/net";
const IFNAMSIZ: usize = 16;
const RX: [&str; 8] = ["bytes", "packets", "errs", "drop", "fifo", "frame", "compressed", "multicast"];
const TX: [&str; 8] = ["bytes", "packets", "errs", "drop", "fifo", "colls", "carrier", "compressed"];
const COUNTERS: [&str; 4] = ["bytes", "packets", "errors", "dropped"];

/// network.valid_name: 1-15 bytes, not . or .., printable, no /, : or whitespace.
pub fn valid_ifname(name: &str) -> bool {
    !name.is_empty() && name.len() < IFNAMSIZ && name != "." && name != ".."
        && name.chars().all(|c| py_printable(c) && c != '/' && c != ':' && !py_space(c))
}

type NetRow = [u64; 8];

/// network.parse: ({name: rx bytes, packets, errors, dropped, tx ...}, [(target, why)]); None for a header
/// that is not the known one.
fn parse_net_dev(text: &str) -> Option<(Vec<(String, NetRow)>, Vec<(String, &'static str)>)> {
    let lines: Vec<&str> = text.split('\n').collect();
    if lines.len() < 2 {
        return None;
    }
    let header: Vec<Vec<&str>> = lines[1].split('|').skip(1).map(py_split).collect();
    if header != [RX.to_vec(), TX.to_vec()] {
        return None;
    }
    let (mut rows, mut bad, mut seen): (Vec<(String, NetRow)>, Vec<(String, &'static str)>, Vec<String>) = (vec![], vec![], vec![]);
    for (n, line) in lines.iter().enumerate().skip(2) {
        if py_strip(line).is_empty() {
            continue;
        }
        let (name, rest) = match line.split_once(':') {
            Some((a, b)) => (py_strip(a), Some(b)),
            None => (py_strip(line), None),
        };
        let Some(rest) = rest.filter(|_| valid_ifname(name)) else {
            bad.push((format!("line{}", n + 1), "invalid_data"));
            continue;
        };
        if seen.iter().any(|s| s == name) {
            rows.retain(|(r, _)| r != name);
            bad.push((name.to_string(), "invalid_data"));
            continue;
        }
        seen.push(name.to_string());
        let fields = py_split(rest);
        let values: Option<Vec<u64>> = if fields.len() == 16 { fields.iter().map(|f| counter(f)).collect() } else { None };
        match values {
            Some(v) => rows.push((name.to_string(), [v[0], v[1], v[2], v[3], v[8], v[9], v[10], v[11]])),
            None => bad.push((name.to_string(), "invalid_data")),
        }
    }
    Some((rows, bad))
}

/// network.namespace_id from the device and inode /proc/self/ns/net resolves to.
pub fn namespace_id(dev: u64, ino: u64) -> String {
    let text = format!(r#"{{"dev":{},"ino":{},"v":1}}"#, dev, ino);
    format!("netns1:{}", crate::sha256::hex(text.as_bytes()))
}

/// socket.if_nameindex(): (index, name) of every interface.
pub fn if_indexes() -> io::Result<Vec<(u32, String)>> {
    let mut out = Vec::new();
    unsafe {
        let list = libc::if_nameindex();
        if list.is_null() {
            return Err(io::Error::last_os_error());
        }
        let mut p = list;
        while (*p).if_index != 0 && !(*p).if_name.is_null() {
            out.push(((*p).if_index, std::ffi::CStr::from_ptr((*p).if_name).to_string_lossy().into_owned()));
            p = p.add(1);
        }
        libc::if_freenameindex(list);
    }
    Ok(out)
}

pub fn strerror(e: &io::Error) -> String {
    let s = e.to_string(); // "No such file or directory (os error 2)"
    s.rsplit_once(" (os error").map_or(s.clone(), |(m, _)| m.to_string())
}

/// network.NetworkCounters, kept between collections.
pub struct Network {
    max_gap: Option<i64>,
    last: Option<(i64, Option<String>, Vec<((u32, String), NetRow)>)>,
    ns: Option<((u64, u64), String)>,
}

impl Network {
    pub fn new(max_gap: Option<f64>) -> Network {
        Network { max_gap: max_gap.map(|g| (g * 1e9).round() as i64), last: None, ns: None }
    }

    fn output(ns: Option<String>, interfaces: Vec<Value>) -> Value {
        let mut scope = Value::obj();
        scope.set("kind", "process_network_namespace".into());
        scope.set("id", ns.into());
        let mut out = Value::obj();
        out.set("scope", scope);
        out.set("provider", "proc_net_dev".into());
        out.set("interfaces", Value::Arr(interfaces));
        out.set("read", Value::Null);
        out
    }

    pub fn sample(&mut self, group: &mut Group) -> (Value, Span) {
        use std::os::unix::fs::MetadataExt;
        let mut ns = None;
        match std::fs::metadata(NETNS) {
            Ok(st) => {
                let key = (st.dev(), st.ino());
                if self.ns.as_ref().map_or(true, |(k, _)| *k != key) {
                    self.ns = Some((key, namespace_id(key.0, key.1)));
                }
                ns = self.ns.as_ref().map(|(_, id)| id.clone());
            }
            Err(e) => group.note("netns", sysfs::reason_of(&e), Some(format!("{}: {}", NETNS, strerror(&e)))),
        }
        let (start, read) = timed_read(NET_DEV, sysfs::read_utf8);
        let parsed = match read {
            Ok((text, end)) => parse_net_dev(&text).map(|p| (p, end)).ok_or(None),
            Err(ReadError::Os(e)) => Err(Some(e)),
            Err(ReadError::Utf8) => Err(None),
        };
        let ((rows, bad), end) = match parsed {
            Ok(p) => p,
            Err(e) => {
                self.last = None;
                match e {
                    Some(e) if e.raw_os_error() == Some(libc::ENOENT) => group.note("net_dev", "not_exposed", None),
                    Some(e) => group.note("net_dev", sysfs::reason_of(&e), Some(format!("{}: {}", NET_DEV, strerror(&e)))),
                    None => group.note("net_dev", "invalid_data", Some(NET_DEV.into())),
                }
                return (Network::output(ns, vec![]), None);
            }
        };
        for (target, why) in &bad {
            group.note(target, why, Some(NET_DEV.into()));
        }
        let mut index: Vec<(String, u32)> = Vec::new();
        match if_indexes() {
            Ok(list) => {
                for (i, name) in list {
                    match index.iter_mut().find(|(n, _)| *n == name) {
                        Some(slot) => slot.1 = i, // a dict: the last one wins
                        None => index.push((name, i)),
                    }
                }
            }
            Err(e) => group.note("ifindex", sysfs::reason_of(&e), Some(format!("if_nameindex: {}", strerror(&e)))),
        }

        let last = self.last.take();
        let now = start;
        let window = last.as_ref().map(|l| now - l.0);
        let mut kept = Vec::new();
        let mut interfaces = Vec::new();
        let mut rows = rows;
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, row) in rows {
            let i = index.iter().find(|(n, _)| *n == name).map(|(_, i)| *i);
            let before = last.as_ref().and_then(|l| i.and_then(|i| l.2.iter().find(|(k, _)| k.0 == i && k.1 == name)));
            let mut rates = Value::Null;
            let reason = if ns.is_none() || i.is_none() {
                Some("identity_unavailable")
            } else if last.as_ref().map_or(false, |l| l.1.is_some() && l.1 != ns) {
                Some("namespace_changed")
            } else if before.is_none() {
                Some("warmup")
            } else if window.unwrap() <= 0 {
                Some("invalid_interval")
            } else if self.max_gap.map_or(false, |g| window.unwrap() > g) {
                Some("gap")
            } else if row.iter().zip(before.unwrap().1.iter()).any(|(a, b)| a < b) {
                Some("counter_regressed")
            } else {
                let b = before.unwrap().1;
                let d: Vec<u128> = row.iter().zip(b.iter()).map(|(a, b)| (a - b) as u128).collect();
                let w = window.unwrap();
                let mut r = Value::obj();
                r.set("rx_bytes_per_s", rate(d[0], w).into());
                r.set("tx_bytes_per_s", rate(d[4], w).into());
                r.set("rx_packets_per_s", rate(d[1], w).into());
                r.set("tx_packets_per_s", rate(d[5], w).into());
                rates = r;
                None
            };
            if let (Some(_), Some(i)) = (&ns, i) {
                kept.push(((i, name.clone()), row));
            }
            let dir = |v: &[u64]| {
                let mut o = Value::obj();
                for (k, x) in COUNTERS.iter().zip(v) {
                    o.set(k, (*x).into());
                }
                o
            };
            let mut v = Value::obj();
            v.set("name", name.as_str().into());
            v.set("ifindex", i.map(|i| i as i64).into());
            v.set("rx", dir(&row[..4]));
            v.set("tx", dir(&row[4..]));
            v.set("rates", rates);
            v.set("window_ms", window_ms(window, reason));
            v.set("reason", reason_value(reason));
            interfaces.push(group.got(v));
        }
        self.last = Some((now, ns.clone(), kept));
        (Network::output(ns, interfaces), Some((start, end)))
    }
}

// --- disk I/O -----------------------------------------------------------------------------------------

const DISKSTATS: &str = "/proc/diskstats";
const SYS_BLOCK: &str = "/sys/block";
const SECTOR: u128 = 512;
const MIN_FIELDS: usize = 11;
const IO: [&str; 4] = ["ios", "merges", "bytes", "time_ms"];

type DiskKey = (u64, u64, String);
type DiskRow = [u64; 10];

/// disk_io.valid_name: printable, no / or whitespace, at most 31 characters.
fn valid_disk_name(name: &str) -> bool {
    let n = name.chars().count();
    0 < n && n < 32 && name.chars().all(|c| py_printable(c) && c != '/' && !py_space(c))
}

/// disk_io.is_physical: Some(false) under devices/virtual, Some(true) otherwise, None without a link.
pub fn is_physical(name: &str) -> io::Result<Option<bool>> {
    match std::fs::read_link(format!("{}/{}", SYS_BLOCK, name)) {
        Ok(target) => Ok(Some(!format!("/{}", target.to_string_lossy()).contains("/devices/virtual/"))),
        Err(e) if e.raw_os_error() == Some(libc::ENOENT) => Ok(None),
        Err(e) => Err(e),
    }
}

/// disk_io.DiskCounters, kept between collections.
pub struct Disks {
    max_gap: Option<i64>,
    last: Option<(i64, Vec<(DiskKey, DiskRow)>)>,
    kind: Vec<(DiskKey, bool)>,
}

impl Disks {
    pub fn new(max_gap: Option<f64>) -> Disks {
        Disks { max_gap: max_gap.map(|g| (g * 1e9).round() as i64), last: None, kind: vec![] }
    }

    /// DiskCounters._is_physical: a confirmed classification of the same key is reused.
    fn classify(&self, key: &DiskKey, kind: &mut Vec<(DiskKey, bool)>, group: &mut Group) -> bool {
        if let Some((_, k)) = self.kind.iter().find(|(k, _)| k == key) {
            kind.retain(|(k, _)| k != key);
            kind.push((key.clone(), *k));
            return *k;
        }
        match is_physical(&key.2) {
            Err(e) => {
                group.note(&key.2, sysfs::reason_of(&e), Some(format!("{}/{}: {}", SYS_BLOCK, key.2, strerror(&e))));
                false
            }
            Ok(None) => false,
            Ok(Some(k)) => {
                kind.retain(|(x, _)| x != key);
                kind.push((key.clone(), k));
                k
            }
        }
    }

    /// disk_io.parse with identify: (rows, bad, kind); None for text without any row.
    fn parse(&self, text: &str, group: &mut Group) -> Option<(Vec<(DiskKey, DiskRow)>, Vec<(String, &'static str)>, Vec<(DiskKey, bool)>)> {
        let (mut rows, mut bad, mut kind): (Vec<(DiskKey, DiskRow)>, Vec<(String, &'static str)>, Vec<(DiskKey, bool)>) = (vec![], vec![], vec![]);
        let mut seen: Vec<(String, Option<DiskKey>)> = vec![];
        for (n, line) in text.split('\n').enumerate() {
            let fields = py_split(line);
            if fields.is_empty() {
                continue;
            }
            let name = if fields.len() > 2 { fields[2] } else { "" };
            if !valid_disk_name(name) {
                bad.push((format!("line{}", n + 1), "invalid_data"));
                continue;
            }
            if let Some((_, key)) = seen.iter().find(|(s, _)| s == name) {
                if let Some(key) = key {
                    rows.retain(|(k, _)| k != key);
                }
                bad.push((name.to_string(), "invalid_data"));
                continue;
            }
            seen.push((name.to_string(), None));
            let (Some(major), Some(minor)) = (counter(fields[0]), counter(fields[1])) else {
                bad.push((name.to_string(), "invalid_data"));
                continue;
            };
            let key = (major, minor, name.to_string());
            if !self.classify(&key, &mut kind, group) {
                continue;
            }
            let values: Option<Vec<u64>> = if fields.len() < 3 + MIN_FIELDS { None } else {
                let v: Option<Vec<u64>> = fields[3..3 + MIN_FIELDS].iter().map(|f| counter(f)).collect();
                v.filter(|_| fields[3 + MIN_FIELDS..].iter().all(|f| counter(f).is_some()))
            };
            let Some(v) = values else {
                bad.push((name.to_string(), "invalid_data"));
                continue;
            };
            seen.last_mut().unwrap().1 = Some(key.clone());
            rows.push((key, [v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9]]));
        }
        if seen.is_empty() && bad.is_empty() {
            return None;
        }
        Some((rows, bad, kind))
    }

    fn output(disks: Vec<Value>) -> Value {
        let mut scope = Value::obj();
        scope.set("kind", "host_block_devices".into());
        let mut out = Value::obj();
        out.set("scope", scope);
        out.set("provider", "proc_diskstats".into());
        out.set("disks", Value::Arr(disks));
        out.set("read", Value::Null);
        out
    }

    pub fn sample(&mut self, group: &mut Group) -> (Value, Span) {
        let (start, read) = timed_read(DISKSTATS, sysfs::read_utf8);
        let parsed = match read {
            Ok((text, end)) => self.parse(&text, group).map(|p| (p, end)).ok_or(None),
            Err(ReadError::Os(e)) => Err(Some(e)),
            Err(ReadError::Utf8) => Err(None),
        };
        let ((rows, bad, kind), end) = match parsed {
            Ok(p) => p,
            Err(e) => {
                self.last = None;
                self.kind = vec![];
                match e {
                    Some(e) if e.raw_os_error() == Some(libc::ENOENT) => group.note("diskstats", "not_exposed", None),
                    Some(e) => group.note("diskstats", sysfs::reason_of(&e), Some(format!("{}: {}", DISKSTATS, strerror(&e)))),
                    None => group.note("diskstats", "invalid_data", Some(DISKSTATS.into())),
                }
                return (Disks::output(vec![]), None);
            }
        };
        for (target, why) in &bad {
            group.note(target, why, Some(DISKSTATS.into()));
        }
        self.kind = kind;

        let last = self.last.take();
        let now = start;
        let window = last.as_ref().map(|l| now - l.0);
        let (mut kept, mut disks) = (vec![], vec![]);
        let mut rows = rows;
        rows.sort_by(|a, b| a.0 .2.cmp(&b.0 .2));
        for (key, row) in rows {
            if !self.kind.iter().any(|(k, physical)| *k == key && *physical) {
                continue;
            }
            let before = last.as_ref().and_then(|l| l.1.iter().find(|(k, _)| *k == key)).map(|(_, r)| *r);
            let mut rates = Value::Null;
            let reason = match before {
                None => Some("warmup"),
                Some(_) if window.unwrap() <= 0 => Some("invalid_interval"),
                Some(_) if self.max_gap.map_or(false, |g| window.unwrap() > g) => Some("gap"),
                // 8: in flight, a gauge
                Some(b) if row.iter().zip(b.iter()).enumerate().any(|(i, (a, b))| i != 8 && a < b) => Some("counter_regressed"),
                Some(b) => {
                    let d = |i: usize| row[i].wrapping_sub(b[i]) as u128;
                    let w = window.unwrap();
                    let mut r = Value::obj();
                    r.set("read_bytes_per_s", rate(d(2) * SECTOR, w).into());
                    r.set("write_bytes_per_s", rate(d(6) * SECTOR, w).into());
                    r.set("reads_per_s", rate(d(0), w).into());
                    r.set("writes_per_s", rate(d(4), w).into());
                    r.set("io_time_ratio", round_to(d(9) as f64 * 1e6 / w as f64, 4).into());
                    rates = r;
                    None
                }
            };
            let dir = |o: usize| {
                let mut v = Value::obj();
                let x = [row[o] as u128, row[o + 1] as u128, row[o + 2] as u128 * SECTOR, row[o + 3] as u128];
                for (k, x) in IO.iter().zip(x) {
                    v.set(k, x.into());
                }
                v
            };
            let mut v = Value::obj();
            v.set("name", key.2.as_str().into());
            v.set("major", key.0.into());
            v.set("minor", key.1.into());
            v.set("read", dir(0));
            v.set("write", dir(4));
            v.set("in_flight", row[8].into());
            v.set("io_time_ms", row[9].into());
            v.set("rates", rates);
            v.set("window_ms", window_ms(window, reason));
            v.set("reason", reason_value(reason));
            disks.push(group.got(v));
            kept.push((key, row));
        }
        self.last = Some((now, kept));
        if disks.is_empty() && group.reasons.is_empty() {
            group.note("disks", "not_detected", None);
        }
        (Disks::output(disks), Some((start, end)))
    }
}

// --- pressure -----------------------------------------------------------------------------------------

const PRESSURE: &str = "/proc/pressure";
const RESOURCES: [&str; 3] = ["cpu", "memory", "io"];

/// \d+\.\d+ (the file is ASCII, so ASCII digits)
fn decimal(s: &str) -> Option<f64> {
    let (a, b) = s.split_once('.')?;
    let digits = |x: &str| !x.is_empty() && x.bytes().all(|c| c.is_ascii_digit());
    if digits(a) && digits(b) { s.parse().ok() } else { None }
}

/// pressure.parse: [("some", avg10, avg60, avg300, total), ("full", ...)]; None if a line is not the known
/// format, a kind repeats or "some" is missing. A total beyond u128 is not taken (Python's int would be).
fn parse_pressure(text: &str) -> Option<Vec<(&str, [f64; 3], u128)>> {
    let mut out: Vec<(&str, [f64; 3], u128)> = vec![];
    for line in text.split('\n') {
        let line = py_strip(line);
        if line.is_empty() {
            continue;
        }
        let parts: Vec<&str> = line.split(' ').collect();
        let [kind, a10, a60, a300, total] = parts[..] else { return None };
        if !(kind == "some" || kind == "full") || out.iter().any(|(k, ..)| *k == kind) {
            return None;
        }
        let avg = |s: &str, key: &str| s.strip_prefix(key).and_then(decimal);
        let total = total.strip_prefix("total=").filter(|t| !t.is_empty() && t.bytes().all(|c| c.is_ascii_digit()))?;
        out.push((kind, [avg(a10, "avg10=")?, avg(a60, "avg60=")?, avg(a300, "avg300=")?], total.parse().ok()?));
    }
    if out.iter().any(|(k, ..)| *k == "some") { Some(out) } else { None }
}

/// pressure.Pressure: nothing kept between collections.
pub fn pressure(group: &mut Group) -> (Value, Span) {
    let mut resources = Value::obj();
    let output = |resources: Value| {
        let mut scope = Value::obj();
        scope.set("kind", "host".into());
        let mut out = Value::obj();
        out.set("scope", scope);
        out.set("provider", "proc_pressure".into());
        out.set("resources", resources);
        out.set("read", Value::Null);
        out
    };
    if !sysfs::is_dir(PRESSURE) {
        group.note("pressure", "not_exposed", None);
        return (output(resources), None);
    }
    let (start, mut end) = (clock(), None);
    for name in RESOURCES {
        let path = format!("{}/{}", PRESSURE, name);
        let text = match sysfs::read_ascii(&path) {
            Ok(t) => t,
            Err(ReadError::Os(e)) if e.raw_os_error() == Some(libc::ENOENT) => {
                group.note(name, "not_exposed", None);
                continue;
            }
            Err(ReadError::Os(e)) => {
                group.note(name, sysfs::reason_of(&e), Some(format!("{}: {}", path, strerror(&e))));
                continue;
            }
            Err(ReadError::Utf8) => {
                group.note(name, "invalid_data", Some(path));
                continue;
            }
        };
        end = Some(clock());
        let Some(kinds) = parse_pressure(&text) else {
            group.note(name, "invalid_data", Some(path));
            continue;
        };
        let kind = |k: &str| kinds.iter().find(|(x, ..)| *x == k).map(|(_, a, t)| {
            let mut v = Value::obj();
            v.set("avg10", a[0].into());
            v.set("avg60", a[1].into());
            v.set("avg300", a[2].into());
            v.set("total_us", (*t).into());
            v
        });
        let mut v = Value::obj();
        v.set("some", kind("some").into());
        // "CPU full is undefined at the system level" (psi.rst): reported as 0, not a value
        v.set("full", if name == "cpu" { Value::Null } else { kind("full").into() });
        resources.set(name, group.got(v));
    }
    let span = if matches!(&resources, Value::Obj(r) if !r.is_empty()) { end.map(|e| (start, e)) } else { None };
    (output(resources), span)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counters_like_python() {
        assert_eq!(counter("0"), Some(0));
        assert_eq!(counter("18446744073709551615"), Some(u64::MAX));
        assert_eq!(counter("18446744073709551616"), None);
        assert_eq!(counter("99999999999999999999999999999999999999999"), None);
        assert_eq!(counter("+1"), None);
        assert_eq!(counter("1.0"), None);
        assert_eq!(counter(""), None);
        assert_eq!(counter("١"), None); // an Arabic-Indic digit: isdigit, not isascii
    }

    #[test]
    fn names_like_python() {
        assert!(valid_ifname("eth0") && valid_ifname("wlé0") && !valid_ifname("a:b") && !valid_ifname("a b"));
        assert!(!valid_ifname(".") && !valid_ifname("0123456789abcdef") && !valid_ifname("ab\u{7f}"));
        assert!(valid_disk_name("nvme0n1") && !valid_disk_name("a/b") && !valid_disk_name(&"x".repeat(32)));
        assert!(valid_disk_name(&"é".repeat(31))); // characters, not bytes, for disks
    }

    #[test]
    fn netns_id_matches_python() {
        // python3 -c 'import hashlib;print(hashlib.sha256(b"{\"dev\":4,\"ino\":4026531840,\"v\":1}").hexdigest())'
        assert_eq!(namespace_id(4, 4026531840), format!("netns1:{}", crate::sha256::hex(br#"{"dev":4,"ino":4026531840,"v":1}"#)));
    }

    #[test]
    fn pressure_lines() {
        let ok = parse_pressure("some avg10=0.12 avg60=1.00 avg300=0.00 total=123\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n");
        assert_eq!(ok.unwrap()[0], ("some", [0.12, 1.0, 0.0], 123));
        assert!(parse_pressure("some avg10=0.12  avg60=1.00 avg300=0.00 total=1").is_none());
        assert!(parse_pressure("full avg10=0.00 avg60=0.00 avg300=0.00 total=0").is_none());
        assert!(parse_pressure("some avg10=1 avg60=1.00 avg300=0.00 total=1").is_none());
    }
}
