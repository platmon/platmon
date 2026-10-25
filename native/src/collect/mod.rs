//! The collectors, one module per OS: what the Sampler and the slow groups call to read the host.
#[cfg(target_os = "linux")]
pub mod linux;
pub mod probe;
#[cfg(target_os = "linux")]
pub use linux::{observe, Collector};
#[cfg(windows)]
pub mod windows;
#[cfg(windows)]
pub use windows::{observe, Collector};

use crate::json::{round_to, Value};
use crate::metadata::{self, clock, pointer, Group, Slot, Span};
use std::collections::BTreeMap;

/// The required reads' spans, by name (Trace.required).
pub type Required = Vec<(&'static str, Span)>;

pub fn timed<T>(req: &mut Required, name: &'static str, f: impl FnOnce() -> T) -> T {
    let start = clock();
    let v = f();
    req.push((name, Some((start, clock()))));
    v
}

/// A counter rate: round(delta * 1e9 / window_ns, 3) with Python's int to float conversions.
pub fn rate(delta: u128, window_ns: i64) -> f64 {
    round_to(delta as f64 * 1e9 / window_ns as f64, 3)
}

/// A counter group's window_ms: also given with gap and counter_regressed, null with other reasons.
pub fn window_ms(window: Option<i64>, reason: Option<&str>) -> Value {
    match (window, reason) {
        (Some(w), None | Some("gap") | Some("counter_regressed")) => round_to(w as f64 / 1e6, 3).into(),
        _ => Value::Null,
    }
}

pub fn reason_value(reason: Option<&'static str>) -> Value {
    reason.map_or(Value::Null, Into::into)
}

/// CPU usage between two readings of per-CPU counters: {id: [user, nice, system, idle, iowait, irq,
/// softirq, steal, ...]} in any one unit, idle and iowait counted as idle (common.CpuCounters).
pub struct CpuCounters {
    max_gap: Option<i64>,
    mode: &'static str,
    last: Option<(i64, BTreeMap<i64, Vec<i64>>)>,
    pub span: Span,
}

impl CpuCounters {
    pub fn new(max_gap_s: Option<f64>, mode: &'static str) -> CpuCounters {
        CpuCounters { max_gap: max_gap_s.map(|s| (s * 1e9).round() as i64), mode, last: None, span: None }
    }

    /// ({id: usage}, cpu_sampling) from read's counters; see CpuCounters.sample.
    pub fn sample(&mut self, read: impl FnOnce() -> Result<BTreeMap<i64, Vec<i64>>, String>) -> Result<(Vec<(i64, f64)>, Value), String> {
        let now = clock();
        let times = match read() {
            Ok(t) => t,
            Err(e) => { self.last = None; return Err(e); }
        };
        self.span = Some((now, clock()));
        let mut window = self.last.as_ref().map(|l| now - l.0);
        let (usage, unavailable): (Vec<(i64, f64)>, Vec<(i64, &str)>) = match (&self.last, window) {
            (None, _) => (vec![], times.keys().map(|n| (*n, "warmup")).collect()),
            (Some(_), Some(w)) if w <= 0 => { window = None; (vec![], times.keys().map(|n| (*n, "invalid_interval")).collect()) }
            (Some(_), Some(w)) if self.max_gap.map_or(false, |m| w > m) => (vec![], times.keys().map(|n| (*n, "gap")).collect()),
            (Some((_, before)), _) => {
                let (mut u, mut un) = (vec![], vec![]);
                for (n, after) in &times {
                    let Some(b) = before.get(n) else { un.push((*n, "warmup")); continue };
                    let (a, b) = (&after[..8], &b[..8]);
                    let delta: Vec<i64> = a.iter().zip(b).map(|(x, y)| x - y).collect();
                    let total: i64 = delta.iter().sum();
                    let idle = delta[3] + delta[4];
                    if delta.iter().any(|d| *d < 0) {
                        un.push((*n, "counter_regressed"));
                    } else if total == 0 {
                        un.push((*n, "no_ticks"));
                    } else {
                        u.push((*n, round_to(100.0 * (total - idle) as f64 / total as f64, 1)));
                    }
                }
                (u, un)
            }
        };
        self.last = Some((now, times));
        let mut s = Value::obj();
        s.set("mode", self.mode.into());
        s.set("window_ms", window.map(|w| round_to(w as f64 / 1e6, 3)).into());
        s.set("unavailable", Value::Arr(unavailable.into_iter().map(|(n, r)| {
            let mut e = Value::obj();
            e.set("id", n.into());
            e.set("reason", r.into());
            e
        }).collect()));
        Ok((usage, s))
    }
}

pub struct Collected {
    pub stats: Value,
    pub sensors: Vec<(String, Value)>,
    pub required: Required,
    /// the reads of network, disk_io and pressure (Collected.network, .disk_io, .pressure)
    pub passive: [metadata::Span; 3],
}

impl Collected {
    /// {"stats", "sensors", "required", "passive"}: Collected's fields as the Python harnesses print them.
    pub fn to_value(self) -> Value {
        let mut required = Value::obj();
        for name in ["cpu", "memory", "disk", "uptime"] { // record.REQUIRED_READS
            let span = self.required.iter().find(|(n, _)| *n == name).and_then(|(_, s)| *s);
            required.set(name, span_value(span));
        }
        let mut out = Value::obj();
        out.set("stats", self.stats);
        out.set("sensors", Value::Obj(self.sensors));
        out.set("required", required);
        let mut spans = Value::obj();
        for (name, span) in ["network", "disk_io", "pressure"].iter().zip(self.passive) {
            spans.set(name, span_value(span));
        }
        out.set("passive", spans);
        out
    }
}

/// core.sensor_entries: {JSON Pointer: metadata} for the sensor values in the final stats, from the groups
/// named temperature, power, fans, cpu_frequency, gpu and npu (those given); fan_ids: each fan's id, in order.
pub fn sensor_entries(stats: &Value, fan_ids: &[u64], groups: &[&Group]) -> Vec<(String, Value)> {
    let mut out = Vec::new();
    let mut add = |ptr: String, value: Option<&Value>, name: &str, slot: Slot| {
        let Some(group) = groups.iter().find(|g| g.name == name) else { return };
        if let Some((_, info)) = group.sensors.iter().find(|(s, _)| *s == slot) {
            let mut info = info.clone();
            if value.map_or(true, Value::is_null) {
                info.set("span", Value::Null);
            }
            out.push((ptr, info));
        }
    };
    for table in ["temperature", "power"] {
        if let Some(Value::Obj(items)) = stats.get(table) {
            for (k, v) in items {
                add(pointer(&[table, k]), Some(v), table, Slot::Key(k.clone()));
            }
        }
    }
    if let Some(Value::Arr(fans)) = stats.get("fans") {
        for (i, (f, id)) in fans.iter().zip(fan_ids).enumerate() {
            add(pointer(&["fans", &i.to_string(), "rpm"]), f.get("rpm"), "fans", Slot::Fan(*id, "rpm"));
            add(pointer(&["fans", &i.to_string(), "percent"]), f.get("percent"), "fans", Slot::Fan(*id, "percent"));
        }
    }
    if let Some(Value::Arr(cpus)) = stats.get("cpu") {
        for (i, c) in cpus.iter().enumerate() {
            if let Some(Value::Int(id)) = c.get("id") {
                add(pointer(&["cpu", &i.to_string(), "freq"]), c.get("freq"), "cpu_frequency", Slot::Cpu(*id));
            }
        }
    }
    for kind in ["gpu", "npu"] {
        if let Some(v @ Value::Obj(_)) = stats.get(kind) {
            for field in ["usage", "freq", "max_freq"] {
                if v.contains(field) {
                    add(pointer(&[kind, field]), v.get(field), kind, Slot::Key(field.into()));
                }
            }
        }
    }
    out
}

fn span_value(span: metadata::Span) -> Value {
    span.map_or(Value::Null, |(a, b)| Value::Arr(vec![a.into(), b.into()]))
}
