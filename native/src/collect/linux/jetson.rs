//! collector/jetson.py: Tegra GPU load and clock, nvpmodel power mode, pwm_tach fans, L4T release.
use super::common::{next_fan_id, Fan};
use crate::json::Value;
use crate::metadata::{source, Group, Slot};
use super::sysfs::{self, entries, hwmon_chips};
use crate::Groups;

const GPU_DEVFREQ: [&str; 5] = ["*.gpu", "*.ga10b", "*.gv11b", "*.gp10b", "*.gm20b"];
const GPU_LOAD: [&str; 2] = ["devices/platform/gpu.0/load", "devices/gpu.0/load"];

fn suffix_match(name: &str, pattern: &str) -> bool {
    // fnmatchcase with the patterns above: all are "*<suffix>" with no other wildcard
    name.ends_with(&pattern[1..])
}

fn gpu_source(trace: bool, unit: &str, path: &str, root: &str) -> Option<Value> {
    if !trace { return None; }
    let rel = sysfs::canonical(path, root);
    let basis = if rel.is_some() { "resolved_path" } else { "unresolved" };
    Some(source("gpu", unit, basis, vec![("path", rel.into())]))
}

pub fn gpu(g: &mut Group) -> Option<Value> {
    let root = "/sys";
    let mut listing = Group::new("gpu", false);
    let devices = entries(&format!("{}/class/devfreq", root), &mut [&mut listing], false);
    g.merge(&listing);
    let mut tried = Group::new("gpu", g.trace);
    let devfreq = GPU_DEVFREQ.iter().find_map(|p| devices.iter().find(|n| suffix_match(n, p)))
        .map(|n| format!("{}/class/devfreq/{}", root, n));
    let mut loads: Vec<String> = devfreq.iter().map(|d| format!("{}/device/load", d)).collect();
    loads.extend(GPU_LOAD.iter().map(|p| format!("{}/{}", root, p)));
    let mut found = None;
    for path in &loads {
        if let Some(l) = tried.read_int(path, "load", None) {
            found = Some((l, path.clone()));
            break;
        }
    }
    let Some((load, used)) = found else { g.merge(&tried); return None };
    g.sensor(Slot::Key("usage".into()), gpu_source(g.trace, "percent", &used, root), tried.span);
    let usage = g.got(load as f64 / 10.0);
    let clock = |g: &mut Group, name: &str| -> Option<i64> {
        let d = devfreq.as_ref()?;
        let value = g.read_int(&format!("{}/{}", d, name), name, Some(d));
        let span = g.span;
        g.sensor(Slot::Key(name.replace("cur_", "")), gpu_source(g.trace, "hertz", &format!("{}/{}", d, name), root), span);
        value.map(|v| g.got(v))
    };
    let mut v = Value::obj();
    v.set("usage", usage.into());
    v.set("freq", clock(g, "cur_freq").into());
    v.set("max_freq", clock(g, "max_freq").into());
    Some(v)
}

fn pmode(text: &str) -> Option<String> {
    let d = text.strip_prefix("pmode:")?;
    if !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit()) { Some(text.to_string()) } else { None }
}

/// The mode's name from /etc/nvpmodel.conf (POWER_MODEL ID=<id> NAME=<name> >), else its id.
fn power_mode(status: &str, conf: Option<&str>) -> String {
    let id = status.strip_prefix("pmode:").unwrap_or(status).trim_start_matches('0');
    let id = if id.is_empty() { "0" } else { id };
    for line in conf.unwrap_or("").split("POWER_MODEL").skip(1) {
        // re: POWER_MODEL\s+ID={id}\s+NAME=(\S+?)\s*>
        let rest = line.trim_start();
        if rest.len() == line.len() { continue; }
        let Some(r) = rest.strip_prefix(&format!("ID={}", id)) else { continue };
        let r2 = r.trim_start();
        if r2.len() == r.len() { continue; }
        let Some(r3) = r2.strip_prefix("NAME=") else { continue };
        let name: String = r3.chars().take_while(|c| !c.is_whitespace() && *c != '>').collect();
        let after = r3[name.len()..].trim_start();
        if !name.is_empty() && after.starts_with('>') {
            return name;
        }
    }
    id.to_string()
}

fn l4t_release(text: &str) -> Option<String> {
    // R(\d+) \(release\), REVISION: ([\d.]+)
    let i = text.find(" (release), REVISION: ")?;
    let major: String = text[..i].chars().rev().take_while(|c| c.is_ascii_digit()).collect::<Vec<_>>().into_iter().rev().collect();
    if major.is_empty() || !text[..i - major.len()].ends_with('R') { return None; }
    let rev: String = text[i + " (release), REVISION: ".len()..].chars().take_while(|c| c.is_ascii_digit() || *c == '.').collect();
    if rev.is_empty() { None } else { Some(format!("R{}.{}", major, rev)) }
}

pub fn tach_fans(g: &mut Group) -> Vec<Fan> {
    let root = "/sys/class/hwmon";
    let mut fans = Vec::new();
    let chips = hwmon_chips(root, &mut [g]);
    let ids = if g.trace { sysfs::chip_identities(&chips, "/sys") } else { Vec::new() };
    for (d, chip) in &chips {
        let rpm_path = format!("{}/rpm", d);
        if !sysfs::exists(&rpm_path) { continue; }
        let base = d.rsplit('/').next().unwrap();
        let rpm = g.read_int(&rpm_path, &format!("{}.rpm", base), Some(&rpm_path));
        let span = g.span;
        let fan = Fan { id: next_fan_id(), name: chip.clone(), rpm: rpm.map(|r| g.got(r)), percent: None };
        if g.trace {
            let (basis, mut w) = ids.iter().find(|(x, _, _)| x == d).map(|(_, b, w)| (*b, w.clone())).unwrap_or(("unresolved", vec![]));
            w.push(("attr", "rpm".into()));
            g.sensor(Slot::Fan(fan.id, "rpm"), Some(source("hwmon", "rpm", basis, w)), span);
        }
        fans.push(fan);
    }
    fans
}

pub fn extend(stats: &mut Value, fans: &mut Vec<Fan>, g: &mut Groups) {
    stats.set("power_mode", Value::Null);
    stats.set("gpu", Value::Null);
    if let Some(status) = g.power_mode.read_with("/var/lib/nvpmodel/status", "status", None, false, pmode) {
        let conf = g.power_mode.read_str("/etc/nvpmodel.conf", "conf", None);
        let m = power_mode(&status, conf.as_deref());
        let m = g.power_mode.got(m);
        stats.set("power_mode", m.into());
    }
    let gv = gpu(&mut g.gpu);
    stats.set("gpu", gv.into());
    fans.extend(tach_fans(&mut g.fans));
    if let Some(release) = g.board_info.read_with(&sysfs::host_path("/etc/nv_tegra_release"), "l4t", None, false, l4t_release) {
        let r = g.board_info.got(release);
        if let Some(sys) = stats.get_mut("system") {
            sys.set("l4t", r.into());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modes_and_releases() {
        let conf = "< POWER_MODEL ID=0 NAME=15W >\n< POWER_MODEL ID=2 NAME=MAXN_SUPER >";
        assert_eq!(power_mode("pmode:0002", Some(conf)), "MAXN_SUPER");
        assert_eq!(power_mode("pmode:0000", Some(conf)), "15W");
        assert_eq!(power_mode("pmode:0007", Some(conf)), "7");
        assert_eq!(l4t_release("# R36 (release), REVISION: 5.2, GCID: 1"), Some("R36.5.2".into()));
        assert_eq!(pmode("pmode:0002"), Some("pmode:0002".into()));
        assert_eq!(pmode("pmode:x"), None);
    }
}
