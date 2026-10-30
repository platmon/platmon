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
    /// {"stats", "sensors", "required", "passive"}: Collected's fields as one value, as the Sampler and the
    /// collect and script commands take them.
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

// tests/collector/test_common.py's CpuCounters and cpu_percent tests, ported
// (docs/development/python-test-port.md). Readings are given as the read function; Python's 5-counter
// readings have their missing counters as 0, since cpu_times never gives fewer than 8.
#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    const S: i64 = 1_000_000_000;

    fn reading(cores: &[(i64, &[i64])]) -> BTreeMap<i64, Vec<i64>> {
        cores.iter().map(|(n, c)| { let mut v = c.to_vec(); v.resize(v.len().max(8), 0); (*n, v) }).collect()
    }

    fn idle_busy(idle: i64, busy: i64) -> Vec<i64> {
        vec![busy, 0, 0, idle, 0, 0, 0, 0]
    }

    /// One sample at t ns: (usage, cpu_sampling as JSON).
    fn at(c: &mut CpuCounters, t: i64, r: BTreeMap<i64, Vec<i64>>) -> (Vec<(i64, f64)>, String) {
        metadata::set_clocks(t, 0);
        let (u, s) = c.sample(|| Ok(r)).unwrap();
        (u, s.to_string(false))
    }

    fn counters() -> CpuCounters {
        CpuCounters::new(Some(5.0), "interval")
    }

    fn unavailable(s: &str) -> String {
        s.split_once(r#""unavailable":"#).unwrap().1.trim_end_matches('}').to_string()
    }

    #[test]
    fn cpu_percent_table() {
        let cases: Vec<(Vec<(i64, Vec<i64>)>, Vec<(i64, Vec<i64>)>, Vec<(i64, f64)>, &str)> = vec![
            (vec![(0, vec![100, 0, 100, 700, 100, 0, 0, 0])], vec![(0, vec![150, 0, 150, 800, 100, 0, 0, 0])], vec![(0, 50.0)], "[]"),
            (vec![(0, vec![0; 8])], vec![(0, vec![0, 0, 0, 90, 10, 0, 0, 0])], vec![(0, 0.0)], "[]"), // iowait counts as idle
            (vec![(0, vec![0; 8])], vec![(0, vec![40, 10, 20, 0, 0, 10, 10, 10])], vec![(0, 100.0)], "[]"),
            (vec![(0, vec![0; 8])], vec![(0, vec![1, 0, 0, 2, 0, 0, 0, 0])], vec![(0, 33.3)], "[]"), // rounded once at the end
            (vec![(0, vec![1; 5])], vec![(0, vec![1; 5])], vec![], r#"[{"id":0,"reason":"no_ticks"}]"#), // not 0 %
            (vec![(0, vec![0; 5]), (2, vec![0; 5]), (3, vec![0; 5])], vec![(0, vec![10, 0, 0, 10, 0]), (2, vec![0, 0, 0, 10, 0])],
             vec![(0, 50.0), (2, 0.0)], "[]"), // cpu1 offline, cpu3 went offline: ids kept, nothing mispaired
            // guest and guest_nice are already in user and nice: not counted twice
            (vec![(0, vec![0; 10])], vec![(0, vec![50, 0, 0, 50, 0, 0, 0, 0, 50, 0])], vec![(0, 50.0)], "[]"),
            (vec![(0, vec![0; 10])], vec![(0, vec![0, 50, 0, 50, 0, 0, 0, 0, 0, 50])], vec![(0, 50.0)], "[]"),
            (vec![(0, vec![0; 8]), (1, vec![0, 0, 0, 0, 50, 0, 0, 0])], vec![(0, [vec![10], vec![0; 7]].concat()), (1, vec![0, 0, 0, 100, 40, 0, 0, 0]), (2, vec![5; 8])],
             vec![(0, 100.0)], r#"[{"id":1,"reason":"counter_regressed"},{"id":2,"reason":"warmup"}]"#),
        ];
        for (before, after, usage, missing) in cases {
            let mut c = counters();
            let r = |x: &Vec<(i64, Vec<i64>)>| reading(&x.iter().map(|(n, v)| (*n, v.as_slice())).collect::<Vec<_>>());
            at(&mut c, 10 * S, r(&before));
            let (u, s) = at(&mut c, 11 * S, r(&after));
            assert_eq!((u, unavailable(&s)), (usage, missing.to_string()));
        }
    }

    #[test]
    fn first_reading_warms_up_then_measures() {
        let mut c = counters();
        assert_eq!(at(&mut c, 10 * S, reading(&[(0, &idle_busy(0, 0)), (1, &idle_busy(0, 0))])),
                   (vec![], r#"{"mode":"interval","window_ms":null,"unavailable":[{"id":0,"reason":"warmup"},{"id":1,"reason":"warmup"}]}"#.into()));
        // readings 1.25 s apart: the window is what was measured; usage is a tick ratio
        let (u, s) = at(&mut c, 10 * S + 1_250_000_000, reading(&[(0, &idle_busy(100, 100)), (1, &idle_busy(200, 0))]));
        assert_eq!((u.clone(), s), (vec![(0, 50.0), (1, 0.0)], r#"{"mode":"interval","window_ms":1250.0,"unavailable":[]}"#.into()));
        assert_eq!(at(&mut c, 14 * S + 250_000_000, reading(&[(0, &idle_busy(200, 200)), (1, &idle_busy(400, 0))])).0, u); // a longer window
    }

    #[test]
    fn cores_come_and_go() {
        let mut c = counters();
        at(&mut c, 10 * S, reading(&[(0, &idle_busy(0, 0)), (1, &idle_busy(0, 0))]));
        let (u, s) = at(&mut c, 11 * S, reading(&[(0, &idle_busy(10, 10)), (1, &idle_busy(20, 0)), (2, &idle_busy(5, 5))]));
        assert_eq!((u, unavailable(&s)), (vec![(0, 50.0), (1, 0.0)], r#"[{"id":2,"reason":"warmup"}]"#.into()));
        let (u, s) = at(&mut c, 12 * S, reading(&[(0, &idle_busy(20, 20)), (2, &idle_busy(10, 10))])); // cpu1 went offline
        assert_eq!((u, unavailable(&s)), (vec![(0, 50.0), (2, 50.0)], "[]".into())); // gone: neither shown nor listed
        let (u, s) = at(&mut c, 13 * S, reading(&[(0, &idle_busy(30, 30)), (1, &idle_busy(500, 500)), (2, &idle_busy(20, 10))]));
        assert_eq!((u, unavailable(&s)), (vec![(0, 50.0), (2, 0.0)], r#"[{"id":1,"reason":"warmup"}]"#.into())); // a fresh baseline
        let (u, _) = at(&mut c, 14 * S, reading(&[(0, &idle_busy(40, 40)), (1, &idle_busy(510, 510)), (2, &idle_busy(30, 10))]));
        assert_eq!(u[1], (1, 50.0));
    }

    #[test]
    fn counter_regression_is_dropped_and_rebased() {
        let mut c = counters();
        at(&mut c, 10 * S, reading(&[(0, &idle_busy(100, 100)), (1, &idle_busy(100, 100))]));
        let (u, s) = at(&mut c, 11 * S, reading(&[(0, &idle_busy(50, 150)), (1, &idle_busy(110, 110))]));
        assert_eq!((u, unavailable(&s)), (vec![(1, 50.0)], r#"[{"id":0,"reason":"counter_regressed"}]"#.into()));
        assert_eq!(at(&mut c, 12 * S, reading(&[(0, &idle_busy(60, 160)), (1, &idle_busy(120, 120))])).0, [(0, 50.0), (1, 50.0)]);
    }

    #[test]
    fn no_ticks_is_not_zero() {
        let mut c = counters();
        at(&mut c, 10 * S, reading(&[(0, &idle_busy(10, 10))]));
        let (u, s) = at(&mut c, 11 * S, reading(&[(0, &idle_busy(10, 10))]));
        assert_eq!((u, unavailable(&s)), (vec![], r#"[{"id":0,"reason":"no_ticks"}]"#.into()));
    }

    #[test]
    fn invalid_interval() {
        for step in [0, -S] {
            let mut c = counters();
            at(&mut c, 10 * S, reading(&[(0, &idle_busy(0, 0))]));
            assert_eq!(at(&mut c, 10 * S + step, reading(&[(0, &idle_busy(10, 10))])),
                       (vec![], r#"{"mode":"interval","window_ms":null,"unavailable":[{"id":0,"reason":"invalid_interval"}]}"#.into()));
            assert_eq!(at(&mut c, 11 * S + step, reading(&[(0, &idle_busy(20, 20))])).0, [(0, 50.0)]); // rebased on the reading it could not use
        }
    }

    #[test]
    fn gap_boundary_and_recovery() {
        let mut c = counters();
        at(&mut c, 10 * S, reading(&[(0, &idle_busy(0, 0))]));
        assert_eq!(at(&mut c, 15 * S, reading(&[(0, &idle_busy(10, 10))])).0, [(0, 50.0)]); // exactly max_gap: still averaged
        assert_eq!(at(&mut c, 20 * S + 1, reading(&[(0, &idle_busy(20, 20))])),
                   (vec![], r#"{"mode":"interval","window_ms":5000.0,"unavailable":[{"id":0,"reason":"gap"}]}"#.into()));
        let (u, s) = at(&mut c, 21 * S + 1, reading(&[(0, &idle_busy(30, 20))]));
        assert_eq!((u, s.contains(r#""window_ms":1000.0"#)), (vec![(0, 0.0)], true));
    }

    #[test]
    fn read_failure_raises_and_restarts_warmup() {
        let mut c = counters();
        at(&mut c, 10 * S, reading(&[(0, &idle_busy(0, 0))]));
        metadata::set_clocks(11 * S, 0);
        assert!(c.sample(|| Err("unreadable".into())).is_err());
        let (u, s) = at(&mut c, 12 * S, reading(&[(0, &idle_busy(10, 10))]));
        assert_eq!((u, unavailable(&s)), (vec![], r#"[{"id":0,"reason":"warmup"}]"#.into()));
    }

    #[test]
    fn counters_are_independent() {
        let (mut a, mut b) = (counters(), counters());
        at(&mut a, 10 * S, reading(&[(0, &idle_busy(0, 0))]));
        at(&mut a, 11 * S, reading(&[(0, &idle_busy(10, 10))]));
        assert_eq!(unavailable(&at(&mut b, 11 * S, reading(&[(0, &idle_busy(20, 20))])).1), r#"[{"id":0,"reason":"warmup"}]"#);
        // a compares with its own last reading (10, 10): 33.3 %; against b's (20, 20) it would read 0 %
        assert_eq!(at(&mut a, 12 * S, reading(&[(0, &idle_busy(30, 20))])).0, [(0, 33.3)]);
    }
}
