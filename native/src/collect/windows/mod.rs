//! Windows: the required data (CPU, memory, the system volume, identity) from the Win32 and NT system
//! information calls, the CPU clocks (cpu_clock.rs), the GPU (gpu.rs), temperatures (temperature.rs), Network
//! (network.rs), Disk I/O (disk_io.rs), the slow groups (storage.rs, wifi.rs, battery.rs and the probe's
//! connection); the other optional groups have no provider here (unsupported_platform). Rust only: the Python
//! service has no Windows collector to compare with.
pub mod battery;
pub mod cpu_clock;
pub mod disk_io;
pub mod gpu;
pub mod network;
pub mod storage;
pub mod temperature;
pub mod wifi;

use super::{timed, Collected, CpuCounters, Required};
use crate::json::{round_to, Value};
use crate::metadata::{self, Group};
use std::collections::BTreeMap;
use windows_sys::Wdk::System::SystemInformation::{NtQuerySystemInformation, SystemProcessorPerformanceInformation};
use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY_LOCAL_MACHINE, RRF_RT_REG_DWORD, RRF_RT_REG_SZ};
use windows_sys::Win32::System::SystemInformation as si;
use windows_sys::Win32::System::Threading::{GetActiveProcessorCount, GetActiveProcessorGroupCount};
use windows_sys::Win32::System::WindowsProgramming::SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION;

/// The slow groups, in Linux's order: storage (storage.rs), Wi-Fi (wifi.rs), battery (battery.rs) and probe
/// (src/collect/probe.rs).
pub mod observe {
    pub const GROUPS: [&str; 4] = ["storage", "wifi", "battery", "probe"];

    pub fn reader(name: &str, cfg: &crate::config::Config) -> crate::slow::Reader {
        match name {
            "storage" => Box::new(|g| Ok(super::storage::storage(g))),
            "wifi" => Box::new(|g| Ok(super::wifi::wifi(g))),
            "battery" => Box::new(|g| Ok(super::battery::battery(g))),
            "probe" => {
                let mut p = crate::collect::probe::Probe::new(cfg.targets.clone(), cfg.float("probe", "timeout"));
                Box::new(move |g| Ok(p.observe(g)))
            }
            _ => panic!("no {} reader on this platform", name),
        }
    }
}

/// probe.connect_ms with std's connect_timeout: (ms to complete the connection, None) or (None, reason).
/// Windows retries a refused connection attempt before it reports it, so a closed port can take longer
/// than on Linux, and a timeout shorter than that reads as timeout.
pub fn connect_ms(address: &str, port: u16, timeout: f64) -> (Option<f64>, Option<&'static str>) {
    use std::io::ErrorKind;
    let Ok(ip) = address.parse::<std::net::IpAddr>() else { return (None, Some("error")) };
    let Ok(limit) = std::time::Duration::try_from_secs_f64(timeout) else { return (None, Some("error")) };
    let start = std::time::Instant::now();
    match std::net::TcpStream::connect_timeout(&std::net::SocketAddr::new(ip, port), limit) {
        Ok(_) => (Some(round_to(start.elapsed().as_secs_f64() * 1e3, 3)), None),
        Err(e) => (None, Some(match e.kind() {
            ErrorKind::ConnectionRefused => "refused",
            ErrorKind::TimedOut | ErrorKind::WouldBlock => "timeout",
            ErrorKind::HostUnreachable | ErrorKind::NetworkUnreachable => "unreachable",
            _ => "error",
        })),
    }
}

/// The optional groups of a Linux collection, in its order; those without a provider here are unsupported_platform.
const OPTIONAL: [&str; 8] = ["cpu_frequency", "gpu", "npu", "temperature", "power", "fans", "power_mode", "board_info"];
const READ: [&str; 3] = ["cpu_frequency", "gpu", "temperature"];

/// SystemPageFileInformation and its entries (winternl.h leaves them out; stable since Windows NT).
const SYSTEM_PAGE_FILE_INFORMATION: i32 = 18;
#[repr(C)]
struct PageFile {
    next_entry_offset: u32,
    total_size: u32,  // pages
    total_in_use: u32, // pages
    peak_usage: u32,
    name: [usize; 2], // UNICODE_STRING
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(Some(0)).collect()
}

fn last_error(what: &str) -> String {
    format!("{}: {}", what, std::io::Error::last_os_error())
}

/// A REG_SZ value under HKEY_LOCAL_MACHINE, None if it cannot be read.
fn registry_text(key: &str, value: &str) -> Option<String> {
    let (key, value) = (wide(key), wide(value));
    let mut buf = [0u16; 256];
    let mut len = std::mem::size_of_val(&buf) as u32;
    let rc = unsafe { RegGetValueW(HKEY_LOCAL_MACHINE, key.as_ptr(), value.as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(),
                                   buf.as_mut_ptr().cast(), &mut len) };
    let n = (len as usize / 2).min(buf.len());
    (rc == 0).then(|| String::from_utf16_lossy(&buf[..n]).trim_end_matches('\0').trim().to_string())
}

/// A REG_DWORD value under HKEY_LOCAL_MACHINE.
fn registry_dword(key: &str, value: &str) -> Option<u32> {
    let (key, value) = (wide(key), wide(value));
    let (mut data, mut len) = (0u32, 4u32);
    let rc = unsafe { RegGetValueW(HKEY_LOCAL_MACHINE, key.as_ptr(), value.as_ptr(), RRF_RT_REG_DWORD, std::ptr::null_mut(),
                                   (&mut data as *mut u32).cast(), &mut len) };
    (rc == 0).then_some(data)
}

// winternl.h leaves it out: with a processor group number as its input, SystemProcessorPerformanceInformation
// gives that group's processors (the plain call gives only the calling thread's group).
#[link(name = "ntdll", kind = "raw-dylib")]
extern "system" {
    fn NtQuerySystemInformationEx(class: i32, input: *const std::ffi::c_void, input_len: u32, info: *mut std::ffi::c_void,
                                  info_len: u32, return_len: *mut u32) -> i32;
}

/// Each processor group's first CPU id: a CPU's id is its group's first id plus its number in the group,
/// counting the groups' active processors in group order, as Task Manager numbers them.
pub fn group_starts() -> Vec<i64> {
    let mut first = 0i64;
    (0..unsafe { GetActiveProcessorGroupCount() }).map(|g| {
        let start = first;
        first += unsafe { GetActiveProcessorCount(g) } as i64;
        start
    }).collect()
}

/// {cpu: [user, nice, system, idle, iowait, irq, softirq, steal]} in 100 ns units, for the CPUs of every
/// processor group: system is kernel time without idle (it includes DPCs and interrupts), the fields
/// Windows has no counter for are 0.
fn cpu_times() -> Result<BTreeMap<i64, Vec<i64>>, String> {
    let mut out = BTreeMap::new();
    for (group, first) in group_starts().into_iter().enumerate() {
        let mut buf = [SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION::default(); 64]; // a group has at most 64
        let (number, mut len) = (group as u16, 0u32);
        let status = unsafe { NtQuerySystemInformationEx(SystemProcessorPerformanceInformation, (&number as *const u16).cast(), 2,
                                                         buf.as_mut_ptr().cast(), std::mem::size_of_val(&buf) as u32, &mut len) };
        if status < 0 {
            return Err(format!("processor times of group {}: NTSTATUS {:#x}", group, status));
        }
        let n = (len as usize / std::mem::size_of::<SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION>()).min(buf.len());
        out.extend(buf[..n].iter().enumerate()
            .map(|(i, p)| (first + i as i64, vec![p.UserTime, 0, p.KernelTime - p.IdleTime, p.IdleTime, 0, 0, 0, 0])));
    }
    if out.is_empty() {
        return Err("processor times: no processors".into());
    }
    Ok(out)
}

/// (total, used, (swap total, swap used)) in bytes; swap is the paging files, None if they cannot be read:
/// the physical memory is the required data, not the paging files.
fn memory() -> Result<(i64, i64, Option<(i64, i64)>), String> {
    let mut m = si::MEMORYSTATUSEX { dwLength: std::mem::size_of::<si::MEMORYSTATUSEX>() as u32, ..Default::default() };
    if unsafe { si::GlobalMemoryStatusEx(&mut m) } == 0 {
        return Err(last_error("memory"));
    }
    let mut info = si::SYSTEM_INFO::default();
    unsafe { si::GetSystemInfo(&mut info) };
    let page = info.dwPageSize as i64;
    let total = m.ullTotalPhys as i64;
    let used = total - m.ullAvailPhys as i64;
    let mut buf = vec![0u64; 512]; // 4 KiB, 8-byte aligned: a few entries with their names
    let mut len = 0u32;
    let status = unsafe { NtQuerySystemInformation(SYSTEM_PAGE_FILE_INFORMATION, buf.as_mut_ptr().cast(), (buf.len() * 8) as u32,
                                                   &mut len) };
    if status < 0 {
        return Ok((total, used, None));
    }
    let (mut swap_total, mut swap_used, mut offset) = (0i64, 0i64, 0usize);
    let bytes = (len as usize).min(buf.len() * 8);
    while offset + std::mem::size_of::<PageFile>() <= bytes {
        let entry = unsafe { &*(buf.as_ptr().cast::<u8>().add(offset).cast::<PageFile>()) };
        swap_total += entry.total_size as i64 * page;
        swap_used += entry.total_in_use as i64 * page;
        if entry.next_entry_offset == 0 {
            break;
        }
        offset += entry.next_entry_offset as usize;
    }
    Ok((total, used, Some((swap_total, swap_used))))
}

/// (total, used) bytes of the volume Windows is on, as the root filesystem on Linux.
fn disk_usage() -> Result<(i64, i64), String> {
    let mut dir = [0u16; 261];
    let n = unsafe { si::GetSystemWindowsDirectoryW(dir.as_mut_ptr(), dir.len() as u32) } as usize;
    if n == 0 || n >= dir.len() {
        return Err(last_error("windows directory"));
    }
    let (mut total, mut free) = (0u64, 0u64);
    if unsafe { GetDiskFreeSpaceExW(dir.as_ptr(), std::ptr::null_mut(), &mut total, &mut free) } == 0 {
        return Err(last_error("disk"));
    }
    Ok((total as i64, (total - free) as i64))
}

fn hostname() -> String {
    let mut buf = [0u16; 256];
    let mut len = buf.len() as u32;
    if unsafe { si::GetComputerNameExW(si::ComputerNamePhysicalDnsHostname, buf.as_mut_ptr(), &mut len) } == 0 {
        return String::new();
    }
    String::from_utf16_lossy(&buf[..len as usize])
}

/// The machine's architecture (not this binary's), named as uname's machine on Linux.
fn arch() -> &'static str {
    let mut info = si::SYSTEM_INFO::default();
    unsafe { si::GetNativeSystemInfo(&mut info) };
    match unsafe { info.Anonymous.Anonymous.wProcessorArchitecture } {
        si::PROCESSOR_ARCHITECTURE_AMD64 => "x86_64",
        si::PROCESSOR_ARCHITECTURE_ARM64 => "aarch64",
        si::PROCESSOR_ARCHITECTURE_INTEL => "x86",
        si::PROCESSOR_ARCHITECTURE_ARM => "arm",
        _ => "unknown",
    }
}

const CURRENT_VERSION: &str = "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion";

/// The product name and release, e.g. "Windows 11 Pro 24H2": the registry's ProductName still says
/// "Windows 10" on Windows 11, so a build from 22000 corrects it.
fn os_name(build: u32) -> Option<String> {
    let mut name = registry_text(CURRENT_VERSION, "ProductName")?;
    if build >= 22000 && name.starts_with("Windows 10") {
        name.replace_range(..10, "Windows 11");
    }
    match registry_text(CURRENT_VERSION, "DisplayVersion").filter(|v| !v.is_empty()) {
        Some(v) => Some(format!("{} {}", name, v)),
        None => Some(name),
    }
}

fn system_info() -> Value {
    let major = registry_dword(CURRENT_VERSION, "CurrentMajorVersionNumber");
    let minor = registry_dword(CURRENT_VERSION, "CurrentMinorVersionNumber");
    let build = registry_text(CURRENT_VERSION, "CurrentBuildNumber").and_then(|b| b.parse::<u32>().ok());
    let kernel = match (major, minor, build) {
        (Some(a), Some(b), Some(c)) => match registry_dword(CURRENT_VERSION, "UBR") {
            Some(u) => format!("{}.{}.{}.{}", a, b, c, u),
            None => format!("{}.{}.{}", a, b, c),
        },
        _ => String::new(),
    };
    let mut v = Value::obj();
    v.set("os", build.and_then(os_name).into());
    v.set("kernel", kernel.into());
    v.set("arch", arch().into());
    v.set("hostname", hostname().into());
    v
}

pub struct Collector {
    pub platform: &'static str,
    cpu: CpuCounters,
    clocks: cpu_clock::Clocks,
    gpu: gpu::Gpu,
    network: Option<network::Network>,
    disk_io: Option<disk_io::Disks>,
    logged: Vec<(String, Vec<String>)>,
}

impl Collector {
    /// max_gap: seconds, max(3 × interval, 5 s) in the service; passive: network, disk_io, pressure on.
    /// Pressure has no Windows reader yet and is left out, as when it is off.
    pub fn new(max_gap: f64, passive: [bool; 3]) -> Collector {
        Collector { platform: "Windows", cpu: CpuCounters::new(Some(max_gap), "interval"), clocks: cpu_clock::Clocks::new(), gpu: gpu::Gpu::new(),
                    network: passive[0].then(|| network::Network::new(Some(max_gap))),
                    disk_io: passive[1].then(|| disk_io::Disks::new(Some(max_gap))), logged: Vec::new() }
    }

    /// The fields and order of a Linux collection, the optional groups without a provider unsupported_platform.
    pub fn collect(&mut self) -> Result<Collected, String> {
        let mut required: Required = Vec::new();
        let (usage, sampling) = self.cpu.sample(cpu_times)?;
        required.push(("cpu", self.cpu.span));
        let mut groups: Vec<Group> = OPTIONAL.iter().map(|name| Group::new(name, true)).collect();
        let ids: Vec<i64> = usage.iter().map(|(n, _)| *n).collect();
        let freqs = self.clocks.sample(&ids, &mut groups[0]);
        let gpu = self.gpu.sample(&mut groups[1]);
        let temps = temperature::read(&mut groups[3]);
        for g in groups.iter_mut().filter(|g| !READ.contains(&g.name.as_str())) {
            let name = g.name.clone();
            g.note(&name, "unsupported_platform", None);
        }
        let (total, used, swap) = timed(&mut required, "memory", memory)?;
        let disk = timed(&mut required, "disk", disk_usage)?;
        let uptime = timed(&mut required, "uptime", crate::platform::boottime_ns).ok_or("uptime unreadable")?;
        let host = hostname();
        let model = registry_text("HARDWARE\\DESCRIPTION\\System\\BIOS", "SystemProductName").filter(|m| !m.is_empty())
            .unwrap_or_else(|| host.clone());

        let mut s = Value::obj();
        s.set("platform", self.platform.into());
        s.set("time", metadata::wall().into());
        s.set("model", model.into());
        s.set("system", system_info());
        s.set("uptime", round_to(uptime as f64 / 1e9, 2).into()); // /proc/uptime's two decimals
        s.set("power_mode", Value::Null);
        s.set("cpu", Value::Arr(usage.iter().map(|(n, u)| {
            let mut c = Value::obj();
            c.set("id", (*n).into());
            c.set("usage", (*u).into());
            c.set("freq", freqs.iter().find(|(x, _)| x == n).and_then(|(_, f)| *f).into());
            c
        }).collect()));
        s.set("cpu_sampling", sampling);
        s.set("gpu", gpu.into());
        let mut mv = Value::obj();
        mv.set("total", total.into());
        mv.set("used", used.into());
        mv.set("swap_total", swap.map(|s| s.0).into());
        mv.set("swap_used", swap.map(|s| s.1).into());
        s.set("memory", mv);
        let mut dv = Value::obj();
        dv.set("total", disk.0.into());
        dv.set("used", disk.1.into());
        s.set("disk", dv);
        s.set("temperature", Value::Obj(temps));
        s.set("power", Value::obj());
        s.set("fans", Value::Arr(vec![]));
        s.set("npu", Value::Null);
        // after the required data: a failed collection never reaches it
        let mut network = None;
        if let Some(n) = self.network.as_mut() {
            let mut g = Group::new("network", true);
            let (v, span) = n.sample(&mut g);
            s.set("network", v);
            network = span;
            groups.push(g);
        }
        let mut disk_io = None;
        if let Some(d) = self.disk_io.as_mut() {
            let mut g = Group::new("disk_io", true);
            let (v, span) = d.sample(&mut g);
            s.set("disk_io", v);
            disk_io = span;
            groups.push(g);
        }
        let sensors = super::sensor_entries(&s, &[], &groups.iter().collect::<Vec<_>>());
        s.set("collectors", metadata::summarize(&groups, &mut self.logged));
        Ok(Collected { stats: s, sensors, required, passive: [network, disk_io, None] })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn connect_ms_times_a_connection_and_names_failures() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (ms, why) = connect_ms("127.0.0.1", port, 2.0);
        assert!(matches!(ms, Some(m) if m >= 0.0) && why.is_none(), "{:?} {:?}", ms, why);
        assert_eq!(connect_ms("not an address", port, 2.0), (None, Some("error")));
        drop(listener);
        // nothing listens there now: refused, or timeout where Windows's retries outlast it
        let (ms, why) = connect_ms("127.0.0.1", port, 5.0);
        assert!(ms.is_none() && ["refused", "timeout"].contains(&why.unwrap()), "{:?}", why);
    }

    #[test]
    fn a_second_collection_has_the_required_data_and_cpu_usage() {
        let mut c = Collector::new(5.0, [true; 3]);
        let first = c.collect().unwrap();
        assert_eq!(first.stats.get("cpu"), Some(&Value::Arr(vec![]))); // warmup
        std::thread::sleep(std::time::Duration::from_millis(200));
        let col = c.collect().unwrap();
        let s = &col.stats;
        let int = |v: Option<&Value>| match v { Some(Value::Int(n)) => *n, other => panic!("not an int: {:?}", other) };
        let (total, used) = (int(s.get("memory").and_then(|m| m.get("total"))), int(s.get("memory").and_then(|m| m.get("used"))));
        assert!(total > 0 && (0..=total).contains(&used), "{} {}", total, used);
        let (swap_total, swap_used) = (int(s.get("memory").and_then(|m| m.get("swap_total"))),
                                       int(s.get("memory").and_then(|m| m.get("swap_used"))));
        assert!((0..=swap_total).contains(&swap_used), "{} {}", swap_total, swap_used);
        let disk = int(s.get("disk").and_then(|d| d.get("total")));
        assert!(disk > 0 && (0..=disk).contains(&int(s.get("disk").and_then(|d| d.get("used")))));
        assert!(matches!(s.get("uptime"), Some(Value::Float(u)) if *u > 0.0));
        let Some(Value::Arr(cpus)) = s.get("cpu") else { panic!("no cpu list") };
        let unavailable = match s.get("cpu_sampling").and_then(|c| c.get("unavailable")) { Some(Value::Arr(u)) => u.len(), _ => 0 };
        assert!(!cpus.is_empty() || unavailable > 0);
        // every group's processors, once each
        let active: usize = (0..unsafe { GetActiveProcessorGroupCount() }).map(|g| unsafe { GetActiveProcessorCount(g) } as usize).sum();
        assert_eq!(cpus.len() + unavailable, active);
        let ids: Vec<i64> = cpus.iter().filter_map(|c| match c.get("id") { Some(Value::Int(n)) => Some(*n), _ => None }).collect();
        assert!(ids.iter().enumerate().all(|(i, n)| *n == i as i64 || unavailable > 0), "{:?}", ids);
        for c in cpus {
            assert!(matches!(c.get("usage"), Some(Value::Float(u)) if (0.0..=100.0).contains(u)), "{:?}", c);
        }
        let system = s.get("system").unwrap();
        assert!(matches!(system.get("arch"), Some(Value::Str(a)) if a != "unknown"));
        assert!(matches!(system.get("kernel"), Some(Value::Str(k)) if k.starts_with("10.0.")), "{:?}", system);
        let collectors = s.get("collectors").unwrap();
        for name in OPTIONAL.iter().filter(|n| !READ.contains(n)) {
            let state = collectors.get(name).and_then(|g| g.get("reason"));
            assert_eq!(state, Some(&Value::Str("unsupported_platform".into())), "{}", name);
        }
        // a value has its sensor record, with a read; a VM may have no clock or temperature to give
        let sensor = |ptr: &str| col.sensors.iter().find(|(p, _)| p == ptr).map(|(_, m)| m);
        for (i, c) in cpus.iter().enumerate() {
            let record = sensor(&format!("/cpu/{}/freq", i)).unwrap_or_else(|| panic!("no record for cpu row {}", i));
            match c.get("freq") {
                Some(Value::Int(hz)) => assert!(*hz > 0 && matches!(record.get("span"), Some(Value::Arr(_))), "{:?} {:?}", c, record),
                Some(Value::Null) => assert_eq!(record.get("span"), Some(&Value::Null)),
                other => panic!("freq {:?}", other),
            }
        }
        let Some(Value::Obj(temps)) = s.get("temperature") else { panic!("no temperature table") };
        for (k, v) in temps {
            assert!(matches!(v, Value::Float(t) if (-40.0..150.0).contains(t)), "{} {:?}", k, v);
            let record = sensor(&crate::metadata::pointer(&["temperature", k])).unwrap_or_else(|| panic!("no record for {}", k));
            assert!(matches!(record.get("span"), Some(Value::Arr(_))) && record.get("unit") == Some(&Value::Str("celsius".into())));
        }
        match s.get("gpu") {
            Some(Value::Null) => assert_eq!(collectors.get("gpu").and_then(|g| g.get("reason")), Some(&Value::Str("not_detected".into()))),
            Some(gpu) => {
                assert_eq!(gpu.get("provider"), Some(&Value::Str("pdh_gpu_engine".into())));
                for k in ["usage", "freq", "max_freq"] {
                    let record = sensor(&format!("/gpu/{}", k)).unwrap_or_else(|| panic!("no record for gpu {}", k));
                    assert_eq!(gpu.get(k).map_or(true, Value::is_null), record.get("span") == Some(&Value::Null), "{} {:?}", k, gpu);
                }
            }
            None => panic!("no gpu"),
        }
        for name in READ {
            let state = collectors.get(name).and_then(|g| g.get("state"));
            assert!(matches!(state, Some(Value::Str(s)) if s == "ok" || s == "unavailable"), "{}: {:?}", name, collectors.get(name));
        }
        let network = s.get("network").unwrap();
        assert_eq!(network.get("provider"), Some(&Value::Str("if_table2".into())));
        let Some(Value::Arr(interfaces)) = network.get("interfaces") else { panic!("no interfaces") };
        assert!(!interfaces.is_empty());
        for i in interfaces {
            match i.get("reason") {
                Some(Value::Null) => assert!(matches!(i.get("rates").and_then(|r| r.get("rx_bytes_per_s")), Some(Value::Float(_)))),
                Some(Value::Str(r)) => assert!(["warmup", "counter_regressed"].contains(&r.as_str()), "{:?}", i),
                other => panic!("reason {:?}", other),
            }
        }
        let disk_io = s.get("disk_io").unwrap();
        assert_eq!(disk_io.get("provider"), Some(&Value::Str("ioctl_disk_performance".into())));
        let Some(Value::Arr(disks)) = disk_io.get("disks") else { panic!("no disks") };
        assert!(!disks.is_empty(), "{:?}", collectors.get("disk_io"));
        for d in disks {
            assert_eq!(d.get("major"), Some(&Value::Null));
            match d.get("reason") {
                Some(Value::Null) => assert!(matches!(d.get("rates").and_then(|r| r.get("io_time_ratio")), Some(Value::Float(x)) if x.is_sign_positive())),
                Some(Value::Str(r)) => assert!(["warmup", "counter_regressed"].contains(&r.as_str()), "{:?}", d),
                other => panic!("reason {:?}", other),
            }
        }
        assert!(col.passive[..2].iter().all(|p| matches!(p, Some((a, b)) if a <= b)) && col.passive[2].is_none());
        let names: Vec<&str> = col.required.iter().map(|(n, _)| *n).collect();
        assert_eq!(names, ["cpu", "memory", "disk", "uptime"]);
        assert!(col.required.iter().all(|(_, span)| matches!(span, Some((a, b)) if a <= b)));
    }
}
