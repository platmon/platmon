//! The battery slow group: /sys/class/power_supply. Contract: docs/reference/battery.md.
use crate::json::Value;
use crate::metadata::{py_int, Group};
use crate::pystr::py_space;
use crate::collect::linux::sysfs;
use super::obj;

const POWER_SUPPLY: &str = "/sys/class/power_supply";
const CHARGE_ENERGY: [(&str, &str); 6] = [("charge_now", "charge_now_uah"), ("charge_full", "charge_full_uah"),
    ("charge_full_design", "charge_full_design_uah"), ("energy_now", "energy_now_uwh"), ("energy_full", "energy_full_uwh"),
    ("energy_full_design", "energy_full_design_uwh")];
const SIGNED: [(&str, &str); 2] = [("current_now", "current_ua"), ("power_now", "power_uw")];
const UNSIGNED: [(&str, &str); 2] = [("voltage_now", "voltage_uv"), ("cycle_count", "cycle_count")];
const TEXT: [&str; 4] = ["status", "capacity_level", "technology", "health"];

fn enum_text(v: &str) -> Option<Value> {
    let v = v.trim_matches(py_space);
    (!v.is_empty() && v.chars().count() <= 32).then(|| v.into())
}

fn flag(v: &str) -> Option<Value> {
    match v.trim_matches(py_space) { "0" => Some(Value::Bool(false)), "1" => Some(Value::Bool(true)), _ => None }
}

/// group.read of an attribute this collection listed; None, nothing noted, when the driver has none.
fn attr(g: &mut Group, d: &str, attrs: &[String], name: &str, a: &str, parse: impl Fn(&str) -> Option<Value>) -> Value {
    if !attrs.iter().any(|x| x == a) {
        return Value::Null;
    }
    g.read_with(&format!("{}/{}", d, a), &format!("{}.{}", name, a), Some(d), false, parse).unwrap_or(Value::Null)
}

/// battery.Battery(): every battery's and external supply's reading as the kernel last knew it.
pub fn battery(g: &mut Group) -> Value {
    let (mut batteries, mut external) = (Vec::new(), Vec::new());
    let names = sysfs::entries(POWER_SUPPLY, &mut [&mut *g], false);
    for name in &names {
        let d = format!("{}/{}", POWER_SUPPLY, name);
        let attrs = sysfs::entries(&d, &mut [&mut *g], true);
        if attrs.is_empty() {
            continue; // went away between the two listings, or cannot be listed
        }
        let Some(kind) = g.read_with(&format!("{}/type", d), &format!("{}.type", name), Some(&d), false, enum_text) else { continue };
        let Value::Str(kind) = kind else { continue };
        if kind == "Battery" || kind == "UPS" {
            let mut item = obj(vec![("name", name.as_str().into()), ("present", Value::Null), ("percent", Value::Null)]);
            for t in TEXT { item.set(t, Value::Null); }
            for (_, f) in CHARGE_ENERGY.iter().chain(SIGNED.iter()).chain(UNSIGNED.iter()) { item.set(f, Value::Null); }
            let present = attr(g, &d, &attrs, name, "present", flag);
            item.set("present", present.clone());
            if present != Value::Bool(false) { // an empty bay: what follows is not its reading
                let percent = attr(g, &d, &attrs, name, "capacity", |v| py_int(v).filter(|p| (0..=100).contains(p)).map(Value::Int));
                item.set("percent", percent);
                for (a, f) in CHARGE_ENERGY.iter().chain(UNSIGNED.iter()) {
                    let v = attr(g, &d, &attrs, name, a, |v| py_int(v).filter(|x| *x >= 0).map(Value::Int));
                    item.set(f, v);
                }
                for (a, f) in SIGNED {
                    let v = attr(g, &d, &attrs, name, a, |v| py_int(v).map(Value::Int));
                    item.set(f, v);
                }
                for t in TEXT {
                    let v = attr(g, &d, &attrs, name, t, enum_text);
                    item.set(t, v);
                }
            }
            g.got(());
            batteries.push(item);
        } else {
            let online = attr(g, &d, &attrs, name, "online", flag);
            g.got(());
            external.push(obj(vec![("name", name.as_str().into()), ("type", kind.into()), ("online", online)]));
        }
    }
    if names.is_empty() {
        g.note("power_supply", "not_detected", None);
    }
    obj(vec![("provider", "sysfs_power_supply".into()), ("batteries", Value::Arr(batteries)), ("external", Value::Arr(external))])
}

// tests/collector/test_battery.py, ported.
#[cfg(test)]
mod tests {
    use super::*;
    use crate::collect::linux::sysfs::fake::Root;

    const CHARGE_BATTERY: &[(&str, &str)] = &[ // as read on an ACPI laptop: the charge family, no energy_* and no power_now
        ("type", "Battery"), ("present", "1"), ("capacity", "89"), ("status", "Not charging"), ("capacity_level", "Normal"),
        ("technology", "Li-ion"), ("cycle_count", "237"), ("charge_now", "3026000"), ("charge_full", "3400000"),
        ("charge_full_design", "4023000"), ("voltage_now", "16436000"), ("current_now", "0")];
    const ENERGY_BATTERY: &[(&str, &str)] = &[ // the other gauge family
        ("type", "Battery"), ("present", "1"), ("capacity", "54"), ("status", "Discharging"), ("energy_now", "27000000"),
        ("energy_full", "50000000"), ("energy_full_design", "52000000"), ("voltage_now", "11400000"),
        ("current_now", "-1200000"), ("power_now", "13680000")];

    fn supplies(root: &Root, list: &[(&str, &[(&str, &str)])]) {
        root.mkdir(POWER_SUPPLY);
        for (name, attrs) in list {
            root.mkdir(&format!("{}/{}", POWER_SUPPLY, name));
            for (attr, value) in *attrs {
                root.put(&format!("{}/{}/{}", POWER_SUPPLY, name, attr), value);
            }
        }
    }

    fn observe_battery() -> (Value, Value) {
        let mut g = Group::new("battery", false);
        let data = battery(&mut g);
        (data, g.status())
    }

    fn text(v: &Value, key: &str) -> String {
        v.get(key).unwrap().to_string(false)
    }

    fn first_battery(data: &Value) -> Value {
        data.get("batteries").unwrap().get_index(0).unwrap().clone()
    }

    const OK: &str = r#"{"state":"ok","reason":null,"issues":[],"issues_truncated":0}"#;

    #[test]
    fn charge_family_battery() {
        let root = Root::new(0);
        supplies(&root, &[("BAT1", CHARGE_BATTERY)]);
        let (data, status) = observe_battery();
        assert_eq!(status.to_string(false), OK);
        assert_eq!(data.to_string(false), concat!(r#"{"provider":"sysfs_power_supply","batteries":[{"name":"BAT1","present":true,"#,
            r#""percent":89,"status":"Not charging","capacity_level":"Normal","technology":"Li-ion","health":null,"#,
            r#""charge_now_uah":3026000,"charge_full_uah":3400000,"charge_full_design_uah":4023000,"energy_now_uwh":null,"#,
            r#""energy_full_uwh":null,"energy_full_design_uwh":null,"current_ua":0,"power_uw":null,"voltage_uv":16436000,"#,
            r#""cycle_count":237}],"external":[]}"#));
    }

    #[test]
    fn energy_family_battery_keeps_the_sign() {
        let root = Root::new(0);
        supplies(&root, &[("BAT0", ENERGY_BATTERY)]);
        let (data, status) = observe_battery();
        assert_eq!(text(&status, "state"), r#""ok""#);
        let b = first_battery(&data);
        let got: Vec<String> = ["energy_now_uwh", "energy_full_uwh", "power_uw", "current_ua", "charge_now_uah", "cycle_count"]
            .iter().map(|k| text(&b, k)).collect();
        assert_eq!(got, ["27000000", "50000000", "13680000", "-1200000", "null", "null"]);
    }

    #[test]
    fn an_attribute_the_driver_does_not_expose_is_not_a_problem() {
        let root = Root::new(0);
        supplies(&root, &[("BAT1", &[("type", "Battery"), ("present", "1"), ("capacity", "50")])]);
        let (data, status) = observe_battery();
        assert_eq!(status.to_string(false), OK);
        let b = first_battery(&data);
        assert_eq!((text(&b, "percent"), text(&b, "status"), text(&b, "voltage_uv")), ("50".into(), "null".into(), "null".into()));
    }

    #[test]
    fn no_supply_is_unavailable_not_an_error() {
        for empty in [true, false] {
            let root = Root::new(0);
            if empty {
                root.mkdir(POWER_SUPPLY);
            }
            let (data, status) = observe_battery();
            assert_eq!((text(&status, "state"), text(&status, "reason")), (r#""unavailable""#.into(), r#""not_detected""#.into()));
            assert_eq!((text(&data, "batteries"), text(&data, "external")), ("[]".into(), "[]".into()));
        }
    }

    #[test]
    fn empty_bay_reports_absent_without_inventing_numbers() {
        let root = Root::new(0);
        supplies(&root, &[("BAT2", &[("type", "Battery"), ("present", "0"), ("capacity", "0"), ("status", "Unknown"), ("charge_now", "0")])]);
        let (data, status) = observe_battery();
        assert_eq!(text(&status, "state"), r#""ok""#);
        assert_eq!(first_battery(&data).to_string(false), concat!(r#"{"name":"BAT2","present":false,"percent":null,"status":null,"#,
            r#""capacity_level":null,"technology":null,"health":null,"charge_now_uah":null,"charge_full_uah":null,"#,
            r#""charge_full_design_uah":null,"energy_now_uwh":null,"energy_full_uwh":null,"energy_full_design_uwh":null,"#,
            r#""current_ua":null,"power_uw":null,"voltage_uv":null,"cycle_count":null}"#));
    }

    #[test]
    fn external_supplies_and_their_order() {
        let root = Root::new(0);
        supplies(&root, &[("ADP1", &[("type", "Mains"), ("online", "1")]), ("ucsi-source-psy-USBC000:002", &[("type", "USB"), ("online", "0")]),
                          ("BAT1", CHARGE_BATTERY)]);
        let (data, status) = observe_battery();
        assert_eq!(text(&status, "state"), r#""ok""#);
        assert_eq!(text(&data, "external"),
                   r#"[{"name":"ADP1","type":"Mains","online":true},{"name":"ucsi-source-psy-USBC000:002","type":"USB","online":false}]"#);
        assert_eq!(text(&first_battery(&data), "name"), r#""BAT1""#);
        assert!(data.get("batteries").unwrap().get_index(1).is_none());
    }

    #[test]
    fn a_ups_holds_charge_like_a_battery() {
        let root = Root::new(0);
        supplies(&root, &[("hidev0", &[("type", "UPS"), ("present", "1"), ("capacity", "100"), ("status", "Full")]),
                          ("ADP1", &[("type", "Mains"), ("online", "1")]), ("wl0", &[("type", "Wireless"), ("online", "0")])]);
        let (data, status) = observe_battery();
        assert_eq!(text(&status, "state"), r#""ok""#);
        let b = first_battery(&data);
        assert_eq!((text(&b, "name"), text(&b, "percent"), text(&b, "status")), (r#""hidev0""#.into(), "100".into(), r#""Full""#.into()));
        assert!(data.get("batteries").unwrap().get_index(1).is_none());
        assert_eq!(text(&data, "external"), r#"[{"name":"ADP1","type":"Mains","online":true},{"name":"wl0","type":"Wireless","online":false}]"#);
    }

    #[test]
    fn a_supply_without_a_type_is_left_out() {
        let root = Root::new(0);
        supplies(&root, &[("weird", &[("online", "1")])]);
        let (data, status) = observe_battery();
        assert_eq!((text(&data, "batteries"), text(&data, "external")), ("[]".into(), "[]".into()));
        assert_eq!((text(&status, "state"), text(&status, "reason")), (r#""unavailable""#.into(), r#""not_exposed""#.into()));
    }

    #[test]
    fn a_value_that_is_not_one_is_invalid_data() {
        let cases: [(&[(&str, &str)], &str); 5] = [
            (&[("type", "Battery"), ("present", "1"), ("capacity", "150")], "BAT1.capacity"), // out of 0..100
            (&[("type", "Battery"), ("present", "1"), ("capacity", ""), ("voltage_now", "1")], "BAT1.capacity"),
            (&[("type", "Battery"), ("present", "1"), ("voltage_now", "-5")], "BAT1.voltage_now"), // not a voltage
            (&[("type", "Battery"), ("present", "yes")], "BAT1.present"), // not a flag
            (&[("type", "Mains"), ("online", "2")], "BAT1.online")];
        for (attrs, target) in cases {
            let root = Root::new(0);
            supplies(&root, &[("BAT1", attrs)]);
            let (_, status) = observe_battery();
            assert!(["\"partial\"", "\"error\""].contains(&text(&status, "state").as_str()), "{}", target);
            assert!(["\"some_unreadable\"", "\"invalid_data\""].contains(&text(&status, "reason").as_str()), "{}", target);
            assert_eq!(text(&status, "issues"), format!(r#"[{{"target":"{}","reason":"invalid_data"}}]"#, target));
        }
    }

    #[test]
    fn an_over_long_enum_is_not_one() {
        let root = Root::new(0);
        let long = "x".repeat(33);
        supplies(&root, &[("BAT1", &[("type", "Battery"), ("present", "1"), ("capacity", "50"), ("status", &long)])]);
        let (data, status) = observe_battery();
        assert_eq!(text(&first_battery(&data), "status"), "null");
        assert_eq!(status.to_string(false),
                   r#"{"state":"partial","reason":"some_unreadable","issues":[{"target":"BAT1.status","reason":"invalid_data"}],"issues_truncated":0}"#);
    }

    #[test]
    fn an_unreadable_attribute_is_reported_and_the_rest_kept() {
        if unsafe { libc::geteuid() } == 0 {
            return; // root reads a file of mode 000
        }
        let root = Root::new(0);
        supplies(&root, &[("BAT1", CHARGE_BATTERY)]);
        root.chmod("/sys/class/power_supply/BAT1/capacity", 0);
        let (data, status) = observe_battery();
        assert_eq!((text(&status, "state"), text(&status, "reason")), (r#""partial""#.into(), r#""some_unreadable""#.into()));
        let b = first_battery(&data);
        assert_eq!((text(&b, "percent"), text(&b, "cycle_count")), ("null".into(), "237".into()));
    }

    #[test]
    fn the_host_class_reads() {
        if !std::path::Path::new(POWER_SUPPLY).is_dir() {
            return; // no power supply on this host
        }
        let (data, status) = observe_battery();
        assert!(matches!(&data, Value::Obj(items) if items.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>() == ["provider", "batteries", "external"]));
        assert!(["\"ok\"", "\"unavailable\""].contains(&text(&status, "state").as_str())); // a real host: never partial or error
        if let Some(Value::Arr(bs)) = data.get("batteries") {
            for b in bs {
                assert!(["true", "false", "null"].contains(&text(b, "present").as_str()));
                assert!(matches!(b.get("percent"), Some(Value::Null) | Some(Value::Int(0..=100))));
            }
        }
        if let Some(Value::Arr(es)) = data.get("external") {
            for e in es {
                assert!(matches!(e, Value::Obj(items) if items.len() == 3) && ["true", "false", "null"].contains(&text(e, "online").as_str()));
            }
        }
    }
}
