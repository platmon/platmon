//! The Wi-Fi slow group: /proc/net/wireless, with the network namespace, ifindex and carrier of each
//! interface. Contract: docs/reference/wifi.md.
use crate::json::Value;
use crate::metadata::{py_int, Group};
use crate::pystr::{py_space, py_split};
use crate::collect::linux::passive::{if_indexes, namespace_id, strerror, valid_ifname};
use crate::collect::linux::sysfs::{self, ReadError};
use super::obj;

const WIRELESS: &str = "/proc/net/wireless";
const NETNS: &str = "/proc/self/ns/net";

/// -?\d+\.? : (value, dot); the value as i128 so a long one is out of range rather than unparsed.
fn level_token(t: &str) -> Option<(i128, bool)> {
    let (t, dot) = t.strip_suffix('.').map_or((t, false), |x| (x, true));
    let digits = t.strip_prefix('-').unwrap_or(t);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Some((t.parse::<i128>().unwrap_or(if t.starts_with('-') { i128::MIN } else { i128::MAX }), dot))
}

/// wifi.parse: (interfaces, [(target, why)]); Err for a file that is not the format at all.
fn parse_wireless(text: &str) -> Result<(Vec<Value>, Vec<(String, &'static str)>), ()> {
    let lines: Vec<&str> = text.split('\n').collect();
    if lines.len() < 2 || !lines[0].contains("Quality") || !lines[1].contains("level") {
        return Err(());
    }
    let (mut out, mut bad) = (Vec::new(), Vec::new());
    for (i, line) in lines[2..].iter().enumerate() {
        if line.trim_matches(py_space).is_empty() {
            continue;
        }
        let n = i + 3;
        let t = py_split(line);
        let row = (t.len() == 11).then_some(()).and_then(|_| {
            let name = t[0].strip_suffix(':').filter(|n| !n.is_empty())?;
            if t[1].len() != 4 || !t[1].bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
                return None;
            }
            let (link, link_new) = level_token(t[2])?;
            let (level, level_new) = level_token(t[3])?;
            let (noise, _) = level_token(t[4])?;
            if !t[5..].iter().all(|x| !x.is_empty() && x.bytes().all(|b| b.is_ascii_digit())) {
                return None;
            }
            Some((name, link, link_new, level, level_new, noise))
        }).filter(|r| valid_ifname(r.0));
        let Some((name, link, link_new, level, level_new, noise)) = row else {
            bad.push((format!("line{}", n), "invalid_data"));
            continue;
        };
        if !((0..=255).contains(&link) && (-256..=255).contains(&level) && (-256..=255).contains(&noise)) {
            bad.push((name.to_string(), "invalid_data"));
            continue;
        }
        let (updated, dbm) = (level_new, level < 0);
        let num = |v: i128, ok: bool| if ok { Value::Int(v as i64) } else { Value::Null };
        out.push(obj(vec![("name", name.into()), ("connected", Value::Null), ("connection_state", "unknown".into()),
                          ("signal_dbm", num(level, updated && dbm)), ("signal_raw", num(level, updated && !dbm)),
                          ("link_quality", num(link, updated && link_new)),
                          ("noise_dbm", num(noise, updated && noise < 0 && noise != -256))]));
    }
    Ok((out, bad))
}

/// Why link_connected failed: the three failures the Wi-Fi reader tells apart.
enum LinkError {
    Missing,
    Os(std::io::Error),
    Invalid,
}

fn read_ascii_stripped(path: &str) -> Result<String, LinkError> {
    match sysfs::read_ascii(path) {
        Ok(t) => Ok(t.trim_matches(py_space).to_string()),
        Err(ReadError::Utf8) => Err(LinkError::Invalid),
        Err(ReadError::Os(e)) if e.raw_os_error() == Some(libc::ENOENT) => Err(LinkError::Missing),
        Err(ReadError::Os(e)) => Err(LinkError::Os(e)),
    }
}

/// wifi.link_connected: the kernel's carrier, None when the index moved meanwhile.
fn link_connected(name: &str, ifindex: i64) -> Result<Option<bool>, LinkError> {
    let root = format!("/sys/class/net/{}", name);
    let index = |r: &str| -> Result<i64, LinkError> { py_int(&read_ascii_stripped(&format!("{}/ifindex", r))?).ok_or(LinkError::Invalid) };
    let before = index(&root)?;
    let carrier = match read_ascii_stripped(&format!("{}/carrier", root)) {
        Ok(c) => c,
        // Linux refuses carrier reads on interfaces that are down: confirm IFF_UP is clear
        Err(LinkError::Os(e)) if e.raw_os_error() == Some(libc::EINVAL) => {
            let flags = read_ascii_stripped(&format!("{}/flags", root))?;
            let digits = flags.strip_prefix("0x").or_else(|| flags.strip_prefix("0X")).unwrap_or(&flags);
            let flags = i64::from_str_radix(&digits.replace('_', ""), 16).map_err(|_| LinkError::Invalid)?;
            if flags & 1 != 0 {
                return Err(LinkError::Os(e));
            }
            "0".into()
        }
        Err(e) => return Err(e),
    };
    let after = index(&root)?;
    if before != ifindex || after != ifindex {
        return Ok(None);
    }
    match carrier.as_str() {
        "0" => Ok(Some(false)),
        "1" => Ok(Some(true)),
        _ => Err(LinkError::Invalid),
    }
}

/// wifi.Wifi(): each wireless interface's link as the kernel last knew it.
pub fn wifi(g: &mut Group) -> Value {
    let mut scope = obj(vec![("kind", "process_network_namespace".into())]);
    let out = |scope: Value, interfaces: Vec<Value>| obj(vec![("scope", scope), ("provider", "proc_net_wireless".into()),
                                                              ("interfaces", Value::Arr(interfaces))]);
    let (mut interfaces, bad) = match sysfs::read_ascii(WIRELESS) {
        Ok(t) => match parse_wireless(&t) {
            Ok(x) => x,
            Err(()) => { g.note("wireless", "invalid_data", Some(WIRELESS.into())); return out(scope, vec![]); }
        },
        Err(ReadError::Os(e)) if e.raw_os_error() == Some(libc::ENOENT) => { g.note("wireless", "not_exposed", None); return out(scope, vec![]); }
        Err(ReadError::Os(e)) => {
            g.note("wireless", sysfs::reason_of(&e), Some(format!("{}: {}", WIRELESS, strerror(&e))));
            return out(scope, vec![]);
        }
        Err(ReadError::Utf8) => { g.note("wireless", "invalid_data", Some(WIRELESS.into())); return out(scope, vec![]); }
    };
    let (mut ns, mut index) = (Value::Null, Vec::new());
    if !interfaces.is_empty() {
        use std::os::unix::fs::MetadataExt;
        // the namespace is kept when only the index listing fails
        let listed = std::fs::metadata(&*sysfs::at(NETNS)).and_then(|st| {
            ns = namespace_id(st.dev(), st.ino()).into();
            if_indexes()
        });
        match listed {
            Ok(idx) => index = idx,
            Err(e) => g.note("identity", sysfs::reason_of(&e), None),
        }
    }
    scope.set("id", ns);
    const SIGNALS: [&str; 4] = ["signal_dbm", "signal_raw", "link_quality", "noise_dbm"];
    for item in interfaces.iter_mut() {
        let name = match item.get("name") { Some(Value::Str(n)) => n.clone(), _ => continue };
        let ifindex = index.iter().rev().find(|(_, n)| *n == name).map(|(i, _)| *i as i64);
        item.set("ifindex", ifindex.into());
        let Some(ifindex) = ifindex else {
            for k in SIGNALS { item.set(k, Value::Null); }
            continue;
        };
        match link_connected(&name, ifindex) {
            Ok(connected) => {
                item.set("connected", connected.map_or(Value::Null, Value::Bool));
                item.set("connection_state", match connected { None => "unknown", Some(true) => "connected", Some(false) => "disconnected" }.into());
            }
            Err(LinkError::Missing) => {
                item.set("connection_state", "unsupported".into());
                g.note(&name, "not_exposed", None);
            }
            Err(LinkError::Os(e)) => {
                let why = sysfs::reason_of(&e);
                item.set("connection_state", if why == "permission_denied" { "permission_denied" } else { "unavailable" }.into());
                g.note(&name, why, None);
            }
            Err(LinkError::Invalid) => {
                item.set("connection_state", "unknown".into());
                g.note(&name, "invalid_data", None);
            }
        }
        if item.get("connected") != Some(&Value::Bool(true)) {
            for k in SIGNALS { item.set(k, Value::Null); }
        }
    }
    for (target, why) in &bad {
        g.note(target, why, Some(WIRELESS.into()));
    }
    let empty = interfaces.is_empty();
    interfaces.sort_by(|a, b| match (a.get("name"), b.get("name")) { (Some(Value::Str(x)), Some(Value::Str(y))) => x.cmp(y), _ => std::cmp::Ordering::Equal });
    let interfaces: Vec<Value> = interfaces.into_iter().map(|i| { g.got(()); i }).collect();
    if empty && bad.is_empty() {
        g.note("wireless", "not_detected", None);
    }
    out(scope, interfaces)
}

// tests/collector/test_wifi.py, ported. The interface is "lo", which every network namespace has with
// ifindex 1, so the identity read from the real namespace is known; its files are in the fake tree.
#[cfg(test)]
mod tests {
    use super::*;
    use crate::collect::linux::sysfs::fake::Root;

    const HEAD: &str = "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n \
                        face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n";
    const ORIN: &str = "wlP1p1s0: 0000   60.  -50.  -256        0      0      0      0      0        0\n"; // as read on the boards
    const PI: &str = " wlan0: 0000   46.  -64.  -256        0      0      0    312      0        0\n";
    const DOWN: &str = " wlan1: 0000    0     0     0        0      0      0      0      0        0\n"; // listed, not associated
    const RAW: &str = "  eth9: 0000   70.   70.    0        0      0      0      0      0        0\n"; // a driver without dBm
    const NOISE: &str = " wlan2: 0000   50.  -55.  -95.       0      0      0      0      0        0\n";

    fn rows(text: &str) -> (String, Vec<(String, &'static str)>) {
        let (rows, bad) = parse_wireless(text).ok().unwrap();
        (Value::Arr(rows).to_string(false), bad)
    }

    /// /proc/net/wireless with PI's row as "lo", the namespace file, and lo's ifindex and carrier.
    fn lo_tree(root: &Root, wireless: &str, carrier: Option<&str>) {
        root.put(WIRELESS, wireless.replace("wlan0:", "lo:"));
        root.put(NETNS, "");
        root.put("/sys/class/net/lo/ifindex", "1\n");
        if let Some(c) = carrier {
            root.put("/sys/class/net/lo/carrier", c);
        }
    }

    fn observe_wifi() -> (Value, Value) {
        let mut g = Group::new("wifi", false);
        let out = wifi(&mut g);
        (out, g.status())
    }

    fn lo(out: &Value) -> Value {
        out.get("interfaces").unwrap().get_index(0).unwrap().clone()
    }

    #[test]
    fn connected_rows_from_the_boards() {
        assert_eq!(rows(&format!("{}{}{}", HEAD, ORIN, PI)), (concat!(
            r#"[{"name":"wlP1p1s0","connected":null,"connection_state":"unknown","signal_dbm":-50,"signal_raw":null,"link_quality":60,"noise_dbm":null},"#,
            r#"{"name":"wlan0","connected":null,"connection_state":"unknown","signal_dbm":-64,"signal_raw":null,"link_quality":46,"noise_dbm":null}]"#).into(), vec![]));
    }

    #[test]
    fn unupdated_signal_does_not_determine_connection() {
        assert_eq!(rows(&format!("{}{}", HEAD, DOWN)).0, // never a last value, never 0
                   r#"[{"name":"wlan1","connected":null,"connection_state":"unknown","signal_dbm":null,"signal_raw":null,"link_quality":null,"noise_dbm":null}]"#);
    }

    #[test]
    fn a_value_without_dbm_is_not_called_dbm() {
        assert!(rows(&format!("{}{}", HEAD, RAW)).0.contains(r#""signal_dbm":null,"signal_raw":70"#));
    }

    #[test]
    fn noise_when_the_driver_gives_it() {
        assert!(rows(&format!("{}{}", HEAD, NOISE)).0.contains(r#""noise_dbm":-95"#));
        assert!(rows(&format!("{}{}", HEAD, ORIN)).0.contains(r#""noise_dbm":null"#)); // -256: n/a
    }

    #[test]
    fn bad_rows_and_files() {
        let (text, bad) = rows(&format!("{}{}garbage\na/b: 0000 1. 2. 3. 0 0 0 0 0 0\n", HEAD, ORIN));
        assert!(text.starts_with(r#"[{"name":"wlP1p1s0""#) && !text.contains("},{"));
        assert_eq!(bad.iter().map(|(t, _)| t.as_str()).collect::<Vec<_>>(), ["line4", "line5"]);
        assert!(parse_wireless("not the header\n").is_err());
    }

    #[test]
    fn raw_zero_and_invalid_signal_range() {
        let (text, bad) = rows(&format!("{}{}", HEAD, RAW.replace("70.   70.", "70.    0.")));
        assert!(bad.is_empty() && text.contains(r#""signal_dbm":null,"signal_raw":0"#));
        assert_eq!(rows(&format!("{}{}", HEAD, PI.replace("-64.", "-300."))).1, [("wlan0".to_string(), "invalid_data")]);
    }

    #[test]
    fn observation_states() {
        for (case, state, reason) in [("rows", "ok", "null"), ("header", "unavailable", r#""not_detected""#),
                                      ("missing", "unavailable", r#""not_exposed""#), ("denied", "error", r#""permission_denied""#),
                                      ("junk", "error", r#""invalid_data""#)] {
            if case == "denied" && unsafe { libc::geteuid() } == 0 {
                continue; // root reads a file of mode 000
            }
            let root = Root::new(0);
            lo_tree(&root, &format!("{}{}", HEAD, PI), Some("1\n"));
            match case {
                "header" => root.put(WIRELESS, HEAD),
                "missing" => root.remove(WIRELESS),
                "denied" => root.chmod(WIRELESS, 0),
                "junk" => root.put(WIRELESS, "junk"),
                _ => {}
            }
            let (out, status) = observe_wifi();
            assert_eq!((status.get("state").unwrap().to_string(false), status.get("reason").unwrap().to_string(false)),
                       (format!("{:?}", state), reason.to_string()), "{}", case);
            assert_eq!(out.get("provider").unwrap().to_string(false), r#""proc_net_wireless""#);
            assert_eq!(out.get("scope").unwrap().get("kind").unwrap().to_string(false), r#""process_network_namespace""#);
        }
    }

    #[test]
    fn observation_exports_known_interface_identity() {
        use std::os::unix::fs::MetadataExt;
        let root = Root::new(0);
        lo_tree(&root, &format!("{}{}", HEAD, PI), Some("1\n"));
        let (out, _) = observe_wifi();
        let st = std::fs::metadata(root.path(NETNS)).unwrap();
        assert_eq!(out.get("scope").unwrap().get("id").unwrap().to_string(false), format!("{:?}", namespace_id(st.dev(), st.ino())));
        assert_eq!(lo(&out).get("ifindex").unwrap().to_string(false), "1");
    }

    #[test]
    fn unavailable_identity_is_not_invented() {
        if unsafe { libc::geteuid() } == 0 {
            return; // root passes a directory of mode 000
        }
        let root = Root::new(0);
        lo_tree(&root, &format!("{}{}", HEAD, PI), Some("1\n"));
        root.chmod("/proc/self/ns", 0); // the namespace cannot be looked up
        let (out, status) = observe_wifi();
        root.chmod("/proc/self/ns", 0o755);
        assert_eq!((out.get("scope").unwrap().get("id").unwrap().to_string(false), lo(&out).get("ifindex").unwrap().to_string(false)),
                   ("null".into(), "null".into()));
        assert_eq!(status.get("state").unwrap().to_string(false), r#""partial""#);
    }

    #[test]
    fn signal_not_updated_does_not_claim_disconnection() {
        let root = Root::new(0);
        lo_tree(&root, &format!("{}{}", HEAD, PI.replace("-64.", "-64 ")), Some("1\n"));
        let item = lo(&observe_wifi().0);
        assert_eq!((item.get("connected").unwrap().to_string(false), item.get("signal_dbm").unwrap().to_string(false)),
                   ("true".into(), "null".into()));
    }

    #[test]
    fn connection_states_are_not_inferred_from_signal() {
        // Python's OSError(5) is EINVAL here with IFF_UP set: a carrier read error that is not a down link
        for (case, state, connected) in [("1\n", "connected", "true"), ("0\n", "disconnected", "false"), ("moved", "unknown", "null"),
                                         ("denied", "permission_denied", "null"), ("missing", "unsupported", "null")] {
            if case == "denied" && unsafe { libc::geteuid() } == 0 {
                continue;
            }
            let root = Root::new(0);
            lo_tree(&root, &format!("{}{}", HEAD, PI), Some("1\n"));
            match case {
                "moved" => root.put("/sys/class/net/lo/ifindex", "4\n"),
                "denied" => root.chmod("/sys/class/net/lo/carrier", 0),
                "missing" => root.remove("/sys/class/net/lo/carrier"),
                c => root.put("/sys/class/net/lo/carrier", c),
            }
            let (out, status) = observe_wifi();
            let item = lo(&out);
            assert_eq!((item.get("connection_state").unwrap().to_string(false), item.get("connected").unwrap().to_string(false)),
                       (format!("{:?}", state), connected.to_string()), "{}", case);
            assert_eq!(item.get("signal_dbm").unwrap().to_string(false), if connected == "true" { "-64" } else { "null" }, "{}", case);
            assert!(!(out.to_string(false) + &status.to_string(false)).contains("/sys"), "{}", case); // no private path
        }
    }

    #[test]
    fn carrier_and_index_from_fake_sysfs() {
        let root = Root::new(0);
        root.put("/sys/class/net/wlan0/ifindex", "3\n");
        root.put("/sys/class/net/wlan0/carrier", "1\n");
        assert!(matches!(link_connected("wlan0", 3), Ok(Some(true))));
        root.put("/sys/class/net/wlan0/carrier", "0\n");
        assert!(matches!(link_connected("wlan0", 3), Ok(Some(false))));
        assert!(matches!(link_connected("wlan0", 4), Ok(None)));
        root.put("/sys/class/net/wlan0/carrier", "2\n");
        assert!(matches!(link_connected("wlan0", 3), Err(LinkError::Invalid)));
        if unsafe { libc::geteuid() } != 0 { // carrier errors are not all a down link: EACCES stays an error
            root.chmod("/sys/class/net/wlan0/carrier", 0);
            assert!(matches!(link_connected("wlan0", 3), Err(LinkError::Os(e)) if e.raw_os_error() == Some(libc::EACCES)));
        }
    }

    #[test]
    fn real_host_file() {
        let (out, status) = observe_wifi();
        assert!(["\"ok\"", "\"unavailable\"", "\"partial\""].contains(&status.get("state").unwrap().to_string(false).as_str()));
        if let Some(Value::Arr(items)) = out.get("interfaces") {
            for i in items {
                assert!(matches!(i.get("signal_dbm"), Some(Value::Null) | Some(Value::Int(i64::MIN..=-1))));
            }
        }
    }

    #[test]
    fn interface_replaced_during_carrier_read_is_unknown() {
        let root = Root::new(0);
        crate::collect::linux::sysfs::fake::answers("/sys/class/net/wlan0/ifindex", vec![Ok("3"), Ok("4")]);
        root.put("/sys/class/net/wlan0/carrier", "1");
        assert!(matches!(link_connected("wlan0", 3), Ok(None)));
    }

    #[test]
    fn down_carrier_is_disconnected_without_issue() {
        // Linux refuses a carrier read on a down interface (EINVAL); IFF_UP clear confirms it is down
        for (flags, after, connected, state) in [("0x0", "1", "false", "disconnected"), ("0x1000", "1", "false", "disconnected"),
                                                 ("0x0", "4", "null", "unknown")] {
            let root = Root::new(0);
            lo_tree(&root, &format!("{}{}", HEAD, PI), None);
            root.put("/sys/class/net/lo/flags", flags);
            crate::collect::linux::sysfs::fake::answers("/sys/class/net/lo/ifindex", vec![Ok("1"), Ok(after)]);
            crate::collect::linux::sysfs::fake::answers("/sys/class/net/lo/carrier", vec![Err(libc::EINVAL)]);
            let (out, status) = observe_wifi();
            let item = lo(&out);
            assert_eq!((item.get("connected").unwrap().to_string(false), item.get("connection_state").unwrap().to_string(false)),
                       (connected.to_string(), format!("{:?}", state)), "{} {}", flags, after);
            assert_eq!(item.get("signal_dbm").unwrap().to_string(false), "null");
            assert_eq!(status.to_string(false), r#"{"state":"ok","reason":null,"issues":[],"issues_truncated":0}"#);
        }
    }

    #[test]
    fn carrier_errors_are_not_all_treated_as_down() {
        for (errno, flags) in [(libc::EINVAL, "0x1"), (libc::EIO, "0x0"), (libc::EACCES, "0x0")] {
            let root = Root::new(0);
            root.put("/sys/class/net/wlan0/ifindex", "3");
            root.put("/sys/class/net/wlan0/flags", flags);
            crate::collect::linux::sysfs::fake::answers("/sys/class/net/wlan0/carrier", vec![Err(errno)]);
            assert!(matches!(link_connected("wlan0", 3), Err(LinkError::Os(e)) if e.raw_os_error() == Some(errno)), "{}", errno);
        }
    }

    #[test]
    fn a_carrier_read_error_that_is_not_a_down_link_is_unavailable() {
        // test_connection_states_are_not_inferred_from_signal's OSError(5)
        let root = Root::new(0);
        lo_tree(&root, &format!("{}{}", HEAD, PI), None);
        crate::collect::linux::sysfs::fake::answers("/sys/class/net/lo/carrier", vec![Err(libc::EIO)]);
        let (out, status) = observe_wifi();
        let item = lo(&out);
        assert_eq!((item.get("connection_state").unwrap().to_string(false), item.get("connected").unwrap().to_string(false)),
                   (r#""unavailable""#.into(), "null".into()));
        assert!(!(out.to_string(false) + &status.to_string(false)).contains("/sys"));
    }
}
