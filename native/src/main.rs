//! Experimental Rust port of collector.collect_recorded (docs/performance/rust-feasibility.md, next step 1).
//! Not part of the product. Covers the core groups: CPU, clocks, thermal zones, hwmon, memory, disk,
//! identity, the Jetson extension, DRM GPU and accel NPU, collectors and sensor source ids, and the
//! passive groups Network, Disk I/O and Pressure as the service enables them by default, and the nvidia-smi
//! fallback.
//!
//!   platmon-native collect            one recorded collection 1 s after a first one, as JSON
//!                                     {"stats", "sensors" (with spans), "required", "passive", "clock"}
//!   platmon-native script             fixture tests: for each stdin line "BASE_NS WALL_US", one collection
//!                                     on those clocks (see metadata::set_clocks), one JSON line each
//!                                     ({"error": ...} for a failed collection)
//!   platmon-native loop SECONDS [WARMUP]  collect every 1 s (after WARMUP s, default 3), print own CPU
//!                                     (user/sys %), RSS, collection times and group states as JSON
//!   platmon-native serve [CONFIG] [--frontends LIST] [--web DIR] [--app-version V]
//!                                     the service as platmon.py runs it: platmon.ini, the Sampler, slow
//!                                     groups, history, replay and the HTTP API (src/runtime.rs, src/http.rs)
//!   platmon-native runtime-script     tools/runtime_fixture.py: Sampler and slow-group operations with
//!                                     given times and collections, one JSON line per stdin line
mod accel;
#[cfg(feature = "cache-alloc")]
mod alloc;
mod common;
mod config;
mod history;
mod http;
mod jetson;
mod json;
mod metadata;
mod nvidia;
mod observe;
mod passive;
mod render;
mod replay;
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

#[cfg(all(feature = "cache-alloc", feature = "mimalloc"))]
compile_error!("cache-alloc and mimalloc are two allocators: choose one");

#[cfg(feature = "mimalloc")]
#[global_allocator]
static ALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// The global allocator this binary was built with, for the results.
pub const ALLOCATOR: &str = if cfg!(feature = "cache-alloc") { "cache-alloc" } else if cfg!(feature = "mimalloc") { "mimalloc" } else { "system" };

/// `build-info`: the allocator, also in the binary's own bytes, so that build.sh can check a binary of
/// either target without running it.
pub const ALLOCATOR_MARK: &str = if cfg!(feature = "cache-alloc") { "platmon-native-allocator=cache-alloc;" }
    else if cfg!(feature = "mimalloc") { "platmon-native-allocator=mimalloc;" } else { "platmon-native-allocator=system;" };

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

    /// The groups of a collection, the passive ones only where they are on (Python adds them only then).
    fn into_vec(self, passive: [bool; 3]) -> Vec<Group> {
        let mut out = vec![self.cpu_frequency, self.gpu, self.npu, self.temperature, self.power, self.fans, self.power_mode,
                           self.board_info];
        for (on, g) in passive.into_iter().zip([self.network, self.disk_io, self.pressure]) {
            if on {
                out.push(g);
            }
        }
        out
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
    network: Option<passive::Network>,
    disks: Option<passive::Disks>,
    pressure: bool,
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
    /// max_gap: seconds, max(3 × interval, 5 s) in the service; passive: network, disk_io, pressure on
    fn new(max_gap: f64, passive: [bool; 3]) -> Collector {
        let (platform, board) = detect();
        let ns = runtime::seconds_ns(max_gap);
        Collector { platform, board, cpu: CpuCounters::new(Some(max_gap), "interval"), gpu: accel::Accel::gpu(ns),
                    npu: accel::Accel::npu(ns), network: passive[0].then(|| passive::Network::new(Some(max_gap))),
                    disks: passive[1].then(|| passive::Disks::new(Some(max_gap))), pressure: passive[2], logged: Vec::new() }
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
        // after the required data: a failed collection never reaches them; each only when it is on
        let (mut network, mut disk_io, mut pressure) = (None, None, None);
        if let Some(n) = self.network.as_mut() {
            let (v, span) = n.sample(&mut g.network);
            stats.set("network", v);
            network = span;
        }
        if let Some(d) = self.disks.as_mut() {
            let (v, span) = d.sample(&mut g.disk_io);
            stats.set("disk_io", v);
            disk_io = span;
        }
        if self.pressure {
            let (v, span) = passive::pressure(&mut g.pressure);
            stats.set("pressure", v);
            pressure = span;
        }
        let sensors = sensor_entries(&stats, &fans, &g);
        let groups = g.into_vec([self.network.is_some(), self.disks.is_some(), self.pressure]);
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

/// replay-cost FILE [PAGES [OUT]]: what a replay answer costs, by stage, for the core publications in FILE (one
/// JSON publication per line, at most one page of them). Per record, in µs of elapsed time, the median of 7
/// rounds of PAGES pages: decoding the stored payloads, a page of records kept and then freed together;
/// writing records decoded beforehand, the page of texts kept and then freed together; a whole
/// Replay.view (decoding, ages and stale, the records and the envelope, writing). The same work as
/// tools/replay_cost_py.py. One JSON line; OUT gets the page itself.
fn replay_cost(args: &[String]) {
    let Some(path) = args.first() else { eprintln!("replay-cost FILE [PAGES [OUT]]"); std::process::exit(2) };
    let pages: usize = args.get(1).and_then(|v| v.parse().ok()).unwrap_or(20);
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| { eprintln!("{}: {}", path, e); std::process::exit(1) });
    let data: Vec<Value> = text.lines().filter(|l| !l.is_empty())
        .map(|l| json::parse(l).unwrap_or_else(|e| { eprintln!("{}: {}", path, e); std::process::exit(1) })).collect();
    let n = data.len();
    let r = replay::Replay::new("cost".into(), "monotonic", &[], 1e6, 1 << 30);
    for (i, d) in data.iter().enumerate() {
        let t = (i as i64 + 1) * 1_000_000_000;
        r.append("core", i as i64 + 1, t, t, d, false, || t).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1) });
    }
    let now = (n as i64 + 1) * 1_000_000_000;
    let page = || r.view("core", None, 0, n as i64, || now).unwrap_or_else(|e| { eprintln!("{:?}", e); std::process::exit(1) });
    let got = json::parse(&page()).map(|p| match p.get("records") { Some(Value::Arr(a)) => a.len(), _ => 0 });
    if got != Ok(n) {
        eprintln!("one page holds {:?} of the {} records", got, n);
        std::process::exit(1);
    }
    if let Some(out) = args.get(2) {
        std::fs::write(out, page()).unwrap_or_else(|e| { eprintln!("{}: {}", out, e); std::process::exit(1) });
    }
    let payloads: Vec<String> = data.iter().map(Value::to_py_compact).collect();
    let median = |work: &dyn Fn() -> usize| {
        let mut rounds: Vec<f64> = (0..7).map(|_| {
            let t = std::time::Instant::now();
            let mut sink = 0;
            for _ in 0..pages {
                sink += work();
            }
            std::hint::black_box(sink);
            t.elapsed().as_secs_f64() * 1e6 / (pages * n) as f64
        }).collect();
        rounds.sort_by(f64::total_cmp);
        rounds[3]
    };
    let decoded: Vec<Value> = payloads.iter().map(|p| json::parse(p).unwrap()).collect();
    let decode = median(&|| payloads.iter().map(|p| json::parse(p).unwrap()).collect::<Vec<Value>>().len());
    let write = median(&|| decoded.iter().map(Value::to_py_compact).collect::<Vec<String>>().iter().map(String::len).sum());
    let whole = median(&|| page().len());
    let mut out = Value::obj();
    out.set("allocator", ALLOCATOR.into());
    out.set("records", (n as i64).into());
    out.set("payload_bytes", ((payloads.iter().map(String::len).sum::<usize>() / n.max(1)) as i64).into());
    out.set("pages", (pages as i64).into());
    out.set("decode_us", json::round_to(decode, 3).into());
    out.set("write_us", json::round_to(write, 3).into());
    out.set("page_us", json::round_to(whole, 3).into());
    println!("{}", out.to_string(false));
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("serve") => return serve(&args[2..]),
        Some("runtime-script") => return runtime_script(),
        Some("config-check") => return config_check(&args[2..]),
        Some("observe-script") => return observe_script(&args[2..]),
        Some("replay-cost") => return replay_cost(&args[2..]),
        Some("build-info") => return println!("{}", ALLOCATOR_MARK),
        _ => {}
    }
    let mut c = Collector::new(5.0, [true; 3]); // the service's at its default 1 s interval
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

extern "C" fn on_term(_: libc::c_int) {
    unsafe { libc::_exit(0) } // as platmon.py: a clean exit on SIGTERM (systemd stop, docker stop)
}

/// observe-script TARGETS TIMEOUT (tools/fixture.py, the slow groups): for each stdin line, one observation
/// of storage, wifi, battery and probe (with those [probe] targets and timeout), printed as one JSON line
/// {"observations": {group: {"collector", "data"}}}.
fn observe_script(args: &[String]) {
    use std::io::BufRead;
    let targets = config::parse_targets(args.first().map_or("", String::as_str)).unwrap_or_else(|e| { eprintln!("observe-script: {}", e); std::process::exit(2) });
    let timeout: f64 = args.get(1).and_then(|t| t.parse().ok()).unwrap_or(2.0);
    let mut probe = observe::Probe::new(targets, timeout);
    let mut logged: Vec<Vec<(String, Vec<String>)>> = vec![vec![]; 4];
    for _ in std::io::stdin().lock().lines() {
        let mut groups = Value::obj();
        for (i, name) in observe::GROUPS.iter().enumerate() {
            let mut g = metadata::Group::new(name, false);
            let data = match *name {
                "storage" => observe::storage(&mut g),
                "wifi" => observe::wifi(&mut g),
                "battery" => observe::battery(&mut g),
                _ => probe.observe(&mut g),
            };
            let collector = metadata::summarize(std::slice::from_ref(&g), &mut logged[i]).get(name).cloned().unwrap_or_default();
            let mut entry = Value::obj();
            entry.set("collector", collector);
            entry.set("data", data);
            groups.set(name, entry);
        }
        let mut out = Value::obj();
        out.set("observations", groups);
        println!("{}", out.to_string(false));
    }
}

/// config-check [CONFIG [PLATFORM [FRONTENDS]]]: the configuration as serve would use it, {"error": ...} if
/// refused (tools/config_fixture.py compares it with platmon.load_config); empty CONFIG or PLATFORM: none,
/// FRONTENDS: the --frontends value.
fn config_check(args: &[String]) {
    let platform = args.get(1).map(String::as_str).filter(|p| !p.is_empty());
    let frontends = config::frontends(args.get(2).map(String::as_str));
    let out = match config::load(args.first().map(String::as_str).filter(|p| !p.is_empty()), frontends.as_deref()) {
        Ok(cfg) => {
            let mut v = cfg.value(platform);
            if platform.is_none() {
                v.set("levels", cfg.value(None).get("levels").cloned().unwrap_or_default());
            }
            v
        }
        Err(e) => { let mut v = Value::obj(); v.set("error", e.into()); v }
    };
    println!("{}", out.to_string(false));
}

/// platmon.py: serve [CONFIG] [--frontends LIST] [--web DIR] [--app-version V]. The configuration as
/// platmon.py reads it; the slow groups are on as configured, with their readers (src/observe.rs).
fn serve(args: &[String]) {
    let (mut path, mut web_dir, mut version, mut frontends) = (None, None, "0.0.0".to_string(), None);
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--web" | "--app-version" | "--frontends" => {
                let value = it.next().unwrap_or_else(|| { eprintln!("serve: {} needs a value", arg); std::process::exit(2) }).clone();
                match arg.as_str() {
                    "--web" => web_dir = Some(value),
                    "--app-version" => version = value,
                    _ => frontends = Some(value),
                }
            }
            a if a.starts_with("--frontends=") => frontends = Some(a["--frontends=".len()..].to_string()),
            _ if path.is_none() && !arg.starts_with("--") => path = Some(arg.clone()),
            _ => { eprintln!("serve: unknown argument {}", arg); std::process::exit(2) }
        }
    }
    let names = config::frontends(frontends.as_deref());
    let cfg = config::load(path.as_deref(), names.as_deref()).unwrap_or_else(|e| { eprintln!("platmon-native serve: error: {}", e); std::process::exit(2) });
    unsafe { libc::signal(libc::SIGTERM, on_term as extern "C" fn(libc::c_int) as libc::sighandler_t) };
    let interval = cfg.float("core", "interval");
    let mut c = Collector::new((3.0 * interval).max(5.0), [cfg.flag("network", "enabled"), cfg.flag("disk_io", "enabled"),
                                                            cfg.flag("pressure", "enabled")]);
    println!("platmon-native: platform {}, version {} (step 3 runtime)", c.platform, version);
    let source = metadata::clock_source();
    let mut sampler = runtime::Sampler::new(interval, None, runtime::uuid4(), source);
    let slow_names: Vec<&str> = observe::GROUPS.iter().copied()
        .filter(|g| cfg.flag(g, "enabled") && (*g != "probe" || !cfg.targets.is_empty())).collect();
    let replay = cfg.flag("replay", "enabled").then(|| std::sync::Arc::new(replay::Replay::new(
        sampler.instance_id.clone(), source, &slow_names, cfg.float("replay", "retention"), cfg.int("replay", "max_bytes") as usize)));
    // recent numbers of the published snapshots, in memory only (docs/reference/history.md)
    let history = cfg.flag("history", "enabled").then(|| std::sync::Arc::new(history::History::new(cfg.float("history", "retention"), interval)));
    sampler.replay = replay.clone();
    sampler.history = history.clone();
    let sampler = std::sync::Arc::new(sampler);
    let groups: Vec<std::sync::Arc<slow::Slow>> = slow_names.iter().map(|name| {
        let mut g = slow::Slow::new(name, cfg.float(name, "interval"));
        g.replay = replay.clone();
        g.history = history.clone();
        std::sync::Arc::new(g)
    }).collect();
    let observations = std::sync::Arc::new(slow::Observations { groups: groups.clone(), instance_id: sampler.instance_id.clone(),
                                                                clock_source: source });
    let levels = config::levels_for(&cfg, c.platform);
    let (bind, port) = (cfg.text("http", "bind"), cfg.int("http", "port") as u16);
    let listener = http::bind(&bind, port).unwrap_or_else(|e| { eprintln!("platmon-native: cannot listen on {}:{}: {}", bind, port, e); std::process::exit(1) });
    let s = sampler.clone();
    std::thread::Builder::new().name("sampler".into()).spawn(move || {
        static STOP: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
        s.run(|| c.collect().map(Collected::to_value), &STOP)
    }).expect("cannot start the sampler");
    for g in &groups {
        g.start(observe::reader(&g.name, &cfg));
    }
    println!("platmon http on {}:{}", bind, port);
    let web = cfg.flag("http", "web").then_some(web_dir).flatten();
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

static SCRIPT_NOW: std::sync::atomic::AtomicI64 = std::sync::atomic::AtomicI64::new(0);

/// The runtime script's elapsed clock: the time its last operation gave.
fn script_now() -> i64 {
    SCRIPT_NOW.load(std::sync::atomic::Ordering::SeqCst)
}

/// The runtime script (tools/runtime_fixture.py; tools/runtime_py.py is the Python side). One JSON
/// operation per line, one JSON line out for each; every "now" also sets the clock that history, replay
/// and the HTTP answers read:
///   {"op": "setup", "interval", "instance_id", "clock", "slow": [[name, interval], ...], "levels",
///    "history": {"retention"} or null, "replay": {"retention", "max_bytes"} or null}
///                                             also starts the HTTP server on 127.0.0.1 (web off)
///   {"op": "begin", "wall", "now"}            a collection starts
///   {"op": "finish", "wall", "now", "result", "clock"}  it ends: result is Collected.to_value()'s shape or
///                                             {"error": ...}; clock "other": its spans are on another clock
///   {"op": "view", "now"}                     -> {"stats": the /api/stats JSON or null, "status": /api/status's}
///   {"op": "slow_begin", "group", "wall", "now"}
///   {"op": "slow_finish", "group", "now", "wall", "data", "notes": [[target, reason]], "values", "raise"}
///   {"op": "observations", "now"}             -> {"observations": the /api/observations JSON}
///   {"op": "http", "now", "request"}          -> {"response": the raw answer to that raw request}
fn runtime_script() {
    use std::io::{BufRead, Read, Write};
    use std::sync::Arc;
    let mut sampler: Option<Arc<runtime::Sampler>> = None;
    let mut groups: Vec<Arc<slow::Slow>> = vec![];
    let mut obs: Option<Arc<slow::Observations>> = None;
    let mut port = 0;
    let (mut begun, mut slow_begun) = ((0.0, 0), vec![]);
    let set_now = |op: &Value| SCRIPT_NOW.store(integer(op, "now"), std::sync::atomic::Ordering::SeqCst);
    for line in std::io::stdin().lock().lines() {
        let line = line.unwrap_or_default();
        let op = json::parse(&line).unwrap_or_else(|e| { eprintln!("runtime-script: {}: {:?}", e, line); std::process::exit(2) });
        let group = |op: &Value| groups.iter().position(|g| g.name == text(op, "group"))
            .unwrap_or_else(|| { eprintln!("runtime-script: no group {:?}", op.get("group")); std::process::exit(2) });
        let mut out = Value::obj();
        if op.contains("now") {
            set_now(&op);
        }
        match text(&op, "op") {
            "setup" => {
                let source: &'static str = if text(&op, "clock") == "boottime" { "boottime" } else { "monotonic" };
                let id: String = text(&op, "instance_id").into();
                let interval = number(&op, "interval");
                let names: Vec<(String, f64)> = match op.get("slow") {
                    Some(Value::Arr(items)) => items.iter().map(|g| match (g.get_index(0), g.get_index(1)) {
                        (Some(Value::Str(name)), Some(Value::Int(i))) => (name.clone(), *i as f64),
                        (Some(Value::Str(name)), Some(Value::Float(f))) => (name.clone(), *f),
                        _ => { eprintln!("runtime-script: slow is [[name, interval], ...]"); std::process::exit(2) }
                    }).collect(),
                    _ => vec![],
                };
                let history = op.get("history").filter(|h| !h.is_null())
                    .map(|h| Arc::new(history::History::new(number(h, "retention"), interval)));
                let group_names: Vec<&str> = names.iter().map(|(n, _)| n.as_str()).collect();
                let replay = op.get("replay").filter(|r| !r.is_null()).map(|r| Arc::new(replay::Replay::new(
                    id.clone(), source, &group_names, number(r, "retention"), integer(r, "max_bytes") as usize)));
                let mut s = runtime::Sampler::new(interval, None, id.clone(), source);
                s.clock = Box::new(script_now);
                s.history = history.clone();
                s.replay = replay.clone();
                let s = Arc::new(s);
                groups = names.iter().map(|(name, interval)| {
                    let mut g = slow::Slow::new(name, *interval);
                    g.clock = Box::new(script_now);
                    g.history = history.clone();
                    g.replay = replay.clone();
                    Arc::new(g)
                }).collect();
                slow_begun = vec![(0.0, 0); groups.len()];
                let o = Arc::new(slow::Observations { groups: groups.clone(), instance_id: id, clock_source: source });
                let mut srv = http::Server::new(s.clone(), o.clone(), None, "0".into(), op.get("levels").cloned().unwrap_or_default());
                srv.clock = script_now;
                let listener = http::bind("127.0.0.1", 0).unwrap_or_else(|e| { eprintln!("runtime-script: {}", e); std::process::exit(1) });
                port = listener.local_addr().unwrap().port();
                std::thread::spawn(move || Arc::new(srv).serve(listener));
                sampler = Some(s);
                obs = Some(o);
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
                        out.set("observations", obs.as_ref().unwrap().view(|| now).to_py().into());
                    }
                    "http" => {
                        let mut c = std::net::TcpStream::connect(("127.0.0.1", port)).expect("runtime-script: connect");
                        c.write_all(text(&op, "request").as_bytes()).expect("runtime-script: send");
                        let mut answer = Vec::new();
                        let _ = c.read_to_end(&mut answer);
                        out.set("response", String::from_utf8_lossy(&answer).into_owned().into());
                    }
                    other => { eprintln!("runtime-script: unknown op {:?}", other); std::process::exit(2) }
                }
            }
        }
        println!("{}", out.to_string(false));
    }
}
