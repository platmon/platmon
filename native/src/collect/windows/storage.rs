//! Storage on Windows (the storage slow group, docs/reference/storage.md#windows): the volumes Windows lists
//! (FindFirstVolumeW) on a fixed or removable disk with at least one mount point, named by their volume GUID,
//! with GetDiskFreeSpaceExW's capacity, and the partitions of each PhysicalDriveN
//! (IOCTL_DISK_GET_DRIVE_LAYOUT_EX), linked to their volume by its disk extent. Nothing is opened for reading
//! or writing: the drive and volume handles have no access rights.
use super::disk_io::{drives, error_code, Drive};
use crate::json::Value;
use crate::metadata::Group;
use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_MORE_DATA, ERROR_NOT_READY,
                                     ERROR_NO_MORE_FILES, ERROR_PATH_NOT_FOUND, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{FindFirstVolumeW, FindNextVolumeW, FindVolumeClose, GetDiskFreeSpaceExW,
                                              GetDriveTypeW, GetVolumeInformationW, GetVolumePathNamesForVolumeNameW,
                                              IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS};
use windows_sys::Win32::System::Ioctl::{DISK_EXTENT, DRIVE_LAYOUT_INFORMATION_EX, IOCTL_DISK_GET_DRIVE_LAYOUT_EX,
                                        PARTITION_INFORMATION_EX, VOLUME_DISK_EXTENTS};
use windows_sys::Win32::System::WindowsProgramming::{DRIVE_FIXED, DRIVE_REMOVABLE};

/// FILE_READ_ONLY_VOLUME (winnt.h; windows-sys has it under another feature).
const READ_ONLY_VOLUME: u32 = 0x0008_0000;

fn obj(items: Vec<(&str, Value)>) -> Value {
    Value::Obj(items.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(Some(0)).collect()
}

fn until_nul(units: &[u16]) -> String {
    let n = units.iter().position(|c| *c == 0).unwrap_or(units.len());
    String::from_utf16_lossy(&units[..n])
}

fn reason(e: &std::io::Error) -> &'static str {
    match error_code(e) {
        ERROR_ACCESS_DENIED => "permission_denied",
        ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND => "disappeared",
        ERROR_NOT_READY => "no_data", // an empty card reader
        _ => "io_error",
    }
}

/// The volume GUID paths (\\?\Volume{…}\) Windows lists.
fn volumes() -> Result<Vec<String>, std::io::Error> {
    let mut buf = [0u16; 64];
    let h = unsafe { FindFirstVolumeW(buf.as_mut_ptr(), buf.len() as u32) };
    if h == INVALID_HANDLE_VALUE {
        return Err(std::io::Error::last_os_error());
    }
    let mut out = vec![until_nul(&buf)];
    let result = loop {
        if unsafe { FindNextVolumeW(h, buf.as_mut_ptr(), buf.len() as u32) } == 0 {
            let e = std::io::Error::last_os_error();
            break if error_code(&e) == ERROR_NO_MORE_FILES { Ok(()) } else { Err(e) };
        }
        out.push(until_nul(&buf));
    };
    unsafe { FindVolumeClose(h) };
    result.map(|_| out)
}

/// The drive letters and folders a volume is mounted at.
fn mount_points(volume: &str) -> Result<Vec<String>, std::io::Error> {
    let path = wide(volume);
    let mut buf = vec![0u16; 1024];
    loop {
        let mut len = 0u32;
        if unsafe { GetVolumePathNamesForVolumeNameW(path.as_ptr(), buf.as_mut_ptr(), buf.len() as u32, &mut len) } != 0 {
            return Ok(buf[..(len as usize).min(buf.len())].split(|c| *c == 0).filter(|s| !s.is_empty())
                .map(String::from_utf16_lossy).collect());
        }
        let e = std::io::Error::last_os_error();
        if error_code(&e) != ERROR_MORE_DATA || buf.len() >= 1 << 20 {
            return Err(e);
        }
        buf.resize((len as usize).max(buf.len() * 2), 0);
    }
}

/// (file system name, read only) of a mounted volume.
fn volume_info(volume: &str) -> Result<(String, bool), std::io::Error> {
    let path = wide(volume);
    let (mut flags, mut fs) = (0u32, [0u16; 64]);
    if unsafe { GetVolumeInformationW(path.as_ptr(), std::ptr::null_mut(), 0, std::ptr::null_mut(), std::ptr::null_mut(),
                                      &mut flags, fs.as_mut_ptr(), fs.len() as u32) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok((until_nul(&fs), flags & READ_ONLY_VOLUME != 0))
}

/// (total, total free, free to this user) bytes.
fn capacity(volume: &str) -> Result<(u64, u64, u64), std::io::Error> {
    let path = wide(volume);
    let (mut avail, mut total, mut free) = (0u64, 0u64, 0u64);
    if unsafe { GetDiskFreeSpaceExW(path.as_ptr(), &mut avail, &mut total, &mut free) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok((total, free, avail))
}

/// The (disk number, starting offset) of each extent of a volume.
fn extents(volume: &str) -> Result<Vec<(u32, i64)>, std::io::Error> {
    // \\?\Volume{…}\ opens the root directory; without the backslash, the volume
    let device = volume.trim_start_matches("\\\\?\\").trim_end_matches('\\');
    let mut buf = [0u64; 64]; // the count and up to 21 extents, 8-byte aligned
    let len = Drive::open(device)?.control::<(), _>(IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, &mut buf)?;
    let head = std::mem::offset_of!(VOLUME_DISK_EXTENTS, Extents);
    let size = std::mem::size_of::<DISK_EXTENT>();
    let d = unsafe { &*buf.as_ptr().cast::<VOLUME_DISK_EXTENTS>() };
    let count = (d.NumberOfDiskExtents as usize).min(len.saturating_sub(head) / size);
    Ok((0..count).map(|i| unsafe { &*buf.as_ptr().cast::<u8>().add(head + i * size).cast::<DISK_EXTENT>() })
        .map(|e| (e.DiskNumber, e.StartingOffset)).collect())
}

/// The partitions of a drive: (number, starting offset, length), the unused MBR slots left out.
fn layout(drive: &str) -> Result<Vec<(u32, i64, i64)>, std::io::Error> {
    let mut buf = [0u64; 1024]; // the header and up to 56 partitions, 8-byte aligned
    let len = Drive::open(drive)?.control::<(), _>(IOCTL_DISK_GET_DRIVE_LAYOUT_EX, None, &mut buf)?;
    let head = std::mem::offset_of!(DRIVE_LAYOUT_INFORMATION_EX, PartitionEntry);
    let size = std::mem::size_of::<PARTITION_INFORMATION_EX>();
    let d = unsafe { &*buf.as_ptr().cast::<DRIVE_LAYOUT_INFORMATION_EX>() };
    let count = (d.PartitionCount as usize).min(len.saturating_sub(head) / size);
    Ok((0..count).map(|i| unsafe { &*buf.as_ptr().cast::<u8>().add(head + i * size).cast::<PARTITION_INFORMATION_EX>() })
        .filter(|p| p.PartitionNumber > 0 && p.PartitionLength > 0)
        .map(|p| (p.PartitionNumber, p.StartingOffset, p.PartitionLength)).collect())
}

/// The storage group's data: {partitions, filesystems}, as on Linux with Windows's names.
pub fn storage(g: &mut Group) -> Value {
    // (first mount point, entry, (disk number, offset) of each extent, mount points)
    let mut filesystems: Vec<(String, Value, Vec<(u32, i64)>, Vec<String>)> = Vec::new();
    match volumes() {
        Ok(list) => for volume in list {
            let kind = unsafe { GetDriveTypeW(wide(&volume).as_ptr()) };
            if kind != DRIVE_FIXED && kind != DRIVE_REMOVABLE {
                continue; // optical, network and RAM disks: not disk space
            }
            let guid = volume.trim_start_matches("\\\\?\\").trim_end_matches('\\').to_string();
            let points = match mount_points(&volume) {
                Ok(p) if p.is_empty() => continue, // not mounted: listed as a partition only
                Ok(p) => p,
                Err(e) => { g.note(&guid, reason(&e), Some(format!("mount points: {}", e))); continue }
            };
            let (fstype, read_only) = match volume_info(&volume) {
                Ok((fs, ro)) => (Value::from(fs), ro),
                // an empty card reader or a locked BitLocker volume: mounted, with nothing to measure
                Err(e) => { g.note(&guid, reason(&e), Some(format!("volume information: {}", e))); (Value::Null, false) }
            };
            let mut fs = obj(vec![("device", guid.as_str().into()), ("major", Value::Null), ("minor", Value::Null),
                                  ("fstype", fstype), ("source", volume.as_str().into()),
                                  ("mount_points", Value::Arr(points.iter().map(|p| obj(vec![("path", p.as_str().into()),
                                      ("read_only", Value::Bool(read_only))])).collect())),
                                  ("total_bytes", Value::Null), ("used_bytes", Value::Null), ("available_bytes", Value::Null)]);
            if !fs.get("fstype").map_or(true, Value::is_null) {
                match capacity(&volume) {
                    Ok((total, free, avail)) => {
                        g.got(());
                        fs.set("total_bytes", Value::Big(total as u128));
                        fs.set("used_bytes", Value::Big(total.saturating_sub(free) as u128));
                        fs.set("available_bytes", Value::Big(avail as u128));
                    }
                    Err(e) => g.note(&guid, reason(&e), Some(format!("GetDiskFreeSpaceExW {}: {}", points[0], e))),
                }
            }
            let ext = extents(&volume).unwrap_or_default(); // a volume on no disk (a VHD being detached) links to none
            filesystems.push((points[0].clone(), fs, ext, points));
        },
        Err(e) => g.note("volumes", reason(&e), Some(format!("FindFirstVolumeW: {}", e))),
    }
    let mut partitions = Vec::new();
    match drives() {
        Ok(names) => for drive in names {
            let Some(number) = drive.strip_prefix("PhysicalDrive").and_then(|n| n.parse::<u32>().ok()) else { continue };
            match layout(&drive) {
                Ok(parts) => for (n, offset, length) in parts {
                    let points: Vec<Value> = filesystems.iter().filter(|(_, _, ext, _)| ext.contains(&(number, offset)))
                        .flat_map(|(_, _, _, p)| p.iter().map(|x| Value::from(x.as_str()))).collect();
                    g.got(());
                    partitions.push(obj(vec![("name", format!("Harddisk{}Partition{}", number, n).into()),
                                             ("disk", drive.as_str().into()), ("major", Value::Null), ("minor", Value::Null),
                                             ("size_bytes", length.into()), ("mount_points", Value::Arr(points))]));
                },
                Err(e) => g.note(&drive, reason(&e), Some(format!("drive layout: {}", e))),
            }
        },
        Err(e) => g.note("dos_devices", reason(&e), Some(format!("QueryDosDeviceW: {}", e))),
    }
    filesystems.sort_by(|a, b| a.0.cmp(&b.0));
    if filesystems.is_empty() && partitions.is_empty() && g.reasons.is_empty() {
        g.note("storage", "not_detected", None);
    }
    obj(vec![("partitions", Value::Arr(partitions)),
             ("filesystems", Value::Arr(filesystems.into_iter().map(|(_, fs, _, _)| fs).collect()))])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_system_volume_and_its_partition_are_listed() {
        let mut g = Group::new("storage", false);
        let data = storage(&mut g);
        let Some(Value::Arr(filesystems)) = data.get("filesystems") else { panic!("no filesystems: {:?}", data) };
        let Some(Value::Arr(partitions)) = data.get("partitions") else { panic!("no partitions: {:?}", data) };
        let windows = std::env::var("SystemDrive").unwrap_or_else(|_| "C:".into()) + "\\";
        let system = filesystems.iter().find(|fs| matches!(fs.get("mount_points"), Some(Value::Arr(m))
            if m.iter().any(|p| p.get("path") == Some(&Value::Str(windows.clone())))))
            .unwrap_or_else(|| panic!("{} not listed: {:?} {:?}", windows, filesystems, g.reasons));
        assert!(matches!(system.get("device"), Some(Value::Str(d)) if d.starts_with("Volume{")), "{:?}", system);
        let (total, used) = (system.get("total_bytes"), system.get("used_bytes"));
        assert!(matches!((total, used), (Some(Value::Big(t)), Some(Value::Big(u))) if *t > 0 && u <= t), "{:?}", system);
        for p in partitions {
            assert!(matches!(p.get("disk"), Some(Value::Str(d)) if d.starts_with("PhysicalDrive")), "{:?}", p);
        }
        // the system volume's partition, unless it sits on a disk Windows cannot give the layout of (a VM's)
        assert!(partitions.is_empty() || partitions.iter().any(|p| matches!(p.get("mount_points"), Some(Value::Arr(m))
            if m.contains(&Value::Str(windows.clone())))), "{:?}", partitions);
    }
}
