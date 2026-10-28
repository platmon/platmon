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

// tests/collector/test_npu.py, ported (docs/development/python-test-port.md).
#[cfg(test)]
mod tests {
    use super::*;
    use crate::collect::linux::sysfs::fake::Root;

    const S: i64 = 1_000_000_000;
    const T0: i64 = 1_000_000_000_000;
    const BUSY: &str = "/sys/class/accel/accel0/device/npu_busy_time_us";

    /// class/accel/<name>/device with a uevent naming the driver and the attributes in it.
    fn accel(root: &Root, name: &str, driver: Option<&str>, attrs: &[(&str, &str)]) {
        let device = format!("/sys/class/accel/{}/device", name);
        root.put(&format!("{}/uevent", device), format!("{}PCI_ID=8086:7D1D\n", driver.map_or(String::new(), |d| format!("DRIVER={}\n", d))));
        for (attr, value) in attrs {
            root.put(&format!("{}/{}", device, attr), value);
        }
    }

    fn vpu(root: &Root, busy: i64, freq: i64) {
        accel(root, "accel0", Some("intel_vpu"), &[("npu_busy_time_us", &busy.to_string()),
              ("npu_current_frequency_mhz", &freq.to_string()), ("npu_max_frequency_mhz", "1400")]);
    }

    fn sample(n: &mut Accel, root: &Root, t: i64) -> (Option<Value>, Group) {
        root.at(t);
        let mut g = Group::new("npu", false);
        (n.sample(&mut g), g)
    }

    fn usage(v: &Option<Value>) -> Option<f64> {
        match v.as_ref().and_then(|v| v.get("usage")) { Some(Value::Float(f)) => Some(*f), _ => None }
    }

    #[test]
    fn the_first_reading_has_no_share_and_the_next_one_has() {
        let root = Root::new(T0);
        vpu(&root, 0, 0);
        let mut n = Accel::npu(5 * S);
        let (first, _) = sample(&mut n, &root, T0);
        assert_eq!(first.unwrap().to_string(false),
                   r#"{"usage":null,"freq":0,"max_freq":1400000000,"driver":"intel_vpu","provider":"npu_busy_time"}"#);
        assert_eq!(usage(&sample(&mut n, &root, T0 + S).0), Some(0.0)); // the counter did not move: idle, measured
        root.put(BUSY, "250000"); // 250 ms of work
        root.put("/sys/class/accel/accel0/device/npu_current_frequency_mhz", "1300");
        let (third, _) = sample(&mut n, &root, T0 + 2 * S);
        assert_eq!(usage(&third), Some(25.0));
        assert!(matches!(third.unwrap().get("freq"), Some(Value::Int(1_300_000_000))));
    }

    #[test]
    fn the_share_is_the_time_the_npu_worked() {
        for (worked_us, share) in [(0, 0.0), (1_000_000, 100.0), (500_000, 50.0), (1_200_000, 100.0)] { // more than the window: clamped
            let root = Root::new(T0);
            vpu(&root, 5000, 0);
            let mut n = Accel::npu(5 * S);
            sample(&mut n, &root, T0);
            root.put(BUSY, (5000 + worked_us).to_string());
            assert_eq!(usage(&sample(&mut n, &root, T0 + S).0), Some(share), "{} us", worked_us);
        }
    }

    #[test]
    fn a_counter_reset_or_a_long_break_is_not_averaged_over() {
        let root = Root::new(T0);
        vpu(&root, 900_000, 0);
        let mut n = Accel::npu(5 * S);
        sample(&mut n, &root, T0);
        root.put(BUSY, "10"); // the device was reset
        assert_eq!(usage(&sample(&mut n, &root, T0 + S).0), None);
        root.put(BUSY, "500010");
        assert_eq!(usage(&sample(&mut n, &root, T0 + 7 * S).0), None); // longer than max_gap
        root.put(BUSY, "600010");
        assert_eq!(usage(&sample(&mut n, &root, T0 + 8 * S).0), Some(10.0)); // measuring again from the last reading
    }

    #[test]
    fn an_accelerator_this_module_cannot_read_is_still_named() {
        let root = Root::new(T0);
        accel(&root, "accel0", Some("amdxdna"), &[]);
        let (v, g) = sample(&mut Accel::npu(5 * S), &root, T0);
        assert_eq!(v.unwrap().to_string(false), r#"{"usage":null,"freq":null,"max_freq":null,"driver":"amdxdna","provider":null}"#);
        assert_eq!(g.status().to_string(false), r#"{"state":"unavailable","reason":"not_exposed","issues":[],"issues_truncated":0}"#);
    }

    #[test]
    fn a_host_without_an_accelerator_is_not_detected() {
        let root = Root::new(T0); // no accel class at all
        let (v, g) = sample(&mut Accel::npu(5 * S), &root, T0);
        assert!(v.is_none());
        assert_eq!(g.status().to_string(false), r#"{"state":"unavailable","reason":"not_detected","issues":[],"issues_truncated":0}"#);
    }

    #[test]
    fn an_unreadable_attribute_is_a_failure_and_keeps_the_rest() {
        if unsafe { libc::geteuid() } == 0 {
            return; // root reads a file of mode 000
        }
        let root = Root::new(T0);
        vpu(&root, 0, 950);
        root.chmod(BUSY, 0);
        let (v, g) = sample(&mut Accel::npu(5 * S), &root, T0);
        let v = v.unwrap();
        assert!(v.get("usage").unwrap().is_null() && matches!(v.get("freq"), Some(Value::Int(950_000_000))));
        assert_eq!(g.status().to_string(false),
                   r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"npu_busy_time","reason":"permission_denied"}],"issues_truncated":0}"#);
    }

    #[test]
    fn each_value_carries_where_it_was_read() {
        let root = Root::new(T0);
        vpu(&root, 0, 0);
        root.at(T0);
        let mut g = Group::new("npu", true);
        Accel::npu(5 * S).sample(&mut g);
        let keys: Vec<String> = g.sensors.iter().map(|(s, _)| match s { Slot::Key(k) => k.clone(), _ => String::new() }).collect();
        assert_eq!(keys, ["usage", "freq", "max_freq"]);
        for ((_, record), unit) in g.sensors.iter().zip(["percent", "hertz", "hertz"]) {
            assert_eq!(record.get("provider").unwrap().to_string(false), r#""accel""#);
            assert_eq!(record.get("unit").unwrap().to_string(false), format!("{:?}", unit));
            assert_eq!(record.get("identity_basis").unwrap().to_string(false), r#""resolved_path""#);
            assert!(record.get("id").unwrap().to_string(false).starts_with(r#""src1:"#));
            assert!(!record.get("span").unwrap().is_null());
        }
    }
}

// tests/collector/test_gpu.py's DRM tests, ported; its nvidia-smi tests are fixture.py's nvidia scenarios
// and nvidia.rs's own.
#[cfg(test)]
mod gpu_tests {
    use super::*;
    use crate::collect::linux::sysfs::fake::Root;

    const S: i64 = 1_000_000_000;
    const T0: i64 = 1_000_000_000_000;
    const RC6: &str = "/sys/class/drm/card0/gt/gt0/rc6_residency_ms";

    /// class/drm/<card> with a device/uevent (DRIVER= when given) and the files given, relative to the card.
    fn drm(root: &Root, card: &str, driver: Option<&str>, files: &[(&str, &str)]) {
        let dir = format!("/sys/class/drm/{}", card);
        root.put(&format!("{}/device/uevent", dir),
                 format!("{}PCI_CLASS=30000\nPCI_ID=8086:7D55\n", driver.map_or(String::new(), |d| format!("DRIVER={}\n", d))));
        for (rel, value) in files {
            root.put(&format!("{}/{}", dir, rel), value);
        }
    }

    fn intel(root: &Root, rc6: i64) {
        drm(root, "card0", Some("i915"), &[("gt/gt0/rc6_residency_ms", &rc6.to_string()), ("gt_act_freq_mhz", "300"), ("gt_max_freq_mhz", "1500")]);
    }

    fn gpu() -> Accel {
        Accel { kind: "gpu", max_gap: 5 * S, last: Vec::new(), nvidia_smi: None } // no nvidia-smi: DRM alone
    }

    fn sample(g: &mut Accel, root: &Root, t: i64) -> (Option<Value>, Group) {
        root.at(t);
        let mut group = Group::new("gpu", false);
        (g.sample(&mut group), group)
    }

    fn usage(v: &Option<Value>) -> String {
        v.as_ref().unwrap().get("usage").unwrap().to_string(false)
    }

    #[test]
    fn the_first_reading_has_no_share_and_the_next_one_has() {
        let root = Root::new(T0);
        intel(&root, 1000);
        let mut g = gpu();
        assert_eq!(sample(&mut g, &root, T0).0.unwrap().to_string(false),
                   r#"{"usage":null,"freq":300000000,"max_freq":1500000000,"driver":"i915","provider":"rc6_residency"}"#);
        root.put(RC6, "1400"); // 400 ms idle of 1000 ms
        assert_eq!(usage(&sample(&mut g, &root, T0 + S).0), "60.0");
    }

    #[test]
    fn the_share_is_the_time_the_gpu_was_not_idle() {
        for (idled_ms, share) in [(1000, "0.0"), (0, "100.0"), (500, "50.0"), (999, "0.1"), (1200, "0.0")] { // clamped, never negative
            let root = Root::new(T0);
            intel(&root, 5000);
            let mut g = gpu();
            sample(&mut g, &root, T0);
            root.put(RC6, (5000 + idled_ms).to_string());
            assert_eq!(usage(&sample(&mut g, &root, T0 + S).0), share, "{} ms", idled_ms);
        }
    }

    #[test]
    fn a_counter_reset_or_a_long_break_is_not_averaged_over() {
        let root = Root::new(T0);
        intel(&root, 5000);
        let mut g = gpu();
        sample(&mut g, &root, T0);
        root.put(RC6, "10"); // the device was reset
        assert_eq!(usage(&sample(&mut g, &root, T0 + S).0), "null");
        root.put(RC6, "3010");
        assert_eq!(usage(&sample(&mut g, &root, T0 + 7 * S).0), "null"); // longer than max_gap
        root.put(RC6, "3510");
        assert_eq!(usage(&sample(&mut g, &root, T0 + 8 * S).0), "50.0"); // measuring again from the last reading
    }

    #[test]
    fn amdgpu_publishes_the_share_itself() {
        let root = Root::new(T0);
        drm(&root, "card0", Some("amdgpu"), &[("device/gpu_busy_percent", "37")]);
        let (v, group) = sample(&mut gpu(), &root, T0);
        assert_eq!(v.unwrap().to_string(false), r#"{"usage":37.0,"freq":null,"max_freq":null,"driver":"amdgpu","provider":"gpu_busy_percent"}"#);
        assert_eq!(group.status().get("state").unwrap().to_string(false), r#""ok""#);
    }

    #[test]
    fn a_card_this_module_cannot_read_is_still_named() {
        let root = Root::new(T0);
        drm(&root, "card0", Some("nouveau"), &[]);
        let (v, group) = sample(&mut gpu(), &root, T0);
        assert_eq!(v.unwrap().to_string(false), r#"{"usage":null,"freq":null,"max_freq":null,"driver":"nouveau","provider":null}"#);
        assert_eq!(group.status().to_string(false), r#"{"state":"unavailable","reason":"not_exposed","issues":[],"issues_truncated":0}"#);
    }

    #[test]
    fn no_card_at_all_is_not_detected() {
        for class in [true, false] { // an empty class, and no DRM class at all (a kernel without it): no failure
            let root = Root::new(T0);
            if class {
                root.mkdir("/sys/class/drm");
            }
            let (v, group) = sample(&mut gpu(), &root, T0);
            assert!(v.is_none());
            assert_eq!(group.status().to_string(false), r#"{"state":"unavailable","reason":"not_detected","issues":[],"issues_truncated":0}"#);
        }
    }

    #[test]
    fn connectors_are_not_cards_and_the_first_readable_card_wins() {
        let root = Root::new(T0);
        drm(&root, "card0", Some("nouveau"), &[]);
        drm(&root, "card1", Some("i915"), &[("gt/gt0/rc6_residency_ms", "1000"), ("gt_act_freq_mhz", "700"), ("gt_max_freq_mhz", "1800")]);
        drm(&root, "card1-DP-1", Some("i915"), &[]);
        let v = sample(&mut gpu(), &root, T0).0.unwrap();
        assert_eq!((v.get("driver").unwrap().to_string(false), v.get("freq").unwrap().to_string(false)), (r#""i915""#.into(), "700000000".into()));
    }

    #[test]
    fn an_unreadable_attribute_is_a_failure_and_keeps_the_rest() {
        let root = Root::new(T0);
        intel(&root, 1000);
        crate::collect::linux::sysfs::fake::answers(RC6, vec![Err(libc::EACCES)]);
        let (v, group) = sample(&mut gpu(), &root, T0);
        let v = v.unwrap();
        assert_eq!((v.get("usage").unwrap().to_string(false), v.get("freq").unwrap().to_string(false)), ("null".into(), "300000000".into()));
        assert_eq!(group.status().to_string(false), // the clocks were read
                   r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"rc6_residency","reason":"permission_denied"}],"issues_truncated":0}"#);
    }

    #[test]
    fn each_value_carries_where_it_was_read() {
        let root = Root::new(T0);
        intel(&root, 1000);
        root.at(T0);
        let mut group = Group::new("gpu", true);
        gpu().sample(&mut group);
        let keys: Vec<String> = group.sensors.iter().map(|(s, _)| match s { Slot::Key(k) => k.clone(), _ => String::new() }).collect();
        assert_eq!(keys, ["usage", "freq", "max_freq"]);
        for ((_, record), unit) in group.sensors.iter().zip(["percent", "hertz", "hertz"]) {
            assert_eq!(record.get("provider").unwrap().to_string(false), r#""drm""#);
            assert_eq!(record.get("unit").unwrap().to_string(false), format!("{:?}", unit));
            assert_eq!(record.get("identity_basis").unwrap().to_string(false), r#""resolved_path""#);
            assert!(record.get("id").unwrap().to_string(false).starts_with(r#""src1:"#) && !record.get("span").unwrap().is_null());
        }
        assert_ne!(group.sensors[0].1.get("id"), group.sensors[1].1.get("id"));
    }
}
