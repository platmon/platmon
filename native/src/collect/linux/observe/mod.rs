//! The slow groups' readers: storage, Wi-Fi, battery, probe and processes, each called by its
//! group's thread (src/slow.rs) with the group's diagnostics. Contracts: docs/reference/storage.md,
//! wifi.md, battery.md, probe.md, processes.md.
mod battery;
mod probe;
mod processes;
mod storage;
mod wifi;

pub use battery::battery;
pub use probe::connect_ms;
pub use storage::storage;
pub use wifi::wifi;

use crate::config::Config;
use crate::json::Value;
use crate::slow::Reader;
use super::sysfs::ReadError;

/// The slow groups in their order (of /api/observations and the replay streams).
pub const GROUPS: [&str; 5] = ["storage", "wifi", "battery", "probe", "processes"];

/// The reader of a group, configured from cfg.
pub fn reader(name: &str, cfg: &Config) -> Reader {
    match name {
        "storage" => Box::new(|g| Ok(storage(g))),
        "wifi" => Box::new(|g| Ok(wifi(g))),
        "battery" => Box::new(|g| Ok(battery(g))),
        "processes" => {
            let mut p = processes::Processes::new(cfg.int("processes", "top") as usize);
            Box::new(move |g| Ok(p.observe(g)))
        }
        _ => {
            let mut p = crate::collect::probe::Probe::new(cfg.targets.clone(), cfg.float("probe", "timeout"));
            Box::new(move |g| Ok(p.observe(g)))
        }
    }
}

fn obj(items: Vec<(&str, Value)>) -> Value {
    Value::Obj(items.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn os_error(e: &ReadError) -> Option<&std::io::Error> {
    match e { ReadError::Os(e) => Some(e), ReadError::Utf8 => None }
}
