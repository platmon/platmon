//! platmon.py's configuration: platmon.ini read as configparser reads it (interpolation off), checked
//! as check() and levels() check it, with the same defaults and the same messages.
use crate::json::Value;

/// (section, key, default as configparser holds it, type): platmon.DEFAULTS in its order.
const DEFAULTS: &[(&str, &[(&str, &str, Kind)])] = &[
    ("core", &[("interval", "1.0", Kind::Float)]),
    ("network", &[("enabled", "yes", Kind::Bool)]),
    ("disk_io", &[("enabled", "yes", Kind::Bool)]),
    ("storage", &[("enabled", "yes", Kind::Bool), ("interval", "30.0", Kind::Float)]),
    ("wifi", &[("enabled", "yes", Kind::Bool), ("interval", "5.0", Kind::Float)]),
    ("battery", &[("enabled", "yes", Kind::Bool), ("interval", "60.0", Kind::Float)]),
    ("probe", &[("enabled", "yes", Kind::Bool), ("interval", "10.0", Kind::Float), ("timeout", "2.0", Kind::Float),
                ("targets", "", Kind::Str)]),
    ("history", &[("enabled", "yes", Kind::Bool), ("retention", "600.0", Kind::Float)]),
    ("replay", &[("enabled", "yes", Kind::Bool), ("retention", "600.0", Kind::Float), ("max_bytes", "16777216", Kind::Int)]),
    ("pressure", &[("enabled", "yes", Kind::Bool)]),
    ("http", &[("enabled", "yes", Kind::Bool), ("bind", "0.0.0.0", Kind::Str), ("port", "9797", Kind::Int), ("web", "yes", Kind::Bool)]),
    ("thresholds", &[("cpu", "80, 90", Kind::Str), ("gpu", "80, 90", Kind::Str), ("memory", "80, 90", Kind::Str),
                     ("filesystem", "80, 90", Kind::Str), ("temperature", "", Kind::Str), ("battery", "20, 10", Kind::Str),
                     ("wifi", "-70, -80", Kind::Str)]),
];
const REMOVED: &[(&str, &str)] = &[("terminal", "the terminal view is now the `platmon` command (frontends/cli.py); delete this section")];
const RANGES: &[((&str, &str), (f64, f64))] = &[
    (("core", "interval"), (0.1, 3600.0)), (("storage", "interval"), (5.0, 3600.0)), (("wifi", "interval"), (1.0, 3600.0)),
    (("probe", "interval"), (1.0, 3600.0)), (("battery", "interval"), (10.0, 3600.0)), (("replay", "retention"), (1.0, 3600.0)),
    (("replay", "max_bytes"), (1048576.0, 67108864.0)), (("probe", "timeout"), (0.1, 10.0)), (("history", "retention"), (60.0, 3600.0)),
    (("http", "port"), (1.0, 65535.0)),
];
const LOW_LEVELS: &[(&str, (f64, f64, &str))] = &[("battery", (0.0, 100.0, "%")), ("wifi", (-120.0, 0.0, " dBm"))];
pub const FRONTENDS: &[&str] = &["http"];

#[derive(Clone, Copy, PartialEq)]
enum Kind { Bool, Int, Float, Str }

/// configparser's sections: [(name, [(key, value)])], DEFAULT apart.
#[derive(Default)]
pub struct Ini {
    defaults: Vec<(String, String)>,
    sections: Vec<(String, Vec<(String, String)>)>,
}

/// Python's repr() of a str.
pub fn py_repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => { out.push('\\'); out.push(c); }
            c if (c as u32) < 0x20 || (0x7f..0xa1).contains(&(c as u32)) || c == '\u{ad}' => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

/// Python's float(text) as a config value gives it: surrounding whitespace, a sign, digits with single
/// underscores between them, inf and nan.
pub fn py_float(text: &str) -> Option<f64> {
    let t = text.trim_matches(crate::pystr::py_space);
    let b = t.as_bytes();
    let between_digits = |i: usize| i > 0 && b[i - 1].is_ascii_digit() && b.get(i + 1).is_some_and(u8::is_ascii_digit);
    if (0..b.len()).any(|i| b[i] == b'_' && !between_digits(i)) {
        return None;
    }
    t.replace('_', "").parse().ok()
}

/// repr() of a float, as Python prints it in a message.
fn py_float_repr(f: f64) -> String {
    if f.is_nan() { return "nan".into() }
    if f.is_infinite() { return if f > 0.0 { "inf".into() } else { "-inf".into() } }
    Value::Float(f).to_string(false)
}

/// The %g of a level's floor or ceiling (they are whole numbers).
fn g(f: f64) -> String {
    if f.fract() == 0.0 { format!("{}", f as i64) } else { format!("{}", f) }
}

impl Ini {
    fn section(&self, name: &str) -> Option<&Vec<(String, String)>> {
        self.sections.iter().find(|(n, _)| n == name).map(|(_, v)| v)
    }

    /// A key of a section, falling back to DEFAULT, as configparser looks it up.
    fn get(&self, section: &str, key: &str) -> Option<&str> {
        self.section(section).and_then(|s| s.iter().find(|(k, _)| k == key)).or_else(|| self.defaults.iter().find(|(k, _)| k == key))
            .map(|(_, v)| v.as_str())
    }

    /// cfg[section][key] = value, in a section the defaults made.
    fn set(&mut self, section: &str, key: &str, value: &str) {
        let s = &mut self.sections.iter_mut().find(|(n, _)| n == section).expect("a default section").1;
        match s.iter_mut().find(|(k, _)| k == key) {
            Some(kv) => kv.1 = value.to_string(),
            None => s.push((key.to_string(), value.to_string())),
        }
    }

    /// The keys of a section as configparser lists them: its own, then DEFAULT's.
    fn keys(&self, section: &str) -> Vec<&str> {
        let mut out: Vec<&str> = self.section(section).map_or(vec![], |s| s.iter().map(|(k, _)| k.as_str()).collect());
        for (k, _) in &self.defaults {
            if !out.contains(&k.as_str()) {
                out.push(k);
            }
        }
        out
    }

    /// The defaults, as platmon.load_config sets them with read_dict.
    fn with_defaults() -> Ini {
        let mut ini = Ini::default();
        for (section, keys) in DEFAULTS {
            ini.sections.push((section.to_string(), keys.iter().map(|(k, v, _)| (k.to_string(), v.to_string())).collect()));
        }
        ini
    }

    /// ConfigParser.read of one file on top: Err with configparser's message on a syntax error.
    fn read(&mut self, text: &str, name: &str) -> Result<(), String> {
        let mut errors: Vec<String> = Vec::new();
        let mut added_sections: Vec<String> = Vec::new();
        let mut added_options: Vec<(String, String)> = Vec::new();
        // the section being read (None: DEFAULT) and its option being read, its value lines
        let mut cursect: Option<Option<String>> = None;
        let mut optname: Option<String> = None;
        let mut values: Vec<(Option<String>, String, Vec<String>)> = Vec::new(); // (section, key, lines)
        let mut indent_level = 0usize;
        let mut lines = text.split_inclusive('\n').enumerate().peekable();
        while let Some((i, line)) = lines.next() {
            let lineno = i + 1;
            let stripped = line.trim_matches(crate::pystr::py_space);
            let comment = stripped.starts_with('#') || stripped.starts_with(';');
            let clean = if comment { "" } else { stripped };
            if clean.is_empty() {
                // an empty line goes into the value being read, unless it held a comment
                if !comment && cursect.is_some() {
                    if let Some(o) = &optname {
                        if let Some(v) = values.iter_mut().rev().find(|(s, k, _)| Some(s) == cursect.as_ref() && k == o) {
                            v.2.push(String::new());
                        }
                    }
                }
                continue;
            }
            let cur_indent = line.char_indices().find(|(_, c)| !crate::pystr::py_space(*c)).map_or(0, |(i, _)| line[..i].chars().count());
            if cursect.is_some() && optname.is_some() && cur_indent > indent_level {
                let o = optname.as_ref().unwrap();
                if let Some(v) = values.iter_mut().rev().find(|(s, k, _)| Some(s) == cursect.as_ref() && k == o) {
                    v.2.push(clean.to_string());
                }
                continue;
            }
            indent_level = cur_indent;
            let header = clean.strip_prefix('[').and_then(|h| h.rfind(']').filter(|&e| e > 0).map(|e| &h[..e]));
            if header.is_none() && cursect.is_none() {
                return Err(format!("File contains no section headers.\nfile: {}, line: {}\n{}", py_repr(name), lineno, py_repr(line)));
            }
            if let Some(h) = header {
                if added_sections.iter().any(|s| s == h) {
                    return Err(format!("While reading from {} [line {:2}]: section {} already exists", py_repr(name), lineno, py_repr(h)));
                }
                added_sections.push(h.to_string());
                if h == "DEFAULT" {
                    cursect = Some(None);
                } else {
                    if self.section(h).is_none() {
                        self.sections.push((h.to_string(), vec![]));
                    }
                    cursect = Some(Some(h.to_string()));
                }
                optname = None;
                continue;
            }
            // option: everything before the first = or :, then the value
            let Some(pos) = clean.find(['=', ':']) else {
                errors.push(format!("\n\t[line {:2}]: {}", lineno, py_repr(line)));
                continue;
            };
            let name_part = clean[..pos].trim_end_matches(crate::pystr::py_space);
            if name_part.is_empty() {
                errors.push(format!("\n\t[line {:2}]: {}", lineno, py_repr(line)));
            }
            let key = name_part.to_lowercase();
            let sect = cursect.clone().unwrap();
            let shown = sect.clone().unwrap_or_else(|| "DEFAULT".into());
            if added_options.iter().any(|(s, k)| *s == shown && *k == key) {
                return Err(format!("While reading from {} [line {:2}]: option {} in section {} already exists",
                                   py_repr(name), lineno, py_repr(&key), py_repr(&shown)));
            }
            added_options.push((shown, key.clone()));
            let value = clean[pos + 1..].trim_matches(crate::pystr::py_space).to_string();
            values.retain(|(s, k, _)| !(*s == sect && *k == key));
            values.push((sect, key.clone(), vec![value]));
            optname = Some(key);
        }
        for (sect, key, lines) in values {
            let value = lines.join("\n").trim_end_matches(crate::pystr::py_space).to_string();
            let target = match &sect {
                None => &mut self.defaults,
                Some(s) => &mut self.sections.iter_mut().find(|(n, _)| n == s).unwrap().1,
            };
            match target.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = value,
                None => target.push((key, value)),
            }
        }
        if !errors.is_empty() {
            return Err(format!("Source contains parsing errors: {}{}", py_repr(name), errors.concat()));
        }
        Ok(())
    }
}

/// configparser's getboolean.
fn boolean(v: &str) -> Option<bool> {
    match v.to_lowercase().as_str() {
        "1" | "yes" | "true" | "on" => Some(true),
        "0" | "no" | "false" | "off" => Some(false),
        _ => None,
    }
}

/// The checked configuration.
pub struct Config {
    ini: Ini,
    pub targets: Vec<(String, u16)>,
}

impl Config {
    pub fn text(&self, section: &str, key: &str) -> String {
        self.ini.get(section, key).unwrap_or_default().to_string()
    }
    pub fn flag(&self, section: &str, key: &str) -> bool {
        boolean(&self.text(section, key)).unwrap_or(false)
    }
    pub fn float(&self, section: &str, key: &str) -> f64 {
        py_float(&self.text(section, key)).unwrap_or(0.0)
    }
    pub fn int(&self, section: &str, key: &str) -> i64 {
        crate::metadata::py_int(&self.text(section, key)).unwrap_or(0)
    }

    /// Every key of DEFAULTS with its typed value, and the levels: what config-check prints.
    pub fn value(&self, platform: Option<&str>) -> Value {
        let mut v = Value::obj();
        for (section, keys) in DEFAULTS {
            let mut s = Value::obj();
            for (key, _, kind) in keys.iter() {
                s.set(key, match kind {
                    Kind::Bool => Value::Bool(self.flag(section, key)),
                    Kind::Int => self.int(section, key).into(),
                    Kind::Float => self.float(section, key).into(),
                    Kind::Str => self.text(section, key).into(),
                });
            }
            v.set(section, s);
        }
        v.set("levels", levels(&self.ini, platform).unwrap_or_default());
        v.set("targets", Value::Arr(self.targets.iter().map(|(a, p)| Value::Arr(vec![a.as_str().into(), (*p as i64).into()])).collect()));
        v
    }
}

/// platmon.check: unknown sections and keys, values of the wrong type or out of range.
fn check(ini: &Ini) -> Result<(), String> {
    for (section, why) in REMOVED {
        if ini.section(section).is_some() {
            return Err(format!("[{}] is no longer supported: {}", section, why));
        }
    }
    let mut unknown: Vec<&str> = ini.sections.iter().map(|(n, _)| n.as_str()).filter(|n| !DEFAULTS.iter().any(|(d, _)| d == n)).collect();
    if !unknown.is_empty() {
        unknown.sort();
        unknown.dedup();
        return Err(format!("unknown config section(s): {}", unknown.join(", ")));
    }
    for (section, keys) in DEFAULTS {
        let mut unknown: Vec<&str> = ini.keys(section).into_iter().filter(|k| !keys.iter().any(|(d, _, _)| d == k)).collect();
        if !unknown.is_empty() {
            unknown.sort();
            return Err(format!("[{}] unknown key(s): {}; valid: {}", section, unknown.join(", "),
                               keys.iter().map(|(k, _, _)| *k).collect::<Vec<_>>().join(", ")));
        }
        for (key, _, kind) in keys.iter() {
            let text = ini.get(section, key).unwrap_or_default();
            let value = match kind {
                Kind::Bool => boolean(text).map(|b| b as i64 as f64),
                Kind::Int => crate::metadata::py_int(text).map(|i| i as f64),
                Kind::Float => py_float(text),
                Kind::Str => Some(0.0),
            };
            let Some(value) = value else {
                let name = match kind { Kind::Bool => "bool", Kind::Int => "int", Kind::Float => "float", Kind::Str => "str" };
                return Err(format!("[{}] {} = {} is not a valid {}", section, key, py_repr(text), name));
            };
            if let Some((_, (lo, hi))) = RANGES.iter().find(|((s, k), _)| s == section && k == key) {
                if !(*lo <= value && value <= *hi) {
                    let shown = if *kind == Kind::Int { format!("{}", value as i64) } else { py_float_repr(value) };
                    return Err(format!("[{}] {} = {} is out of range ({} to {})", section, key, shown, py_bound(*lo), py_bound(*hi)));
                }
            }
        }
    }
    Ok(())
}

/// A RANGES bound as platmon.RANGES writes it: whole numbers as ints (3600), the rest as floats (0.1).
fn py_bound(b: f64) -> String {
    if b.fract() == 0.0 { format!("{}", b as i64) } else { py_float_repr(b) }
}

/// platmon.levels: {name: [warning, critical]}; temperature empty is the board's default for platform.
fn levels(ini: &Ini, platform: Option<&str>) -> Result<Value, String> {
    let mut out = Value::obj();
    for (name, default, _) in DEFAULTS.iter().find(|(s, _)| *s == "thresholds").unwrap().1.iter() {
        let text = ini.get("thresholds", name).unwrap_or_default().trim_matches(crate::pystr::py_space);
        if *name == "temperature" && text.is_empty() {
            out.set(name, platform.map_or(Value::Null, |p| {
                let pair = match p { "Jetson Orin" => [85.0, 95.0], "Raspberry Pi" => [75.0, 80.0], _ => [80.0, 90.0] };
                Value::Arr(pair.iter().map(|x| Value::Float(*x)).collect())
            }));
            continue;
        }
        let parts: Vec<&str> = text.split(',').collect();
        let pair: Option<Vec<f64>> = (parts.len() == 2).then(|| parts.iter().map(|p| py_float(p)).collect()).flatten();
        let low = LOW_LEVELS.iter().find(|(n, _)| n == name).map(|(_, l)| *l);
        let Some(pair) = pair else {
            let example = if low.is_some() { *default } else { "80, 90" };
            return Err(format!("[thresholds] {} = {}: give two numbers, warning then critical, e.g. {}", name, py_repr(text), example));
        };
        let (warn, crit) = (pair[0], pair[1]);
        if let Some((floor, ceiling, unit)) = low {
            if !(floor < crit && crit < warn && warn < ceiling) {
                return Err(format!("[thresholds] {} = {}: a low reading is the bad one, so need {}{} > warning > critical > {}{}",
                                   name, py_repr(text), g(ceiling), unit, g(floor), unit));
            }
        } else {
            let top = if *name == "temperature" { 150.0 } else { 100.0 };
            if !(0.0 < warn && warn < crit && crit <= top) {
                return Err(format!("[thresholds] {} = {}: need 0 < warning < critical <= {}", name, py_repr(text), top as i64));
            }
        }
        out.set(name, Value::Arr(vec![warn.into(), crit.into()]));
    }
    Ok(out)
}

/// probe.parse_targets: [(address, port)] of IP literals.
pub fn parse_targets(text: &str) -> Result<Vec<(String, u16)>, String> {
    let mut out: Vec<(String, u16)> = Vec::new();
    for item in text.split(',').map(|t| t.trim_matches(crate::pystr::py_space)).filter(|t| !t.is_empty()) {
        let (host, port) = item.rsplit_once(':').unwrap_or(("", ""));
        let port_ok = !port.is_empty() && port.bytes().all(|b| b.is_ascii_digit()) && port.parse::<u32>().is_ok_and(|p| 0 < p && p < 65536);
        if !item.contains(':') || !port_ok {
            return Err(format!("target {}: expected address:port", py_repr(item)));
        }
        let bracketed = host.starts_with('[') && host.ends_with(']') && host.len() >= 2;
        if host.contains(':') && !bracketed {
            return Err(format!("target {}: write an IPv6 address as [address]:port", py_repr(item)));
        }
        let literal = if bracketed { &host[1..host.len() - 1] } else { host };
        let addr: std::net::IpAddr = literal.parse()
            .map_err(|_| format!("{} does not appear to be an IPv4 or IPv6 address", py_repr(literal)))?;
        out.push((addr.to_string(), port.parse().unwrap()));
    }
    if out.len() > 4 {
        return Err("at most 4 targets".into());
    }
    if (1..out.len()).any(|i| out[..i].contains(&out[i])) {
        return Err("a target is listed twice".into());
    }
    Ok(out)
}

/// platmon.py's --frontends value as it passes it on: None when not given or empty, else split at commas.
pub fn frontends(value: Option<&str>) -> Option<Vec<&str>> {
    value.filter(|v| !v.is_empty()).map(|v| v.split(',').collect())
}

/// platmon.load_config(path, frontends): defaults < the file < frontends (enables exactly those). Err with
/// the message platmon.py gives.
pub fn load(path: Option<&str>, frontends: Option<&[&str]>) -> Result<Config, String> {
    let mut ini = Ini::with_defaults();
    if let Some(path) = path {
        let text = std::fs::read(path).map_err(|_| format!("cannot read config {}", path))?;
        let text = String::from_utf8(text).map_err(|_| format!("cannot read config {}", path))?;
        ini.read(&text, path).map_err(|e| format!("cannot parse config {}: {}", path, e))?;
    }
    check(&ini)?;
    levels(&ini, None)?;
    let targets = parse_targets(ini.get("probe", "targets").unwrap_or_default()).map_err(|e| format!("[probe] targets: {}", e))?;
    let cfg = Config { ini, targets };
    if !cfg.targets.is_empty() && cfg.targets.len() as f64 * cfg.float("probe", "timeout") >= cfg.float("probe", "interval") {
        return Err("[probe] interval must be longer than timeout × number of targets (they are probed one after another)".into());
    }
    let mut cfg = cfg;
    if let Some(names) = frontends {
        let mut unknown: Vec<&str> = names.iter().copied().filter(|n| !FRONTENDS.contains(n)).collect();
        unknown.sort();
        unknown.dedup();
        if !unknown.is_empty() {
            return Err(format!("unknown frontend(s): {}; choose from {}", unknown.join(", "), FRONTENDS.join(", ")));
        }
        for f in FRONTENDS {
            cfg.ini.set(f, "enabled", if names.contains(f) { "yes" } else { "no" });
        }
    }
    if !FRONTENDS.iter().any(|f| cfg.flag(f, "enabled")) {
        return Err("nothing to run: every output is disabled; set enabled = yes in [http] (or pass --frontends)".into());
    }
    Ok(cfg)
}

/// levels for the running service: platform's temperature default filled in.
pub fn levels_for(cfg: &Config, platform: &str) -> Value {
    levels(&cfg.ini, Some(platform)).unwrap_or_default()
}

// tests/collector/test_probe.py's target and config tests, ported (docs/development/python-test-port.md);
// config_fixture.py compares the rest of the parsing with the Python service's answers.
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_targets_like_python() {
        assert_eq!(parse_targets("192.0.2.1:443, [2001:db8::1]:22 ,").unwrap(),
                   [("192.0.2.1".to_string(), 443), ("2001:db8::1".to_string(), 22)]);
        assert!(parse_targets("").unwrap().is_empty());
        let five = (0..5).map(|n| format!("192.0.2.{}:1", n)).collect::<Vec<_>>().join(",");
        for text in ["example.com:80", "192.0.2.1", "192.0.2.1:0", "192.0.2.1:65536", "192.0.2.1:http", "2001:db8::1:22",
                     "192.0.2.1:1,192.0.2.1:1", &five] {
            assert!(parse_targets(text).is_err(), "{}", text); // names are never resolved; at most 4; no duplicates
        }
    }

    #[test]
    fn probe_config_refusals_and_defaults() {
        let dir = std::env::temp_dir().join(format!("platmon-native-config-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        for (i, (text, error)) in [("[probe]\ntargets = host.example:80\n", "targets"),
                                   ("[probe]\nenabled = yes\ntargets = 192.0.2.1:1, 192.0.2.2:1\ntimeout = 5\ninterval = 10\n", "interval must be longer"),
                                   ("[probe]\ntimeout = 20\n", "out of range")].iter().enumerate() {
            let path = dir.join(format!("{}.ini", i));
            std::fs::write(&path, text).unwrap();
            let e = load(Some(path.to_str().unwrap()), None).err().unwrap();
            assert!(e.contains(error), "{}: {}", text, e);
        }
        let _ = std::fs::remove_dir_all(&dir);
        let cfg = load(None, None).ok().unwrap();
        assert!(cfg.flag("probe", "enabled") && cfg.text("probe", "targets").is_empty()); // on by default, without targets
    }

    #[test]
    fn battery_section() {
        // tests/collector/test_battery.py test_config_section
        let cfg = load(None, None).ok().unwrap();
        assert!(cfg.flag("battery", "enabled") && cfg.float("battery", "interval") == 60.0);
        let path = std::env::temp_dir().join(format!("platmon-native-battery-{}.ini", std::process::id()));
        std::fs::write(&path, "[battery]\ninterval = 5\n").unwrap();
        let e = load(Some(path.to_str().unwrap()), None).err().unwrap();
        assert!(e.contains("[battery] interval = 5.0 is out of range (10 to 3600)"), "{}", e);
        std::fs::write(&path, "[battery]\nenabled = no\n").unwrap();
        assert!(!load(Some(path.to_str().unwrap()), None).ok().unwrap().flag("battery", "enabled"));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn wifi_section() {
        // tests/collector/test_wifi.py test_wifi_is_on_by_default_with_a_5_s_candidate_interval
        let cfg = load(None, None).ok().unwrap();
        assert!(cfg.flag("wifi", "enabled") && cfg.float("wifi", "interval") == 5.0);
        let path = std::env::temp_dir().join(format!("platmon-native-wifi-{}.ini", std::process::id()));
        std::fs::write(&path, "[wifi]\ninterval = 0.5\n").unwrap();
        assert!(load(Some(path.to_str().unwrap()), None).err().unwrap().contains("out of range"));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn storage_section() {
        // tests/collector/test_storage.py test_storage_is_on_by_default_and_its_interval_is_checked
        let cfg = load(None, None).ok().unwrap();
        assert!(cfg.flag("storage", "enabled") && cfg.float("storage", "interval") == 30.0);
        let path = std::env::temp_dir().join(format!("platmon-native-storage-{}.ini", std::process::id()));
        for (text, error) in [("[storage]\ninterval = 1\n", "out of range"), ("[storage]\nenabled = sometimes\n", "valid bool")] {
            std::fs::write(&path, text).unwrap();
            let e = load(Some(path.to_str().unwrap()), None).err().unwrap();
            assert!(e.contains(error), "{}: {}", text, e);
        }
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn network_section() {
        // tests/collector/test_network_integration.py: on by default; a bad value and an unknown key refused
        assert!(load(None, None).ok().unwrap().flag("network", "enabled"));
        let path = std::env::temp_dir().join(format!("platmon-native-network-{}.ini", std::process::id()));
        for (text, error) in [("[network]\nenabled = maybe\n", "is not a valid bool"), ("[network]\ninterval = 5\n", "unknown key")] {
            std::fs::write(&path, text).unwrap();
            let e = load(Some(path.to_str().unwrap()), None).err().unwrap();
            assert!(e.contains(error), "{}: {}", text, e);
        }
        let _ = std::fs::remove_file(&path);
    }

    // tests/runtime/test_platmon.py, ported: the configuration platmon.py read, as config-check and serve read it.
    fn ini(text: &str) -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static N: AtomicU32 = AtomicU32::new(0);
        let path = std::env::temp_dir().join(format!("platmon-native-cfg-{}-{}.ini", std::process::id(), N.fetch_add(1, Ordering::Relaxed)));
        std::fs::write(&path, text).unwrap();
        path
    }

    fn load_text(text: &str, frontends: Option<&[&str]>) -> Result<Config, String> {
        let path = ini(text);
        let out = load(Some(path.to_str().unwrap()), frontends);
        let _ = std::fs::remove_file(&path);
        out
    }

    const FEATURES: [&str; 7] = ["network", "disk_io", "storage", "wifi", "pressure", "probe", "history"];

    #[test]
    fn defaults() {
        let cfg = load(None, None).ok().unwrap();
        assert!(cfg.flag("http", "enabled") && cfg.int("http", "port") == 9797 && cfg.flag("http", "web"));
        assert_eq!(cfg.float("core", "interval"), 1.0);
        assert!(FEATURES.iter().all(|f| cfg.flag(f, "enabled")));
    }

    #[test]
    fn explicit_disabled_features_override_defaults() {
        let cfg = load_text(&FEATURES.iter().map(|f| format!("[{}]\nenabled = no\n", f)).collect::<String>(), None).ok().unwrap();
        assert!(FEATURES.iter().all(|f| !cfg.flag(f, "enabled")));
    }

    #[test]
    fn file_overrides_defaults() {
        let cfg = load_text("[http]\nport = 9000\nweb = no\n", None).ok().unwrap();
        assert!(cfg.int("http", "port") == 9000 && !cfg.flag("http", "web") && cfg.flag("http", "enabled"));
        assert_eq!(cfg.text("http", "bind"), "0.0.0.0"); // untouched keys keep their default
    }

    #[test]
    fn frontends_flag_enables_exactly_those() {
        assert!(load_text("[http]\nenabled = no\n", Some(&["http"])).ok().unwrap().flag("http", "enabled"));
    }

    #[test]
    fn rejects_bad_config() {
        let cases: &[(&str, Option<&[&str]>, Option<&str>)] = &[
            ("[htpp]\nport = 1\n", None, Some("unknown config section")), // typos must not be silently ignored
            ("[http]\nprot = 9000\n", None, Some("[http] unknown key(s): prot")),
            ("", Some(&["web"]), Some("unknown frontend")),
            ("", Some(&["terminal"]), Some("unknown frontend")),
            ("[terminal]\nenabled = no\n", None, Some("[terminal] is no longer supported: ")), // says to use the platmon command
            ("[http]\nport = abc\n", None, Some("port = 'abc' is not a valid int")),
            ("[http]\nenabled = maybe\n", None, Some("is not a valid bool")),
            ("[http]\nport = 70000\n", None, Some("out of range")),
            ("[core]\ninterval = 0\n", None, Some("out of range")), // 0 would make the sampler spin
            ("[http]\nenabled = no\n", None, Some("nothing to run: every output is disabled; set enabled = yes in [http]")),
            ("", Some(&[]), Some("nothing to run")), // --frontends with no name
            ("[core]\ninterval = -1\n", None, Some("out of range")),
            ("port = 1\n", None, Some("cannot parse config")), // no section header
            ("[http]\nbind = 10%\n", Some(&["http"]), None), // % is literal, not interpolation
            ("[thresholds]\ncpu = 80\n", None, Some("[thresholds] cpu = '80': give two numbers")),
            ("[thresholds]\ncpu = 80, 90, 95\n", None, Some("give two numbers")),
            ("[thresholds]\nmemory = high, 90\n", None, Some("give two numbers")),
            ("[thresholds]\ngpu = 90, 80\n", None, Some("need 0 < warning < critical <= 100")),
            ("[thresholds]\nfilesystem = 80, 101\n", None, Some("<= 100")),
            ("[thresholds]\ncpu = nan, 90\n", None, Some("need 0 < warning")),
            ("[thresholds]\ntemperature = 0, 90\n", None, Some("<= 150")),
            ("[thresholds]\ntemperature = 85, 151\n", None, Some("<= 150")),
            ("[thresholds]\nswap = 80, 90\n", None, Some("[thresholds] unknown key(s): swap")),
            ("[thresholds]\ntemperature = 70.5, 82\n", None, None),
            // battery and Wi-Fi: the low reading is the bad one, so warning above critical
            ("[thresholds]\nbattery = 10, 20\n", None, Some("battery = '10, 20': a low reading is the bad one, so need 100% > warning > critical > 0%")),
            ("[thresholds]\nbattery = 20, 0\n", None, Some("> 0%")),
            ("[thresholds]\nbattery = 100, 10\n", None, Some("100% > warning")),
            ("[thresholds]\nbattery = 20\n", None, Some("give two numbers, warning then critical, e.g. 20, 10")),
            ("[thresholds]\nwifi = -80, -70\n", None, Some("need 0 dBm > warning > critical > -120 dBm")),
            ("[thresholds]\nwifi = -70, -121\n", None, Some("> -120 dBm")),
            ("[thresholds]\nwifi = 5, -80\n", None, Some("0 dBm > warning")),
            ("[thresholds]\nwifi = nan, -80\n", None, Some("need 0 dBm")),
            ("[thresholds]\nbattery = 30.5, 5\nwifi = -65, -85\n", None, None)];
        for (text, frontends, error) in cases {
            match (load_text(text, *frontends), error) {
                (Ok(_), None) => {}
                (Err(e), Some(want)) => assert!(e.contains(want), "{:?}: {}", text, e),
                (Ok(_), Some(want)) => panic!("{:?} loaded, expected {}", text, want),
                (Err(e), None) => panic!("{:?}: {}", text, e),
            }
        }
    }

    #[test]
    fn levels_and_the_board_temperature() {
        // defaults: percent levels, no temperature (the board's is filled in); the file overrides them
        assert_eq!(load(None, None).ok().unwrap().value(None).get("levels").unwrap().to_string(false),
                   r#"{"cpu":[80.0,90.0],"gpu":[80.0,90.0],"memory":[80.0,90.0],"filesystem":[80.0,90.0],"temperature":null,"battery":[20.0,10.0],"wifi":[-70.0,-80.0]}"#);
        let cfg = load_text("[thresholds]\nmemory = 70, 85\ntemperature = 70.5, 82\n", None).ok().unwrap();
        let l = cfg.value(None).get("levels").unwrap().clone();
        assert_eq!([l.get("memory"), l.get("temperature"), l.get("cpu")].map(|v| v.unwrap().to_string(false)), ["[70.0,85.0]", "[70.5,82.0]", "[80.0,90.0]"]);
        for (platform, expected) in [("Jetson Orin", "[85.0,95.0]"), ("Raspberry Pi", "[75.0,80.0]"), ("Jetson Xavier", "[80.0,90.0]"), ("PC", "[80.0,90.0]"), ("Linux", "[80.0,90.0]")] {
            assert_eq!(levels_for(&load(None, None).ok().unwrap(), platform).get("temperature").unwrap().to_string(false), expected);
            let set = load_text("[thresholds]\ntemperature = 60, 70\n", None).ok().unwrap(); // a configured one wins
            assert_eq!(levels_for(&set, platform).get("temperature").unwrap().to_string(false), "[60.0,70.0]");
        }
    }

    #[test]
    fn missing_file() {
        assert!(load(Some("/nonexistent/platmon.ini"), None).err().unwrap().contains("cannot read"));
    }

    #[test]
    fn replay_config_bounds() {
        for text in ["[replay]\nretention = 0\n", "[replay]\nretention = nan\n", "[replay]\nretention = 3601\n", "[replay]\nmax_bytes = 1048575\n",
                     "[replay]\nmax_bytes = 67108865\n", "[replay]\nmax_bytes = 1.5\n", "[replay]\nenabled = maybe\n"] {
            assert!(load_text(text, None).is_err(), "{:?}", text);
        }
    }
}
