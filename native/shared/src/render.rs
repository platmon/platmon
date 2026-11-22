//! render(): the plain-text screen /text answers with, from an /api/stats snapshot and an /api/observations
//! answer; ported from frontends/cli.py (in the git history), which it matched byte for byte. Where cli.py
//! raised (a key missing, None formatted as a number), Err: /text then answers 500 (runtime_fixture.py's text
//! scenario, recorded answers). The platmon command (frontend/) prints the same screen with --once and draws
//! its live view with the helpers here.
use crate::json::Value;

type R<T> = Result<T, ()>;

const MAX_ROWS: usize = 12;

fn key<'a>(v: &'a Value, k: &str) -> R<&'a Value> {
    v.get(k).ok_or(())
}

/// A number as Python formats it with a float spec: int, float or bool, else Err (TypeError).
pub fn num(v: &Value) -> R<f64> {
    match v {
        Value::Int(i) => Ok(*i as f64),
        Value::Big(i) => Ok(*i as f64),
        Value::Float(f) => Ok(*f),
        Value::Bool(b) => Ok(*b as i64 as f64),
        _ => Err(()),
    }
}

/// Python's truth value.
pub fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Int(i)) => *i != 0,
        Some(Value::Big(i)) => *i != 0,
        Some(Value::Float(f)) => *f != 0.0,
        Some(Value::Str(s)) => !s.is_empty(),
        Some(Value::Arr(a)) => !a.is_empty(),
        Some(Value::Obj(o)) => !o.is_empty(),
    }
}

/// str() of a value inside an f-string.
pub fn s(v: &Value) -> String {
    match v {
        Value::Str(x) => x.clone(),
        other => crate::json::py_str(other).unwrap_or_else(|| other.to_py().replace('"', "'")),
    }
}

/// Python's format(x, ".Nf") for finite and non-finite floats.
pub fn f(x: f64, prec: usize) -> String {
    if x.is_nan() { return "nan".into() }
    if x.is_infinite() { return if x > 0.0 { "inf".into() } else { "-inf".into() } }
    format!("{:.*}", prec, x)
}

fn pad_left(t: String, w: usize) -> String {
    let n = t.chars().count();
    if n >= w { t } else { format!("{}{}", " ".repeat(w - n), t) }
}

pub fn pad_right(t: &str, w: usize) -> String {
    let n = t.chars().count();
    if n >= w { t.to_string() } else { format!("{}{}", t, " ".repeat(w - n)) }
}

fn bar(pct: f64, width: usize) -> String {
    let n = (width as f64 * pct.min(100.0).max(0.0) / 100.0).round_ties_even() as usize;
    "█".repeat(n) + &"·".repeat(width - n)
}

fn gib(b: f64) -> String {
    format!("{}G", f(b / 1073741824.0, 1))
}

fn system_line(system: Option<&Value>) -> String {
    let Some(Value::Obj(items)) = system else { return String::new() };
    let label = |k: &str| match k { "os" | "arch" => String::new(), "kernel" => "kernel ".into(), "hostname" => "host ".into(),
                                    "l4t" => "L4T ".into(), k => format!("{} ", k) };
    items.iter().filter(|(_, v)| truthy(Some(v))).map(|(k, v)| label(k) + &s(v)).collect::<Vec<_>>().join(" · ")
}

fn cpu_reason(r: &str) -> String {
    match r {
        "warmup" => "warming up", "counter_regressed" => "counter went backwards", "no_ticks" => "no ticks counted",
        "invalid_interval" => "invalid interval", "gap" => "restarting after a gap", r => r,
    }.to_string()
}

pub fn cpu_note(st: &Value) -> R<String> {
    let mut groups: Vec<(String, Vec<String>)> = Vec::new();
    let sampling = st.get("cpu_sampling").filter(|v| truthy(Some(v)));
    if let Some(Value::Arr(un)) = sampling.and_then(|c| c.get("unavailable")) {
        for u in un {
            let why = cpu_reason(&s(key(u, "reason")?));
            let id = format!("CPU{}", s(key(u, "id")?));
            match groups.iter_mut().find(|(w, _)| *w == why) {
                Some(g) => g.1.push(id),
                None => groups.push((why, vec![id])),
            }
        }
    }
    if groups.is_empty() {
        return Ok(String::new());
    }
    if !truthy(st.get("cpu")) && groups.len() == 1 {
        return Ok(format!("CPU sampling: {}", groups[0].0));
    }
    Ok("CPU sampling: ".to_string() + &groups.iter().map(|(w, ids)| format!("{} {}", ids.join(" "), w)).collect::<Vec<_>>().join("; "))
}

pub fn rate_reason(r: &Value) -> String {
    match r {
        Value::Str(x) => match x.as_str() {
            "warmup" => "warming up", "gap" => "restarting after a gap", "counter_regressed" => "counter went backwards",
            "invalid_interval" => "invalid interval", "identity_unavailable" => "identity unknown",
            "namespace_changed" => "namespace changed", x => x,
        }.to_string(),
        other => s(other),
    }
}

pub fn per_s(v: f64) -> String {
    let mut v = v;
    for unit in ["B/s", "KiB/s", "MiB/s"] {
        if v.abs() < 1024.0 {
            return if unit == "B/s" { format!("{} {}", f(v, 0), unit) } else { format!("{} {}", f(v, 1), unit) };
        }
        v /= 1024.0;
    }
    format!("{} GiB/s", f(v, 1))
}

fn rows_of(items: &[Value], kind: &str, line: impl Fn(&Value) -> R<String>) -> R<Vec<String>> {
    let mut width = 0;
    for i in items {
        width = width.max(s(key(i, "name")?).chars().count());
    }
    let width = width.min(16);
    let mut out = Vec::new();
    for i in items.iter().take(MAX_ROWS) {
        out.push(format!("  {}  {}", pad_right(&s(key(i, "name")?), width), line(i)?));
    }
    if items.len() > MAX_ROWS {
        out.push(format!("  +{} more {} (all in /api/stats)", items.len() - MAX_ROWS, kind));
    }
    Ok(out)
}

fn window_text(item: &Value) -> String {
    match item.get("window_ms") {
        Some(w @ (Value::Int(_) | Value::Float(_) | Value::Big(_) | Value::Bool(_))) => format!("  window {} s", f(num(w).unwrap() / 1000.0, 3)),
        _ => String::new(),
    }
}

fn network_line(i: &Value) -> R<String> {
    let r = key(i, "rates")?;
    if !truthy(Some(r)) {
        return Ok(rate_reason(i.get("reason").unwrap_or(&Value::Null)));
    }
    let mut bad = Vec::new();
    for k in ["errors", "dropped"] {
        let (rx, tx) = (key(key(i, "rx")?, k)?, key(key(i, "tx")?, k)?);
        if truthy(Some(rx)) || truthy(Some(tx)) {
            bad.push(format!("{} {}/{}", k, s(rx), s(tx)));
        }
    }
    Ok(format!("rx {}  tx {}  {}/{} pkt/s{}", per_s(num(key(r, "rx_bytes_per_s")?)?), per_s(num(key(r, "tx_bytes_per_s")?)?),
               f(num(key(r, "rx_packets_per_s")?)?, 1), f(num(key(r, "tx_packets_per_s")?)?, 1), window_text(i))
       + &if bad.is_empty() { String::new() } else { format!("  {} (rx/tx, total)", bad.join("  ")) })
}

fn disk_line(d: &Value) -> R<String> {
    let r = key(d, "rates")?;
    let value = if truthy(Some(r)) {
        format!("read {} ({}/s)  write {} ({}/s)  I/O time {} %{}", per_s(num(key(r, "read_bytes_per_s")?)?), f(num(key(r, "reads_per_s")?)?, 1),
                per_s(num(key(r, "write_bytes_per_s")?)?), f(num(key(r, "writes_per_s")?)?, 1),
                f(100.0 * num(key(r, "io_time_ratio")?)?, 1), window_text(d))
    } else {
        rate_reason(d.get("reason").unwrap_or(&Value::Null))
    };
    Ok(value + &match d.get("in_flight") { Some(v) if !v.is_null() => format!("  in flight {}", s(v)), _ => String::new() })
}

fn counters_lines(st: &Value) -> R<Vec<String>> {
    let mut out = Vec::new();
    if let Some(Value::Arr(ifs)) = st.get("network").filter(|n| matches!(n, Value::Obj(_))).and_then(|n| n.get("interfaces")) {
        if !ifs.is_empty() {
            // frontends/cli.py net_scope: the host's interfaces on Windows (scope "host"), else this process's
            // network namespace (Linux)
            let host = st.get("network").and_then(|n| n.get("scope")).and_then(|s| s.get("kind"))
                .map_or(false, |k| matches!(k, Value::Str(s) if s == "host"));
            out.push(format!("NET   bytes/s, {}", if host { "this host" } else { "this process's network namespace" }));
            out.extend(rows_of(ifs, "interfaces", network_line)?);
        }
    }
    if let Some(Value::Arr(disks)) = st.get("disk_io").filter(|n| matches!(n, Value::Obj(_))).and_then(|n| n.get("disks")) {
        if !disks.is_empty() {
            out.push("IO    bytes/s per disk".to_string());
            out.extend(rows_of(disks, "disks", disk_line)?);
        }
    }
    if let Some(Value::Obj(res)) = st.get("pressure").filter(|n| matches!(n, Value::Obj(_))).and_then(|p| p.get("resources")) {
        if !res.is_empty() {
            let mut parts = Vec::new();
            for (name, r) in res {
                let mut p = format!("{} some {} %", name, f(num(key(key(r, "some")?, "avg10")?)?, 1));
                if let Some(full) = r.get("full").filter(|v| truthy(Some(v))) {
                    p += &format!(" full {} %", f(num(key(full, "avg10")?)?, 1));
                }
                parts.push(p);
            }
            out.push(format!("PSI   {}  (avg10, time stalled)", parts.join("  ")));
        }
    }
    Ok(out)
}

/// (data, observation) of a group of an /api/observations answer, when both are dicts.
pub fn group<'a>(obs: Option<&'a Value>, name: &str) -> Option<(&'a Value, &'a Value)> {
    let g = obs?.get("groups")?.get(name)?;
    match (g.get("data"), g.get("observation")) {
        (Some(d @ Value::Obj(_)), Some(o @ Value::Obj(_))) => Some((d, o)),
        _ => None,
    }
}

fn age(o: &Value) -> R<String> {
    Ok(format!("observed {} s ago{}", f(num(key(o, "data_age_ms")?)? / 1000.0, 0),
               if truthy(o.get("stale")) { " (not current)" } else { "" }))
}

fn storage_lines(obs: Option<&Value>) -> R<Vec<String>> {
    let Some((data, o)) = group(obs, "storage") else { return Ok(vec![]) };
    let mut out = vec![format!("STORAGE  {}", age(o)?)];
    if let Some(Value::Arr(fss)) = data.get("filesystems").filter(|v| truthy(Some(v))) {
        for fs in fss {
            let Value::Arr(mps) = key(fs, "mount_points")? else { return Err(()) };
            let first = mps.first().ok_or(())?;
            let more = mps.len() - 1;
            let where_ = s(key(first, "path")?) + if truthy(Some(key(first, "read_only")?)) { " (ro)" } else { "" }
                + &if more > 0 { format!(" +{} more", more) } else { String::new() };
            let dev = match key(fs, "device")? {
                v if truthy(Some(v)) => s(v),
                _ => format!("{}:{}", s(key(fs, "major")?), s(key(fs, "minor")?)),
            };
            let total = key(fs, "total_bytes")?;
            if !total.is_null() {
                let (t, u) = (num(total)?, num(key(fs, "used_bytes")?)?);
                let pct = if truthy(Some(total)) { format!(" ({} %)", f(100.0 * u / t, 0)) } else { String::new() };
                out.push(format!("  {}  {} {}  used {}/{}{}  available {}", where_, s(key(fs, "fstype")?), dev, gib(u), gib(t), pct,
                                 gib(num(key(fs, "available_bytes")?)?)));
            } else {
                out.push(format!("  {}  {} {}  capacity unknown", where_, s(key(fs, "fstype")?), dev));
            }
        }
    }
    let mut disks: Vec<(String, Vec<&Value>)> = Vec::new();
    if let Some(Value::Arr(parts)) = data.get("partitions").filter(|v| truthy(Some(v))) {
        for p in parts {
            let d = s(key(p, "disk")?);
            match disks.iter_mut().find(|(k, _)| *k == d) {
                Some(x) => x.1.push(p),
                None => disks.push((d, vec![p])),
            }
        }
    }
    for (disk, parts) in disks {
        let mut mounted = 0;
        for p in &parts {
            mounted += truthy(Some(key(p, "mount_points")?)) as usize;
        }
        out.push(format!("  {}: {} partitions, {} mounted", disk, parts.len(), mounted));
    }
    Ok(out)
}

fn wifi_lines(obs: Option<&Value>) -> R<Vec<String>> {
    let Some((data, o)) = group(obs, "wifi") else { return Ok(vec![]) };
    let Some(Value::Arr(items)) = data.get("interfaces") else { return Ok(vec![]) };
    if items.is_empty() {
        return Ok(vec![]);
    }
    let mut out = vec![format!("WIFI  {}", age(o)?)];
    for i in items {
        let name = s(key(i, "name")?);
        if i.get("connected") != Some(&Value::Bool(true)) {
            let state = if i.get("connected") == Some(&Value::Bool(false)) { "not connected".to_string() }
                        else { i.get("connection_state").map_or("unknown".into(), s) };
            out.push(format!("  {}  {}", name, state));
            continue;
        }
        let signal = if !key(i, "signal_dbm")?.is_null() { format!("{} dBm", s(key(i, "signal_dbm")?)) }
                     else if !key(i, "signal_raw")?.is_null() { format!("signal {} (unit not reported)", s(key(i, "signal_raw")?)) }
                     else { "signal n/a".into() };
        let mut line = format!("  {}  {}", name, signal);
        if !key(i, "link_quality")?.is_null() { line += &format!("  link quality {}", s(key(i, "link_quality")?)); }
        if !key(i, "noise_dbm")?.is_null() { line += &format!("  noise {} dBm", s(key(i, "noise_dbm")?)); }
        out.push(line);
    }
    Ok(out)
}

fn battery_lines(obs: Option<&Value>) -> R<Vec<String>> {
    let Some((data, o)) = group(obs, "battery") else { return Ok(vec![]) };
    let (Some(Value::Arr(cells)), Some(Value::Arr(external))) = (data.get("batteries"), data.get("external")) else { return Ok(vec![]) };
    if cells.is_empty() && external.is_empty() {
        return Ok(vec![]);
    }
    let mut out = vec![format!("BATT  {}", age(o)?)];
    let has = |b: &Value, k: &str| b.get(k).is_some_and(|v| !v.is_null());
    for b in cells {
        let name = s(key(b, "name")?);
        if b.get("present") == Some(&Value::Bool(false)) {
            out.push(format!("  {}  not present", name));
            continue;
        }
        let or = |a: &str, c: &str| b.get(a).filter(|v| truthy(Some(v))).or_else(|| b.get(c).filter(|v| truthy(Some(v))));
        let (full, design) = (or("charge_full_uah", "energy_full_uwh"), or("charge_full_design_uah", "energy_full_design_uwh"));
        let mut parts = vec![
            if has(b, "percent") { format!("{} %", s(&b["percent"])) }
            else { b.get("capacity_level").filter(|v| truthy(Some(v))).map_or("charge n/a".into(), s) },
            b.get("status").filter(|v| truthy(Some(v))).map_or(String::new(), s),
        ];
        if has(b, "voltage_uv") { parts.push(format!("{} V", f(num(&b["voltage_uv"])? / 1e6, 2))); }
        if has(b, "current_ua") {
            parts.push(format!("{:+.2} A", num(&b["current_ua"])? / 1e6));
        }
        if has(b, "power_uw") { parts.push(format!("{} W", f(num(&b["power_uw"])? / 1e6, 2))); }
        if has(b, "cycle_count") { parts.push(format!("{} cycles", s(&b["cycle_count"]))); }
        if has(b, "temp_c") { parts.push(format!("{} °C", f(num(&b["temp_c"])?, 1))); }
        if let (Some(full), Some(design)) = (full, design) {
            parts.push(format!("{} % of design", f(num(full)? * 100.0 / num(design)?, 0)));
        }
        out.push(format!("  {}  {}", name, parts.into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("  ")));
    }
    if !external.is_empty() {
        let mut shown = Vec::new();
        for e in external {
            let online = key(e, "online")?;
            let state = if truthy(Some(online)) { "on" } else if *online == Value::Bool(false) { "off" } else { "n/a" };
            shown.push(format!("{} {}", s(key(e, "name")?), state));
        }
        out.push(format!("  external  {}", shown.join("  ")));
    }
    Ok(out)
}

fn probe_lines(obs: Option<&Value>) -> R<Vec<String>> {
    let Some((data, o)) = group(obs, "probe") else { return Ok(vec![]) };
    let Some(Value::Arr(items)) = data.get("targets") else { return Ok(vec![]) };
    if items.is_empty() {
        return Ok(vec![]);
    }
    let mut out = vec![format!("TCP   connect time, {}", age(o)?)];
    for t in items {
        let (address, port) = (s(key(t, "address")?), s(key(t, "port")?));
        let where_ = if address.contains(':') { format!("[{}]:{}", address, port) } else { format!("{}:{}", address, port) };
        let ms = key(t, "connect_ms")?;
        let now = if ms.is_null() { s(key(t, "reason")?) } else { format!("{} ms", f(num(ms)?, 1)) };
        out.push(format!("  {}  {}  failed {}/{} recent", where_, now, s(key(t, "failures")?), s(key(t, "attempts")?)));
    }
    Ok(out)
}

pub fn collection_note(st: &Value) -> String {
    let Some(Value::Obj(c)) = st.get("collectors") else { return String::new() };
    let bad: Vec<String> = c.iter().filter(|(n, _)| n != "core").filter_map(|(n, v)| match v.get("state") {
        Some(Value::Str(state)) if state == "partial" || state == "error" => Some(format!("{} {}", n, state)),
        _ => None,
    }).collect();
    if bad.is_empty() { String::new() } else { format!("Collection: {}", bad.join(", ")) }
}

/// The /text screen.
pub fn render(st: &Value, obs: Option<&Value>) -> R<String> {
    let up = num(key(st, "uptime")?)?.trunc() as i64;
    let system = system_line(st.get("system"));
    let mode = key(st, "power_mode")?;
    let mut lines = vec![s(key(st, "model")?),
        format!("{}   {}up {}d {:02}:{:02}", s(key(st, "platform")?),
                if truthy(Some(mode)) { format!("mode {}   ", s(mode)) } else { String::new() },
                up.div_euclid(86400), up.rem_euclid(86400) / 3600, up.rem_euclid(3600) / 60)];
    if !system.is_empty() { lines.push(system); }
    let note = collection_note(st);
    if !note.is_empty() { lines.push(note); }
    lines.push(String::new());
    let Value::Arr(cpus) = key(st, "cpu")? else { return Err(()) };
    for c in cpus {
        let freq = key(c, "freq")?;
        let freq = if truthy(Some(freq)) { format!("  {} MHz", pad_left(f(num(freq)? / 1e6, 0), 4)) } else { String::new() };
        let usage = num(key(c, "usage")?)?;
        lines.push(format!("CPU{}{} {} %{}", pad_right(&s(key(c, "id")?), 3), bar(usage, 20), pad_left(f(usage, 1), 5), freq));
    }
    let note = cpu_note(st)?;
    if !note.is_empty() { lines.push(note); }
    for (label, k) in [("GPU", "gpu"), ("NPU", "npu")] {
        let g = if k == "gpu" { key(st, k)? } else { st.get(k).unwrap_or(&Value::Null) };
        if !truthy(Some(g)) { continue; }
        let freq = if truthy(g.get("freq")) { format!("  {} MHz", pad_left(f(num(&g["freq"])? / 1e6, 0), 4)) } else { String::new() };
        let load = match g.get("usage") {
            Some(u) if !u.is_null() => format!("{} {} %", bar(num(u)?, 20), pad_left(f(num(u)?, 1), 5)),
            _ => format!("{}    n/a", bar(0.0, 20)),
        };
        lines.push(format!("{}   {}{}", label, load, freq));
    }
    let m = key(st, "memory")?;
    let (used, total) = (num(key(m, "used")?)?, num(key(m, "total")?)?);
    lines.push(format!("RAM   {} {}/{}", bar(100.0 * used / total, 20), gib(used), gib(total)));
    if truthy(Some(key(m, "swap_total")?)) {
        let (su, stt) = (num(key(m, "swap_used")?)?, num(key(m, "swap_total")?)?);
        lines.push(format!("SWAP  {} {}/{}", bar(100.0 * su / stt, 20), gib(su), gib(stt)));
    }
    let d = key(st, "disk")?;
    let (du, dt) = (num(key(d, "used")?)?, num(key(d, "total")?)?);
    lines.push(format!("DISK  {} {}/{}", bar(100.0 * du / dt, 20), gib(du), gib(dt)));
    lines.push(String::new());
    let temps = key(st, "temperature")?;
    if truthy(Some(temps)) {
        let Value::Obj(items) = temps else { return Err(()) };
        let mut parts = Vec::new();
        for (k, v) in items { parts.push(format!("{} {} °C", k, f(num(v)?, 1))); }
        lines.push(format!("TEMP  {}", parts.join("  ")));
    }
    let power = key(st, "power")?;
    if truthy(Some(power)) {
        let Value::Obj(items) = power else { return Err(()) };
        let mut parts = Vec::new();
        for (k, v) in items { parts.push(format!("{} {}W", k, f(num(v)?, 2))); }
        lines.push(format!("POWER {}", parts.join("  ")));
    }
    let Value::Arr(fans) = key(st, "fans")? else { return Err(()) };
    for fan in fans {
        let (rpm, pct) = (key(fan, "rpm")?, key(fan, "percent")?);
        let vals: Vec<String> = [(!rpm.is_null()).then(|| format!("{} rpm", s(rpm))), (!pct.is_null()).then(|| format!("{} %", s(pct)))]
            .into_iter().flatten().collect();
        lines.push(format!("FAN   {}  {}", s(key(fan, "name")?), if vals.is_empty() { "n/a".into() } else { vals.join("  ") }));
    }
    let mut extra = counters_lines(st)?;
    extra.extend(storage_lines(obs)?);
    extra.extend(wifi_lines(obs)?);
    extra.extend(battery_lines(obs)?);
    extra.extend(probe_lines(obs)?);
    if !extra.is_empty() && !lines.last().is_some_and(|l| l.is_empty()) {
        lines.push(String::new());
    }
    lines.extend(extra);
    Ok(lines.join("\n"))
}

/// What tests/frontends/test_cli.py checked of frontends/cli.py's render() and its sections, kept when cli.py
/// was removed (the platmon command's own tests are in frontend/).
#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::parse;

    const FULL: &str = r#"{"platform": "Jetson Orin", "model": "Test Board", "uptime": 90061, "power_mode": "MAXN_SUPER",
        "system": {"os": "Ubuntu 22.04.5 LTS", "kernel": "5.15.199-tegra", "arch": "aarch64", "hostname": "orin", "l4t": "R36.5.2"},
        "cpu": [{"id": 0, "usage": 12.5, "freq": 1728000000}, {"id": 2, "usage": 0.0, "freq": 729600000}],
        "gpu": {"usage": 50.0, "freq": 306000000, "max_freq": 1020000000},
        "memory": {"total": 8589934592, "used": 2147483648, "swap_total": 4294967296, "swap_used": 0},
        "disk": {"total": 107374182400, "used": 26843545600},
        "temperature": {"cpu": 51.2}, "power": {"VDD_IN": 4.82},
        "fans": [{"name": "pwmfan", "rpm": null, "percent": 31}, {"name": "pwm_tach", "rpm": 1741, "percent": null}]}"#;

    /// FULL with these keys replaced (Python's dict(FULL, key=value)), each value JSON text.
    fn full(pairs: &[(&str, &str)]) -> Value {
        let mut v = parse(FULL).unwrap();
        for (k, j) in pairs {
            v.set(k, parse(j).unwrap());
        }
        v
    }

    fn text(v: &Value) -> String {
        render(v, None).unwrap()
    }

    /// A bare Linux host (e.g. WSL): no GPU, power mode, cpufreq, swap or sensors.
    fn bare() -> Value {
        full(&[("platform", r#""Linux""#), ("power_mode", "null"), ("gpu", "null"), ("temperature", "{}"), ("power", "{}"), ("fans", "[]"),
               ("cpu", r#"[{"id": 0, "usage": 3.0, "freq": null}]"#),
               ("memory", r#"{"total": 8589934592, "used": 2147483648, "swap_total": 0, "swap_used": 0}"#)])
    }

    fn iface(name: &str, rates: &str, reason: &str) -> String {
        format!(r#"{{"name": "{}", "ifindex": 2, "rx": {{"bytes": 1, "packets": 1, "errors": 0, "dropped": 0}},
                   "tx": {{"bytes": 1, "packets": 1, "errors": 0, "dropped": 0}}, "rates": {}, "window_ms": {}, "reason": {}}}"#,
                name, rates, if rates == "null" { "null" } else { "1000.0" }, reason)
    }

    const NET_RATES: &str = r#"{"rx_bytes_per_s": 1536.0, "tx_bytes_per_s": 0.0, "rx_packets_per_s": 2.0, "tx_packets_per_s": 0.4}"#;

    #[test]
    fn full_shows_every_reading() {
        let out = text(&parse(FULL).unwrap());
        for expected in ["Jetson Orin   mode MAXN_SUPER   up 1d 01:01",
                         "Ubuntu 22.04.5 LTS · kernel 5.15.199-tegra · aarch64 · host orin · L4T R36.5.2",
                         "12.5 %  1728 MHz",
                         "CPU2  ", // labelled by core id, not position (cpu1 offline)
                         "50.0 %   306 MHz", "RAM   █████··············· 2.0G/8.0G", "SWAP  ", "cpu 51.2 °C", "VDD_IN 4.82W",
                         "FAN   pwmfan  31 %", "FAN   pwm_tach  1741 rpm"] {
            assert!(out.contains(expected), "{expected}");
        }
        assert!(!out.contains('\x1b')); // --once, pipes and /text: plain text
    }

    #[test]
    fn bare_shows_the_basics_and_hides_what_is_missing() {
        let out = text(&bare());
        assert!(out.contains("Linux   up 1d 01:01") && out.contains("CPU0  ") && out.contains("DISK  "));
        for absent in ["mode", "MHz", "GPU", "SWAP", "TEMP", "POWER", "FAN"] {
            assert!(!out.contains(absent), "{absent}");
        }
    }

    #[test]
    fn system_entries() {
        // an older server has no "system" entry: no system line, no empty extra line
        let mut old = parse(FULL).unwrap();
        if let Value::Obj(o) = &mut old {
            o.retain(|(k, _)| k != "system");
        }
        assert_eq!(text(&old).split('\n').take(3).collect::<Vec<_>>(), ["Test Board", "Jetson Orin   mode MAXN_SUPER   up 1d 01:01", ""]);
        // entries added by a future board module show up as "key value"
        assert!(text(&full(&[("system", r#"{"os": "X", "firmware": "1.2"}"#)])).contains("firmware 1.2"));
    }

    #[test]
    fn unreadable_gpu_clock_and_fan() {
        // null means "could not read": no 0 MHz / 0 rpm
        let out = text(&full(&[("gpu", r#"{"usage": 50.0, "freq": null, "max_freq": null}"#),
                              ("fans", r#"[{"name": "CPU fan", "rpm": null, "percent": null}]"#)]));
        let gpu = out.split("GPU").nth(1).unwrap().split('\n').next().unwrap();
        assert!(out.contains("GPU   ") && out.contains("50.0 %") && !gpu.contains("MHz"));
        assert!(out.contains("FAN   CPU fan  n/a"));
    }

    const SAMPLING: &str = r#"{"mode": "interval", "window_ms": 1000.0, "unavailable": []}"#;

    #[test]
    fn cpu_sampling() {
        // all cores measured: the same screen as a server without cpu_sampling
        assert_eq!(text(&full(&[("cpu_sampling", SAMPLING)])), text(&parse(FULL).unwrap()));
        // the service's first reading: no CPU rows yet, said as such; everything else still shows
        let out = text(&full(&[("cpu", "[]"), ("cpu_sampling", r#"{"mode": "interval", "window_ms": null, "unavailable": [
            {"id": 0, "reason": "warmup"}, {"id": 2, "reason": "warmup"}]}"#)]));
        assert!(out.split('\n').any(|l| l == "CPU sampling: warming up"));
        assert!(!out.contains("CPU0") && !out.split("GPU").next().unwrap().contains("  0.0 %")); // no made-up 0 % bars
        assert!(out.contains("RAM   ") && out.contains("GPU   "));
        let out = text(&full(&[("cpu", r#"[{"id": 0, "usage": 12.5, "freq": 1728000000}]"#), ("cpu_sampling", r#"{"mode": "interval",
            "window_ms": 1000.0, "unavailable": [{"id": 2, "reason": "warmup"}, {"id": 3, "reason": "counter_regressed"},
            {"id": 4, "reason": "new_code"}]}"#)]));
        assert!(out.contains("CPU0  ") && !out.contains("CPU2  "));
        assert!(out.contains("CPU sampling: CPU2 warming up; CPU3 counter went backwards; CPU4 new_code"));
    }

    fn g(state: &str, reason: &str) -> String {
        format!(r#"{{"state": "{}", "reason": {}, "issues": [], "issues_truncated": 0}}"#, state, reason)
    }

    #[test]
    fn collection_note_only_for_read_failures() {
        let fine = format!(r#"{{"core": {}, "temperature": {}, "gpu": {}, "fans": {}}}"#, g("ok", "null"), g("ok", "null"),
                           g("unavailable", r#""not_detected""#), g("unavailable", r#""not_exposed""#));
        assert_eq!(text(&full(&[("collectors", &fine)])), text(&parse(FULL).unwrap())); // absent sensors are not a warning
        let bad = format!(r#"{{"core": {}, "temperature": {}, "gpu": {}, "fans": {}}}"#, g("ok", "null"),
                          g("partial", r#""some_unreadable""#), g("error", r#""io_error""#), g("ok", "null"));
        let out = text(&full(&[("collectors", &bad)]));
        let lines: Vec<&str> = out.split('\n').collect();
        assert_eq!(lines[3], "Collection: temperature partial, gpu error");
        assert!(lines[4].is_empty() && out.contains("CPU0  "));
        // no failure for unknown shapes, groups or reasons
        let new = format!(r#"{{"new_group": {}}}"#, g("error", r#""new_code""#));
        for odd in [r#""x""#, "null", r#"{"odd": 5, "x": {"state": null}}"#, &new] {
            assert!(text(&full(&[("collectors", odd)])).starts_with("Test Board\n"), "{odd}");
        }
    }

    #[test]
    fn network_and_disk_io() {
        let eth0 = iface("eth0", NET_RATES, "null").replacen(r#""errors": 0"#, r#""errors": 3"#, 1);
        let net = format!(r#"{{"interfaces": [{}, {}, {}]}}"#, eth0, iface("wlan0", "null", r#""warmup""#), iface("x", "null", r#""new_code""#));
        let io = r#"{"disks": [{"name": "nvme0n1", "in_flight": 2, "rates": {"read_bytes_per_s": 3145728, "write_bytes_per_s": 512.0,
            "reads_per_s": 4.0, "writes_per_s": 0.5, "io_time_ratio": 0.034}, "reason": null},
            {"name": "sda", "in_flight": 0, "rates": null, "reason": "counter_regressed"}]}"#;
        let out = text(&full(&[("network", &net), ("disk_io", io)]));
        let out: Vec<&str> = out.split('\n').collect();
        let i = out.iter().position(|l| *l == "NET   bytes/s, this process's network namespace").unwrap();
        assert_eq!(out[i + 1..i + 4], ["  eth0   rx 1.5 KiB/s  tx 0 B/s  2.0/0.4 pkt/s  window 1.000 s  errors 3/0 (rx/tx, total)",
                                       "  wlan0  warming up", "  x      new_code"]);
        assert_eq!(out[i + 4..], ["IO    bytes/s per disk",
                                  "  nvme0n1  read 3.0 MiB/s (4.0/s)  write 512 B/s (0.5/s)  I/O time 3.4 %  in flight 2",
                                  "  sda      counter went backwards  in flight 0"]);
        assert_eq!(out[i - 1], ""); // one blank line before, never two
    }

    #[test]
    fn names_the_host_without_a_network_namespace() {
        // Windows reports scope host: the header says whose interfaces they are
        let host = format!(r#"{{"scope": {{"kind": "host", "id": null}}, "interfaces": [{}]}}"#, iface("Wi-Fi", NET_RATES, "null"));
        let out = text(&full(&[("network", &host)]));
        assert!(out.split('\n').any(|l| l == "NET   bytes/s, this host") && !out.contains("namespace"));
        let linux = format!(r#"{{"scope": {{"kind": "process_network_namespace", "id": "netns1:x"}}, "interfaces": [{}]}}"#,
                            iface("eth0", NET_RATES, "null"));
        assert!(text(&full(&[("network", &linux)])).split('\n').any(|l| l == "NET   bytes/s, this process's network namespace"));
    }

    #[test]
    fn limits_rows_and_says_how_many_were_left_out() {
        let mut names: Vec<String> = (0..15).map(|n| format!("veth{:02}", n)).collect();
        names.push("a-very-long-interface-name".into());
        let items: Vec<String> = names.iter().map(|n| iface(n, NET_RATES, "null")).collect();
        let out = text(&full(&[("network", &format!(r#"{{"interfaces": [{}]}}"#, items.join(", ")))]));
        let out: Vec<&str> = out.split('\n').collect();
        let rows = &out[out.iter().position(|l| *l == "NET   bytes/s, this process's network namespace").unwrap() + 1..];
        assert_eq!(rows.len(), 13);
        assert_eq!(*rows.last().unwrap(), "  +4 more interfaces (all in /api/stats)");
        assert!(rows[0].starts_with("  veth00            rx")); // names padded to at most 16 columns
    }

    #[test]
    fn without_counters_is_unchanged() {
        let plain = text(&parse(FULL).unwrap());
        let extras: [&[(&str, &str)]; 5] = [&[], &[("network", "null"), ("disk_io", "null")], &[("network", r#"{"interfaces": []}"#)],
                                            &[("network", r#"{"interfaces": "odd"}"#), ("disk_io", r#"{"disks": 5}"#)], &[("network", r#""odd""#)]];
        for extra in extras {
            assert_eq!(text(&full(extra)), plain, "{extra:?}");
        }
    }

    #[test]
    fn pressure_without_cpu_full() {
        let psi = r#"{"resources": {"cpu": {"some": {"avg10": 1.25}, "full": null}, "io": {"some": {"avg10": 3.0}, "full": {"avg10": 0.5}}}}"#;
        assert_eq!(text(&full(&[("pressure", psi)])).split('\n').last().unwrap(),
                   "PSI   cpu some 1.2 %  io some 3.0 % full 0.5 %  (avg10, time stalled)");
        let plain = text(&parse(FULL).unwrap());
        assert_eq!(text(&full(&[("pressure", r#"{"resources": {}}"#)])), plain);
        assert_eq!(text(&full(&[("pressure", r#""odd""#)])), plain);
    }

    #[test]
    fn counter_window_and_zero_in_flight_are_observed_values() {
        let net = format!(r#"{{"interfaces": [{}]}}"#, iface("eth0", NET_RATES, "null").replace(r#""window_ms": 1000.0"#, r#""window_ms": 1250"#));
        let out = text(&full(&[("network", &net), ("disk_io", r#"{"disks": [{"name": "sda", "rates": null, "reason": "warmup", "in_flight": 0}]}"#)]));
        assert!(out.contains("window 1.250 s") && out.contains("warming up  in flight 0"));
    }

    #[test]
    fn probe() {
        let obs = parse(r#"{"groups": {"probe": {"observation": {"data_age_ms": 4000, "stale": false}, "data": {"targets": [
            {"address": "192.0.2.1", "port": 443, "connect_ms": 12.34, "reason": null, "attempts": 10, "failures": 1},
            {"address": "2001:db8::1", "port": 22, "connect_ms": null, "reason": "timeout", "attempts": 3, "failures": 3}]}}}}"#).unwrap();
        assert_eq!(probe_lines(Some(&obs)).unwrap(), ["TCP   connect time, observed 4 s ago", "  192.0.2.1:443  12.3 ms  failed 1/10 recent",
                                                     "  [2001:db8::1]:22  timeout  failed 3/3 recent"]);
        assert!(probe_lines(None).unwrap().is_empty());
        assert!(probe_lines(Some(&parse(r#"{"groups": {"probe": {"data": null}}}"#).unwrap())).unwrap().is_empty());
    }

    #[test]
    fn wifi() {
        let obs = |interfaces: &str, stale: bool| parse(&format!(r#"{{"groups": {{"wifi": {{"observation": {{"data_age_ms": 3000, "stale": {}}},
            "data": {{"interfaces": {}}}}}}}}}"#, stale, interfaces)).unwrap();
        let row = |name: &str, extra: &str| format!(r#"{{"name": "{}", "connection_state": "connected", "signal_dbm": null,
            "signal_raw": null, "link_quality": null, "noise_dbm": null, {}}}"#, name, extra);
        let rows = format!("[{}, {}, {}]", row("wlan0", r#""connected": true, "signal_dbm": -64, "link_quality": 46"#),
                           row("wlan1", r#""connected": false"#),
                           row("eth9", r#""connected": true, "signal_raw": 70, "link_quality": 70"#));
        assert_eq!(wifi_lines(Some(&obs(&rows, false))).unwrap(), ["WIFI  observed 3 s ago", "  wlan0  -64 dBm  link quality 46",
            "  wlan1  not connected", "  eth9  signal 70 (unit not reported)  link quality 70"]);
        assert_eq!(wifi_lines(Some(&obs(&rows, true))).unwrap()[0], "WIFI  observed 3 s ago (not current)");
        assert!(wifi_lines(Some(&obs("[]", false))).unwrap().is_empty() && wifi_lines(None).unwrap().is_empty());
        assert!(wifi_lines(Some(&parse(r#"{"groups": {"wifi": {"data": null}}}"#).unwrap())).unwrap().is_empty());
    }

    #[test]
    fn storage_and_ages() {
        let body = |age: u32, stale: bool| parse(&format!(r#"{{"groups": {{"storage": {{"observation": {{"id": 2, "data_age_ms": {}, "stale": {}}},
            "data": {{"filesystems": [{{"device": "nvme0n1p1", "major": 259, "minor": 1, "fstype": "ext4", "total_bytes": 4294967296,
                "used_bytes": 1073741824, "available_bytes": 3221225472,
                "mount_points": [{{"path": "/", "read_only": false}}, {{"path": "/srv", "read_only": true}}]}},
              {{"device": null, "major": 0, "minor": 40, "fstype": "btrfs", "total_bytes": null, "used_bytes": null,
                "available_bytes": null, "mount_points": [{{"path": "/data", "read_only": false}}]}}],
            "partitions": [{{"name": "p1", "disk": "nvme0n1", "mount_points": ["/"]}}, {{"name": "p2", "disk": "nvme0n1", "mount_points": []}}]}}}}}}}}"#,
            age, stale)).unwrap();
        assert_eq!(storage_lines(Some(&body(10000, false))).unwrap(), ["STORAGE  observed 10 s ago",
            "  / +1 more  ext4 nvme0n1p1  used 1.0G/4.0G (25 %)  available 3.0G", "  /data  btrfs 0:40  capacity unknown",
            "  nvme0n1: 2 partitions, 1 mounted"]);
        assert_eq!(storage_lines(Some(&body(95000, true))).unwrap()[0], "STORAGE  observed 95 s ago (not current)");
        for odd in ["null", "{}", r#"{"groups": {}}"#, r#"{"groups": {"storage": {"state": "starting", "observation": null, "data": null}}}"#,
                    r#"{"groups": "x"}"#] {
            assert!(storage_lines(Some(&parse(odd).unwrap())).unwrap().is_empty(), "{odd}");
        }
    }
}
