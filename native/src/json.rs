//! A JSON value with insertion-ordered objects, written the way Python's json module writes it:
//! floats always carry a fraction or exponent (45.0, not 45), so ints and floats stay distinct.
use std::fmt::Write;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
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

    pub fn contains(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }

    pub fn write(&self, out: &mut String, sort_keys: bool) {
        match self {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::Int(i) => { let _ = write!(out, "{}", i); }
            Value::Float(f) => write_float(out, *f),
            Value::Str(s) => write_str(out, s),
            Value::Arr(items) => {
                out.push('[');
                for (i, v) in items.iter().enumerate() {
                    if i > 0 { out.push(','); }
                    v.write(out, sort_keys);
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
                    if i > 0 { out.push(','); }
                    write_str(out, k);
                    out.push(':');
                    v.write(out, sort_keys);
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
}

impl From<&str> for Value { fn from(s: &str) -> Value { Value::Str(s.to_string()) } }
impl From<String> for Value { fn from(s: String) -> Value { Value::Str(s) } }
impl From<i64> for Value { fn from(i: i64) -> Value { Value::Int(i) } }
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

fn write_str(out: &mut String, s: &str) {
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
            c if (c as u32) < 0x20 => { let _ = write!(out, "\\u{:04x}", c as u32); }
            c => out.push(c), // ensure_ascii=False, as the source descriptors use
        }
    }
    out.push('"');
}

/// Python's round(x, digits): correctly rounded from the double's exact value, ties to even.
pub fn round_to(x: f64, digits: usize) -> f64 {
    format!("{:.*}", digits, x).parse().unwrap()
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
    fn sorted_compact_objects() {
        let mut v = Value::obj();
        v.set("v", 1.into());
        v.set("provider", "hwmon".into());
        v.set("attr", "temp1_input".into());
        assert_eq!(v.to_string(true), r#"{"attr":"temp1_input","provider":"hwmon","v":1}"#);
    }
}
