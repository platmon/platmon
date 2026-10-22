//! frontends/cli.py render(): the plain-text screen /text answers with, from an /api/stats snapshot and an
//! /api/observations answer. Where Python would raise (a key missing, None formatted as a number), Err:
//! the server then closes the connection without an answer, as Python's handler does.
use crate::json::Value;

type R<T> = Result<T, ()>;

const MAX_ROWS: usize = 12;

fn key<'a>(v: &'a Value, k: &str) -> R<&'a Value> {
    v.get(k).ok_or(())
}

/// A number as Python formats it with a float spec: int, float or bool, else Err (TypeError).
fn num(v: &Value) -> R<f64> {
    match v {
        Value::Int(i) => Ok(*i as f64),
        Value::Big(i) => Ok(*i as f64),
        Value::Float(f) => Ok(*f),
        Value::Bool(b) => Ok(*b as i64 as f64),
        _ => Err(()),
    }
}

/// Python's truth value.
fn truthy(v: Option<&Value>) -> bool {
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
fn s(v: &Value) -> String {
    match v {
        Value::Str(x) => x.clone(),
        other => crate::history::py_str(other).unwrap_or_else(|| other.to_py().replace('"', "'")),
    }
}

/// Python's format(x, ".Nf") for finite and non-finite floats.
fn f(x: f64, prec: usize) -> String {
    if x.is_nan() { return "nan".into() }
    if x.is_infinite() { return if x > 0.0 { "inf".into() } else { "-inf".into() } }
    format!("{:.*}", prec, x)
}

fn pad_left(t: String, w: usize) -> String {
    let n = t.chars().count();
    if n >= w { t } else { format!("{}{}", " ".repeat(w - n), t) }
}

fn pad_right(t: &str, w: usize) -> String {
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

fn cpu_note(st: &Value) -> R<String> {
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

fn rate_reason(r: &Value) -> String {
    match r {
        Value::Str(x) => match x.as_str() {
            "warmup" => "warming up", "gap" => "restarting after a gap", "counter_regressed" => "counter went backwards",
            "invalid_interval" => "invalid interval", "identity_unavailable" => "identity unknown",
            "namespace_changed" => "namespace changed", x => x,
        }.to_string(),
        other => s(other),
    }
}

fn per_s(v: f64) -> String {
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
        format!("read {} ({}/s)  write {} ({}/s)  I/O time {}%{}", per_s(num(key(r, "read_bytes_per_s")?)?), f(num(key(r, "reads_per_s")?)?, 1),
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
            out.push("NET   bytes/s, this process's network namespace".to_string());
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
                let mut p = format!("{} some {}%", name, f(num(key(key(r, "some")?, "avg10")?)?, 1));
                if let Some(full) = r.get("full").filter(|v| truthy(Some(v))) {
                    p += &format!(" full {}%", f(num(key(full, "avg10")?)?, 1));
                }
                parts.push(p);
            }
            out.push(format!("PSI   {}  (avg10, time stalled)", parts.join("  ")));
        }
    }
    Ok(out)
}

/// (data, observation) of a group of an /api/observations answer, when both are dicts.
fn group<'a>(obs: Option<&'a Value>, name: &str) -> Option<(&'a Value, &'a Value)> {
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
                let pct = if truthy(Some(total)) { format!(" ({}%)", f(100.0 * u / t, 0)) } else { String::new() };
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
            if has(b, "percent") { format!("{}%", s(&b["percent"])) }
            else { b.get("capacity_level").filter(|v| truthy(Some(v))).map_or("charge n/a".into(), s) },
            b.get("status").filter(|v| truthy(Some(v))).map_or(String::new(), s),
        ];
        if has(b, "voltage_uv") { parts.push(format!("{} V", f(num(&b["voltage_uv"])? / 1e6, 2))); }
        if has(b, "current_ua") {
            parts.push(format!("{:+.2} A", num(&b["current_ua"])? / 1e6));
        }
        if has(b, "power_uw") { parts.push(format!("{} W", f(num(&b["power_uw"])? / 1e6, 2))); }
        if has(b, "cycle_count") { parts.push(format!("{} cycles", s(&b["cycle_count"]))); }
        if let (Some(full), Some(design)) = (full, design) {
            parts.push(format!("{}% of design", f(num(full)? * 100.0 / num(design)?, 0)));
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

fn collection_note(st: &Value) -> String {
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
        lines.push(format!("CPU{}{} {}%{}", pad_right(&s(key(c, "id")?), 3), bar(usage, 20), pad_left(f(usage, 1), 5), freq));
    }
    let note = cpu_note(st)?;
    if !note.is_empty() { lines.push(note); }
    for (label, k) in [("GPU", "gpu"), ("NPU", "npu")] {
        let g = if k == "gpu" { key(st, k)? } else { st.get(k).unwrap_or(&Value::Null) };
        if !truthy(Some(g)) { continue; }
        let freq = if truthy(g.get("freq")) { format!("  {} MHz", pad_left(f(num(&g["freq"])? / 1e6, 0), 4)) } else { String::new() };
        let load = match g.get("usage") {
            Some(u) if !u.is_null() => format!("{} {}%", bar(num(u)?, 20), pad_left(f(num(u)?, 1), 5)),
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
        for (k, v) in items { parts.push(format!("{} {}°C", k, f(num(v)?, 1))); }
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
        let vals: Vec<String> = [(!rpm.is_null()).then(|| format!("{} rpm", s(rpm))), (!pct.is_null()).then(|| format!("{}%", s(pct)))]
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
