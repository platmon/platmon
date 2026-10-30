//! Fields every Linux host has (CPU, clocks, thermal zones, hwmon, memory, disk, identity).
use crate::json::{round_to, Value};
use crate::metadata::{self, source, Group, Slot, Span};
use super::sysfs::{self, entries, hwmon_chips, numbered};
use super::Groups;
use crate::collect::{timed, CpuCounters, Required};
use std::collections::BTreeMap;

fn basename(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

fn dirname(path: &str) -> &str {
    path.rsplit_once('/').map_or("", |(h, _)| h)
}

/// "key (hwmonN)" when another chip already reported the same name.
fn unique_key(table: &[(String, Value)], key: String, d: &str) -> String {
    if table.iter().any(|(k, _)| *k == key) { format!("{} ({})", key, basename(d)) } else { key }
}

fn set(table: &mut Vec<(String, Value)>, key: String, value: Value) {
    match table.iter_mut().find(|(k, _)| *k == key) {
        Some(slot) => slot.1 = value,
        None => table.push((key, value)),
    }
}

pub struct Fan {
    pub id: u64,
    pub name: String,
    pub rpm: Option<i64>,
    pub percent: Option<i64>,
}

pub fn next_fan_id() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(1);
    N.fetch_add(1, Ordering::Relaxed)
}

type Where = Vec<(&'static str, Value)>;

/// Temps, power and fans from every hwmon chip, in one pass (common.hwmon_sensors).
pub fn hwmon_sensors(skip: &[String], skip_devices: &[String], g: &mut Groups)
    -> (Vec<(String, Value)>, Vec<(String, Value)>, Vec<Fan>) {
    let root = "/sys/class/hwmon";
    let sysroot = "/sys";
    let (mut temps, mut power, mut fans) = (Vec::new(), Vec::new(), Vec::new());
    let track = g.temperature.trace;
    let chips = hwmon_chips(root, &mut [&mut g.temperature, &mut g.power, &mut g.fans]);
    let ids = if track { sysfs::chip_identities(&chips, sysroot) } else { Vec::new() };
    for (d, chip) in &chips {
        if skip.contains(chip) {
            continue;
        }
        if !skip_devices.is_empty() {
            if let Some(rel) = sysfs::canonical(d, sysroot) {
                if skip_devices.iter().any(|s| s == dirname(&rel)) {
                    continue;
                }
            }
        }
        let dev = basename(d).to_string();
        let (basis, where_): (&str, Where) = ids.iter().find(|(x, _, _)| x == d)
            .map(|(_, b, w)| (*b, w.clone())).unwrap_or(("unresolved", vec![]));
        let src = |unit: &str, attr: Where| -> Option<Value> {
            if !track { return None; }
            let mut w = where_.clone();
            w.extend(attr);
            Some(source("hwmon", unit, basis, w))
        };
        let names = entries(d, &mut [&mut g.temperature, &mut g.power, &mut g.fans], true);
        let label = |group: &mut Group, kind: &str, n: i64, default: String| -> String {
            group.read_str(&format!("{}/{}{}_label", d, kind, n), &format!("{}.{}{}_label", dev, kind, n), Some(d))
                .filter(|s| !s.is_empty()).unwrap_or(default)
        };
        for n in numbered(&names, "temp", "_input") {
            let path = format!("{}/temp{}_input", d, n);
            let t = g.temperature.read_int(&path, &format!("{}.temp{}", dev, n), Some(&path));
            let span = g.temperature.span;
            if let Some(t) = t {
                let l = label(&mut g.temperature, "temp", n, format!("temp{}", n));
                let key = unique_key(&temps, format!("{} {}", chip, l), d);
                let v = g.temperature.got(t as f64 / 1000.0);
                set(&mut temps, key.clone(), v.into());
                g.temperature.sensor(Slot::Key(key), src("celsius", vec![("attr", format!("temp{}_input", n).into())]), span);
            }
        }
        for n in numbered(&names, "power", "_input") {
            let path = format!("{}/power{}_input", d, n);
            let p = g.power.read_int(&path, &format!("{}.power{}", dev, n), Some(&path));
            let span = g.power.span;
            if let Some(p) = p {
                let l = label(&mut g.power, "power", n, format!("{} power{}", chip, n));
                let key = unique_key(&power, l, d);
                let v = g.power.got(round_to(p as f64 / 1e6, 2));
                set(&mut power, key.clone(), v.into());
                g.power.sensor(Slot::Key(key), src("watt", vec![("attr", format!("power{}_input", n).into())]), span);
            }
        }
        for n in numbered(&names, "curr", "_input") {
            let lbl = g.power.read_str(&format!("{}/in{}_label", d, n), &format!("{}.in{}_label", dev, n), Some(d));
            let lbl = match lbl { Some(l) if !l.is_empty() => l, _ => continue };
            let path = format!("{}/curr{}_input", d, n);
            let ma = g.power.read_int(&path, &format!("{}.curr{}", dev, n), Some(&path));
            let span_a = g.power.span;
            let mv = g.power.read_int(&format!("{}/in{}_input", d, n), &format!("{}.in{}", dev, n), Some(d));
            let span_v = g.power.span;
            if let (Some(ma), Some(mv)) = (ma, mv) {
                let key = unique_key(&power, lbl, d);
                let v = g.power.got(round_to((mv * ma) as f64 / 1e6, 2));
                set(&mut power, key.clone(), v.into());
                let span = match (span_a, span_v) { (Some(a), Some(b)) => Some((a.0.min(b.0), a.1.max(b.1))), _ => None };
                let inputs = Value::Arr(vec![
                    Value::Arr(vec!["voltage".into(), format!("in{}_input", n).into()]),
                    Value::Arr(vec!["current".into(), format!("curr{}_input", n).into()]),
                ]);
                g.power.sensor(Slot::Key(key), src("watt", vec![("calc", "product".into()), ("inputs", inputs)]), span);
            }
        }
        let mut add_fan = |g: &mut Groups, name: String, n: i64, rpm: Option<i64>, rpm_span: Option<Span>, found: &str| {
            let pwm = g.fans.read_int(&format!("{}/pwm{}", d, n), &format!("{}.pwm{}", dev, n), Some(found));
            let percent = pwm.map(|p| g.fans.got((p as f64 * 100.0 / 255.0).round_ties_even() as i64));
            let fan = Fan { id: next_fan_id(), name, rpm, percent };
            if let Some(span) = rpm_span { // None: this fan has no rpm attribute at all
                g.fans.sensor(Slot::Fan(fan.id, "rpm"), src("rpm", vec![("attr", format!("fan{}_input", n).into())]), span);
            }
            let span = g.fans.span;
            g.fans.sensor(Slot::Fan(fan.id, "percent"), src("percent", vec![("attr", format!("pwm{}", n).into())]), span);
            fans.push(fan);
        };
        let fan_idx = numbered(&names, "fan", "_input");
        for &n in &fan_idx {
            let path = format!("{}/fan{}_input", d, n);
            let rpm = g.fans.read_with(&path, &format!("{}.fan{}", dev, n), Some(&path), true, metadata::py_int);
            let span = g.fans.span;
            let rpm = rpm.map(|r| g.fans.got(r));
            let name = label(&mut g.fans, "fan", n, format!("{} fan{}", chip, n));
            add_fan(g, name, n, rpm, Some(span), d);
        }
        if fan_idx.is_empty() && names.iter().any(|x| x == "pwm1") {
            add_fan(g, chip.clone(), 1, None, None, &format!("{}/pwm1", d));
        }
    }
    (temps, power, fans)
}

/// Zone temps, the zone types their hwmon twins may be named after, and the zones' resolved paths.
pub fn thermal_zones(g: &mut Group) -> (Vec<(String, Value)>, Vec<String>, Vec<String>) {
    let root = "/sys/class/thermal";
    let sysroot = "/sys";
    let (mut temps, mut types, mut paths): (Vec<(String, Value)>, Vec<String>, Vec<String>) = (Vec::new(), Vec::new(), Vec::new());
    let names = entries(root, &mut [g], false);
    for n in numbered(&names, "thermal_zone", "") {
        let z = format!("{}/thermal_zone{}", root, n);
        let zb = basename(&z).to_string();
        let rel = sysfs::canonical(&z, sysroot);
        if let Some(r) = &rel {
            if !paths.contains(r) { paths.push(r.clone()); }
        }
        let zt = g.read_str(&format!("{}/type", z), &format!("{}.type", zb), Some(&z))
            .filter(|s| !s.is_empty()).unwrap_or_else(|| zb.clone());
        let twin = zt.replace('-', "_");
        if !types.contains(&twin) { types.push(twin); }
        let t = g.read_int(&format!("{}/temp", z), &zb, Some(&z));
        let span = g.span;
        if let Some(t) = t {
            let mut key = zt.strip_suffix("-thermal").map(str::to_string).unwrap_or_else(|| zt.clone());
            if temps.iter().any(|(k, _)| *k == key) {
                key = format!("{} ({})", key, zb);
            }
            let v = g.got(t as f64 / 1000.0);
            set(&mut temps, key.clone(), v.into());
            if g.trace {
                let basis = if rel.is_some() { "resolved_path" } else { "unresolved" };
                g.sensor(Slot::Key(key), Some(source("thermal", "celsius", basis,
                    vec![("path", rel.clone().into()), ("attr", "temp".into())])), span);
            }
        }
    }
    (temps, types, paths)
}

/// {cpu id: jiffies} for online cores; an error fails the collection.
pub fn cpu_times() -> Result<BTreeMap<i64, Vec<i64>>, String> {
    let text = std::fs::read_to_string(&*sysfs::at("/proc/stat")).map_err(|e| e.to_string())?;
    let mut times = BTreeMap::new();
    for line in text.lines() {
        let mut it = line.split_whitespace();
        let name = it.next().unwrap_or("");
        if let Some(id) = name.strip_prefix("cpu").filter(|d| !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit())) {
            let fields: Vec<i64> = it.map(|f| f.parse::<i64>().map_err(|e| e.to_string())).collect::<Result<_, _>>()?;
            if fields.len() < 8 {
                return Err(format!("{}: {} counters in /proc/stat, need at least 8", name, fields.len()));
            }
            times.insert(id.parse::<i64>().unwrap(), fields);
        }
    }
    if times.is_empty() {
        return Err("no per-cpu lines in /proc/stat".into());
    }
    Ok(times)
}

pub fn meminfo() -> Result<Vec<(String, i64)>, String> {
    let text = std::fs::read_to_string(&*sysfs::at("/proc/meminfo")).map_err(|e| e.to_string())?;
    text.lines().map(|l| {
        let (k, v) = l.split_once(':').ok_or("meminfo line")?;
        let n: i64 = v.split_whitespace().next().ok_or("meminfo value")?.parse().map_err(|_| "meminfo value")?;
        Ok((k.to_string(), n * 1024))
    }).collect()
}

/// (sysname, nodename, release, version, machine)
pub fn os_uname() -> [String; 5] {
    let mut u: libc::utsname = unsafe { std::mem::zeroed() };
    unsafe { libc::uname(&mut u) };
    let s = |f: &[libc::c_char]| {
        let b: Vec<u8> = f.iter().take_while(|c| **c != 0).map(|c| *c as u8).collect();
        String::from_utf8_lossy(&b).into_owned()
    };
    [s(&u.sysname), s(&u.nodename), s(&u.release), s(&u.version), s(&u.machine)]
}

/// shutil.disk_usage: (total, used) in bytes.
pub fn disk_usage(path: &str) -> Result<(i64, i64), String> {
    let c = std::ffi::CString::new(&*sysfs::at(path)).map_err(|e| e.to_string())?;
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    let (frsize, blocks, bfree) = (st.f_frsize as u64, st.f_blocks as u64, st.f_bfree as u64);
    Ok(((blocks * frsize) as i64, ((blocks - bfree) * frsize) as i64))
}

pub fn os_release(text: Option<&str>) -> Option<String> {
    let mut fields: Vec<(String, String)> = Vec::new();
    for line in text.unwrap_or("").lines() {
        if let Some((k, v)) = line.split_once('=') {
            let v = v.trim().trim_matches(|c| c == '"' || c == '\'');
            let k = k.trim().to_string();
            fields.retain(|(x, _)| *x != k);
            fields.push((k, v.to_string()));
        }
    }
    let get = |k: &str| fields.iter().find(|(x, _)| x == k).map(|(_, v)| v.clone()).filter(|v| !v.is_empty());
    get("PRETTY_NAME").or_else(|| {
        let parts: Vec<String> = [get("NAME"), get("VERSION")].into_iter().flatten().collect();
        if parts.is_empty() { None } else { Some(parts.join(" ")) }
    })
}

pub fn system_info() -> Result<Value, String> {
    let u = os_uname();
    let osr = match sysfs::read(&sysfs::host_path("/etc/os-release"))?.filter(|s| !s.is_empty()) {
        Some(t) => Some(t),
        None => sysfs::read(&sysfs::host_path("/usr/lib/os-release"))?,
    };
    let mut v = Value::obj();
    v.set("os", os_release(osr.as_deref()).into());
    v.set("kernel", u[2].clone().into());
    v.set("arch", u[4].clone().into());
    v.set("hostname", u[1].clone().into());
    Ok(v)
}

/// common.collect with the service's CpuCounters and groups; Err fails the collection.
pub fn collect(cpu: &mut CpuCounters, g: &mut Groups, req: &mut Required) -> Result<(Value, Vec<Fan>), String> {
    let (usage, sampling) = cpu.sample(cpu_times)?;
    req.push(("cpu", cpu.span));
    let mut freqs: Vec<(i64, Option<i64>)> = Vec::new();
    if usage.is_empty() {
        g.cpu_frequency.note("cpu", "no_data", None);
    }
    for (n, _) in &usage {
        let f = g.cpu_frequency.read_int(&format!("/sys/devices/system/cpu/cpu{}/cpufreq/scaling_cur_freq", n), &format!("cpu{}", n), None);
        let f = f.map(|f| g.cpu_frequency.got(f * 1000));
        freqs.push((*n, f));
        if g.cpu_frequency.trace {
            let span = g.cpu_frequency.span;
            g.cpu_frequency.sensor(Slot::Cpu(*n), Some(source("cpufreq", "hertz", "device_channel",
                vec![("cpu", (*n).into()), ("attr", "scaling_cur_freq".into())])), span);
        }
    }
    let (mut temps, zone_types, zone_paths) = thermal_zones(&mut g.temperature);
    let (hw_temps, power, fans) = hwmon_sensors(&zone_types, &zone_paths, g);
    for (k, v) in hw_temps {
        set(&mut temps, k, v);
    }
    let mem = timed(req, "memory", meminfo)?;
    let disk = timed(req, "disk", || disk_usage(&sysfs::host_root()))?;
    let uptime = timed(req, "uptime", || sysfs::read("/proc/uptime"))?;
    let uptime: f64 = uptime.as_deref().and_then(|u| u.split_whitespace().next()).and_then(|u| u.parse().ok())
        .ok_or("uptime unreadable")?;
    let m = |k: &str| mem.iter().find(|(x, _)| x == k).map(|(_, v)| *v).ok_or(format!("meminfo: no {}", k));
    let model = match sysfs::read(&format!("{}/model", sysfs::device_tree()))?.filter(|s| !s.is_empty()) {
        Some(m) => m,
        None => sysfs::read("/sys/class/dmi/id/product_name")?.filter(|s| !s.is_empty()).unwrap_or_else(|| os_uname()[1].clone()),
    };

    let mut s = Value::obj();
    s.set("time", metadata::wall().into());
    s.set("model", model.trim_end_matches('\0').into());
    s.set("system", system_info()?);
    s.set("uptime", uptime.into());
    s.set("power_mode", Value::Null);
    s.set("cpu", Value::Arr(usage.iter().map(|(n, u)| {
        let mut c = Value::obj();
        c.set("id", (*n).into());
        c.set("usage", (*u).into());
        c.set("freq", freqs.iter().find(|(x, _)| x == n).and_then(|(_, f)| *f).into());
        c
    }).collect()));
    s.set("cpu_sampling", sampling);
    s.set("gpu", Value::Null);
    let mut mv = Value::obj();
    mv.set("total", m("MemTotal")?.into());
    mv.set("used", (m("MemTotal")? - m("MemAvailable")?).into());
    mv.set("swap_total", m("SwapTotal")?.into());
    mv.set("swap_used", (m("SwapTotal")? - m("SwapFree")?).into());
    s.set("memory", mv);
    let mut dv = Value::obj();
    dv.set("total", disk.0.into());
    dv.set("used", disk.1.into());
    s.set("disk", dv);
    s.set("temperature", Value::Obj(temps));
    s.set("power", Value::Obj(power));
    Ok((s, fans))
}

// tests/collector/test_common.py, ported (docs/development/python-test-port.md): /proc/stat, board detection,
// thermal zones, hwmon and the groups' rules for what could not be read. CpuCounters: collect/mod.rs.
#[cfg(test)]
mod tests {
    use super::*;
    use crate::collect::linux::sysfs::fake::{self, Root};

    fn hwmon(root: &Root, chips: &[&[(&str, &str)]]) {
        for (i, files) in chips.iter().enumerate() {
            root.mkdir(&format!("/sys/class/hwmon/hwmon{}", i));
            for (name, value) in *files {
                root.put(&format!("/sys/class/hwmon/hwmon{}/{}", i, name), value);
            }
        }
    }

    type Read = (String, String, String, Groups);

    /// hwmon_sensors as (temps, power, fans) JSON and the groups.
    fn sensors(skip: &[&str], skip_devices: &[&str]) -> Read {
        let mut g = Groups::new(false);
        let (t, p, f) = hwmon_sensors(&skip.iter().map(|s| s.to_string()).collect::<Vec<_>>(),
                                      &skip_devices.iter().map(|s| s.to_string()).collect::<Vec<_>>(), &mut g);
        (Value::Obj(t).to_string(false), Value::Obj(p).to_string(false), fans_json(&f), g)
    }

    fn fans_json(f: &[Fan]) -> String {
        Value::Arr(f.iter().map(|f| {
            let mut v = Value::obj();
            v.set("name", f.name.as_str().into());
            v.set("rpm", f.rpm.into());
            v.set("percent", f.percent.into());
            v
        }).collect()).to_string(false)
    }

    fn zones() -> (String, Vec<String>, Vec<String>, Group) {
        let mut g = Group::new("temperature", false);
        let (t, types, paths) = thermal_zones(&mut g);
        (Value::Obj(t).to_string(false), types, paths, g)
    }

    fn status(g: &Group) -> String {
        g.status().to_string(false)
    }

    const OK: &str = r#"{"state":"ok","reason":null,"issues":[],"issues_truncated":0}"#;

    fn issue(state: &str, target: &str, reason: &str) -> String {
        let r = if state == "partial" { r#""some_unreadable""#.to_string() } else { format!("{:?}", reason) };
        format!(r#"{{"state":"{}","reason":{},"issues":[{{"target":"{}","reason":"{}"}}],"issues_truncated":0}}"#, state, r, target, reason)
    }

    #[test]
    fn cpu_times_rejects_broken_readings() {
        for text in ["cpu  1 2 3 4 5 6 7 8\nintr 0\n", // the total line only: not "0 CPUs"
                     "cpu0 1 2 3 4 5 6 7 8\ncpu2 1 x 3 4 5 6 7 8\n", "cpu0\n", "cpu0 1 0 0 1\n",
                     "cpu0 1 2 3 4 5 6 7 8\ncpu1 1 2 3 4 5 6 7"] { // the last line short, no newline
            let root = Root::new(0);
            root.put("/proc/stat", text);
            assert!(cpu_times().is_err(), "{:?}", text);
        }
    }

    #[test]
    fn cpu_times_reads_eight_or_more_counters() {
        let root = Root::new(0);
        root.put("/proc/stat", "cpu  9 9 9 9 9 9 9 9 9 9\ncpu0 1 2 3 4 5 6 7 8\ncpu1 1 2 3 4 5 6 7 8 9 10\nctxt 5\n");
        let t = cpu_times().unwrap();
        assert_eq!(t.into_iter().collect::<Vec<_>>(), [(0, vec![1, 2, 3, 4, 5, 6, 7, 8]), (1, vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10])]);
    }

    #[test]
    fn a_broken_reading_is_not_a_baseline() {
        for first_ok in [true, false] {
            let root = Root::new(0);
            let mut c = CpuCounters::new(Some(5.0), "interval");
            let mut sample = |line: &str, t: i64| {
                root.put("/proc/stat", format!("cpu  0 0 0 0 0 0 0 0\n{}\n", line));
                crate::metadata::set_clocks(t * 1_000_000_000, 0);
                c.sample(cpu_times).map(|(u, s)| (u, s.to_string(false)))
            };
            if first_ok {
                assert!(sample("cpu0 0 0 0 0 0 0 0 0", 1).unwrap().1.contains("warmup"));
            }
            assert!(sample("cpu0 1 0 0 1", 2).is_err());
            assert_eq!(sample("cpu0 2 0 0 2 0 0 0 0", 3).unwrap(), (vec![], r#"{"mode":"interval","window_ms":null,"unavailable":[{"id":0,"reason":"warmup"}]}"#.into()));
            assert_eq!(sample("cpu0 3 0 0 3 0 0 0 0", 4).unwrap(), (vec![(0, 50.0)], r#"{"mode":"interval","window_ms":1000.0,"unavailable":[]}"#.into()));
        }
    }

    #[test]
    fn detect_the_board() {
        for (compatible, dmi, platform, jetson) in [(Some(&b"nvidia,p3767-0005\0nvidia,tegra234"[..]), false, "Jetson Orin", true),
                                                    (Some(b"raspberrypi,5-model-b\0brcm,bcm2712"), false, "Raspberry Pi", false),
                                                    (None, true, "PC", false), (Some(b"rockchip,rk3588"), false, "Linux", false)] {
            let root = Root::new(0);
            if let Some(c) = compatible {
                root.put("/proc/device-tree/compatible", c);
            }
            if dmi {
                root.mkdir("/sys/class/dmi/id");
            }
            let (p, board) = super::super::detect();
            assert_eq!((p, board == super::super::Board::Jetson), (platform, jetson));
        }
    }

    #[test]
    fn host_paths_from_the_host_root() {
        // the device tree and the host's /etc as the container's bind mounts would show them (here: the tree)
        let root = Root::new(0);
        root.required();
        root.put("/proc/device-tree/compatible", b"nvidia,p3767-0005\0nvidia,tegra234\0");
        root.put("/proc/device-tree/model", b"Test Board\0");
        root.put("/usr/lib/os-release", "NAME=\"Ubuntu\"\nPRETTY_NAME=\"Ubuntu 22.04.5 LTS\"\n");
        root.link("/etc/os-release", "../usr/lib/os-release"); // relative, as on Ubuntu/Debian
        root.put("/etc/nv_tegra_release", "# R36 (release), REVISION: 5.2, GCID: 1, BOARD: generic\n");
        let mut c = super::super::Collector::new(5.0, [false; 3]);
        let stats = c.collect().ok().unwrap().stats;
        assert_eq!((c.platform, stats.get("model").unwrap().to_string(false)), ("Jetson Orin", r#""Test Board""#.into()));
        let sys = stats.get("system").unwrap();
        assert_eq!((sys.get("os").unwrap().to_string(false), sys.get("l4t").unwrap().to_string(false)), (r#""Ubuntu 22.04.5 LTS""#.into(), r#""R36.5.2""#.into()));
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        let p = std::ffi::CString::new(root.dir.to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::statvfs(p.as_ptr(), &mut st) }, 0);
        assert_eq!(stats.get("disk").unwrap().get("total").unwrap().to_string(false), (st.f_blocks as u64 * st.f_frsize as u64).to_string());
    }

    #[test]
    fn os_release_names() {
        assert_eq!(os_release(Some("NAME=\"Ubuntu\"\nVERSION=\"22.04.5 LTS (Jammy Jellyfish)\"\nPRETTY_NAME=\"Ubuntu 22.04.5 LTS\"\n")).as_deref(), Some("Ubuntu 22.04.5 LTS"));
        assert_eq!(os_release(Some("NAME='Raspbian GNU/Linux'\nVERSION='12 (bookworm)'\n")).as_deref(), Some("Raspbian GNU/Linux 12 (bookworm)")); // no PRETTY_NAME
        assert_eq!(os_release(Some("# comment only\n")), None);
        assert_eq!(os_release(None), None); // file missing
    }

    #[test]
    fn hwmon_rails_and_pwm_only_fan() {
        let root = Root::new(0); // ina3221 rails, the unlabeled sum channel ignored, a pwm fan without tach, rpm left to boards
        hwmon(&root, &[&[("name", "pwmfan"), ("pwm1", "76")],
                       &[("name", "ina3221"), ("in1_label", "VDD_IN"), ("in1_input", "5000"), ("curr1_input", "1200"), ("in4_input", "6400"), ("curr4_input", "2416")],
                       &[("name", "pwm_tach"), ("rpm", "1636")]]);
        let (t, p, f, _) = sensors(&[], &[]);
        assert_eq!((t, p, f), ("{}".into(), r#"{"VDD_IN":6.0}"#.into(), r#"[{"name":"pwmfan","rpm":null,"percent":30}]"#.into()));
    }

    #[test]
    fn hwmon_pc() {
        let root = Root::new(0); // acpitz skipped (already a thermal zone), duplicate nvme labels kept apart, a fan with pwm
        hwmon(&root, &[&[("name", "acpitz"), ("temp1_input", "27800")], &[("name", "coretemp"), ("temp1_label", "Package id 0"), ("temp1_input", "45000")],
                       &[("name", "nvme"), ("temp1_label", "Composite"), ("temp1_input", "38850")], &[("name", "nvme"), ("temp1_label", "Composite"), ("temp1_input", "41850")],
                       &[("name", "nct6775"), ("fan2_input", "950"), ("pwm2", "128"), ("power1_input", "12500000")]]);
        let (t, p, f, _) = sensors(&["acpitz"], &[]);
        assert_eq!(t, r#"{"coretemp Package id 0":45.0,"nvme Composite":38.85,"nvme Composite (hwmon3)":41.85}"#);
        assert_eq!((p, f), (r#"{"nct6775 power1":12.5}"#.into(), r#"[{"name":"nct6775 fan2","rpm":950,"percent":50}]"#.into()));
    }

    #[test]
    fn hwmon_twin_of_a_zone_is_skipped_by_its_path() {
        let root = Root::new(0); // the twin is named after the zone (acpitz_0), not its type: only its path can skip it
        for (n, (zt, milli, twin)) in [("acpitz", "54000", "acpitz_0"), ("iwlwifi_1", "41000", "iwlwifi_1_8")].iter().enumerate() {
            let z = format!("/sys/devices/virtual/thermal/thermal_zone{}", n);
            root.put(&format!("{}/type", z), zt);
            root.put(&format!("{}/temp", z), milli);
            root.link(&format!("/sys/class/thermal/thermal_zone{}", n), &format!("../../devices/virtual/thermal/thermal_zone{}", n));
            root.put(&format!("{}/hwmon{}/name", z, n), twin); // the twin sits inside the zone's own directory
            root.put(&format!("{}/hwmon{}/temp1_input", z, n), milli);
            root.link(&format!("/sys/class/hwmon/hwmon{}", n), &format!("../../devices/virtual/thermal/thermal_zone{}/hwmon{}", n, n));
        }
        root.put("/sys/devices/platform/coretemp.0/hwmon/hwmon9/name", "coretemp"); // real hardware, not a twin
        root.put("/sys/devices/platform/coretemp.0/hwmon/hwmon9/temp1_input", "58000");
        root.link("/sys/class/hwmon/hwmon9", "../../devices/platform/coretemp.0/hwmon/hwmon9");
        let (t, types, paths, _) = zones();
        assert_eq!(t, r#"{"acpitz":54.0,"iwlwifi_1":41.0}"#);
        assert_eq!(types, ["acpitz", "iwlwifi_1"]); // neither names the twin
        assert_eq!(paths, ["devices/virtual/thermal/thermal_zone0", "devices/virtual/thermal/thermal_zone1"]);
        let (hw, _, _, _) = sensors(&types.iter().map(String::as_str).collect::<Vec<_>>(), &paths.iter().map(String::as_str).collect::<Vec<_>>());
        assert_eq!(hw, r#"{"coretemp temp1":58.0}"#);
    }

    fn zone(root: &Root, n: usize, zt: &[u8], temp: &str) {
        root.put(&format!("/sys/class/thermal/thermal_zone{}/type", n), zt);
        root.put(&format!("/sys/class/thermal/thermal_zone{}/temp", n), temp);
    }

    #[test]
    fn thermal_zone_types_and_paths() {
        let root = Root::new(0);
        for (i, (zt, t)) in [("cpu-thermal", "51200"), ("acpitz", "27800"), ("acpitz", "30000")].iter().enumerate() {
            zone(&root, i, zt.as_bytes(), t);
        }
        let (t, types, paths, _) = zones();
        assert_eq!(t, r#"{"cpu":51.2,"acpitz":27.8,"acpitz (thermal_zone2)":30.0}"#);
        assert_eq!(types, ["cpu_thermal", "acpitz"]); // as hwmon names ("-" -> "_")
        assert_eq!(paths.iter().map(|p| p.rsplit('/').next().unwrap()).collect::<Vec<_>>(), ["thermal_zone0", "thermal_zone1", "thermal_zone2"]);
    }

    #[test]
    fn hwmon_same_names_kept_apart() {
        let root = Root::new(0); // the first keeps the plain name
        hwmon(&root, &[&[("name", "ina219"), ("power1_input", "1000000")], &[("name", "ina219"), ("power1_input", "2000000")],
                       &[("name", "ina3221"), ("in1_label", "VDD_IN"), ("in1_input", "5000"), ("curr1_input", "1000")],
                       &[("name", "ina3221"), ("in1_label", "VDD_IN"), ("in1_input", "5000"), ("curr1_input", "2000")]]);
        assert_eq!(sensors(&[], &[]).1, r#"{"ina219 power1":1.0,"ina219 power1 (hwmon1)":2.0,"VDD_IN":5.0,"VDD_IN (hwmon3)":10.0}"#);
    }

    #[test]
    fn hwmon_unreadable_fan_is_none_not_zero() {
        let root = Root::new(0);
        hwmon(&root, &[&[("name", "nct6775"), ("fan1_label", "CPU fan")]]);
        root.mkdir("/sys/class/hwmon/hwmon0/fan1_input"); // exists, but reading it fails
        assert_eq!(sensors(&[], &[]).2, r#"[{"name":"CPU fan","rpm":null,"percent":null}]"#);
    }

    #[test]
    fn group_read_values() {
        for (content, expected, reason) in [("0", Some(0), None), ("42000", Some(42000), None), ("", None, Some("invalid_data")), ("4x", None, Some("invalid_data"))] {
            let root = Root::new(0); // a measured 0 is a value; an empty number is not 0
            root.put("/v", content);
            let mut g = Group::new("t", false);
            assert_eq!(g.read_int("/v", "v", None), expected);
            assert_eq!(g.reasons, reason.into_iter().collect::<Vec<_>>());
        }
    }

    #[test]
    fn group_read_errors() {
        for (code, reason) in [(libc::EACCES, "permission_denied"), (libc::EPERM, "permission_denied"), (libc::EIO, "io_error"), (libc::ENODATA, "no_data")] {
            let root = Root::new(0);
            root.put("/v", "1");
            fake::answers("/v", vec![Err(code)]);
            let mut g = Group::new("t", false);
            assert!(g.read_int("/v", "v", None).is_none() && g.reasons == [reason]);
            assert_eq!(g.issues.len(), if reason == "no_data" { 0 } else { 1 }); // no_data: not a problem
            assert!(!g.status().to_string(false).contains('/')); // paths and error text stay out of the API
        }
    }

    #[test]
    fn group_read_missing_file() {
        let root = Root::new(0);
        root.mkdir("/dev0");
        let mut g = Group::new("t", false);
        assert!(g.read_int("/dev0/pwm1", "pwm1", Some("/dev0")).is_none());
        assert!(g.reasons == ["not_exposed"] && g.issues.is_empty()); // an attribute this device does not have
        assert!(g.read_int("/gone/temp1_input", "temp1", Some("/gone")).is_none());
        assert_eq!(g.issues, [("temp1".to_string(), "disappeared")]); // listed, then gone before the read
    }

    #[test]
    fn group_status() {
        for (values, reasons, state, reason) in [(2, vec![], "ok", "null"), (2, vec!["not_exposed"], "ok", "null"),
            (1, vec!["invalid_data"], "partial", r#""some_unreadable""#), (0, vec!["invalid_data", "permission_denied", "io_error"], "error", r#""permission_denied""#),
            (0, vec!["disappeared", "invalid_data"], "error", r#""disappeared""#), (0, vec!["internal_error", "permission_denied"], "error", r#""internal_error""#),
            (0, vec!["not_exposed", "no_data"], "unavailable", r#""no_data""#), (0, vec!["not_detected", "not_exposed"], "unavailable", r#""not_exposed""#),
            (0, vec!["unsupported_platform"], "unavailable", r#""unsupported_platform""#), (0, vec![], "unavailable", r#""not_detected""#)] {
            let mut g = Group::new("t", false);
            g.values = values;
            for (i, r) in reasons.iter().enumerate() {
                g.note(&format!("x{}", i), r, None);
            }
            let st = g.status();
            assert_eq!((st.get("state").unwrap().to_string(false), st.get("reason").unwrap().to_string(false)), (format!("{:?}", state), reason.to_string()));
        }
    }

    #[test]
    fn group_issues_are_capped() {
        let mut g = Group::new("t", false);
        for i in 0..40 {
            g.note(&format!("hwmon0.temp{}{}", i, "x".repeat(100)), "io_error", Some("detail".into()));
        }
        let st = g.status();
        let Some(Value::Arr(issues)) = st.get("issues") else { panic!() };
        assert_eq!((issues.len(), st.get("issues_truncated").unwrap().to_string(false), g.details.len()), (32, "8".into(), 32));
        assert!(issues.iter().all(|i| matches!(i.get("target"), Some(Value::Str(t)) if t.chars().count() <= 64)));
    }

    #[test]
    fn one_bad_temperature_keeps_the_rest() {
        let root = Root::new(0);
        hwmon(&root, &[&[("name", "coretemp"), ("temp1_input", "45000"), ("temp2_input", ""), ("temp3_input", "garbage"), ("temp4_input", "0")],
                       &[("name", "ina219"), ("power1_input", "1000000")], &[("name", "nct6775"), ("fan1_input", "900"), ("pwm1", "255")]]);
        let (t, p, f, g) = sensors(&[], &[]);
        assert_eq!((t, p, f), (r#"{"coretemp temp1":45.0,"coretemp temp4":0.0}"#.into(), r#"{"ina219 power1":1.0}"#.into(), r#"[{"name":"nct6775 fan1","rpm":900,"percent":100}]"#.into()));
        assert_eq!(status(&g.temperature), r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"hwmon0.temp2","reason":"invalid_data"},{"target":"hwmon0.temp3","reason":"invalid_data"}],"issues_truncated":0}"#);
        assert!(status(&g.power) == OK && status(&g.fans) == OK);
    }

    #[test]
    fn partial_power_and_fan_channels() {
        let root = Root::new(0); // a rail without its voltage is left out; a fan without its speed keeps its pwm; 0 rpm stays 0
        hwmon(&root, &[&[("name", "ina3221"), ("in1_label", "VDD_IN"), ("in1_input", "5000"), ("curr1_input", "1200"), ("in2_label", "VDD_SOC"), ("in2_input", "5000"), ("curr2_input", "400")],
                       &[("name", "nct6775"), ("fan1_input", "950"), ("pwm1", "128"), ("fan2_input", "0"), ("pwm2", "0")]]);
        fake::answers("/sys/class/hwmon/hwmon0/in2_input", vec![Err(libc::EIO)]);
        fake::answers("/sys/class/hwmon/hwmon1/fan1_input", vec![Err(libc::EACCES)]);
        let (_, p, f, g) = sensors(&[], &[]);
        assert_eq!((p, f), (r#"{"VDD_IN":6.0}"#.into(), r#"[{"name":"nct6775 fan1","rpm":null,"percent":50},{"name":"nct6775 fan2","rpm":0,"percent":0}]"#.into()));
        assert_eq!(g.power.status().get("issues").unwrap().to_string(false), r#"[{"target":"hwmon0.in2","reason":"io_error"}]"#);
        assert_eq!(g.fans.status().get("issues").unwrap().to_string(false), r#"[{"target":"hwmon1.fan1","reason":"permission_denied"}]"#);
        assert_eq!(g.temperature.status().get("state").unwrap().to_string(false), r#""unavailable""#); // no temperature channel: absent
    }

    #[test]
    fn channel_that_disappears_then_recovers() {
        let root = Root::new(0);
        hwmon(&root, &[&[("name", "coretemp"), ("temp1_input", "45000")]]);
        root.link("/sys/class/hwmon/hwmon0/temp2_input", "gone"); // listed, gone when read
        let (t, _, _, g) = sensors(&[], &[]);
        assert_eq!((t, g.temperature.status().get("issues").unwrap().to_string(false)),
                   (r#"{"coretemp temp1":45.0}"#.into(), r#"[{"target":"hwmon0.temp2","reason":"disappeared"}]"#.into()));
        root.remove("/sys/class/hwmon/hwmon0/temp2_input");
        assert_eq!(status(&sensors(&[], &[]).3.temperature), OK);
    }

    #[test]
    fn unlistable_hwmon_class() {
        let root = Root::new(0);
        root.mkdir("/sys/class/hwmon");
        fake::unlistable("/sys/class/hwmon", libc::EACCES);
        let (t, p, f, g) = sensors(&[], &[]);
        assert_eq!((t, p, f), ("{}".into(), "{}".into(), "[]".into()));
        assert_eq!(g.temperature.status().get("reason").unwrap().to_string(false), r#""permission_denied""#); // could not look
    }

    #[test]
    fn inactive_thermal_zone_is_no_data() {
        let root = Root::new(0);
        zone(&root, 0, b"cpu-thermal", "51200");
        zone(&root, 1, b"cv0-thermal", "0");
        fake::answers("/sys/class/thermal/thermal_zone1/temp", vec![Err(libc::ENODATA)]);
        let (t, _, _, g) = zones();
        assert_eq!((t, status(&g)), (r#"{"cpu":51.2}"#.into(), OK.into()));
    }

    #[test]
    fn fan_speed_the_firmware_does_not_report_is_no_data() {
        for (code, state) in [(libc::EIO, "unavailable"), (libc::EACCES, "error")] {
            let root = Root::new(0); // acpi_fan: EIO on fan1_input is no tachometer, not a failure; EACCES stays one
            hwmon(&root, &[&[("name", "acpi_fan"), ("fan1_input", "0")]]);
            fake::answers("/sys/class/hwmon/hwmon0/fan1_input", vec![Err(code)]);
            let (_, _, f, g) = sensors(&[], &[]);
            assert_eq!(f, r#"[{"name":"acpi_fan fan1","rpm":null,"percent":null}]"#);
            if state == "unavailable" {
                assert_eq!(status(&g.fans), r#"{"state":"unavailable","reason":"no_data","issues":[],"issues_truncated":0}"#);
                assert!(g.fans.details.is_empty()); // nothing for the service log either
            } else {
                assert_eq!(status(&g.fans), issue("error", "hwmon0.fan1", "permission_denied"));
            }
        }
    }

    #[test]
    fn cpu_frequency_failure_keeps_usage() {
        let root = Root::new(0);
        let freq = |n: i64| format!("/sys/devices/system/cpu/cpu{}/cpufreq/scaling_cur_freq", n);
        root.put(&freq(1), "1200000\n");
        let mut c = CpuCounters::new(Some(5.0), "interval");
        let mut run = |stat: &str, t: i64| {
            root.required();
            root.put("/proc/stat", stat);
            fake::answers(&freq(0), vec![Err(libc::EIO)]);
            crate::metadata::set_clocks(t, 1_700_000_000_000_000);
            let mut g = Groups::new(false);
            let (s, _) = collect(&mut c, &mut g, &mut Vec::new()).unwrap();
            (s, g.cpu_frequency.status())
        };
        let (first, st) = run("cpu0 0 0 0 0 0 0 0 0\ncpu1 0 0 0 0 0 0 0 0\n", 1_000_000_000_000);
        assert_eq!((first.get("cpu").unwrap().to_string(false), st.get("reason").unwrap().to_string(false)), ("[]".into(), r#""no_data""#.into())); // warmup: no rows
        let (s, st) = run("cpu0 10 0 0 10 0 0 0 0\ncpu1 0 0 0 20 0 0 0 0\n", 1_001_000_000_000);
        assert_eq!(s.get("cpu").unwrap().to_string(false), r#"[{"id":0,"usage":50.0,"freq":null},{"id":1,"usage":0.0,"freq":1200000000}]"#);
        assert_eq!(st.to_string(false), issue("partial", "cpu0", "io_error"));
        assert!(!s.get("cpu_sampling").unwrap().get("window_ms").unwrap().is_null()); // the CPU baseline went on as before
    }

    #[test]
    fn no_optional_sensors_is_not_an_error() {
        let root = Root::new(0); // a plain Linux host without cpufreq, thermal zones, hwmon chips or a DRM card
        root.required();
        let mut c = super::super::Collector::new(5.0, [false; 3]);
        c.collect().ok().unwrap();
        root.put("/proc/stat", "cpu  1 0 1 900 10 0 5 0 0 0\ncpu0 101 0 51 900 10 0 5 0 0 0\n");
        root.at(2_000_000_000);
        let s = c.collect().ok().unwrap().stats;
        let Some(Value::Obj(groups)) = s.get("collectors") else { panic!() };
        assert!(groups.iter().all(|(_, v)| v.get("state").unwrap().to_string(false) == r#""unavailable""#), "{}", s.get("collectors").unwrap().to_string(false));
        assert!(s.get("cpu").unwrap().get_index(0).is_some());
    }

    #[test]
    fn undecodable_temperature_keeps_the_rest() {
        let root = Root::new(0);
        hwmon(&root, &[&[("name", "chip"), ("temp1_input", "45000"), ("temp3_input", "42000"), ("power1_input", "1000000"), ("fan1_input", "900"), ("pwm1", "128")]]);
        root.put("/sys/class/hwmon/hwmon0/temp2_input", b"\xff\n");
        let (t, p, f, g) = sensors(&[], &[]);
        assert_eq!((t, p, f), (r#"{"chip temp1":45.0,"chip temp3":42.0}"#.into(), r#"{"chip power1":1.0}"#.into(), r#"[{"name":"chip fan1","rpm":900,"percent":50}]"#.into()));
        assert_eq!(g.temperature.status().get("issues").unwrap().to_string(false), r#"[{"target":"hwmon0.temp2","reason":"invalid_data"}]"#);
        assert!(status(&g.power) == OK && status(&g.fans) == OK);
    }

    #[test]
    fn unlistable_chip_is_reported_and_the_others_kept() {
        for (code, reason) in [(libc::EACCES, "permission_denied"), (libc::EIO, "io_error")] {
            let root = Root::new(0);
            hwmon(&root, &[&[("name", "a"), ("temp1_input", "40000")], &[("name", "b"), ("temp1_input", "50000")]]);
            fake::unlistable("/sys/class/hwmon/hwmon0", code);
            let (t, _, _, g) = sensors(&[], &[]);
            assert_eq!((t, status(&g.temperature)), (r#"{"b temp1":50.0}"#.into(), issue("partial", "hwmon0", reason)));
        }
    }

    #[test]
    fn unlistable_thermal_class() {
        let root = Root::new(0);
        root.mkdir("/sys/class/thermal/thermal_zone0");
        fake::unlistable("/sys/class/thermal", libc::EIO);
        let (t, _, _, g) = zones();
        assert_eq!((t, g.status().get("reason").unwrap().to_string(false)), ("{}".into(), r#""io_error""#.into()));
    }

    #[test]
    fn empty_or_missing_classes_are_not_errors() {
        for empty in [true, false] {
            let root = Root::new(0);
            if empty {
                root.mkdir("/sys/class/hwmon");
            }
            let (t, p, f, g) = sensors(&[], &[]);
            assert_eq!((t, p, f), ("{}".into(), "{}".into(), "[]".into()));
            let (zt, _, _, zg) = zones();
            assert_eq!(zt, "{}");
            for s in [&g.temperature, &g.power, &g.fans, &zg] {
                assert_eq!(s.status().get("state").unwrap().to_string(false), r#""unavailable""#);
            }
        }
    }

    #[test]
    fn unreadable_rail_label_is_reported() {
        let root = Root::new(0); // channel 3 has no label: a sum channel, skipped
        hwmon(&root, &[&[("name", "ina3221"), ("in1_label", "VDD_IN"), ("in1_input", "5000"), ("curr1_input", "1000"), ("in2_label", "VDD_SOC"),
                         ("in2_input", "5000"), ("curr2_input", "400"), ("in3_input", "5000"), ("curr3_input", "1400")]]);
        fake::answers("/sys/class/hwmon/hwmon0/in1_label", vec![Err(libc::EIO)]);
        let (_, p, _, g) = sensors(&[], &[]);
        assert_eq!((p, status(&g.power)), (r#"{"VDD_SOC":2.0}"#.into(), issue("partial", "hwmon0.in1_label", "io_error")));
        let (_, p, _, g) = sensors(&[], &[]); // next collection: readable again
        assert_eq!((p, status(&g.power)), (r#"{"VDD_IN":5.0,"VDD_SOC":2.0}"#.into(), OK.into()));
    }

    #[test]
    fn undecodable_name_or_label_keeps_the_values() {
        let chip_b = (r#""b temp1":50.0"#, r#""b power1":2.0"#, r#"{"name":"b fan1","rpm":800,"percent":0}"#);
        for (file, temps, power, fan, with_issue) in [
            // the chip name is in every key: hwmon0's values are kept under its directory name
            ("name", r#""hwmon0 Package":40.0"#, r#""VIN":1.0"#, r#"{"name":"CPU fan","rpm":900,"percent":100}"#, vec!["temperature", "power", "fans"]),
            ("temp1_label", r#""a temp1":40.0"#, r#""VIN":1.0"#, r#"{"name":"CPU fan","rpm":900,"percent":100}"#, vec!["temperature"]),
            ("power1_label", r#""a Package":40.0"#, r#""a power1":1.0"#, r#"{"name":"CPU fan","rpm":900,"percent":100}"#, vec!["power"]),
            ("fan1_label", r#""a Package":40.0"#, r#""VIN":1.0"#, r#"{"name":"a fan1","rpm":900,"percent":100}"#, vec!["fans"])] {
            let root = Root::new(0);
            hwmon(&root, &[&[("name", "a"), ("temp1_input", "40000"), ("temp1_label", "Package"), ("power1_input", "1000000"), ("power1_label", "VIN"),
                             ("fan1_input", "900"), ("fan1_label", "CPU fan"), ("pwm1", "255")],
                           &[("name", "b"), ("temp1_input", "50000"), ("power1_input", "2000000"), ("fan1_input", "800"), ("pwm1", "0")]]);
            let good = std::fs::read(root.path(&format!("/sys/class/hwmon/hwmon0/{}", file))).unwrap();
            root.put(&format!("/sys/class/hwmon/hwmon0/{}", file), b"\xff\n");
            let (t, p, f, g) = sensors(&[], &[]);
            assert_eq!((t, p, f), (format!("{{{},{}}}", temps, chip_b.0), format!("{{{},{}}}", power, chip_b.1), format!("[{},{}]", fan, chip_b.2)), "{}", file);
            for (name, grp) in [("temperature", &g.temperature), ("power", &g.power), ("fans", &g.fans)] {
                let want = if with_issue.contains(&name) { issue("partial", &format!("hwmon0.{}", file), "invalid_data") } else { OK.into() };
                assert_eq!(status(grp), want, "{} {}", file, name);
            }
            root.put(&format!("/sys/class/hwmon/hwmon0/{}", file), &good); // readable again in the next collection
            let g = sensors(&[], &[]).3;
            assert!([&g.temperature, &g.power, &g.fans].iter().all(|x| status(x) == OK));
        }
    }

    #[test]
    fn undecodable_zone_type_keeps_the_zones() {
        let root = Root::new(0);
        zone(&root, 0, b"\xff\n", "51200");
        zone(&root, 1, b"gpu-thermal", "52000");
        let (t, _, _, g) = zones();
        assert_eq!((t, g.status().get("issues").unwrap().to_string(false)),
                   (r#"{"thermal_zone0":51.2,"gpu":52.0}"#.into(), r#"[{"target":"thermal_zone0.type","reason":"invalid_data"}]"#.into()));
    }

    #[test]
    fn problems_are_logged_when_they_change() {
        let mut logged = Vec::new();
        let run = |logged: &mut Vec<(String, Vec<String>)>, reason: Option<&'static str>| {
            let mut g = Group::new("temperature", false);
            if let Some(r) = reason {
                g.note("hwmon0.temp1", r, Some("/sys/class/hwmon/hwmon0/temp1_input: Input/output error".into()));
            }
            metadata::summarize(&[g], logged);
            logged.clone()
        };
        let first = run(&mut logged, Some("io_error"));
        assert!(first[0].1[0].starts_with("hwmon0.temp1: io_error (/sys/class/hwmon/hwmon0/temp1_input"));
        assert_eq!(run(&mut logged, Some("io_error")), first); // the same problem again: not logged every second
        assert_eq!(run(&mut logged, None), [("temperature".to_string(), vec![])]); // logged once as readable again
    }

    // tests/collector/test_provenance.py's source identity and read span tests, ported. The clock moves one
    // FAKE_TICK per call, as Python's Tick moves 1 µs.
    const CHIP: &[(&str, &str)] = &[("name", "ina"), ("temp1_input", "40000"), ("temp1_label", "Board"), ("power1_input", "2000000"),
        ("power1_label", "VIN"), ("in2_label", "VDD"), ("in2_input", "5000"), ("curr2_input", "1000"), ("fan1_input", "900"), ("pwm1", "128")];

    /// A hwmon chip at /sys/<device>/hwmon/hwmon<n>, linked from class/hwmon as the kernel does.
    fn chip(root: &Root, device: &str, n: usize, files: &[(&str, &str)]) -> String {
        let d = format!("/sys/{}/hwmon/hwmon{}", device, n);
        root.mkdir(&d);
        for (name, value) in files {
            root.put(&format!("{}/{}", d, name), value);
        }
        root.link(&format!("/sys/class/hwmon/hwmon{}", n), &format!("../../{}/hwmon/hwmon{}", device, n));
        d
    }

    fn traced() -> Groups {
        crate::metadata::set_clocks(1_000_000_000_000, 0);
        Groups::new(true)
    }

    fn traced_sensors() -> (Vec<(String, Value)>, Vec<(String, Value)>, Vec<Fan>, Groups) {
        let mut g = traced();
        let (t, p, f) = hwmon_sensors(&[], &[], &mut g);
        (t, p, f, g)
    }

    fn record<'a>(g: &'a Group, key: &str) -> &'a Value {
        &g.sensors.iter().find(|(s, _)| *s == Slot::Key(key.into())).unwrap().1
    }

    fn field(v: &Value, k: &str) -> String {
        v.get(k).unwrap().to_string(false)
    }

    fn span(v: &Value) -> (i64, i64) {
        match v.get("span") { Some(Value::Arr(a)) => match (&a[0], &a[1]) { (Value::Int(x), Value::Int(y)) => (*x, *y), _ => panic!() }, _ => panic!("no span") }
    }

    #[test]
    fn descriptor_id_is_fixed() {
        // sha256 of a sorted, compact JSON descriptor: the same in any process (Python's hash seed)
        let src = metadata::source("hwmon", "celsius", "device_channel", vec![("device", "devices/platform/i2c/1-0040".into()), ("attr", "temp1_input".into())]);
        assert_eq!(src.to_string(false), r#"{"id":"src1:dc1e592a9185fe57929e05c59a5759c4bb2cdd10e04a85525241df68d9a80bd0","provider":"hwmon","identity_basis":"device_channel","unit":"celsius"}"#);
        assert_eq!(metadata::source("hwmon", "celsius", "unresolved", vec![]).to_string(false), r#"{"id":null,"provider":"hwmon","identity_basis":"unresolved","unit":"celsius"}"#);
    }

    #[test]
    fn label_change_keeps_id_and_channels_differ() {
        let root = Root::new(0);
        let d = chip(&root, "devices/platform/i2c/1-0040", 3, CHIP);
        let (_, _, _, g) = traced_sensors();
        let first = field(record(&g.temperature, "ina Board"), "id");
        root.put(&format!("{}/temp1_label", d), "Renamed");
        let (_, _, _, g2) = traced_sensors();
        assert_eq!(field(record(&g2.temperature, "ina Renamed"), "id"), first);
        let mut ids = vec![field(record(&g.power, "VIN"), "id"), field(record(&g.power, "VDD"), "id"), first];
        ids.extend(g.fans.sensors.iter().map(|(_, v)| field(v, "id")));
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), 5); // temp, direct power, computed power, rpm, pwm: all different
        assert_eq!(field(record(&g.power, "VIN"), "identity_basis"), r#""device_channel""#);
    }

    #[test]
    fn same_name_on_two_devices_differs() {
        let root = Root::new(0);
        chip(&root, "devices/platform/i2c/1-0040", 1, &[("name", "nvme"), ("temp1_input", "40000"), ("temp1_label", "Composite")]);
        chip(&root, "devices/platform/i2c/1-0041", 2, &[("name", "nvme"), ("temp1_input", "41000"), ("temp1_label", "Composite")]);
        let (t, _, _, g) = traced_sensors();
        assert_eq!(t.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>(), ["nvme Composite", "nvme Composite (hwmon2)"]);
        assert_ne!(field(record(&g.temperature, "nvme Composite"), "id"), field(record(&g.temperature, "nvme Composite (hwmon2)"), "id"));
    }

    #[test]
    fn renumbered_hwmon_keeps_device_id() {
        let ids = |n: usize| { // the same device, enumerated as another hwmon node, under another temporary root
            let root = Root::new(0);
            chip(&root, "devices/platform/i2c/1-0040", n, CHIP);
            let (_, _, _, g) = traced_sensors();
            [&g.temperature, &g.power].map(|x| x.sensors.iter().map(|(_, v)| field(v, "id")).collect::<Vec<_>>())
        };
        assert_eq!(ids(3), ids(7));
    }

    #[test]
    fn two_hwmon_nodes_on_one_device_are_not_merged() {
        let root = Root::new(0);
        chip(&root, "devices/platform/i2c/1-0040", 1, &[("name", "x"), ("temp1_input", "40000")]);
        chip(&root, "devices/platform/i2c/1-0040", 2, &[("name", "y"), ("temp1_input", "41000")]);
        let (_, _, _, g) = traced_sensors();
        let (a, b) = (record(&g.temperature, "x temp1"), record(&g.temperature, "y temp1"));
        assert_eq!([field(a, "identity_basis"), field(b, "identity_basis")], [r#""resolved_path""#; 2]); // the device alone is ambiguous
        assert_ne!(field(a, "id"), field(b, "id"));
    }

    #[test]
    fn virtual_hwmon_and_thermal_zone_are_path_based() {
        let root = Root::new(0);
        chip(&root, "devices/virtual", 0, &[("name", "acpitz"), ("temp1_input", "30000")]);
        root.put("/sys/devices/virtual/thermal/thermal_zone0/type", "cpu-thermal");
        root.put("/sys/devices/virtual/thermal/thermal_zone0/temp", "51000");
        root.link("/sys/class/thermal/thermal_zone0", "../../devices/virtual/thermal/thermal_zone0");
        let (_, _, _, g) = traced_sensors();
        assert_eq!(field(record(&g.temperature, "acpitz temp1"), "identity_basis"), r#""resolved_path""#); // not "devices/virtual"
        let mut tg = Group::new("temperature", true);
        let (t, _, _) = thermal_zones(&mut tg);
        assert_eq!(Value::Obj(t).to_string(false), r#"{"cpu":51.0}"#);
        assert_eq!((field(record(&tg, "cpu"), "provider"), field(record(&tg, "cpu"), "identity_basis")), (r#""thermal""#.into(), r#""resolved_path""#.into()));
    }

    #[test]
    fn class_alias_and_device_path_resolve_alike() {
        let root = Root::new(0);
        let d = chip(&root, "devices/platform/i2c/1-0040", 3, CHIP);
        let via_class = sysfs::canonical("/sys/class/hwmon/hwmon3/temp1_input", "/sys");
        assert_eq!(via_class, sysfs::canonical(&format!("{}/temp1_input", d), "/sys"));
        assert_eq!(via_class.as_deref(), Some("devices/platform/i2c/1-0040/hwmon/hwmon3/temp1_input"));
        assert_eq!(sysfs::canonical("/sys/missing", "/sys"), None);
        root.put("/etc/hostname", "x");
        assert_eq!(sysfs::canonical("/etc/hostname", "/sys"), None); // outside the sysfs root
    }

    #[test]
    fn canonical_agrees_with_the_portable_way() {
        // aliases, a symlinked root, links inside the path, outside the root, missing, dangling, a loop, a file
        // in place of a directory; the portable way is also canonical's fallback without /proc/self/fd
        let root = Root::new(0);
        let d = chip(&root, "devices/platform/i2c/1-0040", 3, CHIP);
        root.link("/sys/devices/virtual/alias", &root.path("/sys/devices/platform").to_string_lossy());
        root.link("/sys/class/dangling", &root.path("/sys/nowhere").to_string_lossy());
        root.link("/sys/class/loop", &root.path("/sys/class/loop").to_string_lossy());
        root.put("/sys/file", "x");
        root.mkdir("/outside");
        root.link("/sys/class/out", &root.path("/outside").to_string_lossy());
        root.link("/sys-link", "sys");
        let paths = ["/sys/class/hwmon/hwmon3".to_string(), "/sys/class/hwmon/hwmon3/temp1_input".into(), d,
                     "/sys/devices/virtual/alias/i2c/1-0040/hwmon/hwmon3/name".into(), "/sys".into(), "/sys/.".into(),
                     "/sys/class/../class/hwmon/hwmon3".into(), "/sys/missing".into(), "/sys/class/dangling".into(), "/sys/class/loop".into(),
                     "/sys/file/below".into(), "/sys/class/out".into(), "/etc/hostname".into()];
        for base in ["/sys", "/sys-link"] {
            for p in &paths {
                assert_eq!(sysfs::canonical(p, base), sysfs::canonical_portable(p, base), "{} {}", p, base);
            }
        }
        assert_eq!(sysfs::canonical(&paths[3], "/sys-link").as_deref(), Some("devices/platform/i2c/1-0040/hwmon/hwmon3/name"));
    }

    fn identity(chips: &[(&str, &str)]) -> Vec<(String, &'static str, String)> {
        let chips: Vec<(String, String)> = chips.iter().map(|(d, n)| (d.to_string(), n.to_string())).collect();
        sysfs::chip_identities(&chips, "/sys").into_iter().map(|(d, b, w)| (d, b, Value::Obj(w.into_iter().map(|(k, v)| (k.to_string(), v)).collect()).to_string(false))).collect()
    }

    #[test]
    fn a_retargeted_link_resolves_to_its_new_device() {
        let root = Root::new(0); // same name, new target: the next resolution follows it; nothing from before is reused
        chip(&root, "devices/platform/i2c/1-0040", 3, CHIP);
        let link = "/sys/class/hwmon/hwmon3";
        assert_eq!(identity(&[(link, "ina")]), [(link.to_string(), "device_channel", r#"{"device":"devices/platform/i2c/1-0040"}"#.to_string())]);
        root.mkdir("/sys/devices/platform/i2c/1-0041/hwmon/hwmon3");
        root.remove(link);
        root.link(link, "../../devices/platform/i2c/1-0041/hwmon/hwmon3");
        assert_eq!(identity(&[(link, "ina")]), [(link.to_string(), "device_channel", r#"{"device":"devices/platform/i2c/1-0041"}"#.to_string())]);
    }

    #[test]
    fn an_unresolved_path_recovers() {
        let root = Root::new(0);
        let link = "/sys/class/hwmon/hwmon3";
        root.link(link, "../../devices/platform/i2c/1-0040/hwmon/hwmon3"); // dangling for now
        assert_eq!(identity(&[(link, "ina")])[0].1, "unresolved");
        root.mkdir("/sys/devices/platform/i2c/1-0040/hwmon/hwmon3");
        assert_eq!(identity(&[(link, "ina")])[0].1, "device_channel");
    }

    #[test]
    fn a_second_node_on_the_device_is_seen_when_it_comes_and_goes() {
        let root = Root::new(0); // the shared-device decision is made from the current listing every time
        chip(&root, "devices/platform/i2c/1-0040", 3, CHIP);
        assert_eq!(identity(&[("/sys/class/hwmon/hwmon3", "ina")])[0].1, "device_channel");
        chip(&root, "devices/platform/i2c/1-0040", 4, CHIP);
        let both = identity(&[("/sys/class/hwmon/hwmon3", "ina"), ("/sys/class/hwmon/hwmon4", "ina")]);
        assert!(both.iter().all(|(_, b, _)| *b == "resolved_path"));
        root.remove("/sys/devices/platform/i2c/1-0040/hwmon/hwmon4");
        root.remove("/sys/class/hwmon/hwmon4");
        assert_eq!(identity(&[("/sys/class/hwmon/hwmon3", "ina")])[0].1, "device_channel");
    }

    #[test]
    fn unresolved_source_keeps_the_value() {
        let root = Root::new(0); // a chip outside the sysfs root: no identity, the values kept
        for (name, value) in CHIP {
            root.put(&format!("/elsewhere/hwmon3/{}", name), value);
        }
        root.link("/sys/class/hwmon/hwmon3", &root.path("/elsewhere/hwmon3").to_string_lossy());
        let (t, p, f, g) = traced_sensors();
        assert_eq!((Value::Obj(t).to_string(false), Value::Obj(p).to_string(false), f[0].rpm), (r#"{"ina Board":40.0}"#.into(), r#"{"VIN":2.0,"VDD":5.0}"#.into(), Some(900)));
        assert!(g.temperature.sensors.iter().all(|(_, v)| field(v, "identity_basis") == r#""unresolved""#));
        let r = record(&g.temperature, "ina Board");
        assert!(r.get("id").unwrap().is_null() && !r.get("span").unwrap().is_null());
        assert_eq!(g.temperature.status().get("state").unwrap().to_string(false), r#""ok""#); // no id is not a read error
    }

    #[test]
    fn gpu_sources_follow_the_file_used() {
        let ids = |files: &[(&str, &str)]| {
            let root = Root::new(0);
            for (p, v) in files {
                root.put(&format!("/sys/{}", p), v);
            }
            crate::metadata::set_clocks(1_000_000_000_000, 0);
            let mut g = Group::new("gpu", true);
            super::super::jetson::gpu(&mut g);
            (field(record(&g, "usage"), "id"), g.sensors.iter().find(|(s, _)| *s == Slot::Key("freq".into())).map(|(_, v)| field(v, "id")))
        };
        let a = ids(&[("class/devfreq/17000000.gpu/device/load", "500"), ("class/devfreq/17000000.gpu/cur_freq", "1")]);
        let b = ids(&[("devices/platform/gpu.0/load", "500")]); // another BSP: another source
        assert_ne!(a.0, b.0);
        assert_ne!(Some(a.0.clone()), a.1);
        let root = Root::new(0); // gpu.0 linked to the devfreq device: the same file, so the same source
        root.put("/sys/class/devfreq/17000000.gpu/device/load", "500");
        root.link("/sys/devices/platform/gpu.0", &root.path("/sys/class/devfreq/17000000.gpu/device").to_string_lossy());
        assert_eq!(sysfs::canonical("/sys/devices/platform/gpu.0/load", "/sys"), sysfs::canonical("/sys/class/devfreq/17000000.gpu/device/load", "/sys"));
    }

    #[test]
    fn label_read_later_does_not_refresh_the_number() {
        let root = Root::new(0);
        chip(&root, "devices/platform/i2c/1-0040", 3, &[("name", "c"), ("temp1_input", "0"), ("temp1_label", "Zero")]);
        let (t, _, _, g) = traced_sensors();
        assert_eq!(Value::Obj(t).to_string(false), r#"{"c Zero":0.0}"#); // a measured 0 has its read too
        let (_, end) = span(record(&g.temperature, "c Zero"));
        let after_label = crate::metadata::clock();
        assert!(end < after_label - 2 * crate::metadata::FAKE_TICK); // the label was read after it; its read is not this span
    }

    #[test]
    fn computed_and_fan_spans() {
        let root = Root::new(0);
        chip(&root, "devices/platform/i2c/1-0040", 3, CHIP);
        let (_, _, _, g) = traced_sensors();
        let (vdd, vin) = (span(record(&g.power, "VDD")), span(record(&g.power, "VIN")));
        assert!(vdd.1 - vdd.0 > vin.1 - vin.0); // from the current read's start to the voltage read's end
        let fan: Vec<(i64, i64)> = g.fans.sensors.iter().map(|(_, v)| span(v)).collect();
        assert!(fan[0].1 < fan[1].0); // rpm, then pwm: two reads, two spans
    }

    #[test]
    fn gpu_spans_skip_the_failed_candidate() {
        let root = Root::new(0);
        for (p, v) in [("class/devfreq/17000000.gpu/device/load", "bad"), ("devices/platform/gpu.0/load", "500"),
                       ("class/devfreq/17000000.gpu/cur_freq", "1"), ("class/devfreq/17000000.gpu/max_freq", "2")] {
            root.put(&format!("/sys/{}", p), v);
        }
        crate::metadata::set_clocks(1_000_000_000_000, 0);
        let mut g = Group::new("gpu", true);
        super::super::jetson::gpu(&mut g);
        let [usage, freq, max_freq] = ["usage", "freq", "max_freq"].map(|k| span(record(&g, k)));
        assert!(usage.0 > 1_000_000_000_000); // after the failed candidate's read, which started at the first tick
        assert!(usage.1 < freq.0 && freq.0 < freq.1 && freq.1 < max_freq.0);
    }

    #[test]
    fn virtual_devices_are_path_based() {
        for (device, basis) in [("devices/virtual", "resolved_path"), ("devices/virtual/thermal/thermal_zone0", "resolved_path"),
                                ("devices/virtual/platform/fan_controller", "resolved_path"), ("devices/platform/i2c/1-0040", "device_channel"),
                                ("devices/platform/virtual_fan", "device_channel")] { // a real device named "virtual..."
            let root = Root::new(0);
            chip(&root, device, 0, &[("name", "t"), ("temp1_input", "1000")]);
            let (_, _, _, g) = traced_sensors();
            assert_eq!(field(record(&g.temperature, "t temp1"), "identity_basis"), format!("{:?}", basis), "{}", device);
        }
    }
}
