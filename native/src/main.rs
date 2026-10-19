//! Experimental Rust port of collector.collect_recorded (docs/performance/rust-feasibility.md, next step 1).
//! Not part of the product. Covers the core groups: CPU, clocks, thermal zones, hwmon, memory, disk,
//! identity, the Jetson extension, DRM GPU and accel NPU, collectors and sensor source ids, and the
//! passive groups Network, Disk I/O and Pressure as the service enables them by default. The slow groups
//! and nvidia-smi are not ported.
//!
//!   platmon-native collect            one recorded collection 1 s after a first one, as JSON
//!                                     {"stats", "sensors" (with spans), "required", "passive", "clock"}
//!   platmon-native script             fixture tests: for each stdin line "BASE_NS WALL_US", one collection
//!                                     on those clocks (see metadata::set_clocks), one JSON line each
//!                                     ({"error": ...} for a failed collection)
//!   platmon-native loop SECONDS [WARMUP]  collect every 1 s (after WARMUP s, default 3), print own CPU
//!                                     (user/sys %), RSS, collection times and group states as JSON
//!   platmon-native serve [--interval S] [--bind ADDR] [--port N] [--web DIR] [--app-version V]
//!                                     step 3A: the service's core (Sampler) and live HTTP API, no slow
//!                                     groups, history or replay (src/runtime.rs, src/http.rs)
//!   platmon-native runtime-script     tools/runtime_fixture.py: Sampler and slow-group operations with
//!                                     given times and collections, one JSON line per stdin line
mod accel;
#[cfg(feature = "cache-alloc")]
mod alloc;
mod common;
mod http;
mod jetson;
mod json;
mod metadata;
mod passive;
mod runtime;
mod sha256;
mod slow;
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
    pub network: Group,
    pub disk_io: Group,
    pub pressure: Group,
}

impl Groups {
    fn new(trace: bool) -> Groups {
        let g = |n| Group::new(n, trace);
        Groups { cpu_frequency: g("cpu_frequency"), gpu: g("gpu"), npu: g("npu"), temperature: g("temperature"),
                 power: g("power"), fans: g("fans"), power_mode: g("power_mode"), board_info: g("board_info"),
                 network: g("network"), disk_io: g("disk_io"), pressure: g("pressure") }
    }

    fn into_vec(self) -> Vec<Group> {
        vec![self.cpu_frequency, self.gpu, self.npu, self.temperature, self.power, self.fans, self.power_mode, self.board_info,
             self.network, self.disk_io, self.pressure]
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
    network: passive::Network,
    disks: passive::Disks,
    logged: Vec<(String, Vec<String>)>,
}

struct Collected {
    stats: Value,
    sensors: Vec<(String, Value)>,
    required: common::Required,
    /// the reads of network, disk_io and pressure (Collected.network, .disk_io, .pressure)
    passive: [metadata::Span; 3],
}

impl Collected {
    /// {"stats", "sensors", "required", "passive"}: Collected's fields as the Python harnesses print them.
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

impl Collector {
    /// max_gap: seconds, max(3 × interval, 5 s) in the service
    fn new(max_gap: f64) -> Collector {
        let (platform, board) = detect();
        let ns = runtime::seconds_ns(max_gap);
        Collector { platform, board, cpu: CpuCounters::new(Some(max_gap), "interval"), gpu: accel::Accel::gpu(ns),
                    npu: accel::Accel::npu(ns), network: passive::Network::new(Some(max_gap)),
                    disks: passive::Disks::new(Some(max_gap)), logged: Vec::new() }
    }

    /// collect_recorded(cpu, logged, clock, network, disk_io, pressure, gpu, npu), wired as the service does
    /// by default.
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
        // after the required data: a failed collection never reaches them
        let (v, network) = self.network.sample(&mut g.network);
        stats.set("network", v);
        let (v, disk_io) = self.disks.sample(&mut g.disk_io);
        stats.set("disk_io", v);
        let (v, pressure) = passive::pressure(&mut g.pressure);
        stats.set("pressure", v);
        let sensors = sensor_entries(&stats, &fans, &g);
        let groups = g.into_vec();
        stats.set("collectors", metadata::summarize(&groups, &mut self.logged));
        Ok(Collected { stats, sensors, required, passive: [network, disk_io, pressure] })
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
/// RSS over the run, per group how often each state came up, the number of source ids of the last
/// collection, and how many different read scopes (read_scope) came up, with the last one.
#[derive(Default)]
struct Stats {
    elapsed_ms: Vec<f64>,
    missed: u32,
    rss: Vec<i64>,
    states: Vec<(String, Vec<(String, u32)>)>,
    sources: usize,
    scopes: Vec<String>,
    scope: Value,
}

/// pyloop.read_scope: the SHA-256 (16 hex digits) of the sorted source ids, and the names of the interfaces,
/// disks and pressure resources read.
fn read_scope(col: &Collected) -> Value {
    let mut ids: Vec<&str> = col.sensors.iter().filter_map(|(_, v)| match v.get("id") { Some(Value::Str(s)) => Some(s.as_str()), _ => None }).collect();
    ids.sort();
    let names = |group: &str, list: &str| Value::Arr(match col.stats.get(group).and_then(|g| g.get(list)) {
        Some(Value::Arr(items)) => items.iter().filter_map(|x| x.get("name").cloned()).collect(),
        _ => vec![],
    });
    let mut v = Value::obj();
    v.set("source_ids", crate::sha256::hex(ids.join("\n").as_bytes())[..16].into());
    v.set("interfaces", names("network", "interfaces"));
    v.set("disks", names("disk_io", "disks"));
    let pressure = match col.stats.get("pressure").and_then(|p| p.get("resources")) {
        Some(Value::Obj(items)) => items.iter().map(|(k, _)| Value::Str(k.clone())).collect(),
        _ => vec![],
    };
    v.set("pressure", Value::Arr(pressure));
    v
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
        let scope = read_scope(col);
        let key = scope.to_string(true);
        if !self.scopes.contains(&key) {
            self.scopes.push(key);
        }
        self.scope = scope;
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
        v.set("read_scopes", (self.scopes.len() as i64).into());
        v.set("read_scope", self.scope.clone());
        v
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("serve") => return serve(&args[2..]),
        Some("runtime-script") => return runtime_script(),
        _ => {}
    }
    let mut c = Collector::new(5.0); // the service's at its default 1 s interval
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
            eprintln!("usage: platmon-native collect | script | loop SECONDS [WARMUP] | serve [OPTIONS] | runtime-script");
            std::process::exit(2);
        }
    }
}

/// platmon.levels with the default [thresholds]: [warning, critical] per name; temperature the board's.
fn default_levels(platform: &str) -> Value {
    let temperature = match platform {
        "Jetson Orin" => [85.0, 95.0],
        "Raspberry Pi" => [75.0, 80.0],
        _ => [80.0, 90.0],
    };
    let mut v = Value::obj();
    for (name, pair) in [("cpu", [80.0, 90.0]), ("gpu", [80.0, 90.0]), ("memory", [80.0, 90.0]), ("filesystem", [80.0, 90.0]),
                         ("temperature", temperature), ("battery", [20.0, 10.0]), ("wifi", [-70.0, -80.0])] {
        v.set(name, Value::Arr(pair.iter().map(|x| Value::Float(*x)).collect()));
    }
    v
}

extern "C" fn on_term(_: libc::c_int) {
    unsafe { libc::_exit(0) } // as platmon.py: a clean exit on SIGTERM (systemd stop, docker stop)
}

/// platmon.py with [core] interval and [http] from the options, the rest at its defaults except the slow
/// groups, history and replay, which step 3A does not have.
fn serve(args: &[String]) {
    let (mut interval, mut bind, mut port, mut web, mut version) = (1.0f64, "0.0.0.0".to_string(), 9797u16, None, "0.0.0".to_string());
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        let value = it.next().unwrap_or_else(|| { eprintln!("serve: {} needs a value", flag); std::process::exit(2) });
        let bad = || -> ! { eprintln!("serve: bad value for {}: {}", flag, value); std::process::exit(2) };
        match flag.as_str() {
            "--interval" => interval = value.parse().ok().filter(|i| (0.1..=3600.0).contains(i)).unwrap_or_else(|| bad()),
            "--bind" => bind = value.clone(),
            "--port" => port = value.parse().ok().filter(|p| *p >= 1).unwrap_or_else(|| bad()),
            "--web" => web = Some(value.clone()),
            "--app-version" => version = value.clone(),
            _ => { eprintln!("serve: unknown option {}", flag); std::process::exit(2) }
        }
    }
    unsafe { libc::signal(libc::SIGTERM, on_term as extern "C" fn(libc::c_int) as libc::sighandler_t) };
    let mut c = Collector::new((3.0 * interval).max(5.0));
    println!("platmon-native: platform {}, version {} (step 3A runtime)", c.platform, version);
    let source = metadata::clock_source();
    let sampler = std::sync::Arc::new(runtime::Sampler::new(interval, None, runtime::uuid4(), source));
    let observations = std::sync::Arc::new(slow::Observations { groups: vec![], instance_id: sampler.instance_id.clone(),
                                                                clock_source: source });
    let levels = default_levels(c.platform);
    let listener = http::bind(&bind, port).unwrap_or_else(|e| { eprintln!("platmon-native: cannot listen on {}:{}: {}", bind, port, e); std::process::exit(1) });
    let s = sampler.clone();
    std::thread::Builder::new().name("sampler".into()).spawn(move || {
        static STOP: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
        s.run(|| c.collect().map(Collected::to_value), &STOP)
    }).expect("cannot start the sampler");
    println!("platmon http on {}:{}", bind, port);
    std::sync::Arc::new(http::Server::new(sampler, observations, web, version, levels)).serve(listener);
}

fn number(op: &Value, key: &str) -> f64 {
    match op.get(key) {
        Some(Value::Int(i)) => *i as f64,
        Some(Value::Float(f)) => *f,
        _ => { eprintln!("runtime-script: {} must be a number", key); std::process::exit(2) }
    }
}

fn integer(op: &Value, key: &str) -> i64 {
    match op.get(key) {
        Some(Value::Int(i)) => *i,
        _ => { eprintln!("runtime-script: {} must be an integer", key); std::process::exit(2) }
    }
}

fn text<'a>(op: &'a Value, key: &str) -> &'a str {
    match op.get(key) {
        Some(Value::Str(s)) => s,
        _ => { eprintln!("runtime-script: {} must be a string", key); std::process::exit(2) }
    }
}

/// The runtime script (tools/runtime_fixture.py; tools/runtime_py.py is the Python side). One JSON
/// operation per line, one JSON line out for each:
///   {"op": "setup", "interval", "instance_id", "clock", "slow": [[name, interval], ...]}
///   {"op": "begin", "wall", "now"}            a collection starts
///   {"op": "finish", "wall", "now", "result", "clock"}  it ends: result is Collected.to_value()'s shape or
///                                             {"error": ...}; clock "other": its spans are on another clock
///   {"op": "view", "now"}                     -> {"stats": the /api/stats JSON or null, "status": /api/status's}
///   {"op": "slow_begin", "group", "wall", "now"}
///   {"op": "slow_finish", "group", "now", "wall", "data", "notes": [[target, reason]], "values", "raise"}
///   {"op": "observations", "now"}             -> {"observations": the /api/observations JSON}
fn runtime_script() {
    use std::io::BufRead;
    let mut sampler: Option<runtime::Sampler> = None;
    let mut groups: Vec<std::sync::Arc<slow::Slow>> = vec![];
    let mut obs: Option<slow::Observations> = None;
    let (mut begun, mut slow_begun) = ((0.0, 0), vec![]);
    for line in std::io::stdin().lock().lines() {
        let line = line.unwrap_or_default();
        let op = json::parse(&line).unwrap_or_else(|e| { eprintln!("runtime-script: {}: {:?}", e, line); std::process::exit(2) });
        let group = |op: &Value| groups.iter().position(|g| g.name == text(op, "group"))
            .unwrap_or_else(|| { eprintln!("runtime-script: no group {:?}", op.get("group")); std::process::exit(2) });
        let mut out = Value::obj();
        match text(&op, "op") {
            "setup" => {
                let source: &'static str = if text(&op, "clock") == "boottime" { "boottime" } else { "monotonic" };
                sampler = Some(runtime::Sampler::new(number(&op, "interval"), None, text(&op, "instance_id").into(), source));
                groups = match op.get("slow") {
                    Some(Value::Arr(items)) => items.iter().map(|g| match (g.get_index(0), g.get_index(1)) {
                        (Some(Value::Str(name)), Some(Value::Int(_) | Value::Float(_))) => {
                            let interval = match g.get_index(1) { Some(Value::Int(i)) => *i as f64, Some(Value::Float(f)) => *f, _ => unreachable!() };
                            std::sync::Arc::new(slow::Slow::new(name, interval))
                        }
                        _ => { eprintln!("runtime-script: slow is [[name, interval], ...]"); std::process::exit(2) }
                    }).collect(),
                    _ => vec![],
                };
                slow_begun = vec![(0.0, 0); groups.len()];
                obs = Some(slow::Observations { groups: groups.clone(), instance_id: text(&op, "instance_id").into(), clock_source: source });
            }
            name => {
                let Some(s) = sampler.as_ref() else { eprintln!("runtime-script: setup first"); std::process::exit(2) };
                match name {
                    "begin" => {
                        begun = (number(&op, "wall"), integer(&op, "now"));
                        s.begin(begun.1);
                    }
                    "finish" => {
                        let result = op.get("result").cloned().unwrap_or_default();
                        let result = match result.get("error") {
                            Some(e) => Err(e.to_string(false)),
                            None => Ok(result),
                        };
                        let same = op.get("clock").map_or(true, |c| *c != Value::from("other"));
                        s.finish(result, same, begun.0, begun.1, number(&op, "wall"), integer(&op, "now"));
                    }
                    "view" => {
                        let now = integer(&op, "now");
                        let (stats, status) = s.view(|| now);
                        out.set("stats", stats.into());
                        out.set("status", status.to_py().into());
                    }
                    "slow_begin" => {
                        let i = group(&op);
                        slow_begun[i] = (number(&op, "wall"), integer(&op, "now"));
                        groups[i].begin(slow_begun[i].1);
                    }
                    "slow_finish" => {
                        let i = group(&op);
                        let mut g = metadata::Group::new(&groups[i].name, false);
                        if let Some(Value::Arr(notes)) = op.get("notes") {
                            for n in notes {
                                let (Some(Value::Str(target)), Some(Value::Str(reason))) = (n.get_index(0), n.get_index(1)) else { continue };
                                let reason: &'static str = metadata::PROBLEMS.iter().chain(metadata::ABSENT.iter())
                                    .find(|r| **r == reason.as_str()).copied().unwrap_or_else(|| Box::leak(reason.clone().into_boxed_str()));
                                g.note(target, reason, None);
                            }
                        }
                        for _ in 0..integer(&op, "values") {
                            g.got(());
                        }
                        let data = match op.get("raise") {
                            Some(Value::Str(e)) => Err(e.clone()),
                            _ => Ok(op.get("data").cloned().unwrap_or_default()),
                        };
                        let wall = number(&op, "wall");
                        groups[i].finish(g, data, slow_begun[i].0, slow_begun[i].1, integer(&op, "now"), || wall);
                    }
                    "observations" => {
                        let now = integer(&op, "now");
                        out.set("observations", obs.as_ref().unwrap().view(|| now).into());
                    }
                    other => { eprintln!("runtime-script: unknown op {:?}", other); std::process::exit(2) }
                }
            }
        }
        println!("{}", out.to_string(false));
    }
}
