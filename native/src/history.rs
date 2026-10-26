//! runtime/history.py: the last few minutes of selected numbers from the published core snapshots and slow
//! observations, in memory only, for trend graphs. One point per publication, appended by its publisher
//! after it is published; reading never adds points. Contract: docs/reference/history.md.
use crate::json::Value;
use crate::metadata::pointer;
use crate::runtime::{ms, seconds_ns};
use std::collections::{HashSet, VecDeque};
use std::sync::Mutex;

pub const MAX_SERIES: usize = 64;
const MAX_POINTS_PER_SERIES: usize = 3602;
const MAX_ID_LENGTH: usize = 512;
const MAX_DROPPED_IDS: usize = 256;
/// Which series keep a slot when there are more than MAX_SERIES (history.PRIORITY); an id with none of
/// these prefixes ranks with temperature.
const PRIORITY: [&str; 9] = ["memory/", "cpu/", "gpu/", "npu/", "power/", "network/", "disk_io/", "observation/", "temperature/"];
pub const OBSERVED: [&str; 4] = ["storage", "wifi", "battery", "probe"];

fn priority(sid: &str) -> usize {
    PRIORITY.iter().position(|p| sid.starts_with(p)).unwrap_or(PRIORITY.len() - 1)
}

/// Python's str() of a scalar in an f-string; None for what the collector never puts in an id.
/// The namespace part of a network or Wi-Fi series id: the scope's id, or "host" for a host scope without
/// one (Windows, which has no network namespaces); None when neither identifies it.
fn scope_id(scope: Option<&Value>) -> Result<Option<String>, ()> {
    let Some(scope) = truthy(scope) else { return Ok(None) };
    match get_or_null(Some(scope), "id") {
        Value::Null if scope.get("kind") == Some(&Value::Str("host".into())) => Ok(Some("host".into())),
        Value::Null => Ok(None),
        id => py_str(&id).map(Some).ok_or(()),
    }
}

pub fn py_str(v: &Value) -> Option<String> {
    match v {
        Value::Null => Some("None".into()),
        Value::Bool(b) => Some(if *b { "True" } else { "False" }.into()),
        Value::Int(i) => Some(i.to_string()),
        Value::Big(i) => Some(i.to_string()),
        Value::Float(_) => Some(v.to_string(false)),
        Value::Str(s) => Some(s.clone()),
        _ => None,
    }
}

/// Python's `x or default` for a JSON value: the value when it is truthy.
fn truthy(v: Option<&Value>) -> Option<&Value> {
    v.filter(|v| match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Int(i) => *i != 0,
        Value::Big(i) => *i != 0,
        Value::Float(f) => *f != 0.0,
        Value::Str(s) => !s.is_empty(),
        Value::Arr(a) => !a.is_empty(),
        Value::Obj(o) => !o.is_empty(),
    })
}

type Points = Vec<(String, Value)>;

/// The items of `stats.get(key) or []` as Python iterates them; Err where it would raise.
fn list<'a>(v: Option<&'a Value>) -> Result<&'a [Value], ()> {
    match truthy(v) {
        None => Ok(&[]),
        Some(Value::Arr(items)) => Ok(items),
        Some(_) => Err(()),
    }
}

fn obj<'a>(v: Option<&'a Value>) -> Result<&'a [(String, Value)], ()> {
    match truthy(v) {
        None => Ok(&[]),
        Some(Value::Obj(items)) => Ok(items),
        Some(_) => Err(()),
    }
}

fn field(v: &Value, k: &str) -> Result<String, ()> {
    v.get(k).and_then(py_str).ok_or(())
}

fn get_or_null(v: Option<&Value>, k: &str) -> Value {
    v.and_then(|v| v.get(k)).cloned().unwrap_or(Value::Null)
}

/// history.points_of: {series id: value} of one snapshot, in Python's order. Err where Python raises (the
/// snapshot then adds no point), for shapes the collector does not make.
pub fn points_of(stats: &Value, meta: &Value) -> Result<Points, ()> {
    let mut out: Points = Vec::new();
    let put = |out: &mut Points, k: String, v: Value| match out.iter_mut().find(|(x, _)| *x == k) {
        Some(slot) => slot.1 = v,
        None => out.push((k, v)),
    };
    for c in list(stats.get("cpu"))? {
        if !matches!(c, Value::Obj(_)) { return Err(()); }
        put(&mut out, format!("cpu/{}/usage", field(c, "id")?), get_or_null(Some(c), "usage"));
    }
    if let Some(m) = truthy(stats.get("memory")) {
        let Value::Obj(_) = m else { return Err(()) };
        if let Some(used) = m.get("used") {
            put(&mut out, "memory/used_bytes".into(), used.clone());
        }
    }
    for kind in ["gpu", "npu"] {
        if let Some(v @ Value::Obj(_)) = stats.get(kind) {
            if let Some(u) = v.get("usage") {
                put(&mut out, format!("{}/usage", kind), u.clone());
            }
        }
    }
    let meta = truthy(Some(meta));
    for kind in ["temperature", "power"] {
        for (key, value) in obj(stats.get(kind))? {
            let info = meta.and_then(|m| truthy(m.get(&pointer(&[kind, key]))));
            let sid = match info {
                Some(i @ Value::Obj(_)) => truthy(i.get("id")).map(|v| py_str(v).ok_or(())).transpose()?,
                Some(_) => return Err(()),
                None => None,
            };
            put(&mut out, format!("{}/{}", kind, sid.unwrap_or_else(|| format!("name:{}", key))), value.clone());
        }
    }
    if let Some(net) = truthy(stats.get("network")) {
        let Value::Obj(_) = net else { return Err(()) };
        let ns = scope_id(net.get("scope"))?;
        for i in list(net.get("interfaces"))? {
            if !matches!(i, Value::Obj(_)) { return Err(()); }
            let Some(ns) = ns.as_ref().filter(|_| !i.get("ifindex").map_or(true, Value::is_null)) else {
                continue; // no identity to tie points of one interface together
            };
            if i.get("oper_status").is_some_and(|s| *s != Value::Str("up".into())) {
                continue; // Windows: a down or absent interface (most of its virtual adapters) carries nothing
            }
            for k in ["rx_bytes_per_s", "tx_bytes_per_s"] {
                let id = format!("network/{}/{}/{}/{}", ns, field(i, "ifindex")?, field(i, "name")?, k);
                put(&mut out, id, get_or_null(truthy(i.get("rates")), k));
            }
        }
    }
    if let Some(dio) = truthy(stats.get("disk_io")) {
        let Value::Obj(_) = dio else { return Err(()) };
        for d in list(dio.get("disks"))? {
            if !matches!(d, Value::Obj(_)) { return Err(()); }
            // Windows has no device numbers: its PhysicalDriveN name is the identity
            let identity = match d.get("major") {
                Some(Value::Null) => field(d, "name")?,
                _ => format!("{}:{}/{}", field(d, "major")?, field(d, "minor")?, field(d, "name")?),
            };
            for k in ["read_bytes_per_s", "write_bytes_per_s"] {
                let id = format!("disk_io/{}/{}", identity, k);
                put(&mut out, id, get_or_null(truthy(d.get("rates")), k));
            }
        }
    }
    Ok(out)
}

/// history.observation_points: numbers from one slow observation.
pub fn observation_points(name: &str, data: &Value) -> Result<Points, ()> {
    let mut out: Points = Vec::new();
    let data = truthy(Some(data));
    let get = |k: &str| data.and_then(|d| d.get(k));
    match name {
        "storage" => {
            for fs in list(get("filesystems"))? {
                if matches!(fs.get("major"), Some(Value::Null)) { // Windows: the volume GUID is the identity
                    let base = format!("observation/storage/{}", field(fs, "device")?);
                    for f in ["used_bytes", "available_bytes"] {
                        out.push((format!("{}/{}", base, f), get_or_null(Some(fs), f)));
                    }
                    continue;
                }
                let identity = Value::Arr(["major", "minor", "device", "fstype", "source"].iter().map(|k| get_or_null(Some(fs), k)).collect());
                let token = &crate::sha256::hex(identity.to_py_compact().as_bytes())[..24];
                let base = format!("observation/storage/{}:{}/{}", field(fs, "major")?, field(fs, "minor")?, token);
                for f in ["used_bytes", "available_bytes"] {
                    out.push((format!("{}/{}", base, f), get_or_null(Some(fs), f)));
                }
            }
        }
        "wifi" => {
            let ns = scope_id(get("scope"))?;
            for item in list(get("interfaces"))? {
                let Some(ns) = ns.as_ref().filter(|_| !item.get("ifindex").map_or(true, Value::is_null)) else { continue };
                let sid = format!("observation/wifi/{}/{}/{}/signal_dbm", ns, field(item, "ifindex")?, field(item, "name")?);
                let connected = truthy(item.get("connected")).is_some();
                out.push((sid, if connected { get_or_null(Some(item), "signal_dbm") } else { Value::Null }));
            }
        }
        "battery" => {
            for cell in list(get("batteries"))? {
                let base = format!("observation/battery/{}", field(cell, "name")?);
                for f in ["percent", "charge_now_uah", "energy_now_uwh", "power_uw"] {
                    if let Some(v) = cell.get(f).filter(|v| !v.is_null()) {
                        out.push((format!("{}/{}", base, f), v.clone()));
                    }
                }
            }
        }
        "probe" => {
            for t in list(get("targets"))? {
                let address = field(t, "address")?;
                let host = if address.contains(':') { format!("[{}]", address) } else { address };
                out.push((format!("observation/probe/{}:{}/connect_ms", host, field(t, "port")?), get_or_null(Some(t), "connect_ms")));
            }
        }
        _ => {}
    }
    Ok(out)
}

struct Series {
    id: String,
    owner: String,
    seen: i64,
    points: VecDeque<(i64, i64, Value)>,
}

#[derive(Default)]
struct Inner {
    series: Vec<Series>, // in the order they were made, as Python's dict
    periods: Vec<(String, f64)>,
    latest: Vec<(String, (i64, i64))>,
    dropped: HashSet<String>,
    dropped_series: usize,
}

pub struct History {
    pub retention: f64,
    retention_ns: i64,
    interval: f64,
    maxlen: usize,
    max_series: usize,
    inner: Mutex<Inner>,
}

fn number(v: Value) -> Value {
    match v {
        Value::Int(_) => v,
        Value::Big(i) if i <= 1 << 64 => v,
        Value::Float(f) if f.is_finite() && f.abs() <= 18446744073709551616.0 => v,
        _ => Value::Null,
    }
}

impl History {
    pub fn new(retention: f64, interval: f64) -> History {
        let maxlen = MAX_POINTS_PER_SERIES.min((retention / interval.min(1.0)) as usize + 2);
        let mut inner = Inner::default();
        inner.periods.push(("core".into(), interval));
        History { retention, retention_ns: seconds_ns(retention), interval, maxlen, max_series: MAX_SERIES,
                  inner: Mutex::new(inner) }
    }

    /// History.append: one core publication.
    pub fn append(&self, sequence: i64, started: i64, stats: &Value, meta: &Value, gap_before: bool) {
        match points_of(stats, meta) {
            Ok(values) => self.add("core", sequence, started, self.interval, values, gap_before),
            Err(()) => eprintln!("platmon: history append failed: unexpected snapshot shape"),
        }
    }

    /// History.append_observation: one slow observation; stale or failed ones contribute gaps.
    pub fn append_observation(&self, name: &str, interval: f64, id: i64, started: i64, completed: i64, state: &str,
                              data: &Value) -> Result<(), ()> {
        if !OBSERVED.contains(&name) {
            return Ok(());
        }
        let mut values = observation_points(name, data)?;
        let stale = completed - started > seconds_ns(3.0 * interval);
        if stale || state == "error" || state == "unavailable" {
            for v in values.iter_mut() {
                v.1 = Value::Null;
            }
        }
        self.add(name, id, started, interval, values, false);
        Ok(())
    }

    fn add(&self, owner: &str, sequence: i64, started: i64, interval: f64, mut values: Points, gap_before: bool) {
        let gap_ns = seconds_ns((3.0 * interval).max(5.0));
        let mut h = self.inner.lock().unwrap();
        match h.latest.iter_mut().find(|(o, _)| o == owner) {
            Some((_, last)) => {
                if sequence <= last.0 || started < last.1 {
                    return; // repeated or out-of-order publication cannot add a second point
                }
                *last = (sequence, started);
            }
            None => h.latest.push((owner.into(), (sequence, started))),
        }
        match h.periods.iter_mut().find(|(o, _)| o == owner) {
            Some(p) => p.1 = interval,
            None => h.periods.push((owner.into(), interval)),
        }
        let cutoff = started - self.retention_ns;
        h.series.retain(|s| s.seen >= cutoff);
        // missing identities keep gaps, but those gaps do not keep a vanished identity alive
        let present: HashSet<String> = values.iter().map(|(k, _)| k.clone()).collect();
        for s in &h.series {
            if s.owner == owner && !present.contains(&s.id) {
                values.push((s.id.clone(), Value::Null));
            }
        }
        let values = self.fitting(&mut h, owner, values);
        for (sid, value) in values {
            let pos = match h.series.iter().position(|s| s.id == sid) {
                Some(p) => p,
                None => {
                    h.series.push(Series { id: sid.clone(), owner: owner.into(), seen: started, points: VecDeque::new() });
                    h.series.len() - 1
                }
            };
            let s = &mut h.series[pos];
            if present.contains(&sid) {
                s.seen = started;
            }
            while s.points.front().is_some_and(|p| p.1 < cutoff) {
                s.points.pop_front(); // slow publishers release expired points before reaching the cap
            }
            let maxlen = self.maxlen;
            let push = |points: &mut VecDeque<(i64, i64, Value)>, p| {
                if points.len() == maxlen {
                    points.pop_front();
                }
                points.push_back(p);
            };
            if let Some(last) = s.points.back().map(|p| p.1) {
                if gap_before || started - last > gap_ns {
                    push(&mut s.points, (sequence, started - 1, Value::Null)); // an explicit break after a stalled publisher
                }
            }
            push(&mut s.points, (sequence, started, number(value)));
        }
    }

    /// History._fitting: the part of one publication that fits, by PRIORITY; each id left out counted once.
    fn fitting(&self, h: &mut Inner, owner: &str, values: Points) -> Points {
        let taken = h.series.iter().filter(|s| s.owner != owner).count();
        let room = self.max_series.saturating_sub(taken);
        let mut ids: Vec<&String> = values.iter().map(|(k, _)| k).filter(|k| k.chars().count() <= MAX_ID_LENGTH).collect();
        // established series win ties, so two sensors of one rank never take turns dropping each other
        ids.sort_by_key(|sid| (priority(sid), !h.series.iter().any(|s| &s.id == *sid)));
        let keep: HashSet<String> = ids.into_iter().take(room).cloned().collect();
        for (sid, _) in &values {
            if keep.contains(sid) {
                continue;
            }
            if h.dropped.len() < MAX_DROPPED_IDS {
                h.dropped.insert(sid.clone());
            }
            h.series.retain(|s| &s.id != sid); // lost its slot to a more important series of this publisher
        }
        h.dropped_series = h.dropped.len();
        values.into_iter().filter(|(k, _)| keep.contains(k)).collect()
    }

    /// {series id: its publisher's cadence in ms}, in the series' order.
    pub fn intervals(&self) -> Vec<(String, f64)> {
        let h = self.inner.lock().unwrap();
        h.series.iter().map(|s| {
            let p = h.periods.iter().find(|(o, _)| *o == s.owner).map_or(0.0, |p| p.1);
            (s.id.clone(), crate::json::round_to(p * 1000.0, 3))
        }).collect()
    }

    /// History.view: ({series id: [[id, age_ms, value]]}, dropped_series). Points are copied under the lock
    /// and filtered, reduced and turned into JSON values outside it.
    pub fn view(&self, now: i64, prefix: &str, seconds: Option<f64>, points: usize) -> (Value, usize) {
        let seconds = seconds.map_or(self.retention, |s| s.min(self.retention));
        let since = now - seconds_ns(seconds);
        let (captured, dropped): (Vec<(String, Vec<(i64, i64, Value)>)>, usize) = {
            let h = self.inner.lock().unwrap();
            (h.series.iter().filter(|s| s.id.starts_with(prefix)).map(|s| (s.id.clone(), s.points.iter().cloned().collect())).collect(),
             h.dropped_series)
        };
        let mut out = Vec::new();
        for (sid, samples) in captured {
            let mut ps: Vec<(i64, i64, Value)> = samples.into_iter().filter(|p| since <= p.1 && p.1 <= now).collect();
            if ps.len() > points {
                if points == 1 {
                    ps = ps.split_off(ps.len() - 1);
                } else {
                    // keep the newest exactly; a bucket with missing data stays a gap
                    let newest = ps.pop().unwrap();
                    let n = ps.len();
                    let mut reduced = Vec::with_capacity(points);
                    for i in 0..points - 1 {
                        let bucket = &ps[i * n / (points - 1)..(i + 1) * n / (points - 1)];
                        let end = bucket.last().unwrap();
                        let v = if bucket.iter().any(|p| p.2.is_null()) { Value::Null } else { end.2.clone() };
                        reduced.push((end.0, end.1, v));
                    }
                    reduced.push(newest);
                    ps = reduced;
                }
            }
            out.push((sid, Value::Arr(ps.into_iter().map(|(seq, t, v)| Value::Arr(vec![seq.into(), ms(now - t).into(), v])).collect())));
        }
        (Value::Obj(out), dropped)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_disk_without_a_device_number_is_named_by_its_drive() {
        let disk = |name: &str, number: &str| format!(
            r#"{{"name": "{}", "major": {}, "minor": {}, "rates": {{"read_bytes_per_s": 1.0, "write_bytes_per_s": 2.0}}}}"#,
            name, number, number);
        let stats = crate::json::parse(&format!(r#"{{"disk_io": {{"disks": [{}, {}]}}}}"#, disk("PhysicalDrive0", "null"),
                                                disk("sda", "8"))).unwrap();
        let ids: Vec<String> = points_of(&stats, &Value::Null).unwrap().into_iter().map(|(id, _)| id).collect();
        assert_eq!(ids, ["disk_io/PhysicalDrive0/read_bytes_per_s", "disk_io/PhysicalDrive0/write_bytes_per_s",
                         "disk_io/8:8/sda/read_bytes_per_s", "disk_io/8:8/sda/write_bytes_per_s"]);
    }

    #[test]
    fn a_host_scope_names_interfaces_by_host_and_index_and_an_unknown_namespace_has_none() {
        let net = |scope: &str| crate::json::parse(&format!(
            r#"{{"network": {{"scope": {}, "interfaces": [{{"name": "Wi-Fi", "ifindex": 18, "rates": {{"rx_bytes_per_s": 1.0,
               "tx_bytes_per_s": 2.0}}}}, {{"name": "x", "ifindex": null, "rates": null}}]}}}}"#, scope)).unwrap();
        let ids = |v: &Value| points_of(v, &Value::Null).unwrap().into_iter().map(|(id, _)| id).collect::<Vec<_>>();
        assert_eq!(ids(&net(r#"{"kind": "host", "id": null}"#)),
                   ["network/host/18/Wi-Fi/rx_bytes_per_s", "network/host/18/Wi-Fi/tx_bytes_per_s"]);
        let statuses = crate::json::parse(r#"{"network": {"scope": {"kind": "host", "id": null}, "interfaces": [
            {"name": "Wi-Fi", "ifindex": 18, "oper_status": "up", "rates": null},
            {"name": "6to4 Adapter", "ifindex": 2, "oper_status": "notPresent", "rates": null},
            {"name": "Ethernet", "ifindex": 7, "oper_status": "down", "rates": null}]}}"#).unwrap();
        assert_eq!(ids(&statuses), ["network/host/18/Wi-Fi/rx_bytes_per_s", "network/host/18/Wi-Fi/tx_bytes_per_s"]);
        assert!(ids(&net(r#"{"kind": "process_network_namespace", "id": null}"#)).is_empty());
        assert_eq!(ids(&net(r#"{"kind": "process_network_namespace", "id": "netns1:ab"}"#))[0], "network/netns1:ab/18/Wi-Fi/rx_bytes_per_s");
        let wifi = crate::json::parse(r#"{"scope": {"kind": "host", "id": null}, "interfaces": [{"name": "Wi-Fi", "ifindex": 18,
            "connected": true, "signal_dbm": -69}]}"#).unwrap();
        assert_eq!(observation_points("wifi", &wifi).unwrap(), [("observation/wifi/host/18/Wi-Fi/signal_dbm".to_string(), Value::Int(-69))]);
    }

    #[test]
    fn a_volume_without_a_device_number_is_named_by_its_guid() {
        let data = crate::json::parse(r#"{"filesystems": [{"device": "Volume{c722dea1-cc0a-40ca-99a0-911c7e3bfdc6}",
            "major": null, "minor": null, "fstype": "NTFS", "used_bytes": 5, "available_bytes": 7}]}"#).unwrap();
        let points = observation_points("storage", &data).unwrap();
        assert_eq!(points, [
            ("observation/storage/Volume{c722dea1-cc0a-40ca-99a0-911c7e3bfdc6}/used_bytes".to_string(), Value::Int(5)),
            ("observation/storage/Volume{c722dea1-cc0a-40ca-99a0-911c7e3bfdc6}/available_bytes".to_string(), Value::Int(7)),
        ]);
    }
}
