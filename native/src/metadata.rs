//! collector/metadata.py: one optional group's diagnostics, timed reads, sensor sources and pointers.
use crate::json::Value;
use crate::{sha256, sysfs};
use std::sync::atomic::{AtomicI64, Ordering};

pub const PROBLEMS: [&str; 6] = ["internal_error", "permission_denied", "io_error", "disappeared", "mount_changed", "invalid_data"];
pub const ABSENT: [&str; 4] = ["no_data", "not_exposed", "not_detected", "unsupported_platform"];
const MAX_ISSUES: usize = 32;
const MAX_TARGET: usize = 64;

// sampler.pick_clock: CLOCK_BOOTTIME, which keeps counting through suspend, so a window across a suspend
// is as long as it really was (and a gap); CLOCK_MONOTONIC where it is not available. std's Instant is
// CLOCK_MONOTONIC on Linux and stops while suspended, so it is not used for collection times.
fn os_clock(id: libc::clockid_t) -> Option<i64> {
    let mut t = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    (unsafe { libc::clock_gettime(id, &mut t) } == 0).then(|| t.tv_sec as i64 * 1_000_000_000 + t.tv_nsec as i64)
}

/// The clock this process uses, chosen once like pick_clock: "boottime" or "monotonic".
pub fn clock_source() -> &'static str {
    static SOURCE: std::sync::OnceLock<&'static str> = std::sync::OnceLock::new();
    SOURCE.get_or_init(|| if os_clock(libc::CLOCK_BOOTTIME).is_some() { "boottime" } else { "monotonic" })
}

const UNSET: i64 = i64::MIN;
static FAKE_BASE: AtomicI64 = AtomicI64::new(UNSET);
static FAKE_CALLS: AtomicI64 = AtomicI64::new(0);
static FAKE_WALL_US: AtomicI64 = AtomicI64::new(UNSET);
/// The step between two calls of a set clock (fixture tests: the same reads give the same spans).
pub const FAKE_TICK: i64 = 1_000;

/// Sets the clocks for the next collection (script mode): clock() returns base, base + FAKE_TICK, ...
/// and wall() wall_us / 1e6. Collections never set them.
pub fn set_clocks(base: i64, wall_us: i64) {
    FAKE_BASE.store(base, Ordering::Relaxed);
    FAKE_CALLS.store(0, Ordering::Relaxed);
    FAKE_WALL_US.store(wall_us, Ordering::Relaxed);
}

/// Elapsed nanoseconds on the Sampler's clock (see clock_source).
pub fn clock() -> i64 {
    let base = FAKE_BASE.load(Ordering::Relaxed);
    if base != UNSET {
        return base + FAKE_TICK * FAKE_CALLS.fetch_add(1, Ordering::Relaxed);
    }
    let id = if clock_source() == "boottime" { libc::CLOCK_BOOTTIME } else { libc::CLOCK_MONOTONIC };
    os_clock(id).unwrap_or(0)
}

/// Unix time in seconds (time.time()).
pub fn wall() -> f64 {
    let us = FAKE_WALL_US.load(Ordering::Relaxed);
    if us != UNSET {
        return us as f64 / 1e6;
    }
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

pub type Span = Option<(i64, i64)>;

/// Where a sensor value sits in the output; fans are named by a fan's own id, like Python's id(fan).
#[derive(Clone, Debug, PartialEq)]
pub enum Slot {
    Key(String),
    Cpu(i64),
    Fan(u64, &'static str),
}

pub struct Group {
    pub name: String,
    pub values: usize,
    pub reasons: Vec<&'static str>,
    pub issues: Vec<(String, &'static str)>,
    pub truncated: usize,
    pub details: Vec<String>,
    pub trace: bool,
    pub span: Span,
    pub sensors: Vec<(Slot, Value)>,
}

impl Group {
    pub fn new(name: &str, trace: bool) -> Group {
        Group { name: name.to_string(), values: 0, reasons: Vec::new(), issues: Vec::new(), truncated: 0,
                details: Vec::new(), trace, span: None, sensors: Vec::new() }
    }

    pub fn note(&mut self, target: &str, reason: &'static str, detail: Option<String>) {
        if !self.reasons.contains(&reason) {
            self.reasons.push(reason);
        }
        if PROBLEMS.contains(&reason) {
            if self.issues.len() < MAX_ISSUES {
                self.issues.push((target.chars().take(MAX_TARGET).collect(), reason));
                self.details.push(format!("{}: {}", target, reason)
                    + &detail.map(|d| format!(" ({})", d)).unwrap_or_default());
            } else {
                self.truncated += 1;
            }
        }
    }

    pub fn merge(&mut self, other: &Group) {
        for r in &other.reasons {
            if !self.reasons.contains(r) {
                self.reasons.push(r);
            }
        }
        for (issue, detail) in other.issues.iter().zip(&other.details) {
            if self.issues.len() < MAX_ISSUES {
                self.issues.push(issue.clone());
                self.details.push(detail.clone());
            } else {
                self.truncated += 1;
            }
        }
        self.truncated += other.truncated;
    }

    pub fn got<T>(&mut self, value: T) -> T {
        self.values += 1;
        value
    }

    /// Group.read: parse(content of path), or None with the reason noted. found: what this collection
    /// discovered by listing; if that is gone too the file disappeared, else it is not exposed.
    pub fn read_with<T>(&mut self, path: &str, target: &str, found: Option<&str>, io_absent: bool,
                        parse: impl Fn(&str) -> Option<T>) -> Option<T> {
        self.span = None;
        let start = if self.trace { clock() } else { 0 };
        let text = match sysfs::read_text(path) {
            Ok(t) => t,
            Err(sysfs::ReadError::Utf8) => { self.note(target, "invalid_data", Some(path.into())); return None; }
            Err(sysfs::ReadError::Os(e)) => {
                if e.raw_os_error() == Some(2) {
                    let gone = found.map_or(false, |f| !std::path::Path::new(f).exists());
                    self.note(target, if gone { "disappeared" } else { "not_exposed" }, None);
                } else {
                    let r = sysfs::reason_of(&e);
                    if io_absent && r == "io_error" {
                        self.note(target, "no_data", None);
                    } else {
                        self.note(target, r, Some(format!("{}: {}", path, e)));
                    }
                }
                return None;
            }
        };
        match parse(&text) {
            Some(v) => {
                if self.trace {
                    self.span = Some((start, clock()));
                }
                Some(v)
            }
            None => { self.note(target, "invalid_data", Some(path.into())); None }
        }
    }

    pub fn read_int(&mut self, path: &str, target: &str, found: Option<&str>) -> Option<i64> {
        self.read_with(path, target, found, false, py_int)
    }

    pub fn read_str(&mut self, path: &str, target: &str, found: Option<&str>) -> Option<String> {
        self.read_with(path, target, found, false, |t| Some(t.to_string()))
    }

    pub fn sensor(&mut self, slot: Slot, source: Option<Value>, span: Span) {
        if let (true, Some(Value::Obj(mut items))) = (self.trace, source) {
            items.push(("span".into(), match span {
                Some((a, b)) => Value::Arr(vec![a.into(), b.into()]),
                None => Value::Null,
            }));
            self.sensors.retain(|(s, _)| *s != slot);
            self.sensors.push((slot, Value::Obj(items)));
        }
    }

    pub fn status(&self) -> Value {
        let problems: Vec<&str> = PROBLEMS.iter().copied().filter(|r| self.reasons.contains(r)).collect();
        let (state, reason): (&str, Option<&str>) = if self.values > 0 {
            if problems.is_empty() { ("ok", None) } else { ("partial", Some("some_unreadable")) }
        } else if let Some(p) = problems.first() {
            ("error", Some(p))
        } else {
            ("unavailable", Some(ABSENT.iter().copied().find(|r| self.reasons.contains(r)).unwrap_or("not_detected")))
        };
        let mut v = Value::obj();
        v.set("state", state.into());
        v.set("reason", reason.into());
        v.set("issues", Value::Arr(self.issues.iter().map(|(t, r)| {
            let mut i = Value::obj();
            i.set("target", t.as_str().into());
            i.set("reason", (*r).into());
            i
        }).collect()));
        v.set("issues_truncated", (self.truncated as i64).into());
        v
    }
}

/// Python's int() on stripped text: an optional sign, then digits with single underscores between them.
pub fn py_int(text: &str) -> Option<i64> {
    let t = text.trim();
    let (neg, digits) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__")
        || !digits.bytes().all(|b| b.is_ascii_digit() || b == b'_') {
        return None;
    }
    let v: i64 = digits.replace('_', "").parse().ok()?;
    Some(if neg { -v } else { v })
}

/// Sensor metadata without the read. basis: device_channel, resolved_path or unresolved (id None).
pub fn source(provider: &str, unit: &str, basis: &str, mut where_: Vec<(&str, Value)>) -> Value {
    let mut out = Value::obj();
    if basis == "unresolved" {
        out.set("id", Value::Null);
    } else {
        let mut d = Value::obj();
        d.set("v", 1i64.into());
        d.set("provider", provider.into());
        d.set("unit", unit.into());
        d.set("basis", basis.into());
        for (k, v) in where_.drain(..) {
            d.set(k, v);
        }
        out.set("id", format!("src1:{}", sha256::hex(d.to_string(true).as_bytes())).into());
    }
    out.set("provider", provider.into());
    out.set("identity_basis", basis.into());
    out.set("unit", unit.into());
    out
}

/// JSON Pointer (RFC 6901) to a value of the response.
pub fn pointer(parts: &[&str]) -> String {
    parts.iter().map(|p| format!("/{}", p.replace('~', "~0").replace('/', "~1"))).collect()
}

/// collectors entries of the groups, in their order; problems go to stderr when they change.
pub fn summarize(groups: &[Group], logged: &mut Vec<(String, Vec<String>)>) -> Value {
    let mut out = Value::obj();
    for g in groups {
        out.set(&g.name, g.status());
        let mut details = g.details.clone();
        if g.truncated > 0 {
            details.push(format!("+{} more", g.truncated));
        }
        let before = logged.iter().find(|(n, _)| *n == g.name).map(|(_, d)| d.clone()).unwrap_or_default();
        if details != before {
            if !details.is_empty() {
                eprintln!("platmon: {}: {}", g.name, details.join("; "));
            } else if !before.is_empty() {
                eprintln!("platmon: {}: readable again", g.name);
            }
            logged.retain(|(n, _)| *n != g.name);
            logged.push((g.name.clone(), details));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ints_like_python() {
        assert_eq!(py_int("42"), Some(42));
        assert_eq!(py_int(" -7 "), Some(-7));
        assert_eq!(py_int("+1_000"), Some(1000));
        assert_eq!(py_int(""), None);
        assert_eq!(py_int("1__0"), None);
        assert_eq!(py_int("4.5"), None);
    }

    #[test]
    fn source_id_matches_python() {
        // python3 -c 'from collector.metadata import source; print(source("hwmon","celsius","device_channel",
        //   device="devices/platform/x", attr="temp1_input")["id"])'
        let s = source("hwmon", "celsius", "device_channel",
                       vec![("device", "devices/platform/x".into()), ("attr", "temp1_input".into())]);
        assert_eq!(s.get("id").unwrap(), &Value::Str(
            "src1:".to_string() + &crate::sha256::hex(
                br#"{"attr":"temp1_input","basis":"device_channel","device":"devices/platform/x","provider":"hwmon","unit":"celsius","v":1}"#)));
    }
}
