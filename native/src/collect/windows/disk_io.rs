//! Disk I/O on Windows: the counters IOCTL_DISK_PERFORMANCE gives for each \\.\PhysicalDriveN, with the
//! rates and reasons of docs/reference/disk-io.md. Windows has no device numbers (major and minor are null)
//! and no merge or cumulative I/O time counter (null); the I/O time ratio comes from the idle time.
use crate::collect::{rate, reason_value, window_ms};
use crate::json::{round_to, Value};
use crate::metadata::{clock, Group, Span};
use windows_sys::Win32::Foundation::{CloseHandle, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_INSUFFICIENT_BUFFER,
                                     ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED, ERROR_PATH_NOT_FOUND, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{CreateFileW, QueryDosDeviceW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING};
use windows_sys::Win32::System::Ioctl::{DISK_PERFORMANCE, IOCTL_DISK_PERFORMANCE};
use windows_sys::Win32::System::IO::DeviceIoControl;

const IO: [&str; 4] = ["ios", "merges", "bytes", "time_ms"];
const PREFIX: &str = "PhysicalDrive";

/// reads, read bytes, read time, writes, write bytes, write time (100 ns), queue depth (a gauge), idle time
/// (100 ns).
type Row = [u64; 8];
const QUEUE: usize = 6;

fn error_code(e: &std::io::Error) -> u32 {
    e.raw_os_error().unwrap_or(0) as u32
}

/// The PhysicalDriveN names Windows lists, sorted.
fn drives() -> Result<Vec<String>, std::io::Error> {
    let mut buf = vec![0u16; 1 << 16];
    loop {
        let n = unsafe { QueryDosDeviceW(std::ptr::null(), buf.as_mut_ptr(), buf.len() as u32) } as usize;
        if n > 0 {
            let mut out: Vec<String> = buf[..n].split(|c| *c == 0).map(String::from_utf16_lossy)
                .filter(|s| s.strip_prefix(PREFIX).is_some_and(|d| !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit())))
                .collect();
            out.sort();
            return Ok(out);
        }
        let e = std::io::Error::last_os_error();
        if error_code(&e) != ERROR_INSUFFICIENT_BUFFER || buf.len() >= 1 << 24 {
            return Err(e);
        }
        buf.resize(buf.len() * 2, 0);
    }
}

/// One drive's counters; opened without access rights, which the query does not need.
fn read(name: &str) -> Result<Row, std::io::Error> {
    let path: Vec<u16> = format!("\\\\.\\{}", name).encode_utf16().chain(Some(0)).collect();
    let h = unsafe { CreateFileW(path.as_ptr(), 0, FILE_SHARE_READ | FILE_SHARE_WRITE, std::ptr::null(), OPEN_EXISTING, 0,
                                 std::ptr::null_mut()) };
    if h == INVALID_HANDLE_VALUE {
        return Err(std::io::Error::last_os_error());
    }
    let mut p = DISK_PERFORMANCE::default();
    let mut len = 0u32;
    let ok = unsafe { DeviceIoControl(h, IOCTL_DISK_PERFORMANCE, std::ptr::null(), 0, (&mut p as *mut DISK_PERFORMANCE).cast(),
                                      std::mem::size_of::<DISK_PERFORMANCE>() as u32, &mut len, std::ptr::null_mut()) };
    let e = std::io::Error::last_os_error();
    unsafe { CloseHandle(h) };
    if ok == 0 {
        return Err(e);
    }
    let u = |x: i64| x.max(0) as u64;
    Ok([p.ReadCount as u64, u(p.BytesRead), u(p.ReadTime), p.WriteCount as u64, u(p.BytesWritten), u(p.WriteTime),
        p.QueueDepth as u64, u(p.IdleTime)])
}

/// The disk counters, kept between collections (disk_io.DiskCounters without major and minor).
pub struct Disks {
    max_gap: Option<i64>,
    last: Option<(i64, Vec<(String, Row)>)>,
}

impl Disks {
    pub fn new(max_gap: Option<f64>) -> Disks {
        Disks { max_gap: max_gap.map(|g| (g * 1e9).round() as i64), last: None }
    }

    fn output(disks: Vec<Value>) -> Value {
        let mut scope = Value::obj();
        scope.set("kind", "host_block_devices".into());
        let mut out = Value::obj();
        out.set("scope", scope);
        out.set("provider", "ioctl_disk_performance".into());
        out.set("disks", Value::Arr(disks));
        out.set("read", Value::Null);
        out
    }

    pub fn sample(&mut self, group: &mut Group) -> (Value, Span) {
        let start = clock();
        let names = match drives() {
            Ok(names) => names,
            Err(e) => {
                self.last = None;
                let reason = if error_code(&e) == ERROR_ACCESS_DENIED { "permission_denied" } else { "io_error" };
                group.note("dos_devices", reason, Some(format!("QueryDosDeviceW: {}", e)));
                return (Disks::output(vec![]), None);
            }
        };
        let mut rows = Vec::new();
        for name in names {
            match read(&name) {
                Ok(row) => rows.push((name, row)),
                Err(e) => {
                    let reason = match error_code(&e) {
                        ERROR_ACCESS_DENIED => "permission_denied",
                        ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND => "disappeared",
                        ERROR_INVALID_FUNCTION | ERROR_NOT_SUPPORTED => "not_exposed", // no counters on this disk
                        _ => "io_error",
                    };
                    group.note(&name, reason, Some(format!("IOCTL_DISK_PERFORMANCE: {}", e)));
                }
            }
        }
        let end = clock();

        let last = self.last.take();
        let window = last.as_ref().map(|l| start - l.0);
        let (mut kept, mut disks) = (vec![], vec![]);
        for (name, row) in rows {
            let before = last.as_ref().and_then(|l| l.1.iter().find(|(n, _)| *n == name)).map(|(_, r)| *r);
            let mut rates = Value::Null;
            let reason = match (before, window) {
                (None, _) | (_, None) => Some("warmup"),
                (_, Some(w)) if w <= 0 => Some("invalid_interval"),
                (_, Some(w)) if self.max_gap.map_or(false, |g| w > g) => Some("gap"),
                // the read and write counts are 32-bit: a wrap shows here for one reading
                (Some(b), _) if row.iter().zip(b.iter()).enumerate().any(|(i, (a, b))| i != QUEUE && a < b) => Some("counter_regressed"),
                (Some(b), Some(w)) => {
                    let d = |i: usize| (row[i] - b[i]) as u128;
                    let mut r = Value::obj();
                    r.set("read_bytes_per_s", rate(d(1), w).into());
                    r.set("write_bytes_per_s", rate(d(4), w).into());
                    r.set("reads_per_s", rate(d(0), w).into());
                    r.set("writes_per_s", rate(d(3), w).into());
                    // the window less the idle time; Windows's and platmon's clocks differ, and an idle time
                    // a little longer than the window gives 0, never a negative share (or -0.0)
                    let busy = round_to((w as f64 - d(7) as f64 * 100.0) / w as f64, 4);
                    r.set("io_time_ratio", (if busy > 0.0 { busy } else { 0.0 }).into());
                    rates = r;
                    None
                }
            };
            let dir = |o: usize| {
                let mut v = Value::obj();
                let x = [row[o].into(), Value::Null, row[o + 1].into(), (row[o + 2] / 10_000).into()];
                for (k, x) in IO.iter().zip(x) {
                    v.set(k, x);
                }
                v
            };
            let mut v = Value::obj();
            v.set("name", name.as_str().into());
            v.set("major", Value::Null);
            v.set("minor", Value::Null);
            v.set("read", dir(0));
            v.set("write", dir(3));
            v.set("in_flight", row[QUEUE].into());
            v.set("io_time_ms", Value::Null);
            v.set("rates", rates);
            v.set("window_ms", window_ms(window, reason));
            v.set("reason", reason_value(reason));
            disks.push(group.got(v));
            kept.push((name, row));
        }
        self.last = Some((start, kept));
        if disks.is_empty() && group.reasons.is_empty() {
            group.note("disks", "not_detected", None);
        }
        (Disks::output(disks), Some((start, end)))
    }
}
