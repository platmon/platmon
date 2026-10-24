//! The slow groups' readers: collector/storage.py, wifi.py, battery.py and probe.py, each called by its
//! group's thread (src/slow.rs) with the group's diagnostics. Contracts: docs/reference/storage.md,
//! wifi.md, battery.md, probe.md.
use crate::config::Config;
use crate::json::Value;
use crate::metadata::{py_int, Group};
use super::passive::{if_indexes, is_physical, namespace_id, strerror, valid_ifname};
use crate::slow::Reader;
use crate::pystr::{py_space, py_split};
use super::sysfs::{self, ReadError};
use std::collections::VecDeque;

/// The slow groups in platmon.py's order.
pub const GROUPS: [&str; 4] = ["storage", "wifi", "battery", "probe"];

/// The reader of a group, configured as platmon.py configures it.
pub fn reader(name: &str, cfg: &Config) -> Reader {
    match name {
        "storage" => Box::new(|g| Ok(storage(g))),
        "wifi" => Box::new(|g| Ok(wifi(g))),
        "battery" => Box::new(|g| Ok(battery(g))),
        _ => {
            let mut p = Probe::new(cfg.targets.clone(), cfg.float("probe", "timeout"));
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

// --- storage -------------------------------------------------------------------------------------------

const PARTITIONS: &str = "/proc/partitions";
const MOUNTINFO: &str = "/proc/self/mountinfo";
const SYS_CLASS_BLOCK: &str = "/sys/class/block";
const LOCAL_FS: [&str; 11] = ["ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "exfat", "ntfs3", "jfs", "reiserfs"];

/// storage.unescape: \NNN (octal) as the character with that code.
fn unescape(field: &str) -> String {
    let (b, mut out, mut i) = (field.as_bytes(), String::new(), 0);
    while i < b.len() {
        if b[i] == b'\\' && i + 4 <= b.len() && b[i + 1..i + 4].iter().all(|c| (b'0'..=b'7').contains(c)) {
            let code = u32::from_str_radix(&field[i + 1..i + 4], 8).unwrap();
            out.push(char::from_u32(code).unwrap());
            i += 4;
            continue;
        }
        let c = field[i..].chars().next().unwrap();
        out.push(c);
        i += c.len_utf8();
    }
    out
}

struct Mount {
    major: i64,
    minor: i64,
    path: String,
    read_only: bool,
    fstype: String,
    source: String,
}

/// storage.parse_mountinfo for the local filesystem types: (mounts, lines that do not parse).
fn parse_mountinfo(text: &str) -> (Vec<Mount>, Vec<String>) {
    let (mut mounts, mut bad) = (Vec::new(), Vec::new());
    for (n, line) in text.split('\n').enumerate() {
        if line.trim_matches(py_space).is_empty() {
            continue;
        }
        let f: Vec<&str> = line.split(' ').collect();
        let parsed = (|| {
            let sep = f.iter().skip(6).position(|x| *x == "-")? + 6;
            let fstype = *f.get(sep + 1)?;
            if !LOCAL_FS.contains(&fstype) {
                return Some(None);
            }
            let nums: Vec<&str> = f[2].split(':').collect();
            if nums.len() != 2 {
                return None;
            }
            let (major, minor) = (py_int(nums[0])?, py_int(nums[1])?);
            let source = unescape(f.get(sep + 2)?);
            let read_only = f.get(sep + 3)?.split(',').any(|o| o == "ro");
            Some(Some(Mount { major, minor, path: unescape(f[4]), read_only, fstype: fstype.into(), source }))
        })();
        match parsed {
            Some(Some(m)) => mounts.push(m),
            Some(None) => {}
            None => bad.push(format!("line{}", n + 1)),
        }
    }
    (mounts, bad)
}

/// storage.parse_partitions: [((major, minor), (name, bytes))], a later row for a number replacing an
/// earlier one in its place, as a dict does.
fn parse_partitions(text: &str) -> Vec<((i64, i64), (String, i64))> {
    let mut out: Vec<((i64, i64), (String, i64))> = Vec::new();
    for line in text.split('\n').skip(2) {
        let f = py_split(line);
        if f.len() == 4 && f[..3].iter().all(|x| !x.is_empty() && x.bytes().all(|b| b.is_ascii_digit())) {
            let (Ok(major), Ok(minor), Ok(blocks)) = (f[0].parse::<i64>(), f[1].parse::<i64>(), f[2].parse::<i64>()) else { continue };
            let entry = ((major, minor), (f[3].to_string(), blocks * 1024));
            match out.iter_mut().find(|(k, _)| *k == entry.0) {
                Some(slot) => *slot = entry,
                None => out.push(entry),
            }
        }
    }
    out
}

/// storage.parent_of: the whole disk a partition belongs to, None for anything that is not one.
fn parent_of(name: &str) -> std::io::Result<Option<String>> {
    if !sysfs::exists(&format!("{}/{}/partition", SYS_CLASS_BLOCK, name)) {
        return Ok(None);
    }
    let target = std::fs::read_link(format!("{}/{}", SYS_CLASS_BLOCK, name))?;
    let t = target.to_string_lossy();
    let dir = t.rsplit_once('/').map_or("", |(d, _)| d);
    Ok(Some(dir.rsplit('/').next().unwrap_or(dir).to_string()))
}

/// storage.filesystem_capacity: (f_blocks, f_bfree, f_bavail, f_frsize) of path once it is pinned and
/// checked to be on device.
fn capacity(path: &str, device: (i64, i64)) -> std::io::Result<(u64, u64, u64, u64)> {
    let c = std::ffi::CString::new(path).map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
    unsafe {
        let fd = libc::open(c.as_ptr(), libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW);
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let result = (|| {
            let mut st: libc::stat = std::mem::zeroed();
            if libc::fstat(fd, &mut st) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            if (libc::major(st.st_dev) as i64, libc::minor(st.st_dev) as i64) != device {
                return Err(std::io::Error::from_raw_os_error(libc::ESTALE));
            }
            let mut v: libc::statvfs = std::mem::zeroed();
            if libc::fstatvfs(fd, &mut v) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok((v.f_blocks as u64, v.f_bfree as u64, v.f_bavail as u64, v.f_frsize as u64))
        })();
        libc::close(fd);
        result
    }
}

/// storage.Storage(): the partitions of the physical disks and the local filesystems' capacity.
pub fn storage(g: &mut Group) -> Value {
    let names = match sysfs::read_utf8(PARTITIONS) {
        Ok(t) => parse_partitions(&t),
        Err(e) => {
            g.note("partitions", os_error(&e).map_or("invalid_data", sysfs::reason_of), Some(PARTITIONS.into()));
            vec![]
        }
    };
    let (mounts, bad) = match sysfs::read_utf8(MOUNTINFO) {
        Ok(t) => parse_mountinfo(&t),
        Err(e) => {
            g.note("mountinfo", os_error(&e).map_or("invalid_data", sysfs::reason_of), Some(MOUNTINFO.into()));
            (vec![], vec![])
        }
    };
    for target in &bad {
        g.note(target, "invalid_data", Some(MOUNTINFO.into()));
    }
    // one entry per filesystem, however many mount points show it
    let mut filesystems: Vec<((i64, i64), Value, Vec<(String, bool)>)> = Vec::new();
    for m in mounts {
        if !LOCAL_FS.contains(&m.fstype.as_str()) || !m.source.starts_with("/dev/") {
            continue;
        }
        let key = (m.major, m.minor);
        let pos = filesystems.iter().position(|(k, _, _)| *k == key).unwrap_or_else(|| {
            let device = names.iter().find(|(k, _)| *k == key).map_or(Value::Null, |(_, (n, _))| n.as_str().into());
            filesystems.push((key, obj(vec![("device", device), ("major", m.major.into()), ("minor", m.minor.into()),
                                            ("fstype", m.fstype.as_str().into()), ("source", m.source.as_str().into()),
                                            ("mount_points", Value::Arr(vec![])), ("total_bytes", Value::Null),
                                            ("used_bytes", Value::Null), ("available_bytes", Value::Null)]), vec![]));
            filesystems.len() - 1
        });
        filesystems[pos].2.push((m.path, m.read_only));
    }
    for (key, fs, mounts) in filesystems.iter_mut() {
        let label = match fs.get("device") { Some(Value::Str(d)) if !d.is_empty() => d.clone(), _ => format!("{}:{}", key.0, key.1) };
        for (index, (path, _)) in mounts.iter().enumerate() {
            match capacity(path, *key) {
                Ok((blocks, free, avail, frsize)) => {
                    g.got(());
                    fs.set("total_bytes", Value::Big(blocks as u128 * frsize as u128));
                    fs.set("used_bytes", Value::Big((blocks as u128).wrapping_sub(free as u128) * frsize as u128));
                    fs.set("available_bytes", Value::Big(avail as u128 * frsize as u128));
                    break;
                }
                Err(e) => {
                    // a file bind listed before a usable directory of the same filesystem: try the next
                    if e.raw_os_error() == Some(libc::ENOTDIR) && index + 1 < mounts.len() {
                        continue;
                    }
                    let why = match e.raw_os_error() {
                        Some(libc::ESTALE) => "mount_changed",
                        Some(libc::ENOENT) => "disappeared",
                        _ => sysfs::reason_of(&e),
                    };
                    g.note(&label, why, Some(format!("statvfs {}: {}", path, strerror(&e))));
                    break; // no valid capacity: listed without numbers, never as 0
                }
            }
        }
        fs.set("mount_points", Value::Arr(mounts.iter().map(|(p, ro)| obj(vec![("path", p.as_str().into()), ("read_only", Value::Bool(*ro))])).collect()));
    }
    let mut sorted_names: Vec<&((i64, i64), (String, i64))> = names.iter().collect();
    sorted_names.sort_by(|a, b| a.1 .0.cmp(&b.1 .0));
    let mut partitions = Vec::new();
    for ((major, minor), (name, size)) in sorted_names {
        let parent = match parent_of(name).and_then(|p| match p {
            Some(p) => is_physical(&p).map(|phys| (phys == Some(true)).then_some(p)),
            None => Ok(None),
        }) {
            Ok(Some(p)) => p,
            Ok(None) => continue,
            Err(e) => {
                g.note(name, sysfs::reason_of(&e), Some(format!("{}/{}: {}", SYS_CLASS_BLOCK, name, strerror(&e))));
                continue;
            }
        };
        let points = filesystems.iter().find(|(k, _, _)| *k == (*major, *minor))
            .map_or(vec![], |(_, _, m)| m.iter().map(|(p, _)| Value::from(p.as_str())).collect());
        g.got(());
        partitions.push(obj(vec![("name", name.as_str().into()), ("disk", parent.into()), ("major", (*major).into()),
                                 ("minor", (*minor).into()), ("size_bytes", (*size).into()), ("mount_points", Value::Arr(points))]));
    }
    let mut out: Vec<(String, Value)> = filesystems.into_iter().map(|(_, fs, m)| (m[0].0.clone(), fs)).collect();
    out.sort_by(|a, b| a.0.cmp(&b.0));
    if out.is_empty() && partitions.is_empty() && g.reasons.is_empty() {
        g.note("storage", "not_detected", None);
    }
    obj(vec![("partitions", Value::Arr(partitions)), ("filesystems", Value::Arr(out.into_iter().map(|(_, f)| f).collect()))])
}

// --- Wi-Fi ---------------------------------------------------------------------------------------------

const WIRELESS: &str = "/proc/net/wireless";
const NETNS: &str = "/proc/self/ns/net";

/// -?\d+\.? : (value, dot); the value as i128 so a long one is out of range rather than unparsed.
fn level_token(t: &str) -> Option<(i128, bool)> {
    let (t, dot) = t.strip_suffix('.').map_or((t, false), |x| (x, true));
    let digits = t.strip_prefix('-').unwrap_or(t);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some((t.parse::<i128>().unwrap_or(if t.starts_with('-') { i128::MIN } else { i128::MAX }), dot))
}

/// wifi.parse: (interfaces, [(target, why)]); Err for a file that is not the format at all.
fn parse_wireless(text: &str) -> Result<(Vec<Value>, Vec<(String, &'static str)>), ()> {
    let lines: Vec<&str> = text.split('\n').collect();
    if lines.len() < 2 || !lines[0].contains("Quality") || !lines[1].contains("level") {
        return Err(());
    }
    let (mut out, mut bad) = (Vec::new(), Vec::new());
    for (i, line) in lines[2..].iter().enumerate() {
        if line.trim_matches(py_space).is_empty() {
            continue;
        }
        let n = i + 3;
        let t = py_split(line);
        let row = (t.len() == 11).then_some(()).and_then(|_| {
            let name = t[0].strip_suffix(':').filter(|n| !n.is_empty())?;
            if t[1].len() != 4 || !t[1].bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
                return None;
            }
            let (link, link_new) = level_token(t[2])?;
            let (level, level_new) = level_token(t[3])?;
            let (noise, _) = level_token(t[4])?;
            if !t[5..].iter().all(|x| !x.is_empty() && x.bytes().all(|b| b.is_ascii_digit())) {
                return None;
            }
            Some((name, link, link_new, level, level_new, noise))
        }).filter(|r| valid_ifname(r.0));
        let Some((name, link, link_new, level, level_new, noise)) = row else {
            bad.push((format!("line{}", n), "invalid_data"));
            continue;
        };
        if !((0..=255).contains(&link) && (-256..=255).contains(&level) && (-256..=255).contains(&noise)) {
            bad.push((name.to_string(), "invalid_data"));
            continue;
        }
        let (updated, dbm) = (level_new, level < 0);
        let num = |v: i128, ok: bool| if ok { Value::Int(v as i64) } else { Value::Null };
        out.push(obj(vec![("name", name.into()), ("connected", Value::Null), ("connection_state", "unknown".into()),
                          ("signal_dbm", num(level, updated && dbm)), ("signal_raw", num(level, updated && !dbm)),
                          ("link_quality", num(link, updated && link_new)),
                          ("noise_dbm", num(noise, updated && noise < 0 && noise != -256))]));
    }
    Ok((out, bad))
}

/// Why link_connected failed, as Python's exception types tell it apart.
enum LinkError {
    Missing,
    Os(std::io::Error),
    Invalid,
}

fn read_ascii_stripped(path: &str) -> Result<String, LinkError> {
    match sysfs::read_ascii(path) {
        Ok(t) => Ok(t.trim_matches(py_space).to_string()),
        Err(ReadError::Utf8) => Err(LinkError::Invalid),
        Err(ReadError::Os(e)) if e.raw_os_error() == Some(libc::ENOENT) => Err(LinkError::Missing),
        Err(ReadError::Os(e)) => Err(LinkError::Os(e)),
    }
}

/// wifi.link_connected: the kernel's carrier, None when the index moved meanwhile.
fn link_connected(name: &str, ifindex: i64) -> Result<Option<bool>, LinkError> {
    let root = format!("/sys/class/net/{}", name);
    let index = |r: &str| -> Result<i64, LinkError> { py_int(&read_ascii_stripped(&format!("{}/ifindex", r))?).ok_or(LinkError::Invalid) };
    let before = index(&root)?;
    let carrier = match read_ascii_stripped(&format!("{}/carrier", root)) {
        Ok(c) => c,
        // Linux refuses carrier reads on interfaces that are down: confirm IFF_UP is clear
        Err(LinkError::Os(e)) if e.raw_os_error() == Some(libc::EINVAL) => {
            let flags = read_ascii_stripped(&format!("{}/flags", root))?;
            let digits = flags.strip_prefix("0x").or_else(|| flags.strip_prefix("0X")).unwrap_or(&flags);
            let flags = i64::from_str_radix(&digits.replace('_', ""), 16).map_err(|_| LinkError::Invalid)?;
            if flags & 1 != 0 {
                return Err(LinkError::Os(e));
            }
            "0".into()
        }
        Err(e) => return Err(e),
    };
    let after = index(&root)?;
    if before != ifindex || after != ifindex {
        return Ok(None);
    }
    match carrier.as_str() {
        "0" => Ok(Some(false)),
        "1" => Ok(Some(true)),
        _ => Err(LinkError::Invalid),
    }
}

/// wifi.Wifi(): each wireless interface's link as the kernel last knew it.
pub fn wifi(g: &mut Group) -> Value {
    let mut scope = obj(vec![("kind", "process_network_namespace".into())]);
    let out = |scope: Value, interfaces: Vec<Value>| obj(vec![("scope", scope), ("provider", "proc_net_wireless".into()),
                                                              ("interfaces", Value::Arr(interfaces))]);
    let (mut interfaces, bad) = match sysfs::read_ascii(WIRELESS) {
        Ok(t) => match parse_wireless(&t) {
            Ok(x) => x,
            Err(()) => { g.note("wireless", "invalid_data", Some(WIRELESS.into())); return out(scope, vec![]); }
        },
        Err(ReadError::Os(e)) if e.raw_os_error() == Some(libc::ENOENT) => { g.note("wireless", "not_exposed", None); return out(scope, vec![]); }
        Err(ReadError::Os(e)) => {
            g.note("wireless", sysfs::reason_of(&e), Some(format!("{}: {}", WIRELESS, strerror(&e))));
            return out(scope, vec![]);
        }
        Err(ReadError::Utf8) => { g.note("wireless", "invalid_data", Some(WIRELESS.into())); return out(scope, vec![]); }
    };
    let (mut ns, mut index) = (Value::Null, Vec::new());
    if !interfaces.is_empty() {
        use std::os::unix::fs::MetadataExt;
        // as Python: the namespace is kept when only the index listing fails
        let listed = std::fs::metadata(NETNS).and_then(|st| {
            ns = namespace_id(st.dev(), st.ino()).into();
            if_indexes()
        });
        match listed {
            Ok(idx) => index = idx,
            Err(e) => g.note("identity", sysfs::reason_of(&e), None),
        }
    }
    scope.set("id", ns);
    const SIGNALS: [&str; 4] = ["signal_dbm", "signal_raw", "link_quality", "noise_dbm"];
    for item in interfaces.iter_mut() {
        let name = match item.get("name") { Some(Value::Str(n)) => n.clone(), _ => continue };
        let ifindex = index.iter().rev().find(|(_, n)| *n == name).map(|(i, _)| *i as i64);
        item.set("ifindex", ifindex.into());
        let Some(ifindex) = ifindex else {
            for k in SIGNALS { item.set(k, Value::Null); }
            continue;
        };
        match link_connected(&name, ifindex) {
            Ok(connected) => {
                item.set("connected", connected.map_or(Value::Null, Value::Bool));
                item.set("connection_state", match connected { None => "unknown", Some(true) => "connected", Some(false) => "disconnected" }.into());
            }
            Err(LinkError::Missing) => {
                item.set("connection_state", "unsupported".into());
                g.note(&name, "not_exposed", None);
            }
            Err(LinkError::Os(e)) => {
                let why = sysfs::reason_of(&e);
                item.set("connection_state", if why == "permission_denied" { "permission_denied" } else { "unavailable" }.into());
                g.note(&name, why, None);
            }
            Err(LinkError::Invalid) => {
                item.set("connection_state", "unknown".into());
                g.note(&name, "invalid_data", None);
            }
        }
        if item.get("connected") != Some(&Value::Bool(true)) {
            for k in SIGNALS { item.set(k, Value::Null); }
        }
    }
    for (target, why) in &bad {
        g.note(target, why, Some(WIRELESS.into()));
    }
    let empty = interfaces.is_empty();
    interfaces.sort_by(|a, b| match (a.get("name"), b.get("name")) { (Some(Value::Str(x)), Some(Value::Str(y))) => x.cmp(y), _ => std::cmp::Ordering::Equal });
    let interfaces: Vec<Value> = interfaces.into_iter().map(|i| { g.got(()); i }).collect();
    if empty && bad.is_empty() {
        g.note("wireless", "not_detected", None);
    }
    out(scope, interfaces)
}

// --- battery -------------------------------------------------------------------------------------------

const POWER_SUPPLY: &str = "/sys/class/power_supply";
const CHARGE_ENERGY: [(&str, &str); 6] = [("charge_now", "charge_now_uah"), ("charge_full", "charge_full_uah"),
    ("charge_full_design", "charge_full_design_uah"), ("energy_now", "energy_now_uwh"), ("energy_full", "energy_full_uwh"),
    ("energy_full_design", "energy_full_design_uwh")];
const SIGNED: [(&str, &str); 2] = [("current_now", "current_ua"), ("power_now", "power_uw")];
const UNSIGNED: [(&str, &str); 2] = [("voltage_now", "voltage_uv"), ("cycle_count", "cycle_count")];
const TEXT: [&str; 4] = ["status", "capacity_level", "technology", "health"];

fn enum_text(v: &str) -> Option<Value> {
    let v = v.trim_matches(py_space);
    (!v.is_empty() && v.chars().count() <= 32).then(|| v.into())
}

fn flag(v: &str) -> Option<Value> {
    match v.trim_matches(py_space) { "0" => Some(Value::Bool(false)), "1" => Some(Value::Bool(true)), _ => None }
}

/// group.read of an attribute this collection listed; None, nothing noted, when the driver has none.
fn attr(g: &mut Group, d: &str, attrs: &[String], name: &str, a: &str, parse: impl Fn(&str) -> Option<Value>) -> Value {
    if !attrs.iter().any(|x| x == a) {
        return Value::Null;
    }
    g.read_with(&format!("{}/{}", d, a), &format!("{}.{}", name, a), Some(d), false, parse).unwrap_or(Value::Null)
}

/// battery.Battery(): every battery's and external supply's reading as the kernel last knew it.
pub fn battery(g: &mut Group) -> Value {
    let (mut batteries, mut external) = (Vec::new(), Vec::new());
    let names = sysfs::entries(POWER_SUPPLY, &mut [&mut *g], false);
    for name in &names {
        let d = format!("{}/{}", POWER_SUPPLY, name);
        let attrs = sysfs::entries(&d, &mut [&mut *g], true);
        if attrs.is_empty() {
            continue; // went away between the two listings, or cannot be listed
        }
        let Some(kind) = g.read_with(&format!("{}/type", d), &format!("{}.type", name), Some(&d), false, enum_text) else { continue };
        let Value::Str(kind) = kind else { continue };
        if kind == "Battery" || kind == "UPS" {
            let mut item = obj(vec![("name", name.as_str().into()), ("present", Value::Null), ("percent", Value::Null)]);
            for t in TEXT { item.set(t, Value::Null); }
            for (_, f) in CHARGE_ENERGY.iter().chain(SIGNED.iter()).chain(UNSIGNED.iter()) { item.set(f, Value::Null); }
            let present = attr(g, &d, &attrs, name, "present", flag);
            item.set("present", present.clone());
            if present != Value::Bool(false) { // an empty bay: what follows is not its reading
                let percent = attr(g, &d, &attrs, name, "capacity", |v| py_int(v).filter(|p| (0..=100).contains(p)).map(Value::Int));
                item.set("percent", percent);
                for (a, f) in CHARGE_ENERGY.iter().chain(UNSIGNED.iter()) {
                    let v = attr(g, &d, &attrs, name, a, |v| py_int(v).filter(|x| *x >= 0).map(Value::Int));
                    item.set(f, v);
                }
                for (a, f) in SIGNED {
                    let v = attr(g, &d, &attrs, name, a, |v| py_int(v).map(Value::Int));
                    item.set(f, v);
                }
                for t in TEXT {
                    let v = attr(g, &d, &attrs, name, t, enum_text);
                    item.set(t, v);
                }
            }
            g.got(());
            batteries.push(item);
        } else {
            let online = attr(g, &d, &attrs, name, "online", flag);
            g.got(());
            external.push(obj(vec![("name", name.as_str().into()), ("type", kind.into()), ("online", online)]));
        }
    }
    if names.is_empty() {
        g.note("power_supply", "not_detected", None);
    }
    obj(vec![("provider", "sysfs_power_supply".into()), ("batteries", Value::Arr(batteries)), ("external", Value::Arr(external))])
}

// --- probe ---------------------------------------------------------------------------------------------

fn reason(code: i32) -> &'static str {
    match code {
        libc::ECONNREFUSED => "refused",
        libc::ENETUNREACH | libc::EHOSTUNREACH => "unreachable",
        libc::ETIMEDOUT => "timeout",
        _ => "error",
    }
}

fn monotonic_ns() -> i64 {
    let mut t = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t) };
    t.tv_sec as i64 * 1_000_000_000 + t.tv_nsec as i64
}

/// probe.connect_ms: (ms to complete the connection, None) or (None, reason). One socket, closed before
/// returning.
fn connect_ms(address: &str, port: u16, timeout: f64) -> (Option<f64>, Option<&'static str>) {
    let Ok(ip) = address.parse::<std::net::IpAddr>() else { return (None, Some("error")) };
    unsafe {
        let family = if ip.is_ipv6() { libc::AF_INET6 } else { libc::AF_INET };
        let fd = libc::socket(family, libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC, 0);
        if fd < 0 {
            return (None, Some(reason(*libc::__errno_location())));
        }
        let start = monotonic_ns();
        let rc = match ip {
            std::net::IpAddr::V4(a) => {
                let mut sa: libc::sockaddr_in = std::mem::zeroed();
                sa.sin_family = libc::AF_INET as libc::sa_family_t;
                sa.sin_port = port.to_be();
                sa.sin_addr.s_addr = u32::from(a).to_be();
                libc::connect(fd, (&sa as *const libc::sockaddr_in).cast(), std::mem::size_of::<libc::sockaddr_in>() as u32)
            }
            std::net::IpAddr::V6(a) => {
                let mut sa: libc::sockaddr_in6 = std::mem::zeroed();
                sa.sin6_family = libc::AF_INET6 as libc::sa_family_t;
                sa.sin6_port = port.to_be();
                sa.sin6_addr.s6_addr = a.octets();
                libc::connect(fd, (&sa as *const libc::sockaddr_in6).cast(), std::mem::size_of::<libc::sockaddr_in6>() as u32)
            }
        };
        let out = (|| {
            if rc != 0 {
                let e = *libc::__errno_location();
                if e != libc::EINPROGRESS {
                    return (None, Some(reason(e)));
                }
                let mut p = libc::pollfd { fd, events: libc::POLLOUT, revents: 0 };
                let deadline = start + (timeout * 1e9) as i64;
                loop {
                    let left = ((deadline - monotonic_ns()) as f64 / 1e6).ceil().max(0.0) as i32;
                    let n = libc::poll(&mut p, 1, left);
                    if n < 0 && *libc::__errno_location() == libc::EINTR {
                        continue;
                    }
                    if n <= 0 {
                        return (None, Some("timeout"));
                    }
                    break;
                }
                let mut err: libc::c_int = 0;
                let mut len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
                libc::getsockopt(fd, libc::SOL_SOCKET, libc::SO_ERROR, (&mut err as *mut libc::c_int).cast(), &mut len);
                if err != 0 {
                    return (None, Some(reason(err)));
                }
            }
            (Some(crate::json::round_to((monotonic_ns() - start) as f64 / 1e6, 3)), None)
        })();
        libc::close(fd);
        out
    }
}

/// probe.Probe: one attempt per target per observation, one after another, with the last 10 results of
/// each for its failure share.
pub struct Probe {
    targets: Vec<(String, u16)>,
    timeout: f64,
    recent: Vec<VecDeque<bool>>,
}

impl Probe {
    pub fn new(targets: Vec<(String, u16)>, timeout: f64) -> Probe {
        let recent = targets.iter().map(|_| VecDeque::new()).collect();
        Probe { targets, timeout, recent }
    }

    pub fn observe(&mut self, g: &mut Group) -> Value {
        let mut out = Vec::new();
        for (i, (address, port)) in self.targets.iter().enumerate() {
            let (ms, why) = connect_ms(address, *port, self.timeout);
            let recent = &mut self.recent[i];
            if recent.len() == 10 {
                recent.pop_front();
            }
            recent.push_back(ms.is_some());
            let failures = recent.iter().filter(|ok| !**ok).count();
            g.got(());
            out.push(obj(vec![("address", address.as_str().into()), ("port", (*port as i64).into()), ("connect_ms", ms.into()),
                              ("reason", why.into()), ("attempts", (recent.len() as i64).into()), ("failures", (failures as i64).into()),
                              ("failure_ratio", crate::json::round_to(failures as f64 / recent.len() as f64, 3).into())]));
        }
        obj(vec![("provider", "tcp_connect".into()), ("timeout_ms", (self.timeout * 1000.0).into()), ("targets", Value::Arr(out))])
    }
}
