//! runtime/sampler.py: one writer collects every interval and publishes snapshots; readers (HTTP) only read
//! the latest publication and never collect. A publication is immutable: its stats are serialized once,
//! when it is published, and only what depends on the moment of the answer (ages, state) is written per
//! answer. Contract: docs/reference/api.md (/api/stats, /api/status).
//!
//! The state changes in begin() and finish(), which take the times as arguments, so the runtime script
//! (tools/runtime_fixture.py) can drive the same code as the service's thread (run()).
use crate::json::{self, Value};
use crate::metadata;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

pub const SCHEMA_VERSION: i64 = 1;
const BAD: [&str; 2] = ["partial", "error"]; // optional collector states that make a fresh snapshot degraded
const REQUIRED_READS: [&str; 4] = ["cpu", "memory", "disk", "uptime"]; // record.REQUIRED_READS
const PASSIVE: [(&str, &str); 3] = [("network", "interfaces"), ("disk_io", "disks"), ("pressure", "resources")];

/// sampler.ms: nanoseconds as milliseconds, three decimals.
pub fn ms(ns: i64) -> f64 {
    json::round_to(ns as f64 / 1e6, 3)
}

/// Python's round(seconds * 1e9): whole nanoseconds, ties to even.
pub fn seconds_ns(s: f64) -> i64 {
    (s * 1e9).round_ties_even() as i64
}

/// {"source", "suspend_aware"} of the elapsed clock (pick_clock's description).
pub fn clock_value(source: &str) -> Value {
    let mut v = Value::obj();
    v.set("source", source.into());
    v.set("suspend_aware", Value::Bool(source == "boottime"));
    v
}

/// One published snapshot's data, never changed after it is published (a new one replaces it). Built and
/// serialized before the Sampler's lock is taken; the sequence is given under it (Publication).
pub struct Record {
    pub started_at: f64,
    pub completed_at: f64,
    pub started: i64,
    pub completed: i64,
    /// start of the oldest current read, when every record checked out
    pub oldest_read: Option<i64>,
    unreadable: bool,
    /// the stats' members other than collectors, as json.dumps writes them
    head: String,
    /// "collectors" (core first) and "sensor_meta", the same way
    tail: String,
    /// the same as values, for history and replay: the stats (with collectors as collected), the
    /// snapshot's collectors and sensor_meta
    pub stats: Value,
    collectors: Value,
    pub meta: Value,
}

impl Record {
    fn basis(&self) -> i64 {
        self.oldest_read.unwrap_or(self.started)
    }

    fn basis_name(&self) -> &'static str {
        if self.oldest_read.is_some() { "oldest_current_read_start" } else { "cycle_start_upper_bound" }
    }
}

/// What the Sampler holds and readers take: a record and the sequence it was published as.
#[derive(Clone)]
pub struct Publication {
    pub sequence: i64,
    pub record: Arc<Record>,
}

struct Attempt {
    reason: Option<&'static str>,
    completed_at: f64,
    duration_ns: i64,
    consecutive_failures: i64,
}

#[derive(Default)]
struct State {
    record: Option<Publication>,
    attempt: Option<Attempt>,
    collecting_since: Option<i64>,
    sequence: i64,
    failures: i64,
}

pub struct Sampler {
    pub interval: f64,
    pub instance_id: String,
    pub clock_source: &'static str,
    stale_ns: i64,
    state: Mutex<State>,
    ready: Condvar,
    provenance_problem: Mutex<Option<String>>,
    /// the elapsed clock, for what history and replay are given after a publication
    pub clock: Box<dyn Fn() -> i64 + Send + Sync>,
    pub history: Option<Arc<crate::history::History>>,
    pub replay: Option<Arc<crate::replay::Replay>>,
    /// tests: runs inside the serialization of a record, to show that readers are not held up meanwhile
    #[cfg(test)]
    pub during_build: Mutex<Option<Box<dyn FnMut() + Send>>>,
}

/// What a collection gives the Sampler: Collected.to_value()'s {"stats", "sensors", "required", "passive"}.
pub type Collection = Result<Value, String>;

impl Sampler {
    /// stale_after: 3 intervals, at least 5 s, unless given.
    pub fn new(interval: f64, stale_after: Option<f64>, instance_id: String, clock_source: &'static str) -> Sampler {
        let stale_after = stale_after.unwrap_or((3.0 * interval).max(5.0));
        Sampler { interval, instance_id, clock_source, stale_ns: seconds_ns(stale_after), state: Mutex::default(),
                  ready: Condvar::new(), provenance_problem: Mutex::new(None), clock: Box::new(metadata::clock),
                  history: None, replay: None,
                  #[cfg(test)]
                  during_build: Mutex::new(None) }
    }

    /// The writer's loop (Sampler._run): one attempt, then sleep until an interval after its start.
    pub fn run(&self, mut collect: impl FnMut() -> Collection, stop: &std::sync::atomic::AtomicBool) {
        while !stop.load(std::sync::atomic::Ordering::Relaxed) {
            let (started_at, started) = (metadata::wall(), metadata::clock());
            self.begin(started);
            let result = collect();
            let (completed_at, completed) = (metadata::wall(), metadata::clock());
            self.finish(result, true, started_at, started, completed_at, completed);
            let left = self.interval - (metadata::clock() - started) as f64 / 1e9;
            std::thread::sleep(Duration::from_secs_f64(left.max(0.0)));
        }
    }

    /// A collection starts (collecting_for_ms counts from it).
    pub fn begin(&self, started: i64) {
        self.state.lock().unwrap().collecting_since = Some(started);
    }

    /// The collection that began at started ends: publish it or record the failure, as one update. Checking
    /// the records and serializing happen before the lock, which is only held to count the attempt and
    /// swap the publication; the one it replaces is let go after the lock. same_clock: the read spans are on
    /// this Sampler's clock (always, from the service's collector).
    pub fn finish(&self, result: Collection, same_clock: bool, started_at: f64, started: i64, completed_at: f64,
                  completed: i64) -> Option<Publication> {
        let duration = completed - started;
        let mut reason = None;
        let mut prepared = None;
        match result {
            Err(e) => {
                eprintln!("platmon: collect failed: {}", e);
                reason = Some("collection_failed");
            }
            Ok(_) if duration > self.stale_ns => {
                eprintln!("platmon: collect took {:.1} s, longer than stale_after ({} s); result dropped",
                          duration as f64 / 1e9, self.stale_ns as f64 / 1e9);
                reason = Some("collection_too_slow");
            }
            Ok(col) => {
                let (stats, meta, oldest) = self.prepare(col, same_clock, started, completed);
                prepared = Some(Arc::new(self.build(stats, meta, oldest, started_at, completed_at, started, completed)));
            }
        }
        let mut st = self.state.lock().unwrap();
        st.collecting_since = None;
        let gap_before = st.failures > 0;
        st.failures = if reason.is_none() { 0 } else { st.failures + 1 };
        st.attempt = Some(Attempt { reason, completed_at, duration_ns: duration, consecutive_failures: st.failures });
        let record = prepared?;
        st.sequence += 1;
        let publication = Publication { sequence: st.sequence, record };
        let replaced = st.record.replace(publication.clone());
        self.ready.notify_all();
        drop(st);
        drop(replaced); // its last reference may go here: not under the lock
        // after publishing, outside the lock, as Python's _attempt: replay, then history
        if let Some(replay) = &self.replay {
            let data = self.snapshot_value(&publication, completed);
            if let Err(e) = replay.append("core", publication.sequence, started, completed, &data, gap_before, &self.clock) {
                eprintln!("platmon: replay append failed: {}", e);
            }
        }
        if let Some(history) = &self.history {
            let r = &publication.record;
            history.append(publication.sequence, started, &r.stats, &r.meta, gap_before);
        }
        Some(publication)
    }

    /// The stats (sensor_meta removed), sensor_meta and the oldest read, from a collection's records.
    fn prepare(&self, col: Value, same_clock: bool, started: i64, completed: i64) -> (Value, Value, Option<i64>) {
        let mut parts = match col { Value::Obj(items) => items, _ => vec![] };
        let mut take = |k: &str| parts.iter().position(|(n, _)| n == k).map_or(Value::Null, |i| parts.remove(i).1);
        let (mut stats, sensors, required, passive) = (take("stats"), take("sensors"), take("required"), take("passive"));
        if let Value::Obj(items) = &mut stats {
            items.retain(|(k, _)| k != "sensor_meta"); // the Sampler's to add, from records
        }
        let (meta, oldest, problem) = check(&mut stats, &sensors, &required, &passive, same_clock, started, completed);
        let mut logged = self.provenance_problem.lock().unwrap();
        if problem != *logged { // logged when it changes, not every collection
            if let Some(p) = &problem {
                eprintln!("platmon: data age counts from the collection start: {}", p);
            }
            *logged = problem;
        }
        (stats, meta, oldest)
    }

    /// Waits up to timeout for the first publication (the /api/stats wait after the service starts).
    pub fn wait_ready(&self, timeout: Duration) {
        let st = self.state.lock().unwrap();
        let _ = self.ready.wait_timeout_while(st, timeout, |s| s.record.is_none());
    }

    /// (the /api/stats body, or None when there is no current snapshot; the /api/status body), from one
    /// capture of the state; now() is read under the lock, as Sampler._view does.
    pub fn view(&self, now: impl FnOnce() -> i64) -> (Option<String>, Value) {
        let (now, record, attempt, since) = {
            let st = self.state.lock().unwrap();
            let a = st.attempt.as_ref().map(|a| (a.reason, a.completed_at, a.duration_ns, a.consecutive_failures));
            (now(), st.record.clone(), a, st.collecting_since)
        };
        let fresh = record.as_ref().is_some_and(|p| now - p.record.basis() <= self.stale_ns);
        let state = if fresh {
            if attempt.is_some_and(|a| a.0.is_some()) || record.as_ref().unwrap().record.unreadable { "degraded" } else { "ready" }
        } else if record.is_none() && attempt.is_none() && since.map_or(true, |s| now - s <= self.stale_ns) {
            "starting"
        } else {
            "stale"
        };
        let mut status = Value::obj();
        status.set("schema_version", SCHEMA_VERSION.into());
        status.set("instance_id", self.instance_id.as_str().into());
        status.set("state", state.into());
        status.set("ready", Value::Bool(fresh));
        status.set("sample", record.as_ref().map_or(Value::Null, |p| {
            let r = &p.record;
            let mut s = Value::obj();
            s.set("sequence", p.sequence.into());
            s.set("age_ms", ms(now - r.completed).into());
            s.set("cycle_age_ms", ms(now - r.started).into());
            s.set("data_age_ms", ms(now - r.basis()).into());
            s.set("data_age_basis", r.basis_name().into());
            s
        }));
        status.set("last_attempt", attempt.map_or(Value::Null, |(reason, completed_at, duration, failures)| {
            let mut a = Value::obj();
            a.set("state", if reason.is_some() { "error" } else { "ok" }.into());
            a.set("reason", reason.into());
            a.set("completed_at", completed_at.into());
            a.set("duration_ms", ms(duration).into());
            a.set("consecutive_failures", failures.into());
            a
        }));
        status.set("collecting_for_ms", since.map(|s| ms(now - s)).into());
        status.set("clock", clock_value(self.clock_source));
        (if fresh { record.map(|p| self.snapshot(&p, now)) } else { None }, status)
    }

    /// Sampler._snapshot as JSON: the stored members, then schema_version and sample as of now, then the
    /// stored collectors and sensor_meta.
    pub fn snapshot(&self, p: &Publication, now: i64) -> String {
        let r = &p.record;
        let s = self.sample(p, now);
        let mut out = String::with_capacity(r.head.len() + r.tail.len() + 400);
        out.push('{');
        out.push_str(&r.head);
        if !r.head.is_empty() {
            out.push_str(", ");
        }
        Value::write_member(&mut out, "schema_version", &SCHEMA_VERSION.into());
        out.push_str(", ");
        Value::write_member(&mut out, "sample", &s);
        out.push_str(", ");
        out.push_str(&r.tail);
        out.push('}');
        out
    }

    /// The same snapshot as a value (what replay keeps of it).
    pub fn snapshot_value(&self, p: &Publication, now: i64) -> Value {
        let r = &p.record;
        let mut items: Vec<(String, Value)> = match &r.stats {
            Value::Obj(items) => items.iter().filter(|(k, _)| k != "collectors").cloned().collect(),
            _ => vec![],
        };
        items.push(("schema_version".into(), SCHEMA_VERSION.into()));
        items.push(("sample".into(), self.sample(p, now)));
        items.push(("collectors".into(), r.collectors.clone()));
        items.push(("sensor_meta".into(), r.meta.clone()));
        Value::Obj(items)
    }

    fn sample(&self, p: &Publication, now: i64) -> Value {
        let r = &p.record;
        let mut s = Value::obj();
        s.set("instance_id", self.instance_id.as_str().into());
        s.set("sequence", p.sequence.into());
        s.set("started_at", r.started_at.into());
        s.set("completed_at", r.completed_at.into());
        s.set("duration_ms", ms(r.completed - r.started).into());
        s.set("age_ms", ms(now - r.completed).into());
        s.set("cycle_age_ms", ms(now - r.started).into());
        s.set("data_age_ms", ms(now - r.basis()).into());
        s.set("data_age_basis", r.basis_name().into());
        s.set("interval_ms", ms(seconds_ns(self.interval)).into());
        s.set("stale_after_ms", ms(self.stale_ns).into());
        s
    }
}

impl Sampler {
    /// The record of a collection whose records were checked: its members serialized once.
    #[allow(clippy::too_many_arguments)]
    fn build(&self, stats: Value, meta: Value, oldest: Option<i64>, started_at: f64, completed_at: f64,
             started: i64, completed: i64) -> Record {
        let (head, tail, unreadable, collectors) = serialize(&stats, &meta);
        #[cfg(test)]
        if let Some(hook) = self.during_build.lock().unwrap().as_mut() {
            hook();
        }
        Record { started_at, completed_at, started, completed, oldest_read: oldest, unreadable, head, tail, stats,
                 collectors, meta }
    }
}

/// (head, tail, unreadable) of a record: the stats' members but collectors; collectors (core first) and
/// sensor_meta; whether an optional group of these stats is partial or error.
fn serialize(stats: &Value, meta: &Value) -> (String, String, bool, Value) {
    let mut head = String::new();
    let mut optional = Value::obj();
    if let Value::Obj(items) = stats {
        for (k, v) in items {
            if k == "collectors" {
                if let Value::Obj(groups) = v {
                    optional = Value::Obj(groups.iter().filter(|(g, _)| g != "core").cloned().collect());
                }
                continue;
            }
            if !head.is_empty() {
                head.push_str(", ");
            }
            Value::write_member(&mut head, k, v);
        }
    }
    let unreadable = match &optional {
        Value::Obj(groups) => groups.iter().any(|(_, c)| matches!(c.get("state"), Some(Value::Str(s)) if BAD.contains(&s.as_str()))),
        _ => false,
    };
    // this snapshot's own state when it was made: core is the Sampler's, the rest come from the collection
    let mut collectors = Value::obj();
    let mut core = Value::obj();
    core.set("state", "ok".into());
    core.set("reason", Value::Null);
    collectors.set("core", core);
    if let Value::Obj(groups) = optional {
        for (k, v) in groups {
            collectors.set(&k, v);
        }
    }
    let mut tail = String::new();
    Value::write_member(&mut tail, "collectors", &collectors);
    tail.push_str(", ");
    Value::write_member(&mut tail, "sensor_meta", meta);
    (head, tail, unreadable, collectors)
}

fn is_number(v: &Value) -> bool {
    matches!(v, Value::Int(_) | Value::Big(_) | Value::Float(_))
}

/// Python's truth value of a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Int(i) => *i != 0,
        Value::Big(i) => *i != 0,
        Value::Float(f) => *f != 0.0,
        Value::Str(s) => !s.is_empty(),
        Value::Arr(a) => !a.is_empty(),
        Value::Obj(o) => !o.is_empty(),
    }
}

/// record.sensor_values: (JSON Pointer, value) of every value that needs a read record.
fn sensor_values(stats: &Value) -> Vec<(String, &Value)> {
    let mut out = Vec::new();
    for table in ["temperature", "power"] {
        if let Some(Value::Obj(items)) = stats.get(table) {
            out.extend(items.iter().map(|(k, v)| (metadata::pointer(&[table, k]), v)));
        }
    }
    if let Some(Value::Arr(fans)) = stats.get("fans") {
        for (i, fan) in fans.iter().enumerate() {
            for field in ["rpm", "percent"] {
                if let (Value::Obj(_), Some(v)) = (fan, fan.get(field)) {
                    out.push((metadata::pointer(&["fans", &i.to_string(), field]), v));
                }
            }
        }
    }
    if let Some(Value::Arr(cpus)) = stats.get("cpu") {
        for (i, c) in cpus.iter().enumerate() {
            if let Some(v) = c.get("freq") {
                out.push((metadata::pointer(&["cpu", &i.to_string(), "freq"]), v));
            }
        }
    }
    for kind in ["gpu", "npu"] {
        for field in ["usage", "freq", "max_freq"] {
            if let Some(v) = stats.get(kind).and_then(|g| g.get(field)) {
                out.push((metadata::pointer(&[kind, field]), v));
            }
        }
    }
    out
}

/// Sampler._check: (sensor_meta, start of the oldest current read or None, the first problem). Exactly the
/// REQUIRED_READS, each with a valid span; a record only for a sensor value of these stats; every such value
/// that is a number with a valid read; all on this clock, in whole ns, inside the collection; network,
/// disk_io and pressure with the valid read of their counters (written to their "read", or null).
fn check(stats: &mut Value, sensors: &Value, required: &Value, passive: &Value, same_clock: bool, started: i64,
         completed: i64) -> (Value, Option<i64>, Option<String>) {
    let mut problems: Vec<String> = Vec::new();
    let valid = |span: &Value| match span {
        Value::Arr(a) if a.len() == 2 => match (&a[0], &a[1]) {
            (Value::Int(s), Value::Int(e)) => (started <= *s && s <= e && *e <= completed).then_some((*s, *e)),
            _ => None,
        },
        _ => None,
    };
    let offsets = |(s, e): (i64, i64)| {
        let mut r = Value::obj();
        r.set("started_offset_ms", ms(s - started).into());
        r.set("completed_offset_ms", ms(e - started).into());
        r
    };
    if !same_clock {
        problems.push("read records on another clock".into());
    }
    let empty = Value::obj();
    let required = if matches!(required, Value::Obj(_)) { required } else { &empty };
    let Value::Obj(req) = required else { unreachable!() };
    let names_match = req.len() == REQUIRED_READS.len() && REQUIRED_READS.iter().all(|n| required.contains(n));
    if !names_match || !req.iter().all(|(_, s)| valid(s).is_some()) {
        problems.push("a required read is missing or invalid".into());
    }
    let mut reads: Vec<(i64, i64)> = req.iter().filter_map(|(_, s)| valid(s)).collect();
    let records = match sensors {
        Value::Obj(items) => items.as_slice(),
        _ => {
            problems.push("sensor records of the wrong type".into());
            &[]
        }
    };
    let expected = sensor_values(stats);
    let mut meta = Value::obj();
    let mut timed: Vec<&str> = Vec::new();
    for (ptr, info) in records {
        let value = expected.iter().find(|(p, _)| p == ptr).map(|(_, v)| *v);
        let (Some(value), Value::Obj(_)) = (value, info) else {
            problems.push("a record for a value that is not there".into());
            continue; // never published: no pointer to nothing, nor a non-standard one
        };
        let span = info.get("span").and_then(valid).filter(|_| same_clock && is_number(value));
        if is_number(value) && span.is_none() {
            problems.push("a value without a valid read".into());
        }
        let mut m = Value::obj();
        for k in ["id", "provider", "identity_basis", "unit"] {
            m.set(k, info.get(k).cloned().unwrap_or(Value::Null));
        }
        m.set("read", span.map_or(Value::Null, offsets));
        meta.set(ptr, m);
        if let Some(s) = span {
            reads.push(s);
            timed.push(ptr);
        }
    }
    if expected.iter().any(|(p, v)| is_number(v) && !timed.contains(&p.as_str())) {
        problems.push("a value without its record".into());
    }
    for (key, values) in PASSIVE {
        let span = passive.get(key).unwrap_or(&Value::Null);
        match stats.get_mut(key) {
            Some(out @ Value::Obj(_)) => { // one read for every counter; its offsets replace whatever "read" holds
                let good = valid(span).filter(|_| same_clock);
                out.set("read", good.map_or(Value::Null, offsets));
                match good {
                    Some(s) => reads.push(s),
                    None if out.get(values).is_some_and(truthy) => problems.push(format!("{} values without a valid read", key)),
                    None => {}
                }
            }
            _ if !span.is_null() => problems.push("a record for a value that is not there".into()),
            _ => {}
        }
    }
    let problem = problems.into_iter().next();
    let oldest = if problem.is_some() { None } else { reads.iter().map(|s| s.0).min() };
    (meta, oldest, problem)
}

/// A random (version 4) UUID, as uuid.uuid4(): the service's instance id.
pub fn uuid4() -> String {
    let mut b = [0u8; 16];
    if !crate::platform::random_bytes(&mut b) {
        eprintln!("platmon-native: getrandom failed");
        std::process::exit(1);
    }
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: String = b.iter().map(|x| format!("{:02x}", x)).collect();
    format!("{}-{}-{}-{}-{}", &h[..8], &h[8..12], &h[12..16], &h[16..20], &h[20..])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn collection(stats: &str) -> Collection {
        let span = |a: i64, b: i64| Value::Arr(vec![a.into(), b.into()]);
        let mut required = Value::obj();
        for n in REQUIRED_READS {
            required.set(n, span(1_100, 1_200));
        }
        let mut c = Value::obj();
        c.set("stats", json::parse(stats).unwrap());
        c.set("sensors", Value::obj());
        c.set("required", required);
        c.set("passive", Value::obj());
        Ok(c)
    }

    #[test]
    fn publishes_fails_and_recovers() {
        let s = Sampler::new(1.0, None, "id".into(), "boottime");
        assert_eq!(s.view(|| 0).1.get("state"), Some(&"starting".into()));
        s.begin(1_000);
        s.finish(collection(r#"{"a": 1, "collectors": {"core": 1, "gpu": {"state": "ok"}}}"#), true, 1.0, 1_000, 1.5, 2_000);
        let (stats, status) = s.view(|| 3_000);
        assert_eq!(status.get("state"), Some(&"ready".into()));
        let stats = json::parse(&stats.unwrap()).unwrap();
        assert_eq!(stats.get("sample").and_then(|x| x.get("data_age_ms")), Some(&Value::Float(0.002))); // from 1_100
        let Some(Value::Obj(c)) = stats.get("collectors") else { panic!() };
        assert_eq!(c.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>(), ["core", "gpu"]);
        s.begin(4_000);
        s.finish(Err("gone".into()), true, 2.0, 4_000, 2.1, 5_000);
        assert_eq!(s.view(|| 6_000).1.get("state"), Some(&"degraded".into()));
        assert_eq!(s.view(|| 1_100 + 5_000_000_000).1.get("state"), Some(&"degraded".into())); // at stale_after
        let (stats, status) = s.view(|| 1_101 + 5_000_000_000);
        assert!(stats.is_none());
        assert_eq!(status.get("state"), Some(&"stale".into()));
        s.finish(collection(r#"{"a": 2}"#), true, 3.0, 7_000, 3.1, 8_000);
        let (_, status) = s.view(|| 9_000);
        assert_eq!(status.get("sample").and_then(|x| x.get("sequence")), Some(&Value::Int(2)));
        assert_eq!(status.get("last_attempt").and_then(|x| x.get("consecutive_failures")), Some(&Value::Int(0)));
    }

    #[test]
    fn readers_are_not_held_up_while_a_record_is_serialized() {
        use std::sync::mpsc;
        let s = Arc::new(Sampler::new(1.0, None, "id".into(), "boottime"));
        s.finish(collection(r#"{"n": 1}"#), true, 1.0, 1_000, 1.1, 2_000);
        let (inside, entered) = mpsc::channel();
        let (go, wait) = mpsc::channel::<()>();
        *s.during_build.lock().unwrap() = Some(Box::new(move || {
            inside.send(()).unwrap();
            wait.recv().unwrap();
        }));
        let w = s.clone();
        let writer = std::thread::spawn(move || w.finish(collection(r#"{"n": 2}"#), true, 2.0, 3_000, 2.1, 4_000));
        entered.recv_timeout(Duration::from_secs(5)).unwrap(); // the writer is serializing the second record
        let (r, (answer, got)) = (s.clone(), mpsc::channel());
        std::thread::spawn(move || answer.send(r.view(|| 5_000)).unwrap());
        let (stats, _) = got.recv_timeout(Duration::from_secs(2)).expect("a view waited for the serialization");
        assert!(stats.unwrap().starts_with(r#"{"n": 1, "schema_version": 1, "sample": {"instance_id": "id", "sequence": 1,"#));
        go.send(()).unwrap();
        assert_eq!(writer.join().unwrap().map(|p| p.sequence), Some(2));
        assert!(s.view(|| 5_000).0.unwrap().starts_with(r#"{"n": 2,"#));
    }
}
