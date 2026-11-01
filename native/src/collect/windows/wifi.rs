//! Wi-Fi on Windows (the wifi slow group, docs/reference/wifi.md#windows): each wireless interface the WLAN
//! service lists, its connection state and the signal it last measured (RSSI, dBm). Passive: no scan, no
//! connection, no SSID, BSSID or nearby networks read.
use crate::json::Value;
use crate::metadata::Group;
use windows_sys::core::GUID;
use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_INVALID_STATE, ERROR_NOT_FOUND, ERROR_SERVICE_NOT_ACTIVE,
                                     HANDLE};
use windows_sys::Win32::NetworkManagement::IpHelper::{ConvertInterfaceGuidToLuid, ConvertInterfaceLuidToAlias,
                                                      ConvertInterfaceLuidToIndex};
use windows_sys::Win32::NetworkManagement::Ndis::NET_LUID_LH;
use windows_sys::Win32::NetworkManagement::WiFi::{wlan_interface_state_connected, wlan_interface_state_disconnected,
                                                  wlan_interface_state_not_ready, wlan_intf_opcode_rssi, WlanCloseHandle,
                                                  WlanEnumInterfaces, WlanFreeMemory, WlanOpenHandle, WlanQueryInterface,
                                                  WLAN_INTERFACE_INFO, WLAN_INTERFACE_INFO_LIST};

fn obj(items: Vec<(&str, Value)>) -> Value {
    Value::Obj(items.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

fn reason(code: u32) -> &'static str {
    match code {
        ERROR_ACCESS_DENIED => "permission_denied",
        ERROR_NOT_FOUND => "disappeared",
        _ => "io_error",
    }
}

/// The interface's name as Windows shows it (its alias, as in network) and its index.
fn identity(guid: &GUID) -> Option<(String, i64)> {
    let mut luid = NET_LUID_LH { Value: 0 };
    if unsafe { ConvertInterfaceGuidToLuid(guid, &mut luid) } != 0 {
        return None;
    }
    let (mut alias, mut index) = ([0u16; 257], 0u32);
    if unsafe { ConvertInterfaceLuidToAlias(&luid, alias.as_mut_ptr(), alias.len()) } != 0
        || unsafe { ConvertInterfaceLuidToIndex(&luid, &mut index) } != 0 {
        return None;
    }
    let n = alias.iter().position(|c| *c == 0).unwrap_or(alias.len());
    Some((String::from_utf16_lossy(&alias[..n]), index as i64))
}

/// The RSSI (dBm) the interface last measured.
fn rssi(client: HANDLE, guid: &GUID) -> Result<i32, u32> {
    let (mut size, mut data, mut kind) = (0u32, std::ptr::null_mut(), 0);
    let rc = unsafe { WlanQueryInterface(client, guid, wlan_intf_opcode_rssi, std::ptr::null(), &mut size, &mut data, &mut kind) };
    if rc != 0 {
        return Err(rc);
    }
    let value = (size as usize >= 4).then(|| unsafe { *data.cast::<i32>() });
    unsafe { WlanFreeMemory(data) };
    value.ok_or(ERROR_NOT_FOUND)
}

/// A connected interface's state and signal from its RSSI query, and the issue to note, if any. While the
/// link goes down, Windows still lists it connected for some seconds (measured: from "security stopped"
/// to "disconnected", 10 s) but refuses the RSSI with ERROR_INVALID_STATE: the link is changing, unknown.
fn connected_link(rssi: Result<i32, u32>) -> (&'static str, Value, Option<(&'static str, Option<String>)>) {
    match rssi {
        Ok(dbm) if dbm < 0 => ("connected", Value::Int(dbm as i64), None),
        Ok(_) => ("connected", Value::Null, Some(("invalid_data", None))),
        Err(ERROR_INVALID_STATE) => ("unknown", Value::Null, None),
        Err(rc) => ("connected", Value::Null, Some((reason(rc), Some(format!("RSSI: {}", std::io::Error::from_raw_os_error(rc as i32)))))),
    }
}

/// The wifi group's data: {scope, provider, interfaces}, as on Linux.
pub fn wifi(g: &mut Group) -> Value {
    let out = |interfaces: Vec<Value>| obj(vec![("scope", obj(vec![("kind", "host".into()), ("id", Value::Null)])),
                                                ("provider", "wlanapi".into()), ("interfaces", Value::Arr(interfaces))]);
    let (mut client, mut version): (HANDLE, u32) = (std::ptr::null_mut(), 0);
    match unsafe { WlanOpenHandle(2, std::ptr::null(), &mut version, &mut client) } {
        0 => {}
        // no WLAN service: a host without Wi-Fi hardware, or Windows Server without the feature
        ERROR_SERVICE_NOT_ACTIVE => { g.note("wlan", "not_exposed", None); return out(vec![]); }
        rc => { g.note("wlan", reason(rc), Some(format!("WlanOpenHandle: {}", std::io::Error::from_raw_os_error(rc as i32)))); return out(vec![]); }
    }
    let mut list: *mut WLAN_INTERFACE_INFO_LIST = std::ptr::null_mut();
    let rc = unsafe { WlanEnumInterfaces(client, std::ptr::null(), &mut list) };
    let mut interfaces = Vec::new();
    if rc != 0 {
        g.note("wlan", reason(rc), Some(format!("WlanEnumInterfaces: {}", std::io::Error::from_raw_os_error(rc as i32))));
    } else {
        let count = unsafe { (*list).dwNumberOfItems } as usize;
        let items: &[WLAN_INTERFACE_INFO] = unsafe { std::slice::from_raw_parts(std::ptr::addr_of!((*list).InterfaceInfo).cast(), count) };
        for item in items {
            let Some((name, ifindex)) = identity(&item.InterfaceGuid) else { continue }; // gone since the listing
            let mut state = match item.isState {
                s if s == wlan_interface_state_connected => "connected",
                s if s == wlan_interface_state_disconnected || s == wlan_interface_state_not_ready => "disconnected",
                _ => "unknown", // associating, authenticating, discovering: changing
            };
            let mut signal = Value::Null;
            if state == "connected" {
                let issue;
                (state, signal, issue) = connected_link(rssi(client, &item.InterfaceGuid));
                if let Some((why, detail)) = issue {
                    g.note(&name, why, detail);
                }
            }
            let connected = match state { "connected" => Value::Bool(true), "disconnected" => Value::Bool(false), _ => Value::Null };
            g.got(());
            interfaces.push(obj(vec![("name", name.as_str().into()), ("connected", connected), ("connection_state", state.into()),
                                     ("signal_dbm", signal), ("signal_raw", Value::Null), ("link_quality", Value::Null),
                                     ("noise_dbm", Value::Null), ("ifindex", ifindex.into())]));
        }
        unsafe { WlanFreeMemory(list.cast()) };
    }
    unsafe { WlanCloseHandle(client, std::ptr::null()) };
    interfaces.sort_by(|a, b| match (a.get("name"), b.get("name")) { (Some(Value::Str(x)), Some(Value::Str(y))) => x.cmp(y), _ => std::cmp::Ordering::Equal });
    if interfaces.is_empty() && g.reasons.is_empty() {
        g.note("wireless", "not_detected", None);
    }
    out(interfaces)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_wireless_interface_has_a_state_and_a_signal_only_while_connected() {
        let mut g = Group::new("wifi", false);
        let data = wifi(&mut g);
        assert_eq!(data.get("provider"), Some(&Value::from("wlanapi")));
        let Some(Value::Arr(interfaces)) = data.get("interfaces") else { panic!("{:?}", data) };
        for i in interfaces {
            let connected = i.get("connected") == Some(&Value::Bool(true));
            match i.get("signal_dbm") {
                Some(Value::Int(dbm)) => assert!(connected && (-128..0).contains(dbm), "{:?}", i),
                Some(Value::Null) => {}
                other => panic!("signal {:?}", other),
            }
            assert!(matches!(i.get("ifindex"), Some(Value::Int(n)) if *n > 0), "{:?}", i);
        }
        // a host without Wi-Fi: nothing found or no WLAN service, never an error
        assert!(!interfaces.is_empty() || g.reasons.iter().all(|r| ["not_detected", "not_exposed"].contains(r)), "{:?} {:?}", g.reasons, g.details);
    }

    #[test]
    fn a_connected_link_refusing_the_rssi_as_invalid_state_is_changing_without_issue() {
        assert_eq!(connected_link(Ok(-61)), ("connected", Value::Int(-61), None));
        assert_eq!(connected_link(Ok(0)), ("connected", Value::Null, Some(("invalid_data", None))));
        assert_eq!(connected_link(Err(ERROR_INVALID_STATE)), ("unknown", Value::Null, None));
        let (state, signal, issue) = connected_link(Err(ERROR_ACCESS_DENIED));
        assert_eq!((state, signal, issue.map(|(why, _)| why)), ("connected", Value::Null, Some("permission_denied")));
        assert_eq!(connected_link(Err(ERROR_NOT_FOUND)).2.map(|(why, _)| why), Some("disappeared"));
    }
}
