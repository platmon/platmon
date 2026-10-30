//! Replay: complete publications kept for a while, so a consumer can fetch them after its own
//! cursor. Each publication is serialized once by its publisher, outside the replay lock; a request takes
//! references to the payloads under the lock and decodes them outside it. Ages and `stale` are computed
//! per answer. Contract: docs/reference/replay.md.
use crate::json::{self, Value};
use crate::runtime::{clock_value, ms, seconds_ns};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

pub const MAX_RECORD_BYTES: usize = 1024 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_RECORDS: usize = 4096;
pub const MAX_LIMIT: i64 = 100;

/// Why a request is refused: a 409 (cursor), a 400 (value) or a 404 (no such stream).
#[derive(Debug, PartialEq)]
pub enum Refused {
    Cursor(&'static str),
    Invalid(String),
    NoStream,
}

struct Entry {
    stream: String,
    id: i64,
    started: i64,
    completed: i64,
    payload: Arc<str>,
    gap_before: bool,
}

#[derive(Default)]
struct Inner {
    records: VecDeque<Entry>, // in insertion order
    latest: Vec<(String, i64)>,
    bytes: usize,
}

pub struct Replay {
    pub instance_id: String,
    clock_source: &'static str,
    pub retention: f64,
    retention_ns: i64,
    pub max_bytes: usize,
    inner: Mutex<Inner>,
}

impl Replay {
    pub fn new(instance_id: String, clock_source: &'static str, groups: &[&str], retention: f64, max_bytes: usize) -> Replay {
        let mut inner = Inner::default();
        for s in std::iter::once("core").chain(groups.iter().copied()) {
            if !inner.latest.iter().any(|(n, _)| n == s) {
                inner.latest.push((s.into(), 0));
            }
        }
        Replay { instance_id, clock_source, retention, retention_ns: seconds_ns(retention), max_bytes, inner: Mutex::new(inner) }
    }

    /// Replay.append: data is the publication as of its completion; serialized here, before the lock. now:
    /// the clock, read under the lock to expire old records.
    pub fn append(&self, stream: &str, id: i64, started: i64, completed: i64, data: &Value, gap_before: bool,
                  now: impl FnOnce() -> i64) -> Result<(), String> {
        if !data.finite() { // json.dumps(allow_nan=False): never stored, but the id is known
            let mut r = self.inner.lock().unwrap();
            if let Some(l) = r.latest.iter_mut().find(|(n, _)| n == stream) {
                l.1 = l.1.max(id);
            }
            return Err("Out of range float values are not JSON compliant".into());
        }
        let payload: Arc<str> = data.to_py_compact().into();
        let mut r = self.inner.lock().unwrap();
        let Some(l) = r.latest.iter_mut().find(|(n, _)| n == stream) else { return Ok(()) };
        if id <= l.1 {
            return Ok(());
        }
        l.1 = id;
        self.prune(&mut r, now());
        if payload.len() > MAX_RECORD_BYTES.min(self.max_bytes) {
            return Ok(()); // latest still advances: clients see an explicit missing range
        }
        r.bytes += payload.len();
        r.records.push_back(Entry { stream: stream.into(), id, started, completed, payload, gap_before });
        while r.bytes > self.max_bytes || r.records.len() > MAX_RECORDS {
            let e = r.records.pop_front().unwrap();
            r.bytes -= e.payload.len();
        }
        Ok(())
    }

    fn prune(&self, r: &mut Inner, now: i64) {
        // publishers are independent: insertion order need not be time order
        let cutoff = now - self.retention_ns;
        let mut freed = 0;
        r.records.retain(|e| {
            let keep = e.started >= cutoff;
            if !keep { freed += e.payload.len(); }
            keep
        });
        r.bytes -= freed;
    }

    /// Replay.view: the page after `after` for one stream, as the JSON body /api/samples answers with.
    pub fn view(&self, stream: &str, instance_id: Option<&str>, after: i64, limit: i64, now: impl FnOnce() -> i64)
                -> Result<String, Refused> {
        if after < 0 || !(1..=MAX_LIMIT).contains(&limit) {
            return Err(Refused::Invalid("invalid cursor or limit".into()));
        }
        let (now, latest, oldest, selected, more, cursor) = {
            let mut r = self.inner.lock().unwrap();
            let Some(&(_, latest)) = r.latest.iter().find(|(n, _)| n == stream) else { return Err(Refused::NoStream) };
            let now = now();
            self.prune(&mut r, now);
            if instance_id.is_some_and(|i| i != self.instance_id) {
                return Err(Refused::Cursor("instance_changed"));
            }
            if after > latest {
                return Err(Refused::Cursor("cursor_ahead"));
            }
            let available: Vec<&Entry> = r.records.iter().filter(|e| e.stream == stream).collect();
            let oldest = available.first().map(|e| e.id);
            let pending: Vec<&Entry> = available.into_iter().filter(|e| e.id > after).collect();
            let (mut selected, mut size) = (Vec::new(), 4096); // the envelope and per-record overhead
            for e in &pending {
                if selected.len() as i64 >= limit || size + e.payload.len() + 1024 > MAX_RESPONSE_BYTES {
                    break;
                }
                selected.push((e.id, e.completed, e.payload.clone(), e.gap_before));
                size += e.payload.len() + 1024;
            }
            let more = selected.len() < pending.len();
            let cursor = if more { selected.last().unwrap().0 } else { latest };
            (now, latest, oldest, selected, more, cursor)
        };
        let (mut missing, mut expected, mut records) = (Vec::new(), after + 1, Vec::new());
        let gap = |from: i64, to: i64| {
            let mut m = Value::obj();
            m.set("from", from.into());
            m.set("to", to.into());
            m.set("reason", "buffer_unavailable".into());
            m
        };
        for (id, completed, payload, gap_before) in selected {
            if id > expected {
                missing.push(gap(expected, id - 1));
            }
            expected = id + 1;
            let mut data = json::parse(&payload).map_err(Refused::Invalid)?;
            let elapsed = ms(now - completed);
            let core = stream == "core";
            let limit = if core { None } else { data.get("stale_after_ms").cloned() };
            let timing = data.get_mut(if core { "sample" } else { "observation" }).ok_or(Refused::NoStream)?;
            for key in ["age_ms", "cycle_age_ms", "data_age_ms"] {
                let v = match timing.get(key) { Some(Value::Float(v)) => Some(*v), Some(Value::Int(i)) => Some(*i as f64), _ => None };
                if let Some(v) = v {
                    timing.set(key, json::round_to(v + elapsed, 3).into());
                }
            }
            let limit = limit.or_else(|| timing.get("stale_after_ms").cloned());
            let num = |v: Option<&Value>| match v { Some(Value::Float(f)) => *f, Some(Value::Int(i)) => *i as f64, _ => f64::NAN };
            let stale = num(timing.get("data_age_ms")) > num(limit.as_ref());
            timing.set("stale", Value::Bool(stale));
            if !core {
                if stale {
                    data.set("state", "stale".into());
                }
                data.set("collecting_for_ms", Value::Null);
            }
            let mut rec = Value::obj();
            rec.set("id", id.into());
            rec.set("gap_before", Value::Bool(gap_before));
            rec.set("data", data);
            records.push(rec);
        }
        if expected <= cursor {
            missing.push(gap(expected, cursor));
        }
        let mut body = Value::obj();
        body.set("schema_version", 1i64.into());
        body.set("instance_id", self.instance_id.as_str().into());
        body.set("clock", clock_value(self.clock_source));
        body.set("stream", stream.into());
        body.set("data_schema_version", (if stream == "core" { 1i64 } else { 2 }).into());
        body.set("historical", Value::Bool(true));
        body.set("retention_s", self.retention.into());
        body.set("max_bytes", (self.max_bytes as i64).into());
        body.set("max_records", (MAX_RECORDS as i64).into());
        body.set("max_record_bytes", (MAX_RECORD_BYTES as i64).into());
        body.set("max_response_bytes", (MAX_RESPONSE_BYTES as i64).into());
        body.set("oldest_id", oldest.into());
        body.set("latest_id", latest.into());
        body.set("next_after", cursor.into());
        body.set("has_more", Value::Bool(more));
        body.set("missing_ranges", Value::Arr(missing));
        body.set("records", Value::Arr(records));
        Ok(body.to_py_compact())
    }
}

// tests/runtime/test_replay.py, ported where runtime_fixture.py's replay scenario does not show it
// (docs/development/python-test-port.md).
#[cfg(test)]
mod tests {
    use super::*;

    const S: i64 = 1_000_000_000;

    fn core(n: i64, pad: usize) -> Value {
        json::parse(&format!(r#"{{"n": {}, "pad": "{}", "sample": {{"sequence": {}, "age_ms": 0.0, "data_age_ms": 0.0, "stale_after_ms": 5000.0}}}}"#,
                             n, "x".repeat(pad), n)).unwrap()
    }

    fn page(r: &Replay, stream: &str, after: i64, limit: i64) -> Value {
        json::parse(&r.view(stream, None, after, limit, || 10 * S).unwrap()).unwrap()
    }

    fn ids(p: &Value) -> Vec<i64> {
        match p.get("records") { Some(Value::Arr(r)) => r.iter().map(|x| match x.get("id") { Some(Value::Int(i)) => *i, _ => -1 }).collect(), _ => vec![] }
    }

    #[test]
    fn oversized_middle_and_tail_are_reported_and_the_cursor_progresses() {
        let r = Replay::new("i".into(), "boottime", &[], 600.0, 2000);
        for (n, pad) in [(1, 10), (2, 3000), (3, 10), (4, 3000)] { // 2 and 4 are larger than max_bytes: not kept
            r.append("core", n, n * S, n * S, &core(n, pad), false, || n * S).unwrap();
        }
        let first = page(&r, "core", 0, 1);
        assert_eq!((ids(&first), first.get("next_after").unwrap().to_string(false), first.get("has_more").unwrap().to_string(false)),
                   (vec![1], "1".into(), "true".into()));
        let last = page(&r, "core", 1, 100);
        assert_eq!((ids(&last), last.get("next_after").unwrap().to_string(false), last.get("has_more").unwrap().to_string(false)),
                   (vec![3], "4".into(), "false".into()));
        assert_eq!(last.get("missing_ranges").unwrap().to_string(false),
                   r#"[{"from":2,"to":2,"reason":"buffer_unavailable"},{"from":4,"to":4,"reason":"buffer_unavailable"}]"#);
    }

    #[test]
    fn the_response_byte_budget_paginates_before_the_count_limit() {
        let r = Replay::new("i".into(), "boottime", &[], 600.0, 64 * 1024 * 1024);
        for n in 1..5 {
            r.append("core", n, n * S, n * S, &core(n, 600_000), false, || n * S).unwrap();
        }
        let body = r.view("core", None, 0, 100, || 10 * S).unwrap();
        let first = json::parse(&body).unwrap();
        assert_eq!((ids(&first), first.get("has_more").unwrap().to_string(false)), (vec![1, 2, 3], "true".into()));
        assert!(body.len() < MAX_RESPONSE_BYTES);
    }

    #[test]
    fn the_record_cap_holds() {
        let r = Replay::new("i".into(), "boottime", &["wifi"], 3600.0, 64 * 1024 * 1024);
        let obs = json::parse(r#"{"observation": {"id": 1, "data_age_ms": 0.0, "stale_after_ms": 15000.0}, "data": null}"#).unwrap();
        r.append("wifi", 1, 0, 0, &obs, false, || 0).unwrap();
        for n in 1..=(MAX_RECORDS as i64 + 3) {
            r.append("core", n, n, n, &core(n, 0), false, || n).unwrap();
        }
        assert!(r.inner.lock().unwrap().records.len() <= MAX_RECORDS);
        let p = json::parse(&r.view("core", None, 0, 1, || MAX_RECORDS as i64 + 3).unwrap()).unwrap();
        assert_ne!(p.get("missing_ranges").unwrap().to_string(false), "[]"); // the oldest went first
    }

    #[test]
    fn a_serialization_failure_records_its_loss() {
        let r = Replay::new("i".into(), "boottime", &[], 600.0, 1024 * 1024);
        let mut nan = core(1, 0);
        nan.set("value", Value::Float(f64::NAN));
        assert!(r.append("core", 1, S, S, &nan, false, || S).is_err()); // not stored, but its id is known
        let p = page(&r, "core", 0, 100);
        assert_eq!((p.get("latest_id").unwrap().to_string(false), p.get("missing_ranges").unwrap().to_string(false)),
                   ("1".into(), r#"[{"from":1,"to":1,"reason":"buffer_unavailable"}]"#.into()));
    }

    #[test]
    fn an_observation_failure_is_archived() {
        let r = Replay::new("i".into(), "boottime", &["battery"], 600.0, 1024 * 1024);
        let failed = json::parse(r#"{"state": "error", "observation": {"id": 1, "data_age_ms": 0.0, "stale_after_ms": 180000.0},
            "collector": {"state": "error", "reason": "internal_error"}, "data": null}"#).unwrap();
        r.append("battery", 1, S, S, &failed, false, || S).unwrap();
        let entry = page(&r, "battery", 0, 100).get("records").unwrap().get_index(0).unwrap().get("data").unwrap().clone();
        assert!(entry.get("data").unwrap().is_null());
        assert_eq!(entry.get("collector").unwrap().get("state").unwrap().to_string(false), r#""error""#);
    }
}
