//! collector/sysfs.py: reading /proc and /sys, listings, hwmon chips and resolved source identities.
use crate::json::Value;
use crate::metadata::Group;
use std::fs;
use std::io;
use std::path::Path;

pub fn device_tree() -> String {
    std::env::var("PLATMON_DEVICE_TREE").unwrap_or_else(|_| "/proc/device-tree".into())
}

pub fn host_root() -> String {
    std::env::var("PLATMON_HOST_ROOT").unwrap_or_else(|_| "/".into())
}

/// path on the host's root filesystem (os.path.join(HOST_ROOT, path.lstrip("/"))).
pub fn host_path(path: &str) -> String {
    let root = host_root();
    let rel = path.trim_start_matches('/');
    if root.ends_with('/') { format!("{}{}", root, rel) } else { format!("{}/{}", root, rel) }
}

pub enum ReadError {
    Os(io::Error),
    Utf8,
}

fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

// Raw system calls: musl's open() adds an fcntl(F_SETFD) after every O_CLOEXEC open, and a fresh buffer per
// file makes its allocator map and unmap pages every collection. Measured on x86 (strace -c): 4233 fcntl and
// ~1500 mmap/munmap pairs in 23 collections before this; see native/README.md.
#[cfg(target_arch = "x86_64")]
mod nr { pub const OPENAT: i64 = 257; pub const READ: i64 = 0; pub const CLOSE: i64 = 3; pub const READLINKAT: i64 = 267; }
#[cfg(target_arch = "aarch64")]
mod nr { pub const OPENAT: i64 = 56; pub const READ: i64 = 63; pub const CLOSE: i64 = 57; pub const READLINKAT: i64 = 78; }
const AT_FDCWD: i64 = -100;
const O_CLOEXEC: i64 = 0o2000000;
extern "C" {
    fn syscall(number: i64, ...) -> i64;
}

fn sys_open(path: &str, flags: i64) -> io::Result<i64> {
    let c = std::ffi::CString::new(path).map_err(|_| io::Error::from_raw_os_error(22))?;
    let fd = unsafe { syscall(nr::OPENAT, AT_FDCWD, c.as_ptr(), flags | O_CLOEXEC, 0i64) };
    if fd < 0 { Err(io::Error::last_os_error()) } else { Ok(fd) }
}

fn sys_close(fd: i64) {
    unsafe { syscall(nr::CLOSE, fd) };
}

thread_local! {
    static BUF: std::cell::RefCell<Vec<u8>> = std::cell::RefCell::new(vec![0u8; 64 * 1024]);
}

/// The whole file into the reused buffer, then f(bytes): open, read until EOF, close.
fn with_file<T>(path: &str, f: impl FnOnce(&[u8]) -> T) -> io::Result<T> {
    let fd = sys_open(path, 0)?;
    BUF.with(|b| {
        let mut b = b.borrow_mut();
        let mut len = 0;
        loop {
            if len == b.len() {
                let n = b.len() * 2;
                b.resize(n, 0);
            }
            let n = unsafe { syscall(nr::READ, fd, b[len..].as_mut_ptr(), (b.len() - len) as i64) };
            if n < 0 {
                let e = io::Error::last_os_error();
                sys_close(fd);
                return Err(e);
            }
            if n == 0 { break; }
            len += n as usize;
        }
        sys_close(fd);
        Ok(f(&b[..len]))
    })
}

/// open(path).read().strip(): text mode (universal newlines), stripped like str.strip().
pub fn read_text(path: &str) -> Result<String, ReadError> {
    let text = with_file(path, |bytes| std::str::from_utf8(bytes).map(|t| {
        let t = t.trim_matches(py_space);
        if t.contains('\r') { t.replace("\r\n", "\n").replace('\r', "\n").trim_matches(py_space).to_string() } else { t.to_string() }
    }).map_err(|_| ())).map_err(ReadError::Os)?;
    text.map_err(|_| ReadError::Utf8)
}

/// sysfs.read(path, default): the text, or None on an OS error.
pub fn read(path: &str) -> Option<String> {
    match read_text(path) {
        Ok(t) => Some(t),
        Err(ReadError::Os(_)) => None,
        Err(ReadError::Utf8) => None, // Python raises here; the prototype treats it as unreadable
    }
}

pub fn reason_of(e: &io::Error) -> &'static str {
    match e.raw_os_error() {
        Some(13) | Some(1) => "permission_denied", // EACCES, EPERM
        Some(61) => "no_data",                     // ENODATA: e.g. an inactive thermal zone
        _ => "io_error",
    }
}

fn basename(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// Sorted names in directory path; see sysfs.entries.
pub fn entries(path: &str, groups: &mut [&mut Group], listed: bool) -> Vec<String> {
    match fs::read_dir(path) {
        Ok(rd) => {
            let mut names: Vec<String> = rd.filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().into_owned()).collect();
            names.sort();
            names
        }
        Err(e) if e.raw_os_error() == Some(2) => {
            if listed {
                for g in groups.iter_mut() {
                    g.note(basename(path), "disappeared", None);
                }
            }
            Vec::new()
        }
        Err(e) => {
            for g in groups.iter_mut() {
                g.note(basename(path), reason_of(&e), Some(format!("{}: {}", path, e)));
            }
            Vec::new()
        }
    }
}

/// The driver bound to a device, from the DRIVER= line of its uevent.
pub fn driver_of(device: &str, group: &mut Group) -> Option<String> {
    let text = group.read_str(&format!("{}/device/uevent", device), "uevent", Some(device))?;
    text.split('\n').find_map(|l| l.strip_prefix("DRIVER=").map(|d| d.trim().to_string()))
}

/// Sorted channel numbers N of names like prefix{N}suffix.
pub fn numbered(names: &[String], prefix: &str, suffix: &str) -> Vec<i64> {
    let mut out: Vec<i64> = names.iter().filter_map(|n| {
        let mid = n.strip_prefix(prefix)?.strip_suffix(suffix)?;
        if !mid.is_empty() && mid.bytes().all(|b| b.is_ascii_digit()) { mid.parse().ok() } else { None }
    }).collect();
    out.sort();
    out
}

fn is_hwmon(n: &str) -> bool {
    n.strip_prefix("hwmon").map_or(false, |d| !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit()))
}

/// (dir, chip name) for every hwmon device; name failures are noted in groups.
pub fn hwmon_chips(root: &str, groups: &mut [&mut Group]) -> Vec<(String, String)> {
    let mut chips = Vec::new();
    for n in entries(root, groups, false) {
        if is_hwmon(&n) {
            let d = format!("{}/{}", root, n);
            let mut name = Group::new("name", false);
            let chip = name.read_str(&format!("{}/name", d), &format!("{}.name", n), Some(&d))
                .filter(|s| !s.is_empty()).unwrap_or_else(|| n.clone());
            chips.push((d, chip));
            for g in groups.iter_mut() {
                g.merge(&name);
            }
        }
    }
    chips
}

const O_PATH: i64 = 0o10000000; // Linux, the same on x86_64 and aarch64

/// The absolute path the kernel resolved an existing path to (open without reading, then the
/// /proc/self/fd link); None if it went away in between.
fn kernel_path(path: &str) -> io::Result<Option<String>> {
    let fd = sys_open(path, O_PATH)?;
    let link = format!("/proc/self/fd/{}\0", fd);
    let mut buf = [0u8; 4096];
    let n = unsafe { syscall(nr::READLINKAT, AT_FDCWD, link.as_ptr(), buf.as_mut_ptr(), buf.len() as i64) };
    sys_close(fd);
    if n < 0 {
        return Err(io::Error::last_os_error()); // libc's syscall() returns -1 and sets errno
    }
    let real = String::from_utf8_lossy(&buf[..n as usize]).into_owned();
    Ok(if real.ends_with(" (deleted)") || !real.starts_with('/') { None } else { Some(real) })
}

/// path relative to the sysfs root once symlinks are resolved; None if it does not resolve inside it.
pub fn canonical(path: &str, sysroot: &str) -> Option<String> {
    if let (Ok(base), Ok(real)) = (kernel_path(sysroot), kernel_path(path)) {
        if let (Some(base), Some(real)) = (base, real) {
            if real == base {
                return Some(".".into());
            }
            let prefix = format!("{}/", base.trim_end_matches('/'));
            return real.strip_prefix(&prefix).map(str::to_string);
        }
    }
    canonical_portable(path, sysroot)
}

fn canonical_portable(path: &str, sysroot: &str) -> Option<String> {
    let real = fs::canonicalize(path).ok()?;
    let base = fs::canonicalize(sysroot).ok()?;
    let rel = real.strip_prefix(&base).ok()?; // outside the root: relpath would start with ..
    if !real.exists() {
        return None;
    }
    let rel = rel.to_string_lossy().into_owned();
    Some(if rel.is_empty() { ".".into() } else { rel })
}

/// {chip dir: (basis, where)}; see sysfs.chip_identities.
pub fn chip_identities(chips: &[(String, String)], sysroot: &str) -> Vec<(String, &'static str, Vec<(&'static str, Value)>)> {
    let rels: Vec<(String, Option<String>)> = chips.iter().map(|(d, _)| (d.clone(), canonical(d, sysroot))).collect();
    let mut anchors: Vec<(String, String)> = Vec::new();
    for (d, rel) in &rels {
        if let Some(rel) = rel {
            if let Some(dev) = hwmon_anchor(rel) {
                let parts: Vec<&str> = rel.split('/').take(2).collect();
                if parts != ["devices", "virtual"] {
                    anchors.push((d.clone(), dev));
                }
            }
        }
    }
    let shared = |a: &str| anchors.iter().filter(|(_, x)| x == a).count() > 1;
    rels.into_iter().map(|(d, rel)| {
        if let Some((_, a)) = anchors.iter().find(|(x, _)| *x == d).filter(|(_, a)| !shared(a)) {
            (d, "device_channel", vec![("device", a.clone().into())])
        } else if let Some(rel) = rel {
            (d, "resolved_path", vec![("path", rel.into())])
        } else {
            (d, "unresolved", vec![])
        }
    }).collect()
}

/// (.+)/hwmon/hwmon\d+ → the device part.
fn hwmon_anchor(rel: &str) -> Option<String> {
    let (head, last) = rel.rsplit_once('/')?;
    let (dev, mid) = head.rsplit_once('/')?;
    if mid == "hwmon" && is_hwmon(last) && !dev.is_empty() { Some(dev.to_string()) } else { None }
}

pub fn exists(path: &str) -> bool {
    Path::new(path).exists()
}

pub fn is_dir(path: &str) -> bool {
    Path::new(path).is_dir()
}
