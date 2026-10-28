//! collector/sysfs.py: reading /proc and /sys, listings, hwmon chips and resolved source identities.
use crate::json::Value;
use crate::metadata::Group;
use crate::pystr::py_space;
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

/// Where a path is opened: the path itself. In unit tests, an absolute path under the thread's fake root
/// when one is set (fake::Root), so a test reads its own tree at the paths the collectors use.
#[cfg(not(test))]
#[inline(always)]
pub fn at(path: &str) -> std::borrow::Cow<'_, str> {
    std::borrow::Cow::Borrowed(path)
}

#[cfg(test)]
pub fn at(path: &str) -> std::borrow::Cow<'_, str> {
    fake::ROOT.with(|r| match &*r.borrow() {
        Some(root) if path.starts_with('/') => std::borrow::Cow::Owned(format!("{}{}", root, path)),
        _ => std::borrow::Cow::Borrowed(path),
    })
}

pub enum ReadError {
    Os(io::Error),
    Utf8,
}

// Raw system calls: musl's open() adds an fcntl(F_SETFD) after every O_CLOEXEC open, and a fresh buffer per
// file makes its allocator map and unmap pages every collection. Measured on x86 (strace -c): 4233 fcntl and
// ~1500 mmap/munmap pairs in 23 collections before this; see native/README.md as of b96aba8 ("Earlier run").
// syscall() is variadic and reads C longs: pass c_long, never i64 (on 32-bit ARM an i64 takes two registers and
// shifts the rest). Without std's EINTR handling, open and read retry an interrupted call as Python and std::fs
// do; close never does (Linux frees it).

fn retry(mut call: impl FnMut() -> libc::c_long) -> io::Result<libc::c_long> {
    loop {
        let n = call();
        if n >= 0 {
            return Ok(n);
        }
        let e = io::Error::last_os_error(); // libc's syscall() returns -1 and sets errno
        if e.raw_os_error() != Some(libc::EINTR) {
            return Err(e);
        }
    }
}

fn sys_open(path: &str, flags: libc::c_long) -> io::Result<libc::c_long> {
    let c = std::ffi::CString::new(&*at(path)).map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
    let flags = flags | libc::O_CLOEXEC as libc::c_long;
    retry(|| unsafe { libc::syscall(libc::SYS_openat, libc::AT_FDCWD as libc::c_long, c.as_ptr(), flags, 0 as libc::c_long) })
}

fn sys_close(fd: libc::c_long) {
    unsafe { libc::syscall(libc::SYS_close, fd) };
}

thread_local! {
    static BUF: std::cell::RefCell<Vec<u8>> = std::cell::RefCell::new(vec![0u8; 64 * 1024]);
}

/// The whole file into the reused buffer, then f(bytes): open, read until EOF, close.
fn with_file<T>(path: &str, f: impl FnOnce(&[u8]) -> T) -> io::Result<T> {
    #[cfg(test)]
    if let Some(answer) = fake::answer(path) {
        return answer.map(|bytes| f(&bytes));
    }
    let fd = sys_open(path, 0)?;
    BUF.with(|b| {
        let mut b = b.borrow_mut();
        let mut len = 0;
        loop {
            if len == b.len() {
                let n = b.len() * 2;
                b.resize(n, 0);
            }
            let rest = &mut b[len..];
            match retry(|| unsafe { libc::syscall(libc::SYS_read, fd, rest.as_mut_ptr(), rest.len()) }) {
                Ok(0) => break,
                Ok(n) => len += n as usize,
                Err(e) => {
                    sys_close(fd);
                    return Err(e);
                }
            }
        }
        sys_close(fd);
        Ok(f(&b[..len]))
    })
}

/// open(path, "rb").read().decode(): the whole file as UTF-8, as it is (counters.read_file).
pub fn read_utf8(path: &str) -> Result<String, ReadError> {
    let text = with_file(path, |bytes| std::str::from_utf8(bytes).map(str::to_string).map_err(|_| ()))
        .map_err(ReadError::Os)?;
    text.map_err(|_| ReadError::Utf8)
}

/// open(path, encoding="ascii").read(): text mode, so universal newlines; a byte above 0x7f is an error.
pub fn read_ascii(path: &str) -> Result<String, ReadError> {
    let text = with_file(path, |bytes| if bytes.is_ascii() {
        let t = std::str::from_utf8(bytes).unwrap();
        Ok(if t.contains('\r') { t.replace("\r\n", "\n").replace('\r', "\n") } else { t.to_string() })
    } else {
        Err(())
    }).map_err(ReadError::Os)?;
    text.map_err(|_| ReadError::Utf8)
}

/// open(path).read().strip(): text mode (universal newlines), stripped like str.strip().
pub fn read_text(path: &str) -> Result<String, ReadError> {
    let text = with_file(path, |bytes| std::str::from_utf8(bytes).map(|t| {
        let t = t.trim_matches(py_space);
        if t.contains('\r') { t.replace("\r\n", "\n").replace('\r', "\n").trim_matches(py_space).to_string() } else { t.to_string() }
    }).map_err(|_| ())).map_err(ReadError::Os)?;
    text.map_err(|_| ReadError::Utf8)
}

/// sysfs.read(path, default): the text, or None on an OS error. Bytes that are not UTF-8 are an error, as
/// in Python, where UnicodeDecodeError is not an OSError and reaches the caller.
pub fn read(path: &str) -> Result<Option<String>, String> {
    match read_text(path) {
        Ok(t) => Ok(Some(t)),
        Err(ReadError::Os(_)) => Ok(None),
        Err(ReadError::Utf8) => Err(format!("{}: not UTF-8", path)),
    }
}

pub fn reason_of(e: &io::Error) -> &'static str {
    match e.raw_os_error() {
        Some(libc::EACCES) | Some(libc::EPERM) => "permission_denied",
        Some(libc::ENODATA) => "no_data", // e.g. an inactive thermal zone
        _ => "io_error",
    }
}

fn basename(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// Sorted names in directory path; see sysfs.entries. Like os.listdir, all or nothing: an error while
/// iterating fails the whole listing, so a partial one is never taken for the directory's contents.
pub fn entries(path: &str, groups: &mut [&mut Group], listed: bool) -> Vec<String> {
    // in unit tests, a listing the test made fail (fake::unlistable) fails as read_dir would
    #[cfg(test)]
    let failed = fake::unlisted(path).map(|e| Err(io::Error::from_raw_os_error(e)));
    #[cfg(not(test))]
    let failed: Option<io::Result<Vec<String>>> = None;
    let listing = failed.unwrap_or_else(|| fs::read_dir(&*at(path)).and_then(|rd| {
        rd.map(|e| e.map(|e| e.file_name().to_string_lossy().into_owned())).collect::<io::Result<Vec<String>>>()
    }));
    match listing {
        Ok(mut names) => {
            names.sort();
            names
        }
        Err(e) if e.raw_os_error() == Some(libc::ENOENT) => {
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

/// The absolute path the kernel resolved an existing path to (open without reading, then the
/// /proc/self/fd link); None if it went away in between.
fn kernel_path(path: &str) -> io::Result<Option<String>> {
    let fd = sys_open(path, libc::O_PATH as libc::c_long)?;
    let link = format!("/proc/self/fd/{}\0", fd);
    let mut buf = [0u8; 4096];
    let n = unsafe { libc::syscall(libc::SYS_readlinkat, libc::AT_FDCWD as libc::c_long, link.as_ptr(), buf.as_mut_ptr(), buf.len()) };
    let n = if n < 0 { Err(io::Error::last_os_error()) } else { Ok(n) };
    sys_close(fd);
    let n = n?;
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
    let real = fs::canonicalize(&*at(path)).ok()?;
    let base = fs::canonicalize(&*at(sysroot)).ok()?;
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
    Path::new(&*at(path)).exists()
}

pub fn is_dir(path: &str) -> bool {
    Path::new(&*at(path)).is_dir()
}

/// Unit tests' fake trees: a temporary directory that the thread's reads see as / (at), with the clocks
/// set; tests write the files the collectors read at their usual paths.
#[cfg(test)]
pub mod fake {
    use std::cell::RefCell;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;

    thread_local! {
        pub static ROOT: RefCell<Option<String>> = RefCell::new(None);
        // path -> what its next reads give, in order (Python's injected read functions)
        static ANSWERS: RefCell<Vec<(String, std::collections::VecDeque<Result<Vec<u8>, i32>>)>> = RefCell::new(Vec::new());
        static UNLISTABLE: RefCell<Vec<(String, i32)>> = RefCell::new(Vec::new());
    }

    /// Listing directory path (entries) fails with errno until listable(path).
    pub fn unlistable(path: &str, errno: i32) {
        UNLISTABLE.with(|u| u.borrow_mut().push((path.to_string(), errno)));
    }

    pub fn listable(path: &str) {
        UNLISTABLE.with(|u| u.borrow_mut().retain(|(p, _)| p != path));
    }

    pub(super) fn unlisted(path: &str) -> Option<i32> {
        UNLISTABLE.with(|u| u.borrow().iter().find(|(p, _)| p == path).map(|(_, e)| *e))
    }

    /// The next reads of path (a path the collectors use) give these: bytes, or an OS error by errno.
    /// Once they are used up, path is read from the tree again.
    pub fn answers(path: &str, answers: Vec<Result<&str, i32>>) {
        ANSWERS.with(|a| a.borrow_mut().push((path.to_string(), answers.into_iter().map(|r| r.map(|t| t.as_bytes().to_vec())).collect())));
    }

    pub(super) fn answer(path: &str) -> Option<std::io::Result<Vec<u8>>> {
        ANSWERS.with(|a| {
            let mut a = a.borrow_mut();
            let (_, queue) = a.iter_mut().find(|(p, q)| p == path && !q.is_empty())?;
            Some(queue.pop_front().unwrap().map_err(std::io::Error::from_raw_os_error))
        })
    }

    pub struct Root {
        pub dir: PathBuf,
    }

    impl Root {
        /// A new empty root for this thread, the clocks at (base ns, wall 1_700_000_000 s).
        pub fn new(base: i64) -> Root {
            use std::sync::atomic::{AtomicU32, Ordering};
            static N: AtomicU32 = AtomicU32::new(0);
            let dir = std::env::temp_dir().join(format!("platmon-native-root-{}-{}", std::process::id(), N.fetch_add(1, Ordering::Relaxed)));
            std::fs::create_dir_all(&dir).unwrap();
            ROOT.with(|r| *r.borrow_mut() = Some(dir.to_str().unwrap().to_string()));
            crate::metadata::set_clocks(base, 1_700_000_000_000_000);
            Root { dir }
        }

        /// The real path of an absolute path in the tree.
        pub fn path(&self, path: &str) -> PathBuf {
            self.dir.join(path.trim_start_matches('/'))
        }

        /// Writes bytes at path, making its directories.
        pub fn put(&self, path: &str, bytes: impl AsRef<[u8]>) {
            let p = self.path(path);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(p, bytes).unwrap();
        }

        /// A symlink at path to target (as given: relative targets stay relative).
        pub fn link(&self, path: &str, target: &str) {
            let p = self.path(path);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::os::unix::fs::symlink(target, p).unwrap();
        }

        pub fn mkdir(&self, path: &str) {
            std::fs::create_dir_all(self.path(path)).unwrap();
        }

        pub fn remove(&self, path: &str) {
            let p = self.path(path);
            if p.is_dir() && !p.is_symlink() { std::fs::remove_dir_all(p).unwrap() } else { std::fs::remove_file(p).unwrap() }
        }

        pub fn chmod(&self, path: &str, mode: u32) {
            std::fs::set_permissions(self.path(path), std::fs::Permissions::from_mode(mode)).unwrap();
        }

        /// /proc/stat, meminfo and uptime as the required reads need them (fixture.py base_proc).
        pub fn required(&self) {
            self.put("/proc/stat", "cpu  100 0 50 800 10 0 5 0 0 0\ncpu0 100 0 50 800 10 0 5 0 0 0\nintr 1 0\nctxt 100\nbtime 1700000000\n");
            self.put("/proc/meminfo", "MemTotal:        8000000 kB\nMemFree:         1000000 kB\nMemAvailable:    5000000 kB\n\
                                       Buffers:           10000 kB\nSwapTotal:       2000000 kB\nSwapFree:        1500000 kB\n");
            self.put("/proc/uptime", "1234.56 4000.00\n");
        }

        /// The clocks of the next collection: clock() from base ns, one FAKE_TICK per call.
        pub fn at(&self, base: i64) {
            crate::metadata::set_clocks(base, 1_700_000_000_000_000 + base / 1000);
        }
    }

    impl Drop for Root {
        fn drop(&mut self) {
            ROOT.with(|r| *r.borrow_mut() = None);
            ANSWERS.with(|a| a.borrow_mut().clear());
            UNLISTABLE.with(|u| u.borrow_mut().clear());
            crate::metadata::set_clocks(i64::MIN, i64::MIN);
            for e in walk(&self.dir) {
                let _ = std::fs::set_permissions(&e, std::fs::Permissions::from_mode(0o755));
            }
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn walk(dir: &std::path::Path) -> Vec<PathBuf> {
        let mut out = vec![dir.to_path_buf()];
        let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o755));
        if let Ok(rd) = std::fs::read_dir(dir) {
            for e in rd.flatten() {
                let p = e.path();
                if p.is_dir() && !p.is_symlink() { out.extend(walk(&p)) } else if !p.is_symlink() { out.push(p) }
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::OpenOptionsExt;
    use std::sync::atomic::{AtomicU32, Ordering};

    static SIGNALS: AtomicU32 = AtomicU32::new(0);
    extern "C" fn on_signal(_: libc::c_int) {
        SIGNALS.fetch_add(1, Ordering::SeqCst);
    }

    /// A signal without SA_RESTART interrupts the blocking open of a FIFO with EINTR; the read still
    /// succeeds once the writer comes.
    #[test]
    fn interrupted_open_is_retried() {
        let dir = std::env::temp_dir().join(format!("platmon-native-eintr-{}", std::process::id()));
        let _ = fs::create_dir(&dir);
        let fifo = dir.join("fifo");
        let path = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
        unsafe {
            let mut sa: libc::sigaction = std::mem::zeroed();
            sa.sa_sigaction = on_signal as usize;
            sa.sa_flags = 0; // no SA_RESTART: the kernel returns EINTR instead of restarting the call
            assert_eq!(libc::sigaction(libc::SIGUSR1, &sa, std::ptr::null_mut()), 0);
        }
        let reader = unsafe { libc::pthread_self() } as usize;
        let writer_path = fifo.clone();
        let writer = std::thread::spawn(move || {
            std::thread::sleep(std::time::Duration::from_millis(100));
            unsafe { libc::pthread_kill(reader as libc::pthread_t, libc::SIGUSR1) };
            // non-blocking, so a reader that gave up (no retry) fails the test instead of hanging it
            let started = std::time::Instant::now();
            loop {
                std::thread::sleep(std::time::Duration::from_millis(50));
                let open = fs::OpenOptions::new().write(true).custom_flags(libc::O_NONBLOCK).open(&writer_path);
                if let Ok(mut f) = open {
                    std::io::Write::write_all(&mut f, b"42\n").unwrap();
                    break;
                }
                if started.elapsed() > std::time::Duration::from_secs(2) {
                    break;
                }
            }
        });
        let text = read_text(fifo.to_str().unwrap());
        writer.join().unwrap();
        let _ = fs::remove_dir_all(&dir);
        assert!(SIGNALS.load(Ordering::SeqCst) >= 1);
        assert!(matches!(text, Ok(ref t) if t == "42"));
    }

    #[test]
    fn reasons_of_os_errors() {
        assert_eq!(reason_of(&io::Error::from_raw_os_error(libc::EACCES)), "permission_denied");
        assert_eq!(reason_of(&io::Error::from_raw_os_error(libc::EPERM)), "permission_denied");
        assert_eq!(reason_of(&io::Error::from_raw_os_error(libc::ENODATA)), "no_data");
        assert_eq!(reason_of(&io::Error::from_raw_os_error(libc::EIO)), "io_error");
    }
}
