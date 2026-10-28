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

/// The slow groups in platmon.py's order.
pub const GROUPS: [&str; 4] = ["storage", "wifi", "battery", "probe"];

/// The reader of a group, configured as platmon.py configures it.
pub fn reader(name: &str, cfg: &Config) -> Reader {
    match name {
        "storage" => Box::new(|g| Ok(storage(g))),
        "wifi" => Box::new(|g| Ok(wifi(g))),
        "battery" => Box::new(|g| Ok(battery(g))),
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
    let target = std::fs::read_link(&*sysfs::at(&format!("{}/{}", SYS_CLASS_BLOCK, name)))?;
    let t = target.to_string_lossy();
    let dir = t.rsplit_once('/').map_or("", |(d, _)| d);
    Ok(Some(dir.rsplit('/').next().unwrap_or(dir).to_string()))
}

/// storage.filesystem_capacity: (f_blocks, f_bfree, f_bavail, f_frsize) of path once it is pinned and
/// checked to be on device.
fn capacity(path: &str, device: (i64, i64)) -> std::io::Result<(u64, u64, u64, u64)> {
    let c = std::ffi::CString::new(&*sysfs::at(path)).map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
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
        let listed = std::fs::metadata(&*sysfs::at(NETNS)).and_then(|st| {
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
pub fn connect_ms(address: &str, port: u16, timeout: f64) -> (Option<f64>, Option<&'static str>) {
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

// tests/collector/test_probe.py's connect_ms tests, ported (docs/development/python-test-port.md).
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn connect_to_a_loopback_listener() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let (ms, why) = connect_ms("127.0.0.1", listener.local_addr().unwrap().port(), 2.0);
        assert!(matches!(ms, Some(m) if (0.0..2000.0).contains(&m)) && why.is_none(), "{:?} {:?}", ms, why);
    }

    #[test]
    fn refused_is_a_failure_not_a_time() {
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port(); // closed again
        assert_eq!(connect_ms("127.0.0.1", port, 2.0), (None, Some("refused")));
    }

    #[test]
    fn timeout_is_reported_without_a_time() {
        // a listener whose accept queue is full drops the next SYN: the connect waits past the timeout
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let fd = std::os::fd::AsRawFd::as_raw_fd(&listener);
        assert_eq!(unsafe { libc::listen(fd, 0) }, 0);
        let mut held = Vec::new();
        for _ in 0..4 {
            if let Ok(c) = std::net::TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), std::time::Duration::from_millis(100)) {
                held.push(c);
            }
        }
        assert_eq!(connect_ms("127.0.0.1", port, 0.05), (None, Some("timeout")));
    }

    #[test]
    fn a_socket_that_cannot_be_made_is_an_error() {
        assert_eq!(connect_ms("not an address", 80, 0.1), (None, Some("error")));
        assert_eq!(reason(libc::EAFNOSUPPORT), "error"); // socket() refused, e.g. IPv6 unavailable
        assert_eq!((reason(libc::ECONNREFUSED), reason(libc::ENETUNREACH), reason(libc::EHOSTUNREACH), reason(libc::ETIMEDOUT)),
                   ("refused", "unreachable", "unreachable", "timeout"));
    }

    // tests/collector/test_battery.py, ported.
    use crate::collect::linux::sysfs::fake::Root;

    const CHARGE_BATTERY: &[(&str, &str)] = &[ // as read on an ACPI laptop: the charge family, no energy_* and no power_now
        ("type", "Battery"), ("present", "1"), ("capacity", "89"), ("status", "Not charging"), ("capacity_level", "Normal"),
        ("technology", "Li-ion"), ("cycle_count", "237"), ("charge_now", "3026000"), ("charge_full", "3400000"),
        ("charge_full_design", "4023000"), ("voltage_now", "16436000"), ("current_now", "0")];
    const ENERGY_BATTERY: &[(&str, &str)] = &[ // the other gauge family
        ("type", "Battery"), ("present", "1"), ("capacity", "54"), ("status", "Discharging"), ("energy_now", "27000000"),
        ("energy_full", "50000000"), ("energy_full_design", "52000000"), ("voltage_now", "11400000"),
        ("current_now", "-1200000"), ("power_now", "13680000")];

    fn supplies(root: &Root, list: &[(&str, &[(&str, &str)])]) {
        root.mkdir(POWER_SUPPLY);
        for (name, attrs) in list {
            root.mkdir(&format!("{}/{}", POWER_SUPPLY, name));
            for (attr, value) in *attrs {
                root.put(&format!("{}/{}/{}", POWER_SUPPLY, name, attr), value);
            }
        }
    }

    fn observe_battery() -> (Value, Value) {
        let mut g = Group::new("battery", false);
        let data = battery(&mut g);
        (data, g.status())
    }

    fn text(v: &Value, key: &str) -> String {
        v.get(key).unwrap().to_string(false)
    }

    fn first_battery(data: &Value) -> Value {
        data.get("batteries").unwrap().get_index(0).unwrap().clone()
    }

    const OK: &str = r#"{"state":"ok","reason":null,"issues":[],"issues_truncated":0}"#;

    #[test]
    fn charge_family_battery() {
        let root = Root::new(0);
        supplies(&root, &[("BAT1", CHARGE_BATTERY)]);
        let (data, status) = observe_battery();
        assert_eq!(status.to_string(false), OK);
        assert_eq!(data.to_string(false), concat!(r#"{"provider":"sysfs_power_supply","batteries":[{"name":"BAT1","present":true,"#,
            r#""percent":89,"status":"Not charging","capacity_level":"Normal","technology":"Li-ion","health":null,"#,
            r#""charge_now_uah":3026000,"charge_full_uah":3400000,"charge_full_design_uah":4023000,"energy_now_uwh":null,"#,
            r#""energy_full_uwh":null,"energy_full_design_uwh":null,"current_ua":0,"power_uw":null,"voltage_uv":16436000,"#,
            r#""cycle_count":237}],"external":[]}"#));
    }

    #[test]
    fn energy_family_battery_keeps_the_sign() {
        let root = Root::new(0);
        supplies(&root, &[("BAT0", ENERGY_BATTERY)]);
        let (data, status) = observe_battery();
        assert_eq!(text(&status, "state"), r#""ok""#);
        let b = first_battery(&data);
        let got: Vec<String> = ["energy_now_uwh", "energy_full_uwh", "power_uw", "current_ua", "charge_now_uah", "cycle_count"]
            .iter().map(|k| text(&b, k)).collect();
        assert_eq!(got, ["27000000", "50000000", "13680000", "-1200000", "null", "null"]);
    }

    #[test]
    fn an_attribute_the_driver_does_not_expose_is_not_a_problem() {
        let root = Root::new(0);
        supplies(&root, &[("BAT1", &[("type", "Battery"), ("present", "1"), ("capacity", "50")])]);
        let (data, status) = observe_battery();
        assert_eq!(status.to_string(false), OK);
        let b = first_battery(&data);
        assert_eq!((text(&b, "percent"), text(&b, "status"), text(&b, "voltage_uv")), ("50".into(), "null".into(), "null".into()));
    }

    #[test]
    fn no_supply_is_unavailable_not_an_error() {
        for empty in [true, false] {
            let root = Root::new(0);
            if empty {
                root.mkdir(POWER_SUPPLY);
            }
            let (data, status) = observe_battery();
            assert_eq!((text(&status, "state"), text(&status, "reason")), (r#""unavailable""#.into(), r#""not_detected""#.into()));
            assert_eq!((text(&data, "batteries"), text(&data, "external")), ("[]".into(), "[]".into()));
        }
    }

    #[test]
    fn empty_bay_reports_absent_without_inventing_numbers() {
        let root = Root::new(0);
        supplies(&root, &[("BAT2", &[("type", "Battery"), ("present", "0"), ("capacity", "0"), ("status", "Unknown"), ("charge_now", "0")])]);
        let (data, status) = observe_battery();
        assert_eq!(text(&status, "state"), r#""ok""#);
        assert_eq!(first_battery(&data).to_string(false), concat!(r#"{"name":"BAT2","present":false,"percent":null,"status":null,"#,
            r#""capacity_level":null,"technology":null,"health":null,"charge_now_uah":null,"charge_full_uah":null,"#,
            r#""charge_full_design_uah":null,"energy_now_uwh":null,"energy_full_uwh":null,"energy_full_design_uwh":null,"#,
            r#""current_ua":null,"power_uw":null,"voltage_uv":null,"cycle_count":null}"#));
    }

    #[test]
    fn external_supplies_and_their_order() {
        let root = Root::new(0);
        supplies(&root, &[("ADP1", &[("type", "Mains"), ("online", "1")]), ("ucsi-source-psy-USBC000:002", &[("type", "USB"), ("online", "0")]),
                          ("BAT1", CHARGE_BATTERY)]);
        let (data, status) = observe_battery();
        assert_eq!(text(&status, "state"), r#""ok""#);
        assert_eq!(text(&data, "external"),
                   r#"[{"name":"ADP1","type":"Mains","online":true},{"name":"ucsi-source-psy-USBC000:002","type":"USB","online":false}]"#);
        assert_eq!(text(&first_battery(&data), "name"), r#""BAT1""#);
        assert!(data.get("batteries").unwrap().get_index(1).is_none());
    }

    #[test]
    fn a_ups_holds_charge_like_a_battery() {
        let root = Root::new(0);
        supplies(&root, &[("hidev0", &[("type", "UPS"), ("present", "1"), ("capacity", "100"), ("status", "Full")]),
                          ("ADP1", &[("type", "Mains"), ("online", "1")]), ("wl0", &[("type", "Wireless"), ("online", "0")])]);
        let (data, status) = observe_battery();
        assert_eq!(text(&status, "state"), r#""ok""#);
        let b = first_battery(&data);
        assert_eq!((text(&b, "name"), text(&b, "percent"), text(&b, "status")), (r#""hidev0""#.into(), "100".into(), r#""Full""#.into()));
        assert!(data.get("batteries").unwrap().get_index(1).is_none());
        assert_eq!(text(&data, "external"), r#"[{"name":"ADP1","type":"Mains","online":true},{"name":"wl0","type":"Wireless","online":false}]"#);
    }

    #[test]
    fn a_supply_without_a_type_is_left_out() {
        let root = Root::new(0);
        supplies(&root, &[("weird", &[("online", "1")])]);
        let (data, status) = observe_battery();
        assert_eq!((text(&data, "batteries"), text(&data, "external")), ("[]".into(), "[]".into()));
        assert_eq!((text(&status, "state"), text(&status, "reason")), (r#""unavailable""#.into(), r#""not_exposed""#.into()));
    }

    #[test]
    fn a_value_that_is_not_one_is_invalid_data() {
        let cases: [(&[(&str, &str)], &str); 5] = [
            (&[("type", "Battery"), ("present", "1"), ("capacity", "150")], "BAT1.capacity"), // out of 0..100
            (&[("type", "Battery"), ("present", "1"), ("capacity", ""), ("voltage_now", "1")], "BAT1.capacity"),
            (&[("type", "Battery"), ("present", "1"), ("voltage_now", "-5")], "BAT1.voltage_now"), // not a voltage
            (&[("type", "Battery"), ("present", "yes")], "BAT1.present"), // not a flag
            (&[("type", "Mains"), ("online", "2")], "BAT1.online")];
        for (attrs, target) in cases {
            let root = Root::new(0);
            supplies(&root, &[("BAT1", attrs)]);
            let (_, status) = observe_battery();
            assert!(["\"partial\"", "\"error\""].contains(&text(&status, "state").as_str()), "{}", target);
            assert!(["\"some_unreadable\"", "\"invalid_data\""].contains(&text(&status, "reason").as_str()), "{}", target);
            assert_eq!(text(&status, "issues"), format!(r#"[{{"target":"{}","reason":"invalid_data"}}]"#, target));
        }
    }

    #[test]
    fn an_over_long_enum_is_not_one() {
        let root = Root::new(0);
        let long = "x".repeat(33);
        supplies(&root, &[("BAT1", &[("type", "Battery"), ("present", "1"), ("capacity", "50"), ("status", &long)])]);
        let (data, status) = observe_battery();
        assert_eq!(text(&first_battery(&data), "status"), "null");
        assert_eq!(status.to_string(false),
                   r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"BAT1.status","reason":"invalid_data"}],"issues_truncated":0}"#);
    }

    #[test]
    fn an_unreadable_attribute_is_reported_and_the_rest_kept() {
        if unsafe { libc::geteuid() } == 0 {
            return; // root reads a file of mode 000
        }
        let root = Root::new(0);
        supplies(&root, &[("BAT1", CHARGE_BATTERY)]);
        root.chmod("/sys/class/power_supply/BAT1/capacity", 0);
        let (data, status) = observe_battery();
        assert_eq!((text(&status, "state"), text(&status, "reason")), (r#""partial""#.into(), r#""some_unreadable""#.into()));
        let b = first_battery(&data);
        assert_eq!((text(&b, "percent"), text(&b, "cycle_count")), ("null".into(), "237".into()));
    }

    #[test]
    fn the_host_class_reads() {
        if !std::path::Path::new(POWER_SUPPLY).is_dir() {
            return; // no power supply on this host
        }
        let (data, status) = observe_battery();
        assert!(matches!(&data, Value::Obj(items) if items.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>() == ["provider", "batteries", "external"]));
        assert!(["\"ok\"", "\"unavailable\""].contains(&text(&status, "state").as_str())); // a real host: never partial or error
        if let Some(Value::Arr(bs)) = data.get("batteries") {
            for b in bs {
                assert!(["true", "false", "null"].contains(&text(b, "present").as_str()));
                assert!(matches!(b.get("percent"), Some(Value::Null) | Some(Value::Int(0..=100))));
            }
        }
        if let Some(Value::Arr(es)) = data.get("external") {
            for e in es {
                assert!(matches!(e, Value::Obj(items) if items.len() == 3) && ["true", "false", "null"].contains(&text(e, "online").as_str()));
            }
        }
    }

    // tests/collector/test_wifi.py, ported. The interface is "lo", which every network namespace has with
    // ifindex 1, so the identity read from the real namespace is known; its files are in the fake tree.
    const HEAD: &str = "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n \
                        face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n";
    const ORIN: &str = "wlP1p1s0: 0000   60.  -50.  -256        0      0      0      0      0        0\n"; // as read on the boards
    const PI: &str = " wlan0: 0000   46.  -64.  -256        0      0      0    312      0        0\n";
    const DOWN: &str = " wlan1: 0000    0     0     0        0      0      0      0      0        0\n"; // listed, not associated
    const RAW: &str = "  eth9: 0000   70.   70.    0        0      0      0      0      0        0\n"; // a driver without dBm
    const NOISE: &str = " wlan2: 0000   50.  -55.  -95.       0      0      0      0      0        0\n";

    fn rows(text: &str) -> (String, Vec<(String, &'static str)>) {
        let (rows, bad) = parse_wireless(text).ok().unwrap();
        (Value::Arr(rows).to_string(false), bad)
    }

    /// /proc/net/wireless with PI's row as "lo", the namespace file, and lo's ifindex and carrier.
    fn lo_tree(root: &Root, wireless: &str, carrier: Option<&str>) {
        root.put(WIRELESS, wireless.replace("wlan0:", "lo:"));
        root.put(NETNS, "");
        root.put("/sys/class/net/lo/ifindex", "1\n");
        if let Some(c) = carrier {
            root.put("/sys/class/net/lo/carrier", c);
        }
    }

    fn observe_wifi() -> (Value, Value) {
        let mut g = Group::new("wifi", false);
        let out = wifi(&mut g);
        (out, g.status())
    }

    fn lo(out: &Value) -> Value {
        out.get("interfaces").unwrap().get_index(0).unwrap().clone()
    }

    #[test]
    fn connected_rows_from_the_boards() {
        assert_eq!(rows(&format!("{}{}{}", HEAD, ORIN, PI)), (concat!(
            r#"[{"name":"wlP1p1s0","connected":null,"connection_state":"unknown","signal_dbm":-50,"signal_raw":null,"link_quality":60,"noise_dbm":null},"#,
            r#"{"name":"wlan0","connected":null,"connection_state":"unknown","signal_dbm":-64,"signal_raw":null,"link_quality":46,"noise_dbm":null}]"#).into(), vec![]));
    }

    #[test]
    fn unupdated_signal_does_not_determine_connection() {
        assert_eq!(rows(&format!("{}{}", HEAD, DOWN)).0, // never a last value, never 0
                   r#"[{"name":"wlan1","connected":null,"connection_state":"unknown","signal_dbm":null,"signal_raw":null,"link_quality":null,"noise_dbm":null}]"#);
    }

    #[test]
    fn a_value_without_dbm_is_not_called_dbm() {
        assert!(rows(&format!("{}{}", HEAD, RAW)).0.contains(r#""signal_dbm":null,"signal_raw":70"#));
    }

    #[test]
    fn noise_when_the_driver_gives_it() {
        assert!(rows(&format!("{}{}", HEAD, NOISE)).0.contains(r#""noise_dbm":-95"#));
        assert!(rows(&format!("{}{}", HEAD, ORIN)).0.contains(r#""noise_dbm":null"#)); // -256: n/a
    }

    #[test]
    fn bad_rows_and_files() {
        let (text, bad) = rows(&format!("{}{}garbage\na/b: 0000 1. 2. 3. 0 0 0 0 0 0\n", HEAD, ORIN));
        assert!(text.starts_with(r#"[{"name":"wlP1p1s0""#) && !text.contains("},{"));
        assert_eq!(bad.iter().map(|(t, _)| t.as_str()).collect::<Vec<_>>(), ["line4", "line5"]);
        assert!(parse_wireless("not the header\n").is_err());
    }

    #[test]
    fn raw_zero_and_invalid_signal_range() {
        let (text, bad) = rows(&format!("{}{}", HEAD, RAW.replace("70.   70.", "70.    0.")));
        assert!(bad.is_empty() && text.contains(r#""signal_dbm":null,"signal_raw":0"#));
        assert_eq!(rows(&format!("{}{}", HEAD, PI.replace("-64.", "-300."))).1, [("wlan0".to_string(), "invalid_data")]);
    }

    #[test]
    fn observation_states() {
        for (case, state, reason) in [("rows", "ok", "null"), ("header", "unavailable", r#""not_detected""#),
                                      ("missing", "unavailable", r#""not_exposed""#), ("denied", "error", r#""permission_denied""#),
                                      ("junk", "error", r#""invalid_data""#)] {
            if case == "denied" && unsafe { libc::geteuid() } == 0 {
                continue; // root reads a file of mode 000
            }
            let root = Root::new(0);
            lo_tree(&root, &format!("{}{}", HEAD, PI), Some("1\n"));
            match case {
                "header" => root.put(WIRELESS, HEAD),
                "missing" => root.remove(WIRELESS),
                "denied" => root.chmod(WIRELESS, 0),
                "junk" => root.put(WIRELESS, "junk"),
                _ => {}
            }
            let (out, status) = observe_wifi();
            assert_eq!((status.get("state").unwrap().to_string(false), status.get("reason").unwrap().to_string(false)),
                       (format!("{:?}", state), reason.to_string()), "{}", case);
            assert_eq!(out.get("provider").unwrap().to_string(false), r#""proc_net_wireless""#);
            assert_eq!(out.get("scope").unwrap().get("kind").unwrap().to_string(false), r#""process_network_namespace""#);
        }
    }

    #[test]
    fn observation_exports_known_interface_identity() {
        use std::os::unix::fs::MetadataExt;
        let root = Root::new(0);
        lo_tree(&root, &format!("{}{}", HEAD, PI), Some("1\n"));
        let (out, _) = observe_wifi();
        let st = std::fs::metadata(root.path(NETNS)).unwrap();
        assert_eq!(out.get("scope").unwrap().get("id").unwrap().to_string(false), format!("{:?}", namespace_id(st.dev(), st.ino())));
        assert_eq!(lo(&out).get("ifindex").unwrap().to_string(false), "1");
    }

    #[test]
    fn unavailable_identity_is_not_invented() {
        if unsafe { libc::geteuid() } == 0 {
            return; // root passes a directory of mode 000
        }
        let root = Root::new(0);
        lo_tree(&root, &format!("{}{}", HEAD, PI), Some("1\n"));
        root.chmod("/proc/self/ns", 0); // the namespace cannot be looked up
        let (out, status) = observe_wifi();
        root.chmod("/proc/self/ns", 0o755);
        assert_eq!((out.get("scope").unwrap().get("id").unwrap().to_string(false), lo(&out).get("ifindex").unwrap().to_string(false)),
                   ("null".into(), "null".into()));
        assert_eq!(status.get("state").unwrap().to_string(false), r#""partial""#);
    }

    #[test]
    fn signal_not_updated_does_not_claim_disconnection() {
        let root = Root::new(0);
        lo_tree(&root, &format!("{}{}", HEAD, PI.replace("-64.", "-64 ")), Some("1\n"));
        let item = lo(&observe_wifi().0);
        assert_eq!((item.get("connected").unwrap().to_string(false), item.get("signal_dbm").unwrap().to_string(false)),
                   ("true".into(), "null".into()));
    }

    #[test]
    fn connection_states_are_not_inferred_from_signal() {
        // Python's OSError(5) is EINVAL here with IFF_UP set: a carrier read error that is not a down link
        for (case, state, connected) in [("1\n", "connected", "true"), ("0\n", "disconnected", "false"), ("moved", "unknown", "null"),
                                         ("denied", "permission_denied", "null"), ("missing", "unsupported", "null")] {
            if case == "denied" && unsafe { libc::geteuid() } == 0 {
                continue;
            }
            let root = Root::new(0);
            lo_tree(&root, &format!("{}{}", HEAD, PI), Some("1\n"));
            match case {
                "moved" => root.put("/sys/class/net/lo/ifindex", "4\n"),
                "denied" => root.chmod("/sys/class/net/lo/carrier", 0),
                "missing" => root.remove("/sys/class/net/lo/carrier"),
                c => root.put("/sys/class/net/lo/carrier", c),
            }
            let (out, status) = observe_wifi();
            let item = lo(&out);
            assert_eq!((item.get("connection_state").unwrap().to_string(false), item.get("connected").unwrap().to_string(false)),
                       (format!("{:?}", state), connected.to_string()), "{}", case);
            assert_eq!(item.get("signal_dbm").unwrap().to_string(false), if connected == "true" { "-64" } else { "null" }, "{}", case);
            assert!(!(out.to_string(false) + &status.to_string(false)).contains("/sys"), "{}", case); // no private path
        }
    }

    #[test]
    fn carrier_and_index_from_fake_sysfs() {
        let root = Root::new(0);
        root.put("/sys/class/net/wlan0/ifindex", "3\n");
        root.put("/sys/class/net/wlan0/carrier", "1\n");
        assert!(matches!(link_connected("wlan0", 3), Ok(Some(true))));
        root.put("/sys/class/net/wlan0/carrier", "0\n");
        assert!(matches!(link_connected("wlan0", 3), Ok(Some(false))));
        assert!(matches!(link_connected("wlan0", 4), Ok(None)));
        root.put("/sys/class/net/wlan0/carrier", "2\n");
        assert!(matches!(link_connected("wlan0", 3), Err(LinkError::Invalid)));
        if unsafe { libc::geteuid() } != 0 { // carrier errors are not all a down link: EACCES stays an error
            root.chmod("/sys/class/net/wlan0/carrier", 0);
            assert!(matches!(link_connected("wlan0", 3), Err(LinkError::Os(e)) if e.raw_os_error() == Some(libc::EACCES)));
        }
    }

    #[test]
    fn real_host_file() {
        let (out, status) = observe_wifi();
        assert!(["\"ok\"", "\"unavailable\"", "\"partial\""].contains(&status.get("state").unwrap().to_string(false).as_str()));
        if let Some(Value::Arr(items)) = out.get("interfaces") {
            for i in items {
                assert!(matches!(i.get("signal_dbm"), Some(Value::Null) | Some(Value::Int(i64::MIN..=-1))));
            }
        }
    }

    #[test]
    fn interface_replaced_during_carrier_read_is_unknown() {
        let root = Root::new(0);
        crate::collect::linux::sysfs::fake::answers("/sys/class/net/wlan0/ifindex", vec![Ok("3"), Ok("4")]);
        root.put("/sys/class/net/wlan0/carrier", "1");
        assert!(matches!(link_connected("wlan0", 3), Ok(None)));
    }

    #[test]
    fn down_carrier_is_disconnected_without_issue() {
        // Linux refuses a carrier read on a down interface (EINVAL); IFF_UP clear confirms it is down
        for (flags, after, connected, state) in [("0x0", "1", "false", "disconnected"), ("0x1000", "1", "false", "disconnected"),
                                                 ("0x0", "4", "null", "unknown")] {
            let root = Root::new(0);
            lo_tree(&root, &format!("{}{}", HEAD, PI), None);
            root.put("/sys/class/net/lo/flags", flags);
            crate::collect::linux::sysfs::fake::answers("/sys/class/net/lo/ifindex", vec![Ok("1"), Ok(after)]);
            crate::collect::linux::sysfs::fake::answers("/sys/class/net/lo/carrier", vec![Err(libc::EINVAL)]);
            let (out, status) = observe_wifi();
            let item = lo(&out);
            assert_eq!((item.get("connected").unwrap().to_string(false), item.get("connection_state").unwrap().to_string(false)),
                       (connected.to_string(), format!("{:?}", state)), "{} {}", flags, after);
            assert_eq!(item.get("signal_dbm").unwrap().to_string(false), "null");
            assert_eq!(status.to_string(false), r#"{"state":"ok","reason":null,"issues":[],"issues_truncated":0}"#);
        }
    }

    #[test]
    fn carrier_errors_are_not_all_treated_as_down() {
        for (errno, flags) in [(libc::EINVAL, "0x1"), (libc::EIO, "0x0"), (libc::EACCES, "0x0")] {
            let root = Root::new(0);
            root.put("/sys/class/net/wlan0/ifindex", "3");
            root.put("/sys/class/net/wlan0/flags", flags);
            crate::collect::linux::sysfs::fake::answers("/sys/class/net/wlan0/carrier", vec![Err(errno)]);
            assert!(matches!(link_connected("wlan0", 3), Err(LinkError::Os(e)) if e.raw_os_error() == Some(errno)), "{}", errno);
        }
    }

    #[test]
    fn a_carrier_read_error_that_is_not_a_down_link_is_unavailable() {
        // test_connection_states_are_not_inferred_from_signal's OSError(5)
        let root = Root::new(0);
        lo_tree(&root, &format!("{}{}", HEAD, PI), None);
        crate::collect::linux::sysfs::fake::answers("/sys/class/net/lo/carrier", vec![Err(libc::EIO)]);
        let (out, status) = observe_wifi();
        let item = lo(&out);
        assert_eq!((item.get("connection_state").unwrap().to_string(false), item.get("connected").unwrap().to_string(false)),
                   (r#""unavailable""#.into(), "null".into()));
        assert!(!(out.to_string(false) + &status.to_string(false)).contains("/sys"));
    }
}
