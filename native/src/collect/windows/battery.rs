//! Battery and external power on Windows (the battery slow group, docs/reference/battery.md#windows): each
//! battery device interface (GUID_DEVICE_BATTERY) asked through the battery class's IOCTLs, as Windows's
//! own battery meter does, and the AC line from GetSystemPowerStatus. Read access only: nothing is set.
use super::disk_io::error_code;
use crate::json::Value;
use crate::metadata::Group;
use windows_sys::Win32::Devices::DeviceAndDriverInstallation::{CM_Get_Device_Interface_ListW, CM_Get_Device_Interface_List_SizeW,
                                                               CM_GET_DEVICE_INTERFACE_LIST_PRESENT, CR_SUCCESS};
use windows_sys::Win32::Foundation::{CloseHandle, ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_NO_SUCH_DEVICE,
                                     ERROR_PATH_NOT_FOUND, GENERIC_READ, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{CreateFileW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING};
use windows_sys::Win32::System::Power::{BatteryInformation, GetSystemPowerStatus, BATTERY_CAPACITY_RELATIVE, BATTERY_CHARGING,
                                        BATTERY_CRITICAL, BATTERY_DISCHARGING, BATTERY_INFORMATION, BATTERY_QUERY_INFORMATION,
                                        BATTERY_STATUS, BATTERY_UNKNOWN_CAPACITY, BATTERY_UNKNOWN_RATE, BATTERY_UNKNOWN_VOLTAGE,
                                        BATTERY_WAIT_STATUS, GUID_DEVICE_BATTERY, IOCTL_BATTERY_QUERY_INFORMATION,
                                        IOCTL_BATTERY_QUERY_STATUS, IOCTL_BATTERY_QUERY_TAG, SYSTEM_POWER_STATUS};
use windows_sys::Win32::System::IO::DeviceIoControl;

const CHARGE_ENERGY: [&str; 6] = ["charge_now_uah", "charge_full_uah", "charge_full_design_uah", "energy_now_uwh",
                                  "energy_full_uwh", "energy_full_design_uwh"];
const OTHER: [&str; 4] = ["current_ua", "power_uw", "voltage_uv", "cycle_count"];
const TEXT: [&str; 4] = ["status", "capacity_level", "technology", "health"];

fn obj(items: Vec<(&str, Value)>) -> Value {
    Value::Obj(items.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn reason(e: &std::io::Error) -> &'static str {
    match error_code(e) {
        ERROR_ACCESS_DENIED => "permission_denied",
        ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND | ERROR_NO_SUCH_DEVICE => "disappeared",
        _ => "io_error",
    }
}

/// The present battery interfaces' device paths, in Windows's order.
fn interfaces() -> Result<Vec<String>, String> {
    for _ in 0..4 { // an interface can arrive between the size and the list
        let mut len = 0u32;
        let rc = unsafe { CM_Get_Device_Interface_List_SizeW(&mut len, &GUID_DEVICE_BATTERY, std::ptr::null(),
                                                             CM_GET_DEVICE_INTERFACE_LIST_PRESENT) };
        if rc != CR_SUCCESS {
            return Err(format!("CM_Get_Device_Interface_List_SizeW: CONFIGRET {}", rc));
        }
        let mut buf = vec![0u16; len as usize];
        let rc = unsafe { CM_Get_Device_Interface_ListW(&GUID_DEVICE_BATTERY, std::ptr::null(), buf.as_mut_ptr(), len,
                                                        CM_GET_DEVICE_INTERFACE_LIST_PRESENT) };
        if rc == CR_SUCCESS {
            return Ok(buf.split(|c| *c == 0).filter(|s| !s.is_empty()).map(String::from_utf16_lossy).collect());
        }
    }
    Err("CM_Get_Device_Interface_ListW: the list kept growing".into())
}

/// A battery's handle, opened for reading; closed when dropped.
struct Battery(HANDLE);

impl Battery {
    fn open(path: &str) -> Result<Battery, std::io::Error> {
        let path: Vec<u16> = path.encode_utf16().chain(Some(0)).collect();
        let h = unsafe { CreateFileW(path.as_ptr(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, std::ptr::null(),
                                     OPEN_EXISTING, 0, std::ptr::null_mut()) };
        if h == INVALID_HANDLE_VALUE {
            return Err(std::io::Error::last_os_error());
        }
        Ok(Battery(h))
    }

    fn control<I, O: Default>(&self, code: u32, input: &I) -> Result<O, std::io::Error> {
        let (mut out, mut len) = (O::default(), 0u32);
        let ok = unsafe { DeviceIoControl(self.0, code, (input as *const I).cast(), std::mem::size_of::<I>() as u32,
                                          (&mut out as *mut O).cast(), std::mem::size_of::<O>() as u32, &mut len,
                                          std::ptr::null_mut()) };
        if ok == 0 { Err(std::io::Error::last_os_error()) } else { Ok(out) }
    }
}

impl Drop for Battery {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

/// The status in the words Linux drivers use for the same states (Windows reports flags, not words).
fn status_text(power_state: u32) -> Option<&'static str> {
    if power_state & BATTERY_CHARGING != 0 {
        Some("Charging")
    } else if power_state & BATTERY_DISCHARGING != 0 {
        Some("Discharging")
    } else if power_state & 1 != 0 { // BATTERY_POWER_ON_LINE: on AC, neither charging nor discharging
        Some("Not charging")
    } else {
        None
    }
}

/// One battery's entry from its information and status; None where Windows reports a value as unknown.
fn entry(name: &str, info: &BATTERY_INFORMATION, status: &BATTERY_STATUS) -> Value {
    let mut item = obj(vec![("name", name.into()), ("present", Value::Bool(true)), ("percent", Value::Null)]);
    for f in TEXT.iter().chain(CHARGE_ENERGY.iter()).chain(OTHER.iter()) {
        item.set(f, Value::Null);
    }
    let known = |v: u32| (v != BATTERY_UNKNOWN_CAPACITY).then_some(v as i64);
    let (now, full) = (known(status.Capacity), known(info.FullChargedCapacity).filter(|f| *f > 0));
    if let (Some(n), Some(f)) = (now, full) {
        item.set("percent", ((100 * n + f / 2) / f).clamp(0, 100).into()); // as Windows rounds its battery meter
    }
    if info.Capabilities & BATTERY_CAPACITY_RELATIVE == 0 { // mWh; a relative battery counts in its own units
        item.set("energy_now_uwh", now.map(|v| v * 1000).into());
        item.set("energy_full_uwh", full.map(|v| v * 1000).into());
        item.set("energy_full_design_uwh", known(info.DesignedCapacity).filter(|d| *d > 0).map(|v| v * 1000).into());
    }
    if status.Voltage != BATTERY_UNKNOWN_VOLTAGE {
        item.set("voltage_uv", (status.Voltage as i64 * 1000).into());
    }
    if status.Rate as u32 != BATTERY_UNKNOWN_RATE {
        item.set("power_uw", (status.Rate as i64 * 1000).into()); // negative while discharging, as Windows signs it
    }
    if info.CycleCount > 0 { // 0: no cycle counter
        item.set("cycle_count", (info.CycleCount as i64).into());
    }
    item.set("status", status_text(status.PowerState).into());
    if status.PowerState & BATTERY_CRITICAL != 0 {
        item.set("capacity_level", "Critical".into());
    }
    let chem = String::from_utf8_lossy(&info.Chemistry).trim_matches(|c: char| c == '\0' || c.is_whitespace()).to_string();
    if !chem.is_empty() {
        item.set("technology", chem.into());
    }
    item
}

/// An empty bay: what the interface still holds is not a reading of something that is not there.
fn empty_bay(g: &mut Group, name: &str) -> Value {
    g.got(());
    let mut item = obj(vec![("name", name.into()), ("present", Value::Bool(false)), ("percent", Value::Null)]);
    for f in TEXT.iter().chain(CHARGE_ENERGY.iter()).chain(OTHER.iter()) {
        item.set(f, Value::Null);
    }
    item
}

/// The battery group's data: {provider, batteries, external}.
pub fn battery(g: &mut Group) -> Value {
    let mut batteries = Vec::new();
    match interfaces() {
        Ok(paths) => for (i, path) in paths.iter().enumerate() {
            let name = format!("BAT{}", i);
            let read = Battery::open(path).and_then(|b| {
                let tag: u32 = b.control(IOCTL_BATTERY_QUERY_TAG, &0u32)?; // 0 ms: do not wait for a battery to arrive
                if tag == 0 {
                    return Ok(None);
                }
                let q = BATTERY_QUERY_INFORMATION { BatteryTag: tag, InformationLevel: BatteryInformation, AtRate: 0 };
                let info: BATTERY_INFORMATION = b.control(IOCTL_BATTERY_QUERY_INFORMATION, &q)?;
                let w = BATTERY_WAIT_STATUS { BatteryTag: tag, ..Default::default() };
                let status: BATTERY_STATUS = b.control(IOCTL_BATTERY_QUERY_STATUS, &w)?;
                Ok(Some((info, status)))
            });
            match read {
                Ok(Some((info, status))) => {
                    g.got(());
                    batteries.push(entry(&name, &info, &status));
                }
                Ok(None) => batteries.push(empty_bay(g, &name)),
                Err(e) if error_code(&e) == ERROR_NO_SUCH_DEVICE => batteries.push(empty_bay(g, &name)),
                Err(e) => g.note(&name, reason(&e), Some(format!("{}: {}", path, e))),
            }
        },
        Err(e) => g.note("batteries", "io_error", Some(e)),
    }
    let mut external = Vec::new();
    let mut s = SYSTEM_POWER_STATUS::default();
    if unsafe { GetSystemPowerStatus(&mut s) } != 0 {
        g.got(());
        let online = match s.ACLineStatus { 0 => Value::Bool(false), 1 => Value::Bool(true), _ => Value::Null };
        external.push(obj(vec![("name", "AC".into()), ("type", "Mains".into()), ("online", online)]));
    } else {
        g.note("AC", "io_error", Some(format!("GetSystemPowerStatus: {}", std::io::Error::last_os_error())));
    }
    if batteries.is_empty() && g.reasons.is_empty() {
        g.note("power_supply", "not_detected", None);
    }
    obj(vec![("provider", "ioctl_battery".into()), ("batteries", Value::Arr(batteries)), ("external", Value::Arr(external))])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_reading_keeps_windows_units_and_leaves_unknown_values_null() {
        let info = BATTERY_INFORMATION { Chemistry: *b"LION", DesignedCapacity: 62115, FullChargedCapacity: 52496,
                                         CycleCount: 238, ..Default::default() };
        let status = BATTERY_STATUS { PowerState: BATTERY_DISCHARGING, Capacity: 32023, Voltage: 14862, Rate: -34614 };
        let v = entry("BAT0", &info, &status);
        let get = |k: &str| v.get(k).cloned().unwrap_or_else(|| panic!("no {}", k));
        assert_eq!(get("percent"), Value::Int(61));
        assert_eq!(get("energy_now_uwh"), Value::Int(32_023_000));
        assert_eq!(get("energy_full_design_uwh"), Value::Int(62_115_000));
        assert_eq!(get("power_uw"), Value::Int(-34_614_000));
        assert_eq!(get("voltage_uv"), Value::Int(14_862_000));
        assert_eq!((get("status"), get("technology"), get("cycle_count")),
                   (Value::from("Discharging"), Value::from("LION"), Value::Int(238)));
        assert_eq!((get("charge_now_uah"), get("current_ua"), get("health")), (Value::Null, Value::Null, Value::Null));

        // unknown values, a relative gauge, no cycle counter, on AC and not charging
        let info = BATTERY_INFORMATION { Capabilities: BATTERY_CAPACITY_RELATIVE, FullChargedCapacity: 100, ..Default::default() };
        let status = BATTERY_STATUS { PowerState: 1 | BATTERY_CRITICAL, Capacity: 7, Voltage: BATTERY_UNKNOWN_VOLTAGE,
                                      Rate: BATTERY_UNKNOWN_RATE as i32 };
        let v = entry("BAT1", &info, &status);
        let get = |k: &str| v.get(k).cloned().unwrap();
        assert_eq!((get("percent"), get("energy_now_uwh"), get("voltage_uv"), get("power_uw"), get("cycle_count")),
                   (Value::Int(7), Value::Null, Value::Null, Value::Null, Value::Null));
        assert_eq!((get("status"), get("capacity_level"), get("technology")),
                   (Value::from("Not charging"), Value::from("Critical"), Value::Null));
    }

    #[test]
    fn the_group_lists_batteries_and_the_ac_line() {
        let mut g = Group::new("battery", false);
        let data = battery(&mut g);
        assert_eq!(data.get("provider"), Some(&Value::from("ioctl_battery")));
        let Some(Value::Arr(external)) = data.get("external") else { panic!("{:?}", data) };
        assert_eq!(external.len(), 1, "{:?}", external);
        let Some(Value::Arr(batteries)) = data.get("batteries") else { panic!("{:?}", data) };
        for b in batteries {
            assert!(matches!(b.get("percent"), Some(Value::Null) | Some(Value::Int(0..=100))), "{:?}", b);
        }
        assert!(g.reasons.iter().all(|r| *r == "not_detected"), "{:?} {:?}", g.reasons, g.details);
    }
}
