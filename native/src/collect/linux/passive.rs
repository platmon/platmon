//! The passive groups: collector/network.py, disk_io.py and pressure.py. Each is read once per collection,
//! after the required data, and gives its output and the (start, end) of its read.
use crate::json::{round_to, Value};
use crate::metadata::{clock, Group, Span};
use crate::pystr::{py_printable, py_space, py_split, py_strip};
use super::sysfs::{self, ReadError};
use crate::collect::{rate, reason_value, window_ms};
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
        match std::fs::metadata(&*sysfs::at(NETNS)) {
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
    match std::fs::read_link(&*sysfs::at(&format!("{}/{}", SYS_BLOCK, name))) {
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
    use crate::collect::linux::sysfs::fake::{self, Root};

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

    // tests/collector/test_pressure.py, ported (docs/development/python-test-port.md).
    const SOME: &str = "some avg10=1.25 avg60=0.50 avg300=0.10 total=123456";
    const FULL: &str = "full avg10=0.00 avg60=0.00 avg300=0.00 total=0";
    const T0: i64 = 1_000_000_000_000;

    fn write_pressure(root: &Root, files: &[(&str, &str)]) {
        root.mkdir(PRESSURE);
        for (name, text) in files {
            root.put(&format!("{}/{}", PRESSURE, name), text);
        }
    }

    fn read_pressure(root: &Root) -> (Value, Span, Value) {
        root.at(T0);
        let mut g = Group::new("pressure", false);
        let (out, span) = pressure(&mut g);
        (out, span, g.status())
    }

    fn resource_names(out: &Value) -> Vec<String> {
        match out.get("resources") { Some(Value::Obj(r)) => r.iter().map(|(k, _)| k.clone()).collect(), _ => vec![] }
    }

    #[test]
    fn parse_some_and_full() {
        let text = format!("{}\n{}\n", SOME, FULL);
        let both = parse_pressure(&text).unwrap();
        assert_eq!(both, [("some", [1.25, 0.5, 0.1], 123456), ("full", [0.0, 0.0, 0.0], 0)]);
        assert_eq!(parse_pressure(SOME).unwrap(), [("some", [1.25, 0.5, 0.1], 123456)]); // before full existed
    }

    #[test]
    fn parse_refuses_other_formats() {
        for text in ["".to_string(), FULL.into(), format!("{}\n{}", SOME, SOME), "some avg10=1 avg60=0.00 avg300=0.00 total=1".into(),
                     "some avg10=-1.00 avg60=0.00 avg300=0.00 total=1".into(), "some avg10=1.00 total=1".into(),
                     format!("{} extra=1", SOME), "partial avg10=0.00 avg60=0.00 avg300=0.00 total=0".into()] {
            assert!(parse_pressure(&text).is_none(), "{:?}", text);
        }
    }

    #[test]
    fn all_three_with_cpu_full_left_out() {
        let root = Root::new(T0);
        let both = format!("{}\n{}\n", SOME, FULL);
        write_pressure(&root, &[("cpu", &both), ("memory", &both), ("io", &both)]);
        let (out, span, status) = read_pressure(&root);
        let some = r#"{"avg10":1.25,"avg60":0.5,"avg300":0.1,"total_us":123456}"#;
        let full = r#"{"avg10":0.0,"avg60":0.0,"avg300":0.0,"total_us":0}"#;
        assert_eq!(out.to_string(false), format!(
            r#"{{"scope":{{"kind":"host"}},"provider":"proc_pressure","resources":{{"cpu":{{"some":{s},"full":null}},"memory":{{"some":{s},"full":{f}}},"io":{{"some":{s},"full":{f}}}}},"read":null}}"#,
            s = some, f = full)); // cpu's full is undefined at the system level, not a measured 0
        assert!(span.is_some());
        assert_eq!(status.get("state").unwrap().to_string(false), r#""ok""#);
    }

    #[test]
    fn no_pressure_directory_opens_nothing() {
        let root = Root::new(T0);
        let (out, span, status) = read_pressure(&root);
        assert!(resource_names(&out).is_empty() && span.is_none());
        assert_eq!(status.to_string(false), r#"{"state":"unavailable","reason":"not_exposed","issues":[],"issues_truncated":0}"#);
    }

    #[test]
    fn one_unreadable_file_keeps_the_others() {
        for (case, state, reason) in [("missing", "ok", None), ("unreadable", "partial", Some("permission_denied")),
                                      ("garbage", "partial", Some("invalid_data"))] {
            if case == "unreadable" && unsafe { libc::geteuid() } == 0 {
                continue; // root reads a file of mode 000
            }
            let root = Root::new(T0);
            write_pressure(&root, &[("cpu", SOME), ("io", SOME)]);
            match case {
                "unreadable" => { root.put("/proc/pressure/memory", SOME); root.chmod("/proc/pressure/memory", 0) }
                "garbage" => root.put("/proc/pressure/memory", "garbage"),
                _ => {}
            }
            let (out, _, status) = read_pressure(&root);
            assert_eq!(resource_names(&out), ["cpu", "io"], "{}", case);
            assert_eq!(status.get("state").unwrap().to_string(false), format!("{:?}", state), "{}", case);
            if let Some(reason) = reason {
                assert!(status.get("issues").unwrap().to_string(false).contains(&format!(r#""reason":"{}""#, reason)), "{}", case);
            }
        }
    }

    #[test]
    fn all_unreadable_is_an_error_without_a_read() {
        let root = Root::new(T0);
        write_pressure(&root, &[("cpu", "x"), ("memory", "x"), ("io", "x")]);
        let (out, span, status) = read_pressure(&root);
        assert!(resource_names(&out).is_empty() && span.is_none());
        assert_eq!(status.get("state").unwrap().to_string(false), r#""error""#);
    }

    #[test]
    fn the_read_spans_the_three_files() {
        let root = Root::new(T0);
        write_pressure(&root, &[("cpu", SOME), ("memory", SOME), ("io", SOME)]);
        let (_, span, _) = read_pressure(&root);
        assert_eq!(span, Some((T0, T0 + 3 * crate::metadata::FAKE_TICK))); // before the first read to after the last
    }

    #[test]
    fn disabled_reads_nothing() {
        let root = Root::new(T0);
        root.required();
        write_pressure(&root, &[("cpu", SOME), ("memory", SOME), ("io", SOME)]);
        let stats = crate::collect::linux::Collector::new(5.0, [false, false, false]).collect().ok().unwrap().stats;
        assert!(stats.get("pressure").is_none() && stats.get("collectors").unwrap().get("pressure").is_none());
        root.at(T0);
        let stats = crate::collect::linux::Collector::new(5.0, [false, false, true]).collect().ok().unwrap().stats;
        assert_eq!(stats.get("collectors").unwrap().get("pressure").unwrap().get("state").unwrap().to_string(false), r#""ok""#);
    }

    #[test]
    fn real_host_pressure() {
        let mut g = Group::new("pressure", false);
        let (out, span) = pressure(&mut g);
        let Some(Value::Obj(resources)) = out.get("resources") else { panic!("no resources") };
        if !std::path::Path::new(PRESSURE).is_dir() {
            assert!(resources.is_empty());
            return; // no /proc/pressure on this host (PSI not built in or disabled)
        }
        assert!(span.is_some() && resources.iter().all(|(k, _)| ["cpu", "memory", "io"].contains(&k.as_str())));
        for (_, r) in resources {
            let Some(Value::Float(avg)) = r.get("some").and_then(|s| s.get("avg10")) else { panic!("avg10") };
            assert!((0.0..=100.0).contains(avg));
        }
    }

    // tests/collector/test_disk_io.py, ported. A disk is physical, virtual or a partition by its /sys/block
    // link, as the kernel makes them.
    const S: i64 = 1_000_000_000;

    /// One /proc/diskstats line: r/w = (ios, merges, sectors, ms); then in flight, io ms, weighted ms, 4 more.
    fn row(major: u64, minor: u64, name: &str, r: [u64; 4], w: [u64; 4], in_flight: u64, io_ms: u64) -> String {
        format!("{:4} {:7} {} {} {} {} {} {} {} {} {} {} {} 0 0 0 0 0 0 0", major, minor, name, r[0], r[1], r[2], r[3], w[0], w[1], w[2], w[3], in_flight, io_ms)
    }

    fn plain(major: u64, minor: u64, name: &str) -> String {
        row(major, minor, name, [0; 4], [0; 4], 0, 0)
    }

    /// /sys/block/<name>: Some(true) a physical disk, Some(false) a virtual one, None no entry (a partition).
    fn kind(root: &Root, name: &str, physical: Option<bool>) {
        let link = format!("{}/{}", SYS_BLOCK, name);
        if root.path(&link).symlink_metadata().is_ok() {
            root.remove(&link);
        }
        match physical {
            Some(true) => root.link(&link, &format!("../devices/pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:0/block/{}", name)),
            Some(false) => root.link(&link, &format!("../devices/virtual/block/{}", name)),
            None => {}
        }
    }

    fn diskstats(root: &Root, rows: &[String]) {
        root.put(DISKSTATS, rows.iter().map(|r| format!("{}\n", r)).collect::<String>());
    }

    fn sample_disks(d: &mut Disks, root: &Root, t: i64) -> (Value, Span, Value) {
        root.at(t);
        let mut g = Group::new("disk_io", false);
        let (out, span) = d.sample(&mut g);
        (out, span, g.status())
    }

    fn disk_named(out: &Value, name: &str) -> Value {
        match out.get("disks") { Some(Value::Arr(d)) => d.iter().find(|x| x.get("name") == Some(&Value::from(name))).cloned().unwrap_or(Value::Null), _ => Value::Null }
    }

    fn names(out: &Value) -> Vec<String> {
        match out.get("disks") { Some(Value::Arr(d)) => d.iter().map(|x| x.get("name").unwrap().to_string(false)).collect(), _ => vec![] }
    }

    fn reason(out: &Value, name: &str) -> String {
        disk_named(out, name).get("reason").map_or("absent".into(), |r| r.to_string(false))
    }

    #[test]
    fn only_physical_disks_with_rates_over_the_observed_window() {
        let root = Root::new(T0);
        for (n, k) in [("nvme0n1", Some(true)), ("nvme0n1p1", None), ("loop0", Some(false)), ("zram0", Some(false))] {
            kind(&root, n, k);
        }
        diskstats(&root, &[plain(259, 0, "nvme0n1"), plain(259, 1, "nvme0n1p1"), plain(7, 0, "loop0"), plain(252, 0, "zram0")]);
        let mut d = Disks::new(Some(5.0));
        let (out, span, status) = sample_disks(&mut d, &root, T0);
        assert_eq!((names(&out), reason(&out, "nvme0n1")), (vec![r#""nvme0n1""#.to_string()], r#""warmup""#.to_string()));
        assert_eq!(out.get("scope").unwrap().to_string(false), r#"{"kind":"host_block_devices"}"#);
        assert_eq!((out.get("provider").unwrap().to_string(false), out.get("read").unwrap().to_string(false)), (r#""proc_diskstats""#.into(), "null".into()));
        assert_eq!(span, Some((T0, T0 + crate::metadata::FAKE_TICK)));
        assert_eq!(status.get("state").unwrap().to_string(false), r#""ok""#);
        diskstats(&root, &[row(259, 0, "nvme0n1", [10, 1, 80, 4], [20, 2, 160, 6], 3, 500), plain(259, 1, "nvme0n1p1"), plain(7, 0, "loop0")]);
        let (out, _, _) = sample_disks(&mut d, &root, T0 + 2 * S);
        assert_eq!(disk_named(&out, "nvme0n1").to_string(false), concat!(r#"{"name":"nvme0n1","major":259,"minor":0,"#,
            r#""read":{"ios":10,"merges":1,"bytes":40960,"time_ms":4},"write":{"ios":20,"merges":2,"bytes":81920,"time_ms":6},"#,
            r#""in_flight":3,"io_time_ms":500,"rates":{"read_bytes_per_s":20480.0,"write_bytes_per_s":40960.0,"reads_per_s":5.0,"#,
            r#""writes_per_s":10.0,"io_time_ratio":0.25},"window_ms":2000.0,"reason":null}"#));
        let (out, _, _) = sample_disks(&mut d, &root, T0 + 3 * S); // idle: measured 0, not missing
        assert_eq!(disk_named(&out, "nvme0n1").get("rates").unwrap().get("write_bytes_per_s").unwrap().to_string(false), "0.0");
    }

    #[test]
    fn confirmed_kinds_are_reused_and_missing_links_retried() {
        let root = Root::new(T0);
        kind(&root, "sda", Some(true));
        kind(&root, "loop0", Some(false));
        diskstats(&root, &[plain(8, 0, "sda"), plain(8, 1, "sda1"), plain(7, 0, "loop0")]);
        let mut d = Disks::new(Some(5.0));
        sample_disks(&mut d, &root, T0);
        kind(&root, "sda", Some(false)); // a confirmed kind is not looked up again: sda stays a disk
        kind(&root, "loop0", Some(true));
        kind(&root, "sda1", Some(true)); // an entry that was missing is looked up again
        let (out, _, _) = sample_disks(&mut d, &root, T0 + S);
        assert_eq!(names(&out), [r#""sda""#, r#""sda1""#]);
        diskstats(&root, &[plain(7, 0, "loop0")]);
        sample_disks(&mut d, &root, T0 + 2 * S);
        assert_eq!(d.kind.iter().map(|(k, _)| k.2.as_str()).collect::<Vec<_>>(), ["loop0"]); // names that are gone are not kept
        kind(&root, "sda", Some(true));
        diskstats(&root, &[plain(8, 0, "sda"), plain(7, 0, "loop0")]); // back: looked up again, and a new baseline
        assert_eq!(reason(&sample_disks(&mut d, &root, T0 + 3 * S).0, "sda"), r#""warmup""#);
    }

    #[test]
    fn a_failed_lookup_is_reported_and_retried() {
        let root = Root::new(T0);
        kind(&root, "sda", Some(true));
        root.put("/sys/block/sdb", "not a link"); // readlink fails (EINVAL): Python's PermissionError stands for any
        diskstats(&root, &[plain(8, 0, "sda"), plain(8, 16, "sdb")]);
        let mut d = Disks::new(Some(5.0));
        let (out, _, status) = sample_disks(&mut d, &root, T0);
        assert_eq!(names(&out), [r#""sda""#]);
        assert_eq!(status.to_string(false), r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"sdb","reason":"io_error"}],"issues_truncated":0}"#);
        root.remove("/sys/block/sdb");
        kind(&root, "sdb", Some(true));
        let (out, _, status) = sample_disks(&mut d, &root, T0 + S);
        assert_eq!((reason(&out, "sdb"), status.get("state").unwrap().to_string(false)), (r#""warmup""#.into(), r#""ok""#.into()));
    }

    #[test]
    fn any_counter_going_down_gives_no_rate_but_in_flight_may() {
        for field in [0, 1, 2, 3, 4, 5, 6, 7, 9] {
            let root = Root::new(T0);
            kind(&root, "sda", Some(true));
            let v = [100u64; 10];
            diskstats(&root, &[row(8, 0, "sda", [v[0], v[1], v[2], v[3]], [v[4], v[5], v[6], v[7]], v[8], v[9])]);
            let mut d = Disks::new(Some(5.0));
            sample_disks(&mut d, &root, T0);
            let mut l = v;
            l[field] -= 1;
            diskstats(&root, &[row(8, 0, "sda", [l[0], l[1], l[2], l[3]], [l[4], l[5], l[6], l[7]], l[8], l[9])]);
            let x = disk_named(&sample_disks(&mut d, &root, T0 + S).0, "sda");
            assert_eq!(["reason", "rates", "window_ms"].map(|k| x.get(k).unwrap().to_string(false)), [r#""counter_regressed""#, "null", "1000.0"], "{}", field);
            diskstats(&root, &[row(8, 0, "sda", [l[0], l[1], l[2], l[3]], [l[4], l[5], l[6], l[7]], 0, l[9])]); // in flight drops: fine
            assert_eq!(reason(&sample_disks(&mut d, &root, T0 + 2 * S).0, "sda"), "null", "{}", field);
        }
    }

    #[test]
    fn gap_boundary_and_bad_intervals() {
        for (window, why) in [(5 * S, "null"), (5 * S + 1, r#""gap""#), (0, r#""invalid_interval""#), (-S, r#""invalid_interval""#)] {
            let root = Root::new(T0);
            kind(&root, "sda", Some(true));
            diskstats(&root, &[plain(8, 0, "sda")]);
            let mut d = Disks::new(Some(5.0));
            sample_disks(&mut d, &root, T0);
            let x = disk_named(&sample_disks(&mut d, &root, T0 + window).0, "sda");
            assert_eq!(x.get("reason").unwrap().to_string(false), why, "{}", window);
            assert_eq!(x.get("rates").unwrap().is_null(), why != "null");
            assert_eq!(reason(&sample_disks(&mut d, &root, T0 + window + S).0, "sda"), "null");
        }
    }

    #[test]
    fn a_new_device_number_under_the_same_name_starts_over() {
        let root = Root::new(T0);
        kind(&root, "sda", Some(true));
        diskstats(&root, &[row(8, 0, "sda", [100, 0, 0, 0], [0; 4], 0, 0)]);
        let mut d = Disks::new(Some(5.0));
        sample_disks(&mut d, &root, T0);
        diskstats(&root, &[row(8, 32, "sda", [200, 0, 0, 0], [0; 4], 0, 0)]);
        assert_eq!(reason(&sample_disks(&mut d, &root, T0 + S).0, "sda"), r#""warmup""#);
    }

    #[test]
    fn bad_rows_are_left_out_and_others_kept() {
        let root = Root::new(T0);
        for n in ["sda", "sdb", "sdc", "sdd", "sde", "dup", "sdf"] {
            kind(&root, n, Some(true));
        }
        let text = [plain(8, 0, "sda"), "   8  16 sdb 1 2 3".into(), // short
                    plain(8, 32, "sdc").replacen(" 0 ", " -1 ", 1), // signed
                    plain(8, 48, "sdd").replacen(" 0 ", &format!(" {} ", 1u128 << 64), 1), // too large
                    plain(8, 64, "sde") + " x", // a later field not a counter
                    plain(8, 80, "dup"), plain(8, 96, "dup"), plain(8, 112, "a/b"), "8 128".into(),
                    "   8     144 sdf 0 0 0 0 0 0 0 0 0 0 0".into(), "".into()].join("\n"); // 11 fields: an old kernel, fine
        let mut g = Group::new("disk_io", false);
        let (rows, bad, _) = Disks::new(None).parse(&text, &mut g).unwrap();
        assert_eq!(rows.iter().map(|(k, _)| k.2.as_str()).collect::<Vec<_>>(), ["sda", "sdf"]);
        assert_eq!(bad.iter().map(|(t, _)| t.as_str()).collect::<Vec<_>>(), ["sdb", "sdc", "sdd", "sde", "dup", "line8", "line9"]);
        assert!(bad.iter().all(|(_, r)| *r == "invalid_data"));
    }

    #[test]
    fn rows_of_left_out_devices_are_not_converted_or_reported() {
        let root = Root::new(T0);
        kind(&root, "sda", Some(true));
        kind(&root, "loop0", Some(false));
        root.put(DISKSTATS, format!("{}\n7 0 loop0 x y\n8 1 sda1 -1\n", plain(8, 0, "sda")));
        let (out, _, status) = sample_disks(&mut Disks::new(Some(5.0)), &root, T0);
        assert_eq!(names(&out), [r#""sda""#]);
        assert_eq!(status.to_string(false), r#"{"state":"ok","reason":null,"issues":[],"issues_truncated":0}"#);
        let mut g = Group::new("disk_io", false);
        let (rows, bad, _) = Disks::new(None).parse("7 0 loop0 x y\n", &mut g).unwrap(); // listed, so not empty, but nothing to collect
        assert!(rows.is_empty() && bad.is_empty());
    }

    #[test]
    fn a_broken_file_gives_nothing_and_clears_baselines() {
        for text in [&b"\xff\xfe"[..], b"", b"\n\n"] {
            let root = Root::new(T0);
            kind(&root, "sda", Some(true));
            diskstats(&root, &[plain(8, 0, "sda")]);
            let mut d = Disks::new(Some(5.0));
            sample_disks(&mut d, &root, T0);
            root.put(DISKSTATS, text);
            let (out, span, status) = sample_disks(&mut d, &root, T0 + S);
            assert!(names(&out).is_empty() && span.is_none());
            assert_eq!(status.to_string(false), r#"{"state":"error","reason":"invalid_data","issues":[{"target":"diskstats","reason":"invalid_data"}],"issues_truncated":0}"#);
            diskstats(&root, &[plain(8, 0, "sda")]);
            assert_eq!(reason(&sample_disks(&mut d, &root, T0 + 2 * S).0, "sda"), r#""warmup""#);
        }
    }

    #[test]
    fn unreadable_file() {
        for (errno, state, why) in [(libc::ENOENT, "unavailable", "not_exposed"), (libc::EACCES, "error", "permission_denied"), (libc::EIO, "error", "io_error")] {
            let root = Root::new(T0);
            kind(&root, "sda", Some(true));
            diskstats(&root, &[plain(8, 0, "sda")]);
            let mut d = Disks::new(Some(5.0));
            sample_disks(&mut d, &root, T0);
            fake::answers(DISKSTATS, vec![Err(errno)]);
            let (out, span, status) = sample_disks(&mut d, &root, T0 + S);
            assert!(names(&out).is_empty() && span.is_none() && d.last.is_none());
            assert_eq!((status.get("state").unwrap().to_string(false), status.get("reason").unwrap().to_string(false)),
                       (format!("{:?}", state), format!("{:?}", why)));
        }
    }

    #[test]
    fn no_physical_disk_is_not_detected_not_an_error() {
        let root = Root::new(T0);
        kind(&root, "loop0", Some(false));
        kind(&root, "ram0", Some(false));
        diskstats(&root, &[plain(7, 0, "loop0"), plain(1, 0, "ram0")]);
        let (out, span, status) = sample_disks(&mut Disks::new(Some(5.0)), &root, T0);
        assert!(names(&out).is_empty() && span.is_some());
        assert_eq!(status.to_string(false), r#"{"state":"unavailable","reason":"not_detected","issues":[],"issues_truncated":0}"#);
    }

    #[test]
    fn many_devices_and_capped_issues() {
        let root = Root::new(T0);
        let mut rows: Vec<String> = (0..200).map(|n| { kind(&root, &format!("loop{}", n), Some(false)); plain(7, n, &format!("loop{}", n)) }).collect();
        kind(&root, "sda", Some(true));
        rows.push(plain(8, 0, "sda"));
        rows.extend((0..100).map(|n| { kind(&root, &format!("bad{}", n), Some(true)); format!("8 {} bad{} 1", n + 1, n) }));
        diskstats(&root, &rows);
        let (out, _, status) = sample_disks(&mut Disks::new(Some(5.0)), &root, T0);
        assert_eq!(names(&out), [r#""sda""#]);
        assert!(status.get("issues").unwrap().get_index(31).is_some() && status.get("issues").unwrap().get_index(32).is_none());
        assert_eq!(status.get("issues_truncated").unwrap().to_string(false), "68");
    }

    #[test]
    fn changing_the_output_does_not_touch_the_baseline() {
        let root = Root::new(T0);
        kind(&root, "sda", Some(true));
        diskstats(&root, &[row(8, 0, "sda", [10, 0, 0, 0], [0; 4], 0, 0)]);
        let mut d = Disks::new(Some(5.0));
        let (mut out, _, _) = sample_disks(&mut d, &root, T0);
        *out.get_mut("disks").unwrap() = Value::Arr(vec![]);
        assert_eq!(disk_named(&sample_disks(&mut d, &root, T0 + S).0, "sda").get("rates").unwrap().get("reads_per_s").unwrap().to_string(false), "0.0");
    }

    #[test]
    fn is_physical_follows_the_sysfs_link() {
        let root = Root::new(T0);
        kind(&root, "nvme0n1", Some(true));
        kind(&root, "loop0", Some(false));
        assert_eq!((is_physical("nvme0n1").unwrap(), is_physical("loop0").unwrap()), (Some(true), Some(false)));
        assert_eq!(is_physical("nvme0n1p1").unwrap(), None); // a partition has no /sys/block entry
    }

    #[test]
    fn on_unless_disabled_and_disabled_never_reads() {
        let root = Root::new(T0);
        root.required();
        kind(&root, "sda", Some(true));
        diskstats(&root, &[plain(8, 0, "sda")]);
        let stats = crate::collect::linux::Collector::new(5.0, [false, false, false]).collect().ok().unwrap().stats;
        assert!(stats.get("disk_io").is_none() && stats.get("collectors").unwrap().get("disk_io").is_none());
        let mut c = crate::collect::linux::Collector::new(5.0, [false, true, false]);
        assert_eq!(c.disks.as_ref().unwrap().max_gap, Some(5 * S));
        root.at(T0);
        let stats = c.collect().ok().unwrap().stats;
        assert_eq!(stats.get("collectors").unwrap().get("disk_io").unwrap().get("state").unwrap().to_string(false), r#""ok""#);
        assert_eq!(stats.get("disk").unwrap().to_string(false).matches(':').count(), 2); // the filesystem usage: total and used, unchanged
    }

    #[test]
    fn real_host_reading() {
        let mut g = Group::new("disk_io", false);
        let (out, span) = Disks::new(None).sample(&mut g);
        assert!(["\"ok\"", "\"partial\"", "\"unavailable\""].contains(&g.status().get("state").unwrap().to_string(false).as_str()));
        if std::path::Path::new(DISKSTATS).exists() {
            assert!(span.is_some());
            if let Some(Value::Arr(disks)) = out.get("disks") {
                for d in disks {
                    let Some(Value::Str(name)) = d.get("name") else { panic!() };
                    assert_eq!(is_physical(name).unwrap(), Some(true));
                }
            }
        }
    }

    #[test]
    fn changed_device_number_rechecks_classification() {
        let root = Root::new(T0);
        kind(&root, "sda", Some(true));
        diskstats(&root, &[plain(8, 0, "sda")]);
        let mut d = Disks::new(Some(5.0));
        assert_eq!(reason(&sample_disks(&mut d, &root, T0).0, "sda"), r#""warmup""#);
        kind(&root, "sda", Some(false));
        diskstats(&root, &[plain(8, 32, "sda")]);
        assert!(names(&sample_disks(&mut d, &root, T0 + S).0).is_empty());
    }

    #[test]
    fn missing_sysfs_entry_is_retried_while_diskstats_name_remains() {
        let root = Root::new(T0);
        diskstats(&root, &[plain(8, 0, "sda")]);
        let mut d = Disks::new(Some(5.0));
        assert!(names(&sample_disks(&mut d, &root, T0).0).is_empty());
        kind(&root, "sda", Some(true));
        assert_eq!(reason(&sample_disks(&mut d, &root, T0 + S).0, "sda"), r#""warmup""#);
    }

    #[test]
    fn file_failure_discards_classification_cache() {
        let root = Root::new(T0);
        kind(&root, "sda", Some(true));
        diskstats(&root, &[plain(8, 0, "sda")]);
        let mut d = Disks::new(Some(5.0));
        sample_disks(&mut d, &root, T0);
        fake::answers(DISKSTATS, vec![Err(libc::EIO)]);
        sample_disks(&mut d, &root, T0 + S);
        kind(&root, "sda", Some(false));
        assert!(names(&sample_disks(&mut d, &root, T0 + 2 * S).0).is_empty());
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
