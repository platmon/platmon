//! The collectors, one module per OS: what the Sampler and the slow groups call to read the host.
#[cfg(target_os = "linux")]
pub mod linux;
#[cfg(target_os = "linux")]
pub use linux::{observe, Collector};
#[cfg(windows)]
pub mod windows;
#[cfg(windows)]
pub use windows::{observe, Collector};

use crate::json::{round_to, Value};
use crate::metadata::{self, clock, Span};
use std::collections::BTreeMap;

/// The required reads' spans, by name (Trace.required).
pub type Required = Vec<(&'static str, Span)>;

pub fn timed<T>(req: &mut Required, name: &'static str, f: impl FnOnce() -> T) -> T {
    let start = clock();
    let v = f();
    req.push((name, Some((start, clock()))));
    v
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

fn span_value(span: metadata::Span) -> Value {
    span.map_or(Value::Null, |(a, b)| Value::Arr(vec![a.into(), b.into()]))
}
