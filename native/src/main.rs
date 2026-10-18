//! Experimental Rust port of collector.collect_recorded (docs/performance/rust-feasibility.md, next step 1).
//! Not part of the product. Covers the core groups: CPU, clocks, thermal zones, hwmon, memory, disk,
//! identity, the Jetson extension, DRM GPU and accel NPU, collectors and sensor source ids. Network,
//! Disk I/O, Pressure, the slow groups and nvidia-smi are not ported.
//!
//!   platmon-native collect            one recorded collection 1 s after a first one, as JSON
//!                                     {"stats": ..., "sensors": {pointer: source}} (spans left out)
//!   platmon-native loop SECONDS [WARMUP]  collect every 1 s (after WARMUP s, default 3), print own CPU
//!                                     (user/sys %) and peak RSS over SECONDS as JSON
mod accel;
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
    let compatible = sysfs::read(&format!("{}/compatible", sysfs::device_tree())).unwrap_or_default();
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
    #[allow(dead_code)]
    required: common::Required,
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

#[repr(C)]
struct Timeval { sec: i64, usec: i64 }
#[repr(C)]
struct Rusage { utime: Timeval, stime: Timeval, maxrss: i64, rest: [i64; 13] }
extern "C" { fn getrusage(who: i32, usage: *mut Rusage) -> i32; }

fn usage() -> (f64, f64, i64) {
    let mut r = Rusage { utime: Timeval { sec: 0, usec: 0 }, stime: Timeval { sec: 0, usec: 0 }, maxrss: 0, rest: [0; 13] };
    unsafe { getrusage(0, &mut r) };
    let s = |t: &Timeval| t.sec as f64 + t.usec as f64 / 1e6;
    (s(&r.utime), s(&r.stime), r.maxrss)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut c = Collector::new();
    match args.get(1).map(String::as_str) {
        Some("collect") => {
            let _ = c.collect(); // the CPU baseline, as the service's first collection
            std::thread::sleep(std::time::Duration::from_secs(1));
            let col = c.collect().unwrap_or_else(|e| { eprintln!("collection failed: {}", e); std::process::exit(1) });
            let sensors = Value::Obj(col.sensors.into_iter().map(|(p, mut v)| {
                if let Value::Obj(items) = &mut v { items.retain(|(k, _)| k != "span"); }
                (p, v)
            }).collect());
            let mut out = Value::obj();
            out.set("stats", col.stats);
            out.set("sensors", sensors);
            println!("{}", out.to_string(false));
        }
        Some("loop") => {
            let seconds: f64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(60.0);
            let warmup: f64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(3.0);
            let w0 = std::time::Instant::now();
            while w0.elapsed().as_secs_f64() < warmup {
                let _ = c.collect();
                std::thread::sleep(std::time::Duration::from_secs(1));
            }
            let (u0, s0, _) = usage();
            let t0 = std::time::Instant::now();
            let (mut n, mut failed, mut deadline) = (0u32, 0u32, t0);
            while t0.elapsed().as_secs_f64() < seconds {
                match c.collect() {
                    Ok(col) => { std::hint::black_box(&col); }
                    Err(_) => failed += 1,
                }
                n += 1;
                deadline += std::time::Duration::from_secs(1);
                if let Some(d) = deadline.checked_duration_since(std::time::Instant::now()) {
                    std::thread::sleep(d);
                }
            }
            let wall = t0.elapsed().as_secs_f64();
            let (u1, s1, rss) = usage();
            println!("{{\"variant\":\"rust-collector\",\"collections\":{},\"failed\":{},\"wall_s\":{:.3},\"user_pct\":{:.4},\"sys_pct\":{:.4},\"maxrss_kib\":{}}}",
                     n, failed, wall, 100.0 * (u1 - u0) / wall, 100.0 * (s1 - s0) / wall, rss);
        }
        _ => {
            eprintln!("usage: platmon-native collect | loop SECONDS [WARMUP]");
            std::process::exit(2);
        }
    }
}
