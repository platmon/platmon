//! collector/common.py: fields every Linux host has (CPU, clocks, thermal zones, hwmon, memory, disk, identity).
use crate::json::{round_to, Value};
use crate::metadata::{self, clock, source, Group, Slot, Span};
use super::sysfs::{self, entries, hwmon_chips, numbered};
use super::Groups;
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
    let text = std::fs::read_to_string("/proc/stat").map_err(|e| e.to_string())?;
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

pub struct CpuCounters {
    max_gap: Option<i64>,
    mode: &'static str,
    last: Option<(i64, BTreeMap<i64, Vec<i64>>)>,
    pub span: Span,
}

impl CpuCounters {
    pub fn new(max_gap_s: Option<f64>, mode: &'static str) -> CpuCounters {
        CpuCounters { max_gap: max_gap_s.map(|s| (s * 1e9).round() as i64), mode, last: None, span: None }
    }

    /// ({id: usage}, cpu_sampling); see CpuCounters.sample.
    pub fn sample(&mut self) -> Result<(Vec<(i64, f64)>, Value), String> {
        let now = clock();
        let times = match cpu_times() {
            Ok(t) => t,
            Err(e) => { self.last = None; return Err(e); }
        };
        self.span = Some((now, clock()));
        let mut window = self.last.as_ref().map(|l| now - l.0);
        let (usage, unavailable): (Vec<(i64, f64)>, Vec<(i64, &str)>) = match (&self.last, window) {
            (None, _) => (vec![], times.keys().map(|n| (*n, "warmup")).collect()),
            (Some(_), Some(w)) if w <= 0 => { window = None; (vec![], times.keys().map(|n| (*n, "invalid_interval")).collect()) }
            (Some(_), Some(w)) if self.max_gap.map_or(false, |m| w > m) => (vec![], times.keys().map(|n| (*n, "gap")).collect()),
            (Some((_, before)), _) => {
                let (mut u, mut un) = (vec![], vec![]);
                for (n, after) in &times {
                    let Some(b) = before.get(n) else { un.push((*n, "warmup")); continue };
                    let (a, b) = (&after[..8], &b[..8]);
                    let delta: Vec<i64> = a.iter().zip(b).map(|(x, y)| x - y).collect();
                    let total: i64 = delta.iter().sum();
                    let idle = delta[3] + delta[4];
                    if delta.iter().any(|d| *d < 0) {
                        un.push((*n, "counter_regressed"));
                    } else if total == 0 {
                        un.push((*n, "no_ticks"));
                    } else {
                        u.push((*n, round_to(100.0 * (total - idle) as f64 / total as f64, 1)));
                    }
                }
                (u, un)
            }
        };
        self.last = Some((now, times));
        let mut s = Value::obj();
        s.set("mode", self.mode.into());
        s.set("window_ms", window.map(|w| round_to(w as f64 / 1e6, 3)).into());
        s.set("unavailable", Value::Arr(unavailable.into_iter().map(|(n, r)| {
            let mut e = Value::obj();
            e.set("id", n.into());
            e.set("reason", r.into());
            e
        }).collect()));
        Ok((usage, s))
    }
}

pub fn meminfo() -> Result<Vec<(String, i64)>, String> {
    let text = std::fs::read_to_string("/proc/meminfo").map_err(|e| e.to_string())?;
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
    let c = std::ffi::CString::new(path).map_err(|e| e.to_string())?;
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

/// The required reads' spans, by name (Trace.required).
pub type Required = Vec<(&'static str, Span)>;

fn timed<T>(req: &mut Required, name: &'static str, f: impl FnOnce() -> T) -> T {
    let start = clock();
    let v = f();
    req.push((name, Some((start, clock()))));
    v
}

/// common.collect with the service's CpuCounters and groups; Err fails the collection.
pub fn collect(cpu: &mut CpuCounters, g: &mut Groups, req: &mut Required) -> Result<(Value, Vec<Fan>), String> {
    let (usage, sampling) = cpu.sample()?;
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
