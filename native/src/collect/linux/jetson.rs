//! collector/jetson.py: Tegra GPU load and clock, nvpmodel power mode, pwm_tach fans, L4T release.
use super::common::{next_fan_id, Fan};
use crate::json::Value;
use crate::metadata::{source, Group, Slot};
use super::sysfs::{self, entries, hwmon_chips};
use super::Groups;

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

    // tests/collector/test_jetson.py, ported (docs/development/python-test-port.md); the root is /sys.
    use crate::collect::linux::sysfs::fake::{self, Root};

    const OK: &str = r#"{"state":"ok","reason":null,"issues":[],"issues_truncated":0}"#;

    fn make(root: &Root, files: &[(&str, &str)]) {
        for (path, value) in files {
            root.put(&format!("/sys/{}", path), value);
        }
    }

    fn hwmon(root: &Root, chips: &[&[(&str, &str)]]) {
        for (i, files) in chips.iter().enumerate() {
            root.mkdir(&format!("/sys/class/hwmon/hwmon{}", i));
            for (name, value) in *files {
                root.put(&format!("/sys/class/hwmon/hwmon{}/{}", i, name), value);
            }
        }
    }

    fn read_gpu() -> (Option<Value>, Value) {
        let mut g = Group::new("gpu", false);
        let v = gpu(&mut g);
        (v, g.status())
    }

    fn shown(v: &Option<Value>) -> String {
        v.as_ref().map_or("None".into(), |v| v.to_string(false))
    }

    fn fans_text(fans: &[Fan]) -> String {
        fans.iter().map(|f| format!("{} {:?} {:?}", f.name, f.rpm, f.percent)).collect::<Vec<_>>().join(", ")
    }

    #[test]
    fn power_mode_names() {
        let conf = "< POWER_MODEL ID=0 NAME=15W >\n< POWER_MODEL ID=2 NAME=MAXN_SUPER >";
        for (status, expected) in [("pmode:0002", "MAXN_SUPER"), ("pmode:0000", "15W"), ("pmode:0009", "9")] {
            assert_eq!(power_mode(status, Some(conf)), expected);
        }
    }

    #[test]
    fn l4t() {
        for (text, expected) in [("# R36 (release), REVISION: 5.2, GCID: 46426093, BOARD: generic, EABI: aarch64, DATE: Thu Jul 16", Some("R36.5.2")),
                                 ("# R35 (release), REVISION: 4.1, GCID: 33958178, BOARD: t186ref", Some("R35.4.1")), ("garbage", None)] {
            assert_eq!(l4t_release(text).as_deref(), expected);
        }
    }

    #[test]
    fn tach_fans_and_an_unreadable_rpm() {
        let root = Root::new(0); // only chips with the non-standard rpm file count; pwmfan is common hwmon's job
        hwmon(&root, &[&[("name", "pwmfan"), ("pwm1", "76")], &[("name", "pwm_tach"), ("rpm", "1636")]]);
        let mut g = Group::new("fans", false);
        assert_eq!(fans_text(&tach_fans(&mut g)), "pwm_tach Some(1636) None");
        let root = Root::new(0);
        hwmon(&root, &[&[("name", "pwm_tach"), ("rpm", "x")], &[("name", "other_tach"), ("rpm", "0")]]);
        let mut g = Group::new("fans", false);
        assert_eq!(fans_text(&tach_fans(&mut g)), "pwm_tach None None, other_tach Some(0) None");
        assert_eq!(g.status().get("state").unwrap().to_string(false), r#""partial""#);
    }

    #[test]
    fn gpu_and_its_absence() {
        let root = Root::new(0);
        assert_eq!(shown(&read_gpu().0), "None"); // no gpu.0/load: not reported
        make(&root, &[("devices/platform/gpu.0/load", "500"), ("class/devfreq/17000000.gpu/cur_freq", "306000000"),
                      ("class/devfreq/17000000.gpu/max_freq", "1020000000")]);
        assert_eq!(shown(&read_gpu().0), r#"{"usage":50.0,"freq":306000000,"max_freq":1020000000}"#);
    }

    #[test]
    fn gpu_across_bsps() {
        let cases: [(&[(&str, &str)], &str); 4] = [
            // Orin R35 names the GPU devfreq device 17000000.ga10b; load sits in its device directory
            (&[("class/devfreq/17000000.ga10b/cur_freq", "306000000"), ("class/devfreq/17000000.ga10b/max_freq", "1300500000"),
               ("class/devfreq/17000000.ga10b/device/load", "250")], r#"{"usage":25.0,"freq":306000000,"max_freq":1300500000}"#),
            // Xavier: 17000000.gv11b, load through /sys/devices/gpu.0
            (&[("class/devfreq/17000000.gv11b/cur_freq", "114750000"), ("class/devfreq/17000000.gv11b/max_freq", "1377000000"),
               ("devices/gpu.0/load", "500")], r#"{"usage":50.0,"freq":114750000,"max_freq":1377000000}"#),
            // load readable but no GPU devfreq device: clock unknown (null), not 0 MHz
            (&[("devices/platform/gpu.0/load", "500")], r#"{"usage":50.0,"freq":null,"max_freq":null}"#),
            // devfreq device without readable clock files
            (&[("class/devfreq/17000000.gpu/device/load", "0")], r#"{"usage":0.0,"freq":null,"max_freq":null}"#)];
        for (files, expected) in cases {
            let root = Root::new(0);
            make(&root, files);
            assert_eq!(shown(&read_gpu().0), expected);
        }
    }

    #[test]
    fn gpu_load_fallback_hides_failed_candidates() {
        for bad in ["garbage", "bad", "\u{ff}"] { // also test_gpu_matrix_a and an undecodable candidate
            let root = Root::new(0);
            make(&root, &[("class/devfreq/17000000.gpu/cur_freq", "306000000"), ("class/devfreq/17000000.gpu/max_freq", "1020000000"),
                          ("devices/platform/gpu.0/load", "500")]);
            if bad == "\u{ff}" {
                root.put("/sys/class/devfreq/17000000.gpu/device/load", b"\xff\n");
            } else {
                make(&root, &[("class/devfreq/17000000.gpu/device/load", bad)]);
            }
            let (v, status) = read_gpu();
            assert_eq!(shown(&v), r#"{"usage":50.0,"freq":306000000,"max_freq":1020000000}"#);
            assert_eq!(status.to_string(false), OK); // the failed candidate is not the result
        }
    }

    #[test]
    fn gpu_clock_failure_keeps_usage() {
        let root = Root::new(0);
        make(&root, &[("class/devfreq/17000000.gpu/device/load", "250"), ("class/devfreq/17000000.gpu/cur_freq", "1"),
                      ("class/devfreq/17000000.gpu/max_freq", "1020000000")]);
        fake::answers("/sys/class/devfreq/17000000.gpu/cur_freq", vec![Err(libc::EIO)]);
        let (v, status) = read_gpu();
        assert_eq!(shown(&v), r#"{"usage":25.0,"freq":null,"max_freq":1020000000}"#);
        assert_eq!(status.to_string(false), r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"cur_freq","reason":"io_error"}],"issues_truncated":0}"#);
    }

    #[test]
    fn gpu_without_load() {
        let root = Root::new(0);
        let (v, status) = read_gpu(); // nothing there: gpu null, unavailable, not an error
        assert!(v.is_none());
        assert_eq!(status.to_string(false), r#"{"state":"unavailable","reason":"not_exposed","issues":[],"issues_truncated":0}"#);
        make(&root, &[("devices/platform/gpu.0/load", "")]);
        let (v, status) = read_gpu();
        assert!(v.is_none());
        assert_eq!(status.to_string(false), r#"{"state":"error","reason":"invalid_data","issues":[{"target":"load","reason":"invalid_data"}],"issues_truncated":0}"#);
    }

    #[test]
    fn gpu_unlistable_devfreq_without_fallback() {
        let root = Root::new(0);
        root.mkdir("/sys/class/devfreq/17000000.gpu");
        fake::unlistable("/sys/class/devfreq", libc::EACCES);
        let (v, status) = read_gpu();
        assert!(v.is_none());
        assert_eq!((status.get("state").unwrap().to_string(false), status.get("reason").unwrap().to_string(false)),
                   (r#""error""#.into(), r#""permission_denied""#.into())); // not "not_exposed"
    }

    #[test]
    fn gpu_matrix_b_unlistable_devfreq_with_load() {
        for (code, reason) in [(libc::EACCES, "permission_denied"), (libc::EIO, "io_error")] {
            let root = Root::new(0);
            make(&root, &[("class/devfreq/17000000.gpu/cur_freq", "306000000"), ("class/devfreq/17000000.gpu/max_freq", "1020000000"),
                          ("devices/platform/gpu.0/load", "500")]);
            fake::unlistable("/sys/class/devfreq", code);
            let (v, status) = read_gpu();
            assert_eq!(shown(&v), r#"{"usage":50.0,"freq":null,"max_freq":null}"#);
            assert_eq!(status.to_string(false), format!(
                r#"{{"state":"partial","reason":"some_unreadable","issues":[{{"target":"devfreq","reason":"{}"}}],"issues_truncated":0}}"#, reason));
            fake::listable("/sys/class/devfreq"); // listing works again
            let (v, status) = read_gpu();
            assert_eq!(shown(&v), r#"{"usage":50.0,"freq":306000000,"max_freq":1020000000}"#);
            assert_eq!(status.get("state").unwrap().to_string(false), r#""ok""#);
        }
    }

    #[test]
    fn gpu_matrix_c_no_devfreq_is_not_a_warning() {
        let root = Root::new(0);
        make(&root, &[("devices/platform/gpu.0/load", "500")]);
        let (v, status) = read_gpu();
        assert_eq!(shown(&v), r#"{"usage":50.0,"freq":null,"max_freq":null}"#);
        assert_eq!(status.get("state").unwrap().to_string(false), r#""ok""#);
    }

    /// extend() on a tree with the nvpmodel files and the L4T release given (None: absent) and a GPU load.
    fn extend_with(status: Option<&[u8]>, release: Option<&str>, conf: &[u8]) -> (Value, [Value; 3]) {
        let root = Root::new(0);
        root.put("/sys/devices/platform/gpu.0/load", "500");
        if let Some(s) = status {
            root.put("/var/lib/nvpmodel/status", s);
        }
        root.put("/etc/nvpmodel.conf", conf);
        if let Some(r) = release {
            root.put("/etc/nv_tegra_release", r);
        }
        let mut stats = Value::obj();
        stats.set("system", Value::obj());
        let mut g = super::super::Groups::new(false);
        extend(&mut stats, &mut Vec::new(), &mut g);
        (stats, [g.power_mode.status(), g.board_info.status(), g.gpu.status()])
    }

    #[test]
    fn extend_power_mode_and_l4t() {
        let (stats, [mode, board, gpu]) = extend_with(Some(b"pmode:0002\n"), Some("# R36 (release), REVISION: 5.2, GCID: 1"),
                                                      b"< POWER_MODEL ID=2 NAME=MAXN_SUPER >");
        assert_eq!(stats.to_string(false), r#"{"system":{"l4t":"R36.5.2"},"power_mode":"MAXN_SUPER","gpu":{"usage":50.0,"freq":null,"max_freq":null}}"#);
        assert_eq!([mode, board, gpu].map(|s| s.get("state").unwrap().to_string(false)), [r#""ok""#; 3]);
    }

    #[test]
    fn extend_bad_power_mode_and_l4t_do_not_stop_the_rest() {
        let (stats, [mode, board, gpu]) = extend_with(Some(b"garbage"), Some("not a release line"), b"< POWER_MODEL ID=2 NAME=MAXN_SUPER >");
        assert_eq!(stats.to_string(false), r#"{"system":{},"power_mode":null,"gpu":{"usage":50.0,"freq":null,"max_freq":null}}"#);
        assert_eq!(mode.get("issues").unwrap().to_string(false), r#"[{"target":"status","reason":"invalid_data"}]"#);
        assert_eq!(board.get("issues").unwrap().to_string(false), r#"[{"target":"l4t","reason":"invalid_data"}]"#);
        assert_eq!(gpu.get("state").unwrap().to_string(false), r#""ok""#);
    }

    #[test]
    fn extend_without_nvpmodel() {
        // no nvpmodel mount (a container started without compose.jetson.yaml): absent, not an error
        let (stats, [mode, board, _]) = extend_with(None, None, b"");
        assert!(stats.get("power_mode").unwrap().is_null());
        assert_eq!([mode, board].map(|s| s.get("state").unwrap().to_string(false)), [r#""unavailable""#; 2]);
    }

    #[test]
    fn extend_undecodable_nvpmodel_conf_keeps_the_mode_id() {
        let (stats, [mode, _, _]) = extend_with(Some(b"pmode:0002\n"), None, b"\xff\n");
        assert_eq!(stats.get("power_mode").unwrap().to_string(false), r#""2""#); // the id instead of the name
        assert_eq!(mode.to_string(false), r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"conf","reason":"invalid_data"}],"issues_truncated":0}"#);
    }
}
