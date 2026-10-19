//! runtime/slow.py: low-frequency groups, each read on its own cadence by its own thread, apart from the core
//! Sampler, and served by /api/observations with their own id, times, age and state. A blocked read stalls
//! only its group (its age grows, collecting_for_ms says for how long); no new thread is started for it.
//! Contract: docs/reference/observations.md.
use crate::history::History;
use crate::json::Value;
use crate::metadata::{self, Group};
use crate::replay::Replay;
use crate::runtime::{clock_value, ms, seconds_ns};
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub const SCHEMA_VERSION: i64 = 2;

/// One published observation, never changed afterwards.
pub struct Observation {
    pub id: i64,
    pub started: i64,
    pub completed: i64,
    pub started_at: f64,
    pub completed_at: f64,
    pub collector: Value,
    pub data: Value,
}

impl Observation {
    fn state(&self) -> &str {
        match self.collector.get("state") { Some(Value::Str(s)) => s, _ => "" }
    }
}

#[derive(Default)]
struct State {
    last: Option<Arc<Observation>>,
    since: Option<i64>,
    count: i64,
}

/// What a reader returns: the group's data, or an unexpected error (the group's internal_error).
pub type Reader = Box<dyn FnMut(&mut Group) -> Result<Value, String> + Send>;

pub struct Slow {
    pub name: String,
    pub interval: f64,
    interval_ns: i64,
    stale_ns: i64,
    state: Mutex<State>,
    logged: Mutex<Vec<(String, Vec<String>)>>,
    /// the service's elapsed clock, for replay's expiry when publishing
    pub clock: Box<dyn Fn() -> i64 + Send + Sync>,
    pub history: Option<Arc<History>>,
    pub replay: Option<Arc<Replay>>,
}

impl Slow {
    /// interval: seconds between the starts of two observations; stale after 3 intervals.
    pub fn new(name: &str, interval: f64) -> Slow {
        let interval_ns = seconds_ns(interval);
        Slow { name: name.into(), interval, interval_ns, stale_ns: 3 * interval_ns, state: Mutex::default(),
               logged: Mutex::default(), clock: Box::new(metadata::clock), history: None, replay: None }
    }

    /// Starts the group's one thread: observe, then wait until an interval after that observation started
    /// (no catch-up bursts). A read that blocks holds this thread only.
    pub fn start(self: &Arc<Self>, mut read: Reader) -> std::thread::JoinHandle<()> {
        let g = self.clone();
        std::thread::Builder::new().name(format!("slow-{}", self.name)).spawn(move || loop {
            let started = g.observe_once(&mut read);
            let left = (started + g.interval_ns - metadata::clock()) as f64 / 1e9;
            std::thread::sleep(Duration::from_secs_f64(left.max(0.0)));
        }).expect("cannot start a slow group's thread")
    }

    fn observe_once(&self, read: &mut Reader) -> i64 {
        let (started_at, started) = (metadata::wall(), metadata::clock());
        self.begin(started);
        let mut group = Group::new(&self.name, false);
        let data = read(&mut group);
        self.finish(group, data, started_at, started, metadata::clock(), metadata::wall);
        started
    }

    pub fn begin(&self, started: i64) {
        self.state.lock().unwrap().since = Some(started);
    }

    /// Publishes one observation as one update (completed_at is read under the lock, as in Python), then,
    /// outside the lock, gives it to replay and history.
    pub fn finish(&self, mut group: Group, data: Result<Value, String>, started_at: f64, started: i64, completed: i64,
                  completed_at: impl FnOnce() -> f64) -> Arc<Observation> {
        let data = data.unwrap_or_else(|e| { // never the service's: this group's internal_error
            group.note(&self.name.clone(), "internal_error", Some(e));
            Value::Null
        });
        let collector = metadata::summarize(std::slice::from_ref(&group), &mut self.logged.lock().unwrap())
            .get(&self.name).cloned().unwrap_or_default();
        let o = {
            let mut st = self.state.lock().unwrap();
            st.count += 1;
            let o = Arc::new(Observation { id: st.count, started, completed, started_at, completed_at: completed_at(),
                                           collector, data });
            st.last = Some(o.clone());
            st.since = None;
            o
        };
        if let Some(replay) = &self.replay {
            if let Err(e) = replay.append(&self.name, o.id, started, completed, &self.entry_of(Some(&o), completed, None), false, &self.clock) {
                eprintln!("platmon: observation replay append failed: {}", e);
            }
        }
        if let Some(history) = &self.history {
            if history.append_observation(&self.name, self.interval, o.id, started, completed, o.state(), &o.data).is_err() {
                eprintln!("platmon: observation history append failed");
            }
        }
        o
    }

    /// This group's /api/observations entry (Slow.view); now() is read under the lock.
    pub fn entry(&self, now: impl FnOnce() -> i64) -> Value {
        let (now, last, since) = {
            let st = self.state.lock().unwrap();
            (now(), st.last.clone(), st.since)
        };
        self.entry_of(last.as_deref(), now, since)
    }

    /// Slow._entry: one publication with ages as of now; Python's key order (state last).
    fn entry_of(&self, last: Option<&Observation>, now: i64, since: Option<i64>) -> Value {
        let mut e = Value::obj();
        e.set("interval_ms", ms(self.interval_ns).into());
        e.set("stale_after_ms", ms(self.stale_ns).into());
        e.set("collecting_for_ms", since.map(|s| ms(now - s)).into());
        e.set("observation", Value::Null);
        e.set("collector", Value::Null);
        e.set("data", Value::Null);
        let Some(last) = last else {
            let state = if since.map_or(true, |s| now - s <= self.stale_ns) { "starting" } else { "stale" };
            e.set("state", state.into());
            return e;
        };
        let stale = now - last.started > self.stale_ns;
        e.set("state", (if stale { "stale" } else { last.state() }).into());
        let mut o = Value::obj();
        o.set("id", last.id.into());
        o.set("started_at", last.started_at.into());
        o.set("completed_at", last.completed_at.into());
        o.set("duration_ms", ms(last.completed - last.started).into());
        o.set("age_ms", ms(now - last.completed).into());
        o.set("data_age_ms", ms(now - last.started).into());
        o.set("stale", Value::Bool(stale));
        e.set("observation", o);
        e.set("collector", last.collector.clone());
        e.set("data", last.data.clone());
        e
    }
}

/// The service's slow groups, for /api/observations.
pub struct Observations {
    pub groups: Vec<Arc<Slow>>,
    pub instance_id: String,
    pub clock_source: &'static str,
}

impl Observations {
    /// The /api/observations body; each group reads the clock for its own entry, as Python's view() does.
    pub fn view(&self, now: impl Fn() -> i64) -> Value {
        let mut v = Value::obj();
        v.set("schema_version", SCHEMA_VERSION.into());
        v.set("instance_id", self.instance_id.as_str().into());
        v.set("clock", clock_value(self.clock_source));
        v.set("groups", Value::Obj(self.groups.iter().map(|g| (g.name.clone(), g.entry(&now))).collect()));
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn a_blocked_read_stalls_only_its_group() {
        let (release, wait) = mpsc::channel::<()>();
        let calls = Arc::new(Mutex::new(0));
        let (blocked, other) = (Arc::new(Slow::new("storage", 0.02)), Arc::new(Slow::new("wifi", 0.02)));
        let c = calls.clone();
        blocked.start(Box::new(move |_| {
            *c.lock().unwrap() += 1;
            let _ = wait.recv(); // after the test: returns at once
            Ok(Value::obj())
        }));
        other.start(Box::new(|g| Ok(g.got(Value::obj()))));
        std::thread::sleep(Duration::from_millis(300));
        let entry = |s: &Slow| s.entry(metadata::clock);
        let (b, o) = (entry(&blocked), entry(&other));
        assert_eq!(b.get("state"), Some(&"stale".into())); // first one still running after 3 intervals
        assert!(matches!(b.get("collecting_for_ms"), Some(Value::Float(f)) if *f > 200.0));
        assert_eq!(b.get("observation"), Some(&Value::Null));
        assert_eq!(*calls.lock().unwrap(), 1); // no second read started while the first blocks
        assert!(matches!(o.get("observation").and_then(|x| x.get("id")), Some(Value::Int(n)) if *n >= 5));
        assert_eq!(o.get("state"), Some(&"ok".into()));
        release.send(()).unwrap();
        std::thread::sleep(Duration::from_millis(50));
        let b = entry(&blocked);
        assert!(matches!(b.get("observation").and_then(|x| x.get("id")), Some(Value::Int(n)) if *n >= 1));
    }
}
