//! Windows: the required data (CPU, memory, the system volume, identity) from the Win32 and NT system
//! information calls, and Network (network.rs); the other optional groups have no provider here yet
//! (unsupported_platform). Rust only: the Python service has no Windows collector to compare with.
pub mod network;

use super::{timed, Collected, CpuCounters, Required};
use crate::json::{round_to, Value};
use crate::metadata::{self, Group};
use std::collections::BTreeMap;
use windows_sys::Wdk::System::SystemInformation::{NtQuerySystemInformation, SystemProcessorPerformanceInformation};
use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY_LOCAL_MACHINE, RRF_RT_REG_DWORD, RRF_RT_REG_SZ};
use windows_sys::Win32::System::SystemInformation as si;
use windows_sys::Win32::System::WindowsProgramming::SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION;

/// The slow groups this platform has readers for: none yet.
pub mod observe {
    pub const GROUPS: [&str; 0] = [];

    pub fn reader(name: &str, _: &crate::config::Config) -> crate::slow::Reader {
        unreachable!("no {} reader on this platform", name)
    }
}

/// The optional groups of a Linux collection, each without a provider here.
const OPTIONAL: [&str; 8] = ["cpu_frequency", "gpu", "npu", "temperature", "power", "fans", "power_mode", "board_info"];

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

/// {cpu: [user, nice, system, idle, iowait, irq, softirq, steal]} in 100 ns units, for the CPUs of this
/// process's processor group (at most 64): system is kernel time without idle (it includes DPCs and
/// interrupts), the fields Windows has no counter for are 0.
fn cpu_times() -> Result<BTreeMap<i64, Vec<i64>>, String> {
    let mut buf = [SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION::default(); 64];
    let mut len = 0u32;
    let status = unsafe { NtQuerySystemInformation(SystemProcessorPerformanceInformation, buf.as_mut_ptr().cast(),
                                                   std::mem::size_of_val(&buf) as u32, &mut len) };
    if status < 0 {
        return Err(format!("processor times: NTSTATUS {:#x}", status));
    }
    let n = len as usize / std::mem::size_of::<SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION>();
    if n == 0 {
        return Err("processor times: no processors".into());
    }
    Ok(buf[..n].iter().enumerate()
        .map(|(i, p)| (i as i64, vec![p.UserTime, 0, p.KernelTime - p.IdleTime, p.IdleTime, 0, 0, 0, 0]))
        .collect())
}

/// (total, used, swap total, swap used) in bytes; swap is the paging files.
fn memory() -> Result<[i64; 4], String> {
    let mut m = si::MEMORYSTATUSEX { dwLength: std::mem::size_of::<si::MEMORYSTATUSEX>() as u32, ..Default::default() };
    if unsafe { si::GlobalMemoryStatusEx(&mut m) } == 0 {
        return Err(last_error("memory"));
    }
    let mut info = si::SYSTEM_INFO::default();
    unsafe { si::GetSystemInfo(&mut info) };
    let page = info.dwPageSize as i64;
    let mut buf = vec![0u64; 512]; // 4 KiB, 8-byte aligned: a few entries with their names
    let mut len = 0u32;
    let status = unsafe { NtQuerySystemInformation(SYSTEM_PAGE_FILE_INFORMATION, buf.as_mut_ptr().cast(), (buf.len() * 8) as u32,
                                                   &mut len) };
    if status < 0 {
        return Err(format!("paging files: NTSTATUS {:#x}", status));
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
    let total = m.ullTotalPhys as i64;
    Ok([total, total - m.ullAvailPhys as i64, swap_total, swap_used])
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
    network: Option<network::Network>,
    logged: Vec<(String, Vec<String>)>,
}

impl Collector {
    /// max_gap: seconds, max(3 × interval, 5 s) in the service; passive: network, disk_io, pressure on.
    /// Disk I/O and Pressure have no Windows reader yet and are left out, as when they are off.
    pub fn new(max_gap: f64, passive: [bool; 3]) -> Collector {
        Collector { platform: "Windows", cpu: CpuCounters::new(Some(max_gap), "interval"),
                    network: passive[0].then(|| network::Network::new(Some(max_gap))), logged: Vec::new() }
    }

    /// The fields and order of a Linux collection, the optional groups unsupported_platform.
    pub fn collect(&mut self) -> Result<Collected, String> {
        let mut required: Required = Vec::new();
        let (usage, sampling) = self.cpu.sample(cpu_times)?;
        required.push(("cpu", self.cpu.span));
        let [total, used, swap_total, swap_used] = timed(&mut required, "memory", memory)?;
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
            c.set("freq", Value::Null);
            c
        }).collect()));
        s.set("cpu_sampling", sampling);
        s.set("gpu", Value::Null);
        let mut mv = Value::obj();
        mv.set("total", total.into());
        mv.set("used", used.into());
        mv.set("swap_total", swap_total.into());
        mv.set("swap_used", swap_used.into());
        s.set("memory", mv);
        let mut dv = Value::obj();
        dv.set("total", disk.0.into());
        dv.set("used", disk.1.into());
        s.set("disk", dv);
        s.set("temperature", Value::obj());
        s.set("power", Value::obj());
        s.set("fans", Value::Arr(vec![]));
        s.set("npu", Value::Null);
        let mut groups: Vec<Group> = OPTIONAL.iter().map(|name| {
            let mut g = Group::new(name, true);
            g.note(name, "unsupported_platform", None);
            g
        }).collect();
        // after the required data: a failed collection never reaches it
        let mut network = None;
        if let Some(n) = self.network.as_mut() {
            let mut g = Group::new("network", true);
            let (v, span) = n.sample(&mut g);
            s.set("network", v);
            network = span;
            groups.push(g);
        }
        s.set("collectors", metadata::summarize(&groups, &mut self.logged));
        Ok(Collected { stats: s, sensors: Vec::new(), required, passive: [network, None, None] })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
        for c in cpus {
            assert!(matches!(c.get("usage"), Some(Value::Float(u)) if (0.0..=100.0).contains(u)), "{:?}", c);
        }
        let system = s.get("system").unwrap();
        assert!(matches!(system.get("arch"), Some(Value::Str(a)) if a != "unknown"));
        assert!(matches!(system.get("kernel"), Some(Value::Str(k)) if k.starts_with("10.0.")), "{:?}", system);
        let collectors = s.get("collectors").unwrap();
        for name in OPTIONAL {
            let state = collectors.get(name).and_then(|g| g.get("reason"));
            assert_eq!(state, Some(&Value::Str("unsupported_platform".into())), "{}", name);
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
        assert!(matches!(col.passive[0], Some((a, b)) if a <= b) && col.passive[1..] == [None, None]);
        let names: Vec<&str> = col.required.iter().map(|(n, _)| *n).collect();
        assert_eq!(names, ["cpu", "memory", "disk", "uptime"]);
        assert!(col.required.iter().all(|(_, span)| matches!(span, Some((a, b)) if a <= b)));
    }
}
