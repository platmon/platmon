//! collector/gpu.py (DRM class, then nvidia-smi: nvidia.rs) and collector/npu.py (accel class).
use crate::json::{round_to, Value};
use crate::metadata::{clock, source, Group, Slot};
use super::sysfs::{self, driver_of, entries};

fn numbered_name(n: &str, prefix: &str) -> bool {
    n.strip_prefix(prefix).map_or(false, |d| !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit()))
}

fn src(trace: bool, provider: &str, unit: &str, path: &str) -> Option<Value> {
    if !trace { return None; }
    let rel = sysfs::canonical(path, "/sys");
    let basis = if rel.is_some() { "resolved_path" } else { "unresolved" };
    Some(source(provider, unit, basis, vec![("path", rel.into())]))
}

fn mhz_clock(g: &mut Group, provider: &str, dir: &str, attr: &str, key: &str) -> Option<i64> {
    let target = attr.rsplit('/').next().unwrap();
    let mhz = g.read_int(&format!("{}/{}", dir, attr), target, Some(dir));
    let span = g.span;
    g.sensor(Slot::Key(key.into()), src(g.trace, provider, "hertz", &format!("{}/{}", dir, attr)), span);
    mhz.map(|m| g.got(m * 1_000_000))
}

fn entry(usage: Option<f64>, freq: Option<i64>, max_freq: Option<i64>, driver: Option<String>, provider: Option<&str>) -> Value {
    let mut v = Value::obj();
    v.set("usage", usage.into());
    v.set("freq", freq.into());
    v.set("max_freq", max_freq.into());
    v.set("driver", driver.into());
    v.set("provider", provider.into());
    v
}

/// A cumulative counter's share of the window between two collections (i915 RC6 idle, intel_vpu busy).
pub struct Accel {
    kind: &'static str, // "gpu" or "npu"
    max_gap: i64,
    last: Vec<(String, i64, i64)>, // path -> (elapsed ns, counter)
    nvidia_smi: Option<String>, // gpu: found once, as Gpu() does
}

impl Accel {
    /// max_gap: nanoseconds, round(max_gap * 1e9) of the Python Gpu/Npu
    pub fn gpu(max_gap: i64) -> Accel { Accel { kind: "gpu", max_gap, last: Vec::new(), nvidia_smi: super::nvidia::find() } }
    pub fn npu(max_gap: i64) -> Accel { Accel { kind: "npu", max_gap, last: Vec::new(), nvidia_smi: None } }

    fn share(&mut self, path: &str, counter: i64, ns_per_unit: i64, busy: bool) -> Option<f64> {
        let now = clock();
        let before = self.last.iter().find(|(p, _, _)| p == path).map(|(_, t, c)| (*t, *c));
        self.last.retain(|(p, _, _)| p != path);
        self.last.push((path.to_string(), now, counter));
        let (t0, c0) = before?;
        let (elapsed, delta) = (now - t0, (counter - c0) * ns_per_unit);
        if 0 < elapsed && elapsed <= self.max_gap && delta >= 0 {
            let x = delta as f64 / elapsed as f64;
            Some(round_to(100.0 * (if busy { x } else { 1.0 - x }).clamp(0.0, 1.0), 1))
        } else {
            None
        }
    }

    pub fn sample(&mut self, g: &mut Group) -> Option<Value> {
        let (class, prefix, readers): (&str, &str, &[&str]) = if self.kind == "gpu" {
            ("/sys/class/drm", "card", &["i915", "amdgpu"])
        } else {
            ("/sys/class/accel", "accel", &["intel_vpu"])
        };
        let names: Vec<String> = entries(class, &mut [g], false).into_iter().filter(|n| numbered_name(n, prefix)).collect();
        if names.is_empty() {
            if let Some(path) = &self.nvidia_smi {
                return super::nvidia::sample(path, g);
            }
            g.note(self.kind, "not_detected", None);
            return None;
        }
        let drivers: Vec<(String, Option<String>)> = names.iter().map(|n| (n.clone(), driver_of(&format!("{}/{}", class, n), g))).collect();
        let (name, driver) = drivers.iter().find(|(_, d)| d.as_deref().map_or(false, |d| readers.contains(&d)))
            .unwrap_or(&drivers[0]).clone();
        let dir = format!("{}/{}", class, name);
        match driver.as_deref() {
            Some("i915") => {
                let path = format!("{}/gt/gt0/rc6_residency_ms", dir);
                let idle = g.read_int(&path, "rc6_residency", Some(&dir));
                let span = g.span;
                let usage = idle.and_then(|i| self.share(&dir, i, 1_000_000, false));
                g.sensor(Slot::Key("usage".into()), src(g.trace, "drm", "percent", &path), span);
                let usage = usage.map(|u| g.got(u));
                let f = mhz_clock(g, "drm", &dir, "gt_act_freq_mhz", "freq");
                let m = mhz_clock(g, "drm", &dir, "gt_max_freq_mhz", "max_freq");
                Some(entry(usage, f, m, driver, Some("rc6_residency")))
            }
            Some("amdgpu") => {
                let path = format!("{}/device/gpu_busy_percent", dir);
                let busy = g.read_int(&path, "gpu_busy_percent", Some(&dir));
                let span = g.span;
                g.sensor(Slot::Key("usage".into()), src(g.trace, "drm", "percent", &path), span);
                Some(entry(busy.map(|b| g.got(b as f64)), None, None, driver, Some("gpu_busy_percent")))
            }
            Some("intel_vpu") => {
                let path = format!("{}/device/npu_busy_time_us", dir);
                let busy = g.read_int(&path, "npu_busy_time", Some(&dir));
                let span = g.span;
                let usage = busy.and_then(|b| self.share(&dir, b, 1_000, true));
                g.sensor(Slot::Key("usage".into()), src(g.trace, "accel", "percent", &path), span);
                let usage = usage.map(|u| g.got(u));
                let f = mhz_clock(g, "accel", &dir, "device/npu_current_frequency_mhz", "freq");
                let m = mhz_clock(g, "accel", &dir, "device/npu_max_frequency_mhz", "max_freq");
                Some(entry(usage, f, m, driver, Some("npu_busy_time")))
            }
            _ => {
                if let Some(v) = self.nvidia_smi.as_ref().and_then(|path| super::nvidia::sample(path, g)) {
                    return Some(v);
                }
                let target = driver.clone().unwrap_or_else(|| name.clone());
                g.note(&target, "not_exposed", None);
                Some(entry(None, None, None, driver, None))
            }
        }
    }
}
