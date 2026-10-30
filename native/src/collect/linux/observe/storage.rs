//! The storage slow group: local filesystems of physical disks, by device number, with statvfs through a
//! pinned descriptor. Contract: docs/reference/storage.md.
use crate::json::Value;
use crate::metadata::{py_int, Group};
use crate::pystr::{py_space, py_split};
use crate::collect::linux::passive::{is_physical, strerror};
use crate::collect::linux::sysfs;
use super::{obj, os_error};

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

// tests/collector/test_storage.py, ported. Capacity is the kernel's statvfs of the test's own tree, so the
// tree's device number stands in for the partition's (as fixture.py's slow scenario does).
#[cfg(test)]
mod tests {
    use super::*;
    use crate::collect::linux::sysfs::fake::{self, Root};

    fn mi(major_minor: &str, path: &str, fstype: &str, source: &str, opts: &str, optional: &str, super_opts: &str) -> String {
        format!("25 1 {} / {} {} {} - {} {} {}", major_minor, path, opts, optional, fstype, source, super_opts)
    }

    fn mounts_text() -> String {
        [mi("259:1", "/", "ext4", "/dev/nvme0n1p1", "rw,relatime", "shared:1", "rw"),
         mi("259:1", "/srv/bind\\040dir", "ext4", "/dev/nvme0n1p1", "ro,relatime", "shared:2 master:1", "rw"), // a bind mount
         mi("0:25", "/dev/shm", "tmpfs", "tmpfs", "rw,relatime", "shared:1", "rw"),
         mi("7:0", "/snap/core/1", "squashfs", "/dev/loop0", "rw,relatime", "", "rw"),
         mi("0:50", "/mnt/nfs", "nfs4", "server:/export", "rw,relatime", "shared:1", "rw"),
         mi("0:51", "/mnt/fuse", "fuse.sshfs", "user@host:", "rw,relatime", "shared:1", "rw"),
         mi("0:30", "/", "overlay", "overlay", "rw,relatime", "shared:1", "rw")].join("\n") + "\n"
    }

    fn mount_tuple(m: &Mount) -> (i64, i64, &str, bool, &str, &str) {
        (m.major, m.minor, m.path.as_str(), m.read_only, m.fstype.as_str(), m.source.as_str())
    }

    #[test]
    fn parse_mountinfo_escapes_optional_fields_and_bad_lines() {
        let (mounts, bad) = parse_mountinfo(&(mounts_text() + "garbage line\n26 1 x:y / /a rw - ext4 /dev/a rw\n"));
        // only the local filesystem types are kept (Python's parse_mountinfo(text, LOCAL_FS))
        assert_eq!(mounts.iter().map(mount_tuple).collect::<Vec<_>>(),
                   [(259, 1, "/", false, "ext4", "/dev/nvme0n1p1"), (259, 1, "/srv/bind dir", false, "ext4", "/dev/nvme0n1p1")]); // ro bind of an rw filesystem
        assert_eq!(bad, ["line8", "line9"]);
        // a broken line of a type that is never read is not reported (test_only_local_filesystem_lines_are_converted)
        assert!(parse_mountinfo(&(mounts_text() + "26 1 x:y / /odd rw - tmpfs tmpfs rw\n")).1.is_empty());
        assert_eq!(unescape("a\\040b\\011c\\012d\\134e"), "a b\tc\nd\\e");
    }

    #[test]
    fn read_only_is_the_filesystem_not_this_process_mount() {
        // a sandboxed service sees the host's writable filesystems as read-only mounts in its own namespace
        let ro = |text: &str| parse_mountinfo(text).0.first().map(|m| m.read_only);
        assert_eq!(ro(&mi("259:1", "/", "ext4", "/dev/nvme0n1p1", "ro,relatime", "shared:1", "rw")), Some(false));
        assert_eq!(ro(&mi("259:1", "/", "ext4", "/dev/nvme0n1p1", "ro,relatime", "shared:1", "ro")), Some(true));
        assert_eq!(ro(&mi("259:1", "/", "ext4", "/dev/nvme0n1p1", "rw,relatime", "shared:1", "ro,noatime")), Some(true));
        let missing = "25 1 259:1 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p1"; // no super-options: not a mountinfo line
        assert_eq!(parse_mountinfo(missing).1, ["line1"]);
        assert!(parse_mountinfo(missing).0.is_empty());
    }

    #[test]
    fn parse_partitions_rows() {
        let parts = "major minor  #blocks  name\n\n 259        0  976762584 nvme0n1\n 259        1  975237540 nvme0n1p1\n 259        2     131072 nvme0n1p2\n";
        assert!(parse_partitions(parts).contains(&((259, 2), ("nvme0n1p2".to_string(), 131072 * 1024))));
    }

    /// nvme0n1 with partitions p1 (the tree's own device number, mounted at /mnt/root and, read-only bound,
    /// at "/mnt/bind dir") and p2 (not mounted), as /proc/partitions, /sys/class/block and /sys/block show them.
    fn storage_tree(root: &Root) -> (i64, i64) {
        use std::os::unix::fs::MetadataExt;
        let dev = std::fs::metadata(&root.dir).unwrap().dev();
        let (a, b) = (libc::major(dev) as i64, libc::minor(dev) as i64);
        root.put(PARTITIONS, format!("major minor  #blocks  name\n\n 259        0  976762584 nvme0n1\n {} {}  975237540 nvme0n1p1\n \
                                     259        2     131072 nvme0n1p2\n   7        0     100000 loop0\n", a, b));
        let disk = "devices/pci0000:00/0000:00:1d.0/nvme/nvme0/nvme0n1";
        for p in ["nvme0n1p1", "nvme0n1p2"] {
            root.put(&format!("/sys/{}/{}/partition", disk, p), "1\n");
            root.link(&format!("/sys/class/block/{}", p), &format!("../../{}/{}", disk, p));
        }
        root.link("/sys/block/nvme0n1", &format!("../{}", disk));
        root.mkdir("/mnt/root");
        root.mkdir("/mnt/bind dir");
        root.put(MOUNTINFO, [mi(&format!("{}:{}", a, b), "/mnt/root", "ext4", "/dev/nvme0n1p1", "rw,relatime", "shared:1", "rw"),
                             mi(&format!("{}:{}", a, b), "/mnt/bind\\040dir", "ext4", "/dev/nvme0n1p1", "ro,relatime", "shared:2 master:1", "rw"),
                             mi("0:25", "/dev/shm", "tmpfs", "tmpfs", "rw", "", "rw"), mi("0:50", "/mnt/nfs", "nfs4", "server:/export", "rw", "", "rw")]
                 .join("\n") + "\n");
        (a, b)
    }

    fn observe_storage() -> (Value, Value) {
        let mut g = Group::new("storage", false);
        let out = storage(&mut g);
        (out, g.status())
    }

    #[test]
    fn local_block_filesystems_once_each_with_all_mount_points() {
        let root = Root::new(0);
        let (a, b) = storage_tree(&root);
        let (out, status) = observe_storage();
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        let c = std::ffi::CString::new(root.path("/mnt/root").to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::statvfs(c.as_ptr(), &mut st) }, 0);
        let mut fs = out.get("filesystems").unwrap().clone();
        let mut numbers = Vec::new();
        for k in ["total_bytes", "used_bytes", "available_bytes"] { // the tree's filesystem is in use: used and free move
            let Value::Arr(items) = &mut fs else { panic!() };
            numbers.push(std::mem::replace(items[0].get_mut(k).unwrap(), Value::Null));
        }
        assert_eq!(fs.to_string(false), format!(
            r#"[{{"device":"nvme0n1p1","major":{},"minor":{},"fstype":"ext4","source":"/dev/nvme0n1p1","mount_points":[{{"path":"/mnt/root","read_only":false}},{{"path":"/mnt/bind dir","read_only":false}}],"total_bytes":null,"used_bytes":null,"available_bytes":null}}]"#,
            a, b)); // nfs and tmpfs are never statvfs'ed
        assert_eq!(numbers[0], Value::Big(st.f_blocks as u128 * st.f_frsize as u128));
        assert!(numbers[1..].iter().all(|n| matches!(n, Value::Big(_))));
        assert_eq!(out.get("partitions").unwrap().to_string(false), format!(
            r#"[{{"name":"nvme0n1p1","disk":"nvme0n1","major":{},"minor":{},"size_bytes":{},"mount_points":["/mnt/root","/mnt/bind dir"]}},{{"name":"nvme0n1p2","disk":"nvme0n1","major":259,"minor":2,"size_bytes":{},"mount_points":[]}}]"#,
            a, b, 975237540i64 * 1024, 131072 * 1024)); // not mounted: a size, no capacity made up
        assert_eq!(status.get("state").unwrap().to_string(false), r#""ok""#);
    }

    #[test]
    fn statvfs_failure_keeps_the_entry_without_numbers() {
        for (case, reason) in [("gone", "disappeared"), ("denied", "permission_denied"), ("replaced", "mount_changed")] {
            if case == "denied" && unsafe { libc::geteuid() } == 0 {
                continue; // root passes a directory of mode 000
            }
            let root = Root::new(0);
            let (a, b) = storage_tree(&root);
            match case {
                "gone" => { root.remove("/mnt/root"); root.remove("/mnt/bind dir"); }
                "denied" => root.chmod("/mnt", 0),
                // another filesystem mounted there meanwhile: never queried as the old one
                _ => root.put(PARTITIONS, format!("major minor  #blocks  name\n\n {} {}  975237540 nvme0n1p1\n", a, b + 1)),
            }
            if case == "replaced" {
                let text = std::fs::read_to_string(root.path(MOUNTINFO)).unwrap().replace(&format!("{}:{}", a, b), &format!("{}:{}", a, b + 1));
                root.put(MOUNTINFO, text);
            }
            let (out, status) = observe_storage();
            root.chmod("/mnt", 0o755);
            let fs = out.get("filesystems").unwrap().get_index(0).unwrap();
            assert_eq!(["total_bytes", "used_bytes", "available_bytes"].map(|k| fs.get(k).unwrap().to_string(false)), ["null"; 3], "{}", case);
            assert_eq!(status.get("state").unwrap().to_string(false), r#""partial""#, "{}", case);
            assert_eq!(status.get("issues").unwrap().to_string(false), format!(r#"[{{"target":"nvme0n1p1","reason":"{}"}}]"#, reason), "{}", case);
        }
    }

    #[test]
    fn unreadable_mountinfo_or_partitions() {
        let root = Root::new(0);
        storage_tree(&root);
        fake::answers(MOUNTINFO, vec![Err(libc::EACCES)]);
        let (out, status) = observe_storage();
        assert_eq!(out.get("filesystems").unwrap().to_string(false), "[]");
        assert!(out.get("partitions").unwrap().get_index(1).is_some() && status.get("state").unwrap().to_string(false) == r#""partial""#);
        fake::answers(MOUNTINFO, vec![Err(libc::EACCES)]);
        root.remove(PARTITIONS);
        let (out, status) = observe_storage();
        assert_eq!(out.to_string(false), r#"{"partitions":[],"filesystems":[]}"#);
        assert_eq!(status.get("state").unwrap().to_string(false), r#""error""#);
    }

    #[test]
    fn nothing_found_is_unavailable() {
        let root = Root::new(0);
        root.put(PARTITIONS, "major minor  #blocks  name\n\n");
        root.put(MOUNTINFO, "");
        let (out, status) = observe_storage();
        assert_eq!(out.to_string(false), r#"{"partitions":[],"filesystems":[]}"#);
        assert_eq!(status.to_string(false), r#"{"state":"unavailable","reason":"not_detected","issues":[],"issues_truncated":0}"#);
    }

    #[test]
    fn a_device_number_without_a_name_is_kept() {
        use std::os::unix::fs::MetadataExt;
        let root = Root::new(0);
        root.put(PARTITIONS, "major minor  #blocks  name\n\n");
        root.mkdir("/data");
        let dev = std::fs::metadata(&root.dir).unwrap().dev();
        // btrfs: an anonymous device number that /proc/partitions does not list
        root.put(MOUNTINFO, mi(&format!("{}:{}", libc::major(dev), libc::minor(dev)), "/data", "btrfs", "/dev/sdb", "rw", "", "rw") + "\n");
        let (out, _) = observe_storage();
        let fs = out.get("filesystems").unwrap().get_index(0).unwrap();
        assert!(fs.get("device").unwrap().is_null() && !fs.get("total_bytes").unwrap().is_null());
    }

    #[test]
    fn real_host_observation() {
        let (out, status) = observe_storage();
        assert!(["\"ok\"", "\"partial\"", "\"unavailable\""].contains(&status.get("state").unwrap().to_string(false).as_str()));
        if let Some(Value::Arr(fs)) = out.get("filesystems") {
            for f in fs {
                assert!(LOCAL_FS.iter().any(|t| f.get("fstype").unwrap().to_string(false) == format!("{:?}", t)));
            }
        }
    }

    fn tree_dev(root: &Root) -> (i64, i64) {
        use std::os::unix::fs::MetadataExt;
        let dev = std::fs::metadata(&root.dir).unwrap().dev();
        (libc::major(dev) as i64, libc::minor(dev) as i64)
    }

    #[test]
    fn storage_skips_file_binds_before_a_directory() {
        for files in [vec!["/usr/sbin/docker-init"], vec!["/etc/hostname", "/etc/hosts"]] {
            let root = Root::new(0);
            let (a, b) = tree_dev(&root);
            root.put(PARTITIONS, format!("major minor  #blocks  name\n\n {} {}  975237540 nvme0n1p1\n", a, b));
            for f in &files {
                root.put(f, "x");
            }
            root.mkdir("/host/root");
            root.mkdir("/another-dir");
            let paths: Vec<&str> = files.iter().copied().chain(["/host/root", "/another-dir"]).collect();
            root.put(MOUNTINFO, paths.iter().map(|p| mi(&format!("{}:{}", a, b), p, "ext4", "/dev/nvme0n1p1", "rw", "", "rw")).collect::<Vec<_>>().join("\n"));
            let (out, status) = observe_storage();
            let fs = out.get("filesystems").unwrap().get_index(0).unwrap();
            assert!(matches!(fs.get("total_bytes"), Some(Value::Big(_))));
            assert_eq!(status.to_string(false), r#"{"state":"ok","reason":null,"issues":[],"issues_truncated":0}"#);
            let points: Vec<String> = match fs.get("mount_points") { Some(Value::Arr(m)) => m.iter().map(|p| p.get("path").unwrap().to_string(false)).collect(), _ => vec![] };
            assert_eq!(points, paths.iter().map(|p| format!("{:?}", p)).collect::<Vec<_>>());
        }
    }

    #[test]
    fn only_file_binds_keep_unknown_capacity() {
        let root = Root::new(0);
        let (a, b) = storage_tree(&root); // the partitions are listed, as in Python's test
        root.put("/file", "x");
        root.put(MOUNTINFO, mi(&format!("{}:{}", a, b), "/file", "ext4", "/dev/nvme0n1p1", "rw", "", "rw"));
        let (out, status) = observe_storage();
        assert!(out.get("filesystems").unwrap().get_index(0).unwrap().get("total_bytes").unwrap().is_null());
        assert_eq!(status.to_string(false), r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"nvme0n1p1","reason":"io_error"}],"issues_truncated":0}"#);
    }

    #[test]
    fn capacity_does_not_require_directory_read_permission() {
        if unsafe { libc::geteuid() } == 0 {
            return; // requires unprivileged permission enforcement
        }
        let root = Root::new(0);
        root.mkdir("/mount");
        let dev = tree_dev(&root);
        root.chmod("/mount", 0);
        let got = capacity("/mount", dev);
        root.chmod("/mount", 0o700);
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        let c = std::ffi::CString::new(root.path("/mount").to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::statvfs(c.as_ptr(), &mut st) }, 0);
        let (blocks, _, _, frsize) = got.unwrap();
        assert_eq!((blocks, frsize), (st.f_blocks as u64, st.f_frsize as u64));
    }

    #[test]
    fn capacity_rejects_symlinks_and_regular_files_and_other_filesystems() {
        let root = Root::new(0);
        let dev = tree_dev(&root);
        root.put("/file", "x");
        root.link("/link", root.dir.to_str().unwrap());
        assert!(capacity("/file", dev).is_err() && capacity("/link", dev).is_err());
        root.mkdir("/dir");
        assert!(capacity("/dir", dev).is_ok());
        assert_eq!(capacity("/dir", (dev.0, dev.1 + 1)).err().unwrap().raw_os_error(), Some(libc::ESTALE)); // never queried as the old one
    }
}
