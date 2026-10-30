//! Linux: /proc, /sys, the device tree and tegrastats, read directly.
pub mod accel;
pub mod common;
pub mod jetson;
pub mod nvidia;
pub mod observe;
pub mod passive;
pub mod sysfs;

use crate::json::Value;
use crate::metadata::{self, Group};
use crate::runtime;
use super::{Collected, CpuCounters};
use common::Fan;

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

    /// The groups of a collection, the passive ones only where they are on.
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

/// (platform, board) from the device tree's compatible string.
fn detect() -> (&'static str, Board) {
    // a compatible string that is not UTF-8 stops the service
    let compatible = sysfs::read(&format!("{}/compatible", sysfs::device_tree()))
        .unwrap_or_else(|e| { eprintln!("platmon-server: {}", e); std::process::exit(1) }).unwrap_or_default();
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
        stats.set("fans", Value::Null); // keeps the recorded answers' key order; filled below
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
        let fan_ids: Vec<u64> = fans.iter().map(|f| f.id).collect();
        let sensors = super::sensor_entries(&stats, &fan_ids, &[&g.temperature, &g.power, &g.fans, &g.cpu_frequency, &g.gpu, &g.npu]);
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
