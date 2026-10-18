//! Experimental Rust port of collector.collect_recorded (docs/performance/rust-feasibility.md, next step 1).
//! Not part of the product. Covers the core groups: CPU, clocks, thermal zones, hwmon, memory, disk,
//! identity, the Jetson extension, DRM GPU and accel NPU, collectors and sensor source ids. Network,
//! Disk I/O, Pressure, the slow groups and nvidia-smi are not ported.
//!
//!   platmon-native collect            one recorded collection 1 s after a first one, as JSON
//!                                     {"stats", "sensors" (with spans), "required", "clock"}
//!   platmon-native script             fixture tests: for each stdin line "BASE_NS WALL_US", one collection
//!                                     on those clocks (see metadata::set_clocks), one JSON line each
//!                                     ({"error": ...} for a failed collection)
//!   platmon-native loop SECONDS [WARMUP]  collect every 1 s (after WARMUP s, default 3), print own CPU
//!                                     (user/sys %), RSS, collection times and group states as JSON
mod accel;
#[cfg(feature = "cache-alloc")]
mod alloc;
mod common;
mod jetson;
mod json;
mod metadata;
mod sha256;
mod sysfs;

use common::{CpuCounters, Fan};
use json::Value;
use metadata::{pointer, Group, Slot};

#[cfg(feature = "cache-alloc")]
#[global_allocator]
static ALLOC: alloc::Cached = alloc::Cached::new();

pub struct Groups {
    pub cpu_frequency: Group,
    pub gpu: Group,
    pub npu: Group,
    pub temperature: Group,
    pub power: Group,
    pub fans: Group,
    pub power_mode: Group,
    pub board_info: Group,
}

impl Groups {
    fn new(trace: bool) -> Groups {
        let g = |n| Group::new(n, trace);
        Groups { cpu_frequency: g("cpu_frequency"), gpu: g("gpu"), npu: g("npu"), temperature: g("temperature"),
                 power: g("power"), fans: g("fans"), power_mode: g("power_mode"), board_info: g("board_info") }
    }

    fn into_vec(self) -> Vec<Group> {
        vec![self.cpu_frequency, self.gpu, self.npu, self.temperature, self.power, self.fans, self.power_mode, self.board_info]
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Board { Jetson, None }

/// core.detect: (platform, board module) from the device tree's compatible string.
fn detect() -> (&'static str, Board) {
    // not UTF-8: Python's import of collector.core fails, so the service does not start either
    let compatible = sysfs::read(&format!("{}/compatible", sysfs::device_tree()))
        .unwrap_or_else(|e| { eprintln!("platmon-native: {}", e); std::process::exit(1) }).unwrap_or_default();
    for (key, name, board) in [("nvidia,tegra234", "Jetson Orin", Board::Jetson), ("nvidia,tegra194", "Jetson Xavier", Board::Jetson),
                               ("nvidia,tegra", "Jetson", Board::Jetson), ("raspberrypi", "Raspberry Pi", Board::None)] {
        if compatible.contains(key) {
            return (name, board);
        }
    }
    (if sysfs::is_dir("/sys/class/dmi/id") { "PC" } else { "Linux" }, Board::None)
}

struct Collector {
    platform: &'static str,
    board: Board,
    cpu: CpuCounters,
    gpu: accel::Accel,
    npu: accel::Accel,
    logged: Vec<(String, Vec<String>)>,
}

struct Collected {
    stats: Value,
    sensors: Vec<(String, Value)>,
    required: common::Required,
}

impl Collected {
    /// {"stats", "sensors", "required"}: Collected's fields as the Python harnesses print them.
    fn to_value(self) -> Value {
        let mut required = Value::obj();
        for name in ["cpu", "memory", "disk", "uptime"] { // record.REQUIRED_READS
            let span = self.required.iter().find(|(n, _)| *n == name).and_then(|(_, s)| *s);
            required.set(name, span_value(span));
        }
        let mut out = Value::obj();
        out.set("stats", self.stats);
        out.set("sensors", Value::Obj(self.sensors));
        out.set("required", required);
        out
    }
}

fn span_value(span: metadata::Span) -> Value {
    span.map_or(Value::Null, |(a, b)| Value::Arr(vec![a.into(), b.into()]))
}

impl Collector {
    fn new() -> Collector {
        let (platform, board) = detect();
        Collector { platform, board, cpu: CpuCounters::new(Some(5.0), "interval"), gpu: accel::Accel::gpu(),
                    npu: accel::Accel::npu(), logged: Vec::new() }
    }

    /// collect_recorded(cpu, logged, clock) without network, disk_io and pressure.
    fn collect(&mut self) -> Result<Collected, String> {
        let mut g = Groups::new(true);
        let mut required = Vec::new();
        let (common_stats, mut fans) = common::collect(&mut self.cpu, &mut g, &mut required)?;
        let mut stats = Value::obj();
        stats.set("platform", self.platform.into());
        if let Value::Obj(items) = common_stats {
            for (k, v) in items {
                stats.set(&k, v);
            }
        }
        stats.set("fans", Value::Null); // keeps Python's key order; filled below
        if self.board == Board::Jetson {
            jetson::extend(&mut stats, &mut fans, &mut g);
        } else {
            g.power_mode.note("power_mode", "unsupported_platform", None);
            g.board_info.note("board_info", "unsupported_platform", None);
        }
        stats.set("fans", fans_value(&fans));
        if stats.get("gpu").map_or(true, Value::is_null) {
            let v = self.gpu.sample(&mut g.gpu);
            stats.set("gpu", v.into());
        }
        let v = self.npu.sample(&mut g.npu);
        stats.set("npu", v.into());
        let sensors = sensor_entries(&stats, &fans, &g);
        let groups = g.into_vec();
        stats.set("collectors", metadata::summarize(&groups, &mut self.logged));
        Ok(Collected { stats, sensors, required })
    }
}

fn fans_value(fans: &[Fan]) -> Value {
    Value::Arr(fans.iter().map(|f| {
        let mut v = Value::obj();
        v.set("name", f.name.as_str().into());
        v.set("rpm", f.rpm.into());
        v.set("percent", f.percent.into());
        v
    }).collect())
}

/// core.sensor_entries: {JSON Pointer: metadata} for the sensor values in the final stats.
fn sensor_entries(stats: &Value, fans: &[Fan], g: &Groups) -> Vec<(String, Value)> {
    let mut out = Vec::new();
    let mut add = |ptr: String, value: Option<&Value>, group: &Group, slot: Slot| {
        if let Some((_, info)) = group.sensors.iter().find(|(s, _)| *s == slot) {
            let mut info = info.clone();
            if value.map_or(true, Value::is_null) {
                info.set("span", Value::Null);
            }
            out.push((ptr, info));
        }
    };
    for (table, group) in [("temperature", &g.temperature), ("power", &g.power)] {
        if let Some(Value::Obj(items)) = stats.get(table) {
            for (k, v) in items {
                add(pointer(&[table, k]), Some(v), group, Slot::Key(k.clone()));
            }
        }
    }
    for (i, f) in fans.iter().enumerate() {
        let rpm: Value = f.rpm.into();
        let pct: Value = f.percent.into();
        add(pointer(&["fans", &i.to_string(), "rpm"]), Some(&rpm), &g.fans, Slot::Fan(f.id, "rpm"));
        add(pointer(&["fans", &i.to_string(), "percent"]), Some(&pct), &g.fans, Slot::Fan(f.id, "percent"));
    }
    if let Some(Value::Arr(cpus)) = stats.get("cpu") {
        for (i, c) in cpus.iter().enumerate() {
            if let Some(Value::Int(id)) = c.get("id") {
                add(pointer(&["cpu", &i.to_string(), "freq"]), c.get("freq"), &g.cpu_frequency, Slot::Cpu(*id));
            }
        }
    }
    for (kind, group) in [("gpu", &g.gpu), ("npu", &g.npu)] {
        if let Some(v @ Value::Obj(_)) = stats.get(kind) {
            for field in ["usage", "freq", "max_freq"] {
                if v.contains(field) {
                    add(pointer(&[kind, field]), v.get(field), group, Slot::Key(field.into()));
                }
            }
        }
    }
    out
}

/// (user s, system s) of this process.
fn usage() -> (f64, f64) {
    let mut r: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut r) };
    let s = |t: &libc::timeval| t.tv_sec as f64 + t.tv_usec as f64 / 1e6;
    (s(&r.ru_utime), s(&r.ru_stime))
}

/// (VmRSS, VmHWM) in KiB from /proc/self/status.
fn rss_kib() -> (i64, i64) {
    let text = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let field = |k: &str| text.lines().find_map(|l| l.strip_prefix(k)).and_then(|v| v.split_whitespace().next())
        .and_then(|v| v.parse().ok()).unwrap_or(-1);
    (field("VmRSS:"), field("VmHWM:"))
}

/// What the loop reports besides CPU, the same as tools/pyloop.py: collection times, missed 1 s deadlines,
/// RSS over the run, and per group how often each state came up, with the source ids of the last collection.
#[derive(Default)]
struct Stats {
    elapsed_ms: Vec<f64>,
    missed: u32,
    rss: Vec<i64>,
    states: Vec<(String, Vec<(String, u32)>)>,
    sources: usize,
}

impl Stats {
    fn add(&mut self, col: &Collected) {
        if let Some(Value::Obj(groups)) = col.stats.get("collectors") {
            for (name, status) in groups {
                let state = match status.get("state") { Some(Value::Str(s)) => s.clone(), _ => "?".into() };
                let pos = self.states.iter().position(|(n, _)| n == name).unwrap_or_else(|| {
                    self.states.push((name.clone(), vec![]));
                    self.states.len() - 1
                });
                let counts = &mut self.states[pos].1;
                match counts.iter_mut().find(|(s, _)| *s == state) {
                    Some(c) => c.1 += 1,
                    None => counts.push((state, 1)),
                }
            }
        }
        self.sources = col.sensors.iter().filter(|(_, v)| matches!(v.get("id"), Some(Value::Str(_)))).count();
    }

    fn value(&mut self) -> Value {
        let mut v = Value::obj();
        let mut e = self.elapsed_ms.clone();
        e.sort_by(f64::total_cmp);
        let pick = |q: f64| e.get(((e.len() as f64 - 1.0) * q).round() as usize).copied().map(|x| json::round_to(x, 3));
        let mut el = Value::obj();
        el.set("p50", pick(0.5).into());
        el.set("p95", pick(0.95).into());
        el.set("max", e.last().copied().map(|x| json::round_to(x, 3)).into());
        v.set("elapsed_ms", el);
        v.set("missed_deadlines", (self.missed as i64).into());
        v.set("rss_kib", Value::Arr(self.rss.iter().map(|r| (*r).into()).collect()));
        v.set("groups", Value::Obj(self.states.iter().map(|(n, c)| {
            (n.clone(), Value::Obj(c.iter().map(|(s, k)| (s.clone(), (*k as i64).into())).collect()))
        }).collect()));
        v.set("source_ids", (self.sources as i64).into());
        v
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut c = Collector::new();
    match args.get(1).map(String::as_str) {
        Some("collect") => {
            let _ = c.collect(); // the CPU baseline, as the service's first collection
            std::thread::sleep(std::time::Duration::from_secs(1));
            let col = c.collect().unwrap_or_else(|e| { eprintln!("collection failed: {}", e); std::process::exit(1) });
            let mut out = col.to_value();
            out.set("clock", metadata::clock_source().into());
            println!("{}", out.to_string(false));
        }
        Some("script") => {
            use std::io::BufRead;
            for line in std::io::stdin().lock().lines() {
                let line = line.unwrap_or_default();
                let mut it = line.split_whitespace().map(|x| x.parse::<i64>());
                let (Some(Ok(base)), Some(Ok(wall))) = (it.next(), it.next()) else {
                    eprintln!("script: expected BASE_NS WALL_US, got {:?}", line);
                    std::process::exit(2)
                };
                metadata::set_clocks(base, wall);
                let out = match c.collect() {
                    Ok(col) => col.to_value(),
                    Err(e) => { let mut v = Value::obj(); v.set("error", e.into()); v }
                };
                println!("{}", out.to_string(false));
            }
        }
        Some("loop") => {
            let seconds: f64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(60.0);
            let warmup: f64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(3.0);
            let w0 = std::time::Instant::now();
            while w0.elapsed().as_secs_f64() < warmup {
                let _ = c.collect();
                std::thread::sleep(std::time::Duration::from_secs(1));
            }
            let mut st = Stats::default();
            let (u0, s0) = usage();
            let t0 = std::time::Instant::now();
            let (mut n, mut failed, mut deadline) = (0u32, 0u32, t0);
            while t0.elapsed().as_secs_f64() < seconds {
                let start = std::time::Instant::now();
                match c.collect() {
                    Ok(col) => { st.elapsed_ms.push(start.elapsed().as_secs_f64() * 1e3); st.add(&col); }
                    Err(_) => failed += 1,
                }
                if n % 30 == 0 {
                    st.rss.push(rss_kib().0);
                }
                n += 1;
                deadline += std::time::Duration::from_secs(1);
                match deadline.checked_duration_since(std::time::Instant::now()) {
                    Some(d) => std::thread::sleep(d),
                    None => st.missed += 1,
                }
            }
            let wall = t0.elapsed().as_secs_f64();
            let (u1, s1) = usage();
            st.rss.push(rss_kib().0);
            let mut v = Value::obj();
            v.set("variant", if cfg!(feature = "cache-alloc") { "rust-collector-cache-alloc" } else { "rust-collector" }.into());
            v.set("collections", (n as i64).into());
            v.set("failed", (failed as i64).into());
            v.set("wall_s", json::round_to(wall, 3).into());
            v.set("user_pct", json::round_to(100.0 * (u1 - u0) / wall, 4).into());
            v.set("sys_pct", json::round_to(100.0 * (s1 - s0) / wall, 4).into());
            v.set("maxrss_kib", rss_kib().1.into());
            if let Value::Obj(items) = st.value() {
                for (k, x) in items { v.set(&k, x); }
            }
            println!("{}", v.to_string(false));
        }
        _ => {
            eprintln!("usage: platmon-native collect | script | loop SECONDS [WARMUP]");
            std::process::exit(2);
        }
    }
}
