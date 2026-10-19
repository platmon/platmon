//! A JSON value with insertion-ordered objects, written the way Python's json module writes it:
//! floats always carry a fraction or exponent (45.0, not 45), so ints and floats stay distinct.
//! Two styles: compact with UTF-8 (the collector's harness output, source descriptors) and json.dumps'
//! defaults (", " and ": ", ensure_ascii), which the service's HTTP answers use. parse reads JSON text.
use std::fmt::Write;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    /// an unsigned counter, or a product of one, beyond i64 (Python's ints have no limit)
    Big(u128),
    Float(f64),
    Str(String),
    Arr(Vec<Value>),
    Obj(Vec<(String, Value)>),
}

impl Value {
    pub fn obj() -> Value {
        Value::Obj(Vec::new())
    }

    /// Sets key, keeping its position if it is already there (as a Python dict does).
    pub fn set(&mut self, key: &str, value: Value) {
        if let Value::Obj(items) = self {
            match items.iter_mut().find(|(k, _)| k == key) {
                Some(slot) => slot.1 = value,
                None => items.push((key.to_string(), value)),
            }
        }
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Obj(items) => items.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn get_mut(&mut self, key: &str) -> Option<&mut Value> {
        match self {
            Value::Obj(items) => items.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn get_index(&self, i: usize) -> Option<&Value> {
        match self {
            Value::Arr(items) => items.get(i),
            _ => None,
        }
    }

    pub fn contains(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }

    pub fn write(&self, out: &mut String, sort_keys: bool) {
        self.write_styled(out, sort_keys, false, false)
    }

    /// spaced: json.dumps' default separators (", ", ": "), else compact; ascii: its ensure_ascii.
    pub fn write_styled(&self, out: &mut String, sort_keys: bool, spaced: bool, ascii: bool) {
        let (item_sep, key_sep) = if spaced { (", ", ": ") } else { (",", ":") };
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Int(i) => { let _ = write!(out, "{}", i); }
            Value::Big(i) => { let _ = write!(out, "{}", i); }
            Value::Float(f) => write_float(out, *f),
            Value::Str(s) => write_str(out, s, ascii),
            Value::Arr(items) => {
                out.push('[');
                for (i, v) in items.iter().enumerate() {
                    if i > 0 { out.push_str(item_sep); }
                    v.write_styled(out, sort_keys, spaced, ascii);
                }
                out.push(']');
            }
            Value::Obj(items) => {
                let mut refs: Vec<&(String, Value)> = items.iter().collect();
                if sort_keys {
                    refs.sort_by(|a, b| a.0.cmp(&b.0)); // UTF-8 byte order is code point order
                }
                out.push('{');
                for (i, (k, v)) in refs.into_iter().enumerate() {
                    if i > 0 { out.push_str(item_sep); }
                    write_str(out, k, ascii);
                    out.push_str(key_sep);
                    v.write_styled(out, sort_keys, spaced, ascii);
                }
                out.push('}');
            }
        }
    }

    pub fn to_string(&self, sort_keys: bool) -> String {
        let mut s = String::new();
        self.write(&mut s, sort_keys);
        s
    }

    /// json.dumps(self)
    pub fn to_py(&self) -> String {
        let mut s = String::new();
        self.write_styled(&mut s, false, true, true);
        s
    }

    /// json.dumps(self, separators=(",", ":")): compact, non-ASCII escaped
    pub fn to_py_compact(&self) -> String {
        let mut s = String::new();
        self.write_styled(&mut s, false, false, true);
        s
    }

    /// Whether every float in it is finite (json.dumps(allow_nan=False) accepts it).
    pub fn finite(&self) -> bool {
        match self {
            Value::Float(f) => f.is_finite(),
            Value::Arr(items) => items.iter().all(Value::finite),
            Value::Obj(items) => items.iter().all(|(_, v)| v.finite()),
            _ => true,
        }
    }

    /// One `"key": value` member as json.dumps writes it inside an object.
    pub fn write_member(out: &mut String, key: &str, value: &Value) {
        Value::write_key(out, key);
        value.write_styled(out, false, true, true);
    }

    /// `"key": ` as json.dumps writes it before a member's value.
    pub fn write_key(out: &mut String, key: &str) {
        write_str(out, key, true);
        out.push_str(": ");
    }
}

impl Default for Value { fn default() -> Value { Value::Null } }
/// v["key"]: the member, or null when there is none.
impl std::ops::Index<&str> for Value {
    type Output = Value;
    fn index(&self, key: &str) -> &Value {
        static NULL: Value = Value::Null;
        self.get(key).unwrap_or(&NULL)
    }
}
impl From<&str> for Value { fn from(s: &str) -> Value { Value::Str(s.to_string()) } }
impl From<String> for Value { fn from(s: String) -> Value { Value::Str(s) } }
impl From<i64> for Value { fn from(i: i64) -> Value { Value::Int(i) } }
impl From<u64> for Value { fn from(i: u64) -> Value { Value::Big(i as u128) } }
impl From<u128> for Value { fn from(i: u128) -> Value { Value::Big(i) } }
impl From<f64> for Value { fn from(f: f64) -> Value { Value::Float(f) } }
impl<T: Into<Value>> From<Option<T>> for Value {
    fn from(o: Option<T>) -> Value { o.map(Into::into).unwrap_or(Value::Null) }
}

/// Python's repr of a float: the shortest text that reads back as the same double, with ".0" for whole
/// numbers and an exponent from 1e16 and below 1e-4.
fn write_float(out: &mut String, f: f64) {
    if !f.is_finite() {
        out.push_str(if f.is_nan() { "NaN" } else if f > 0.0 { "Infinity" } else { "-Infinity" });
        return;
    }
    let a = f.abs();
    if a != 0.0 && (a >= 1e16 || a < 1e-4) {
        let s = format!("{:e}", f); // 1e-5, 1.5e16
        let (m, e) = s.split_once('e').unwrap();
        let e: i32 = e.parse().unwrap();
        let _ = write!(out, "{}e{}{:02}", m, if e < 0 { '-' } else { '+' }, e.abs());
    } else {
        let s = format!("{}", f);
        out.push_str(&s);
        if !s.contains('.') { out.push_str(".0"); }
    }
}

/// ascii: json.dumps' ensure_ascii, everything outside space..~ as \\uXXXX (pairs beyond the BMP); else only
/// what JSON requires, as with ensure_ascii=False.
fn write_str(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || ascii && !(' '..='~').contains(&c) => {
                let mut units = [0u16; 2];
                for u in c.encode_utf16(&mut units) {
                    let _ = write!(out, "\\u{:04x}", u);
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Python's round(x, digits): correctly rounded from the double's exact value, ties to even.
pub fn round_to(x: f64, digits: usize) -> f64 {
    format!("{:.*}", digits, x).parse().unwrap()
}

/// JSON text as Python's json.loads reads it, for the runtime script: integers stay integers (beyond i64 as
/// Big when positive), objects keep their order. NaN and Infinity are not accepted.
pub fn parse(text: &str) -> Result<Value, String> {
    let mut p = Parser { s: text.as_bytes(), i: 0 };
    let v = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return Err(format!("extra data at {}", p.i));
    }
    Ok(v)
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn err<T>(&self, what: &str) -> Result<T, String> {
        Err(format!("{} at {}", what, self.i))
    }

    fn eat(&mut self, lit: &str) -> bool {
        let ok = self.s[self.i..].starts_with(lit.as_bytes());
        if ok { self.i += lit.len(); }
        ok
    }

    fn value(&mut self) -> Result<Value, String> {
        self.ws();
        match self.s.get(self.i) {
            Some(b'{') => {
                self.i += 1;
                let mut items: Vec<(String, Value)> = Vec::new();
                self.ws();
                if self.eat("}") { return Ok(Value::Obj(items)); }
                loop {
                    self.ws();
                    if self.s.get(self.i) != Some(&b'"') { return self.err("expected a key"); }
                    let k = self.string()?;
                    self.ws();
                    if !self.eat(":") { return self.err("expected ':'"); }
                    let v = self.value()?;
                    match items.iter_mut().find(|(x, _)| *x == k) { // a repeated key: the last value, first place
                        Some(slot) => slot.1 = v,
                        None => items.push((k, v)),
                    }
                    self.ws();
                    if self.eat("}") { return Ok(Value::Obj(items)); }
                    if !self.eat(",") { return self.err("expected ',' or '}'"); }
                }
            }
            Some(b'[') => {
                self.i += 1;
                let mut items = Vec::new();
                self.ws();
                if self.eat("]") { return Ok(Value::Arr(items)); }
                loop {
                    items.push(self.value()?);
                    self.ws();
                    if self.eat("]") { return Ok(Value::Arr(items)); }
                    if !self.eat(",") { return self.err("expected ',' or ']'"); }
                }
            }
            Some(b'"') => Ok(Value::Str(self.string()?)),
            Some(b't') if self.eat("true") => Ok(Value::Bool(true)),
            Some(b'f') if self.eat("false") => Ok(Value::Bool(false)),
            Some(b'n') if self.eat("null") => Ok(Value::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => self.err("expected a value"),
        }
    }

    fn number(&mut self) -> Result<Value, String> {
        let start = self.i;
        let digits = |p: &mut Self| { let a = p.i; while p.i < p.s.len() && p.s[p.i].is_ascii_digit() { p.i += 1; } p.i - a };
        self.eat("-");
        let int_start = self.i;
        let n = digits(self);
        if n == 0 || n > 1 && self.s[int_start] == b'0' { return self.err("bad number"); }
        let mut float = false;
        if self.eat(".") {
            float = true;
            if digits(self) == 0 { return self.err("bad fraction"); }
        }
        if matches!(self.s.get(self.i), Some(b'e' | b'E')) {
            float = true;
            self.i += 1;
            if !self.eat("+") { self.eat("-"); }
            if digits(self) == 0 { return self.err("bad exponent"); }
        }
        let text = std::str::from_utf8(&self.s[start..self.i]).unwrap();
        if float {
            return text.parse().map(Value::Float).map_err(|e| e.to_string());
        }
        match text.parse::<i64>() {
            Ok(i) => Ok(Value::Int(i)),
            Err(_) => text.parse::<u128>().map(Value::Big).map_err(|_| format!("integer out of range: {}", text)),
        }
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let h = self.s.get(self.i..self.i + 4).and_then(|h| std::str::from_utf8(h).ok())
            .and_then(|h| u32::from_str_radix(h, 16).ok());
        self.i += 4;
        h.ok_or_else(|| format!("bad \\u escape at {}", self.i))
    }

    fn string(&mut self) -> Result<String, String> {
        self.i += 1;
        let mut out = String::new();
        loop {
            let start = self.i;
            while self.i < self.s.len() && !matches!(self.s[self.i], b'"' | b'\\') && self.s[self.i] >= 0x20 {
                self.i += 1;
            }
            out.push_str(std::str::from_utf8(&self.s[start..self.i]).map_err(|e| e.to_string())?);
            match self.s.get(self.i) {
                Some(b'"') => { self.i += 1; return Ok(out); }
                Some(b'\\') => {
                    self.i += 1;
                    let c = self.s.get(self.i).copied();
                    self.i += 1;
                    match c {
                        Some(b'"') => out.push('"'),
                        Some(b'\\') => out.push('\\'),
                        Some(b'/') => out.push('/'),
                        Some(b'b') => out.push('\u{8}'),
                        Some(b'f') => out.push('\u{c}'),
                        Some(b'n') => out.push('\n'),
                        Some(b'r') => out.push('\r'),
                        Some(b't') => out.push('\t'),
                        Some(b'u') => {
                            let mut u = self.hex4()?;
                            if (0xd800..0xdc00).contains(&u) && self.s[self.i..].starts_with(b"\\u") {
                                let save = self.i;
                                self.i += 2;
                                let low = self.hex4()?;
                                if (0xdc00..0xe000).contains(&low) {
                                    u = 0x10000 + ((u - 0xd800) << 10) + (low - 0xdc00);
                                } else {
                                    self.i = save;
                                }
                            }
                            // a lone surrogate: Python keeps it; a Rust string cannot
                            out.push(char::from_u32(u).ok_or_else(|| format!("lone surrogate at {}", self.i))?);
                        }
                        _ => return self.err("bad escape"),
                    }
                }
                _ => return self.err("unterminated string"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_read_like_python() {
        let f = |x: f64| Value::Float(x).to_string(false);
        assert_eq!(f(45.0), "45.0");
        assert_eq!(f(45.5), "45.5");
        assert_eq!(f(0.1), "0.1");
        assert_eq!(f(1e-5), "1e-05");
        assert_eq!(f(1e16), "1e+16");
        assert_eq!(f(1234567.25), "1234567.25");
    }

    #[test]
    fn rounding_matches_python() {
        assert_eq!(round_to(2.675, 2), 2.67); // 2.67499999... as a double
        assert_eq!(round_to(0.125, 2), 0.12); // exact tie, to even
        assert_eq!(round_to(0.375, 2), 0.38);
        assert_eq!(round_to(12.25, 1), 12.2);
    }

    #[test]
    fn python_style_and_parse() {
        let v = parse(r#"{"b": [1, -2.5, 1e-05, 18446744073709551616], "a\u00e9\ud83d\ude00": null, "b": true}"#).unwrap();
        assert_eq!(v.to_py(), r#"{"b": true, "a\u00e9\ud83d\ude00": null}"#); // repeated key: last value
        let v = parse(r#"[1, -2.5, 1e-05, 18446744073709551616, "x\u007f\n"]"#).unwrap();
        assert_eq!(v.to_py(), r#"[1, -2.5, 1e-05, 18446744073709551616, "x\u007f\n"]"#);
        assert_eq!(v.to_string(false), "[1,-2.5,1e-05,18446744073709551616,\"x\u{7f}\\n\"]");
        for bad in ["01", "1.", "[1,]", "{\"a\" 1}", "\"\\ud800\"", "NaN", "1 2"] {
            assert!(parse(bad).is_err(), "{}", bad);
        }
    }

    #[test]
    fn sorted_compact_objects() {
        let mut v = Value::obj();
        v.set("v", 1i64.into());
        v.set("provider", "hwmon".into());
        v.set("attr", "temp1_input".into());
        assert_eq!(v.to_string(true), r#"{"attr":"temp1_input","provider":"hwmon","v":1}"#);
    }
}
