//! runtime/replay.py: complete publications kept for a while, so a consumer can fetch them after its own
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
    records: VecDeque<Entry>, // in insertion order, as Python's OrderedDict
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
