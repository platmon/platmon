//! Temperatures on Windows: each GPU's, from the adapter performance data Task Manager shows
//! (D3DKMT_ADAPTER_PERFDATA), and each drive's own report (StorageDeviceTemperatureProperty). Windows has no
//! API for the CPU's temperature, so there is none here.
use super::disk_io::{drives, error_code, Drive};
use crate::json::{round_to, Value};
use crate::metadata::{clock, source, Group, Slot};
use windows_sys::Wdk::Graphics::Direct3D::{D3DKMTCloseAdapter, D3DKMTEnumAdapters2, D3DKMTQueryAdapterInfo,
                                           D3DKMT_ADAPTERINFO, D3DKMT_ADAPTERREGISTRYINFO, D3DKMT_ADAPTERTYPE,
                                           D3DKMT_ADAPTER_PERFDATA, D3DKMT_CLOSEADAPTER, D3DKMT_ENUMADAPTERS2,
                                           D3DKMT_QUERYADAPTERINFO, KMTQAITYPE_ADAPTERPERFDATA,
                                           KMTQAITYPE_ADAPTERREGISTRYINFO, KMTQAITYPE_ADAPTERTYPE, KMTQUERYADAPTERINFOTYPE};
use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_INVALID_FUNCTION,
                                     ERROR_INVALID_PARAMETER, ERROR_NOT_SUPPORTED, ERROR_PATH_NOT_FOUND, STATUS_ACCESS_DENIED,
                                     STATUS_INVALID_PARAMETER, STATUS_NOT_IMPLEMENTED, STATUS_NOT_SUPPORTED};
use windows_sys::Win32::System::Ioctl::{PropertyStandardQuery, StorageDeviceProperty, StorageDeviceTemperatureProperty,
                                        IOCTL_STORAGE_QUERY_PROPERTY, STORAGE_DEVICE_DESCRIPTOR, STORAGE_PROPERTY_QUERY,
                                        STORAGE_TEMPERATURE_DATA_DESCRIPTOR, STORAGE_TEMPERATURE_INFO};

/// D3DKMT_ADAPTERTYPE's SoftwareDevice bit: the Basic Render Driver, which has no sensors.
const SOFTWARE_DEVICE: u32 = 1 << 2;

/// "key (suffix)" when another value already has key's name.
fn unique_key(table: &[(String, Value)], key: String, suffix: &str) -> String {
    if table.iter().any(|(k, _)| *k == key) { format!("{} ({})", key, suffix) } else { key }
}

/// Text that ends at its first NUL.
fn until_nul(units: &[u16]) -> String {
    let n = units.iter().position(|c| *c == 0).unwrap_or(units.len());
    String::from_utf16_lossy(&units[..n]).trim().to_string()
}

fn query<T>(adapter: u32, kind: KMTQUERYADAPTERINFOTYPE, out: &mut T) -> i32 {
    let mut q = D3DKMT_QUERYADAPTERINFO { hAdapter: adapter, Type: kind, pPrivateDriverData: (out as *mut T).cast(),
                                          PrivateDriverDataSize: std::mem::size_of::<T>() as u32 };
    unsafe { D3DKMTQueryAdapterInfo(&mut q) }
}

/// The open handles of the display adapters, which the caller closes.
fn adapters() -> Result<Vec<D3DKMT_ADAPTERINFO>, i32> {
    let mut e = D3DKMT_ENUMADAPTERS2 { NumAdapters: 0, pAdapters: std::ptr::null_mut() };
    let status = unsafe { D3DKMTEnumAdapters2(&mut e) }; // the count only
    if status < 0 {
        return Err(status);
    }
    let mut list = vec![D3DKMT_ADAPTERINFO::default(); e.NumAdapters as usize];
    e.pAdapters = list.as_mut_ptr();
    let status = unsafe { D3DKMTEnumAdapters2(&mut e) };
    if status < 0 {
        return Err(status);
    }
    list.truncate(e.NumAdapters as usize);
    Ok(list)
}

/// Each hardware adapter's temperature, named by the adapter (its driver's name for it).
fn gpus(g: &mut Group, temps: &mut Vec<(String, Value)>) {
    let list = match adapters() {
        Ok(list) => list,
        Err(status) => {
            let reason = if status == STATUS_ACCESS_DENIED { "permission_denied" } else { "io_error" };
            g.note("adapters", reason, Some(format!("D3DKMTEnumAdapters2: NTSTATUS {:#x}", status)));
            return;
        }
    };
    let mut n = 0i64; // the hardware adapter's position, for its target and source id
    for a in &list {
        let mut kind = D3DKMT_ADAPTERTYPE::default();
        if query(a.hAdapter, KMTQAITYPE_ADAPTERTYPE, &mut kind) >= 0 && unsafe { kind.Anonymous.Value } & SOFTWARE_DEVICE != 0 {
            continue;
        }
        let (index, target) = (n, format!("gpu{}", n));
        n += 1;
        let mut info = D3DKMT_ADAPTERREGISTRYINFO::default();
        let name = if query(a.hAdapter, KMTQAITYPE_ADAPTERREGISTRYINFO, &mut info) >= 0 { until_nul(&info.AdapterString) } else { String::new() };
        let name = if name.is_empty() { target.clone() } else { name };
        let mut perf = D3DKMT_ADAPTER_PERFDATA::default(); // physical adapter 0: the adapter itself, not linked
        let start = clock();
        let status = query(a.hAdapter, KMTQAITYPE_ADAPTERPERFDATA, &mut perf);
        let span = Some((start, clock()));
        match status {
            // in tenths of a degree; 0 where the driver reports none (an integrated GPU, as Task Manager shows it)
            s if s >= 0 && perf.Temperature > 0 => {
                let key = unique_key(temps, name.clone(), &target);
                let v = g.got(round_to(perf.Temperature as f64 / 10.0, 1));
                temps.push((key.clone(), v.into()));
                g.sensor(Slot::Key(key), Some(source("d3dkmt", "celsius", "device_channel",
                    vec![("adapter", index.into()), ("name", name.into()), ("attr", "Temperature".into())])), span);
            }
            s if s >= 0 => g.note(&target, "not_exposed", None),
            STATUS_INVALID_PARAMETER | STATUS_NOT_SUPPORTED | STATUS_NOT_IMPLEMENTED => g.note(&target, "not_exposed", None),
            STATUS_ACCESS_DENIED => g.note(&target, "permission_denied", None),
            s => g.note(&target, "io_error", Some(format!("adapter performance data: NTSTATUS {:#x}", s))),
        }
    }
    for a in &list {
        unsafe { D3DKMTCloseAdapter(&D3DKMT_CLOSEADAPTER { hAdapter: a.hAdapter }) };
    }
}

/// The drive's product id (its model), None if it has none.
fn product(drive: &Drive) -> Option<String> {
    let q = STORAGE_PROPERTY_QUERY { PropertyId: StorageDeviceProperty, QueryType: PropertyStandardQuery, AdditionalParameters: [0] };
    let mut buf = [0u64; 128]; // the descriptor and its strings, 8-byte aligned
    let len = drive.control(IOCTL_STORAGE_QUERY_PROPERTY, Some(&q), &mut buf).ok()?;
    let bytes: &[u8] = unsafe { std::slice::from_raw_parts(buf.as_ptr().cast(), len.min(std::mem::size_of_val(&buf))) };
    if bytes.len() < std::mem::size_of::<STORAGE_DEVICE_DESCRIPTOR>() {
        return None;
    }
    let d = unsafe { &*buf.as_ptr().cast::<STORAGE_DEVICE_DESCRIPTOR>() };
    let text = bytes.get(d.ProductIdOffset as usize..).filter(|_| d.ProductIdOffset != 0)?;
    let end = text.iter().position(|b| *b == 0).unwrap_or(text.len());
    Some(String::from_utf8_lossy(&text[..end]).trim().to_string()).filter(|s| !s.is_empty())
}

/// The drive's temperature in °C: its first sensor's (index 0 is the device's own, NVMe's composite).
fn drive_temperature(drive: &Drive) -> Result<Option<i16>, std::io::Error> {
    let q = STORAGE_PROPERTY_QUERY { PropertyId: StorageDeviceTemperatureProperty, QueryType: PropertyStandardQuery,
                                     AdditionalParameters: [0] };
    let mut buf = [0u64; 64]; // the descriptor and up to 22 sensors, 8-byte aligned
    let len = drive.control(IOCTL_STORAGE_QUERY_PROPERTY, Some(&q), &mut buf)?;
    let head = std::mem::offset_of!(STORAGE_TEMPERATURE_DATA_DESCRIPTOR, TemperatureInfo);
    let size = std::mem::size_of::<STORAGE_TEMPERATURE_INFO>();
    let d = unsafe { &*buf.as_ptr().cast::<STORAGE_TEMPERATURE_DATA_DESCRIPTOR>() };
    let count = (d.InfoCount as usize).min(len.saturating_sub(head) / size);
    let info = |i: usize| unsafe { &*buf.as_ptr().cast::<u8>().add(head + i * size).cast::<STORAGE_TEMPERATURE_INFO>() };
    Ok((0..count).map(info).find(|t| t.Index == 0).or_else(|| (count > 0).then(|| info(0))).map(|t| t.Temperature))
}

/// Each drive's temperature, named by its model.
fn disks(g: &mut Group, temps: &mut Vec<(String, Value)>) {
    let names = match drives() {
        Ok(names) => names,
        Err(e) => {
            let reason = if error_code(&e) == ERROR_ACCESS_DENIED { "permission_denied" } else { "io_error" };
            g.note("dos_devices", reason, Some(format!("QueryDosDeviceW: {}", e)));
            return;
        }
    };
    for name in names {
        let start = clock();
        let read = Drive::open(&name).and_then(|d| drive_temperature(&d).map(|t| (t, d)));
        let span = Some((start, clock()));
        match read {
            Ok((Some(t), drive)) => {
                let key = unique_key(temps, product(&drive).unwrap_or_else(|| name.clone()), &name);
                let v = g.got(t as f64);
                temps.push((key.clone(), v.into()));
                g.sensor(Slot::Key(key), Some(source("storage_temperature", "celsius", "device_channel",
                    vec![("drive", name.as_str().into()), ("attr", "Temperature".into())])), span);
            }
            Ok((None, _)) => g.note(&name, "not_exposed", None),
            Err(e) => {
                let reason = match error_code(&e) {
                    ERROR_ACCESS_DENIED => "permission_denied",
                    ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND => "disappeared",
                    // no temperature from this drive or its bus (a virtual disk, most USB bridges)
                    ERROR_INVALID_FUNCTION | ERROR_NOT_SUPPORTED | ERROR_INVALID_PARAMETER => "not_exposed",
                    _ => "io_error",
                };
                g.note(&name, reason, Some(format!("storage temperature: {}", e)));
            }
        }
    }
}

/// {name: °C} of the GPUs, then the drives.
pub fn read(g: &mut Group) -> Vec<(String, Value)> {
    let mut temps = Vec::new();
    gpus(g, &mut temps);
    disks(g, &mut temps);
    if temps.is_empty() && g.reasons.is_empty() {
        g.note("temperature", "not_detected", None);
    }
    temps
}
