//! Linux: /proc, /sys, the device tree and tegrastats, read directly (collector/ in the Python service).
pub mod accel;
pub mod common;
pub mod jetson;
pub mod nvidia;
pub mod observe;
pub mod passive;
pub mod sysfs;

use crate::json::Value;
use crate::metadata::{self, pointer, Group, Slot};
use crate::runtime;
use common::{CpuCounters, Fan};

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

pub struct Collector {
    pub platform: &'static str,
    board: Board,
    cpu: CpuCounters,
    gpu: accel::Accel,
    npu: accel::Accel,
    network: Option<passive::Network>,
    disks: Option<passive::Disks>,
    pressure: bool,
    logged: Vec<(String, Vec<String>)>,
}

pub struct Collected {
    pub stats: Value,
    pub sensors: Vec<(String, Value)>,
    pub required: common::Required,
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

impl Collector {
    /// max_gap: seconds, max(3 × interval, 5 s) in the service; passive: network, disk_io, pressure on
    pub fn new(max_gap: f64, passive: [bool; 3]) -> Collector {
        let (platform, board) = detect();
        let ns = runtime::seconds_ns(max_gap);
        Collector { platform, board, cpu: CpuCounters::new(Some(max_gap), "interval"), gpu: accel::Accel::gpu(ns),
                    npu: accel::Accel::npu(ns), network: passive[0].then(|| passive::Network::new(Some(max_gap))),
                    disks: passive[1].then(|| passive::Disks::new(Some(max_gap))), pressure: passive[2], logged: Vec::new() }
    }

    /// collect_recorded(cpu, logged, clock, network, disk_io, pressure, gpu, npu), wired as the service does
    /// by default.
    pub fn collect(&mut self) -> Result<Collected, String> {
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
