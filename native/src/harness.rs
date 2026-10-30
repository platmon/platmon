//! The commands the comparison and measurement tools drive (native/tools): collections on given clocks
//! (collect, script), the collection loop's own cost (loop), the slow groups (observe-script), the runtime
//! on given times (runtime-script) and a replay answer's cost (replay-cost). The service is serve, in main.rs.
use crate::json::{self, Value};
use crate::{history, http, metadata, replay, runtime, slow};
use crate::collect::Collector;
#[cfg(target_os = "linux")]
use crate::collect::{observe, Collected};

#[cfg(target_os = "linux")]
/// (user s, system s) of this process.
fn usage() -> (f64, f64) {
    let mut r: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut r) };
    let s = |t: &libc::timeval| t.tv_sec as f64 + t.tv_usec as f64 / 1e6;
    (s(&r.ru_utime), s(&r.ru_stime))
}

#[cfg(target_os = "linux")]
/// (VmRSS, VmHWM) in KiB from /proc/self/status.
fn rss_kib() -> (i64, i64) {
    let text = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let field = |k: &str| text.lines().find_map(|l| l.strip_prefix(k)).and_then(|v| v.split_whitespace().next())
        .and_then(|v| v.parse().ok()).unwrap_or(-1);
    (field("VmRSS:"), field("VmHWM:"))
}

#[cfg(target_os = "linux")]
/// What the loop reports besides CPU (tools/perf/bench.sh): collection times, missed 1 s deadlines,
/// RSS over the run, per group how often each state came up, the number of source ids of the last
/// collection, and how many different read scopes (read_scope) came up, with the last one.
#[derive(Default)]
struct Stats {
    elapsed_ms: Vec<f64>,
    missed: u32,
    rss: Vec<i64>,
    states: Vec<(String, Vec<(String, u32)>)>,
    sources: usize,
    scopes: Vec<String>,
    scope: Value,
}

#[cfg(target_os = "linux")]
/// The read scope: the SHA-256 (16 hex digits) of the sorted source ids, and the names of the interfaces,
/// disks and pressure resources read.
fn read_scope(col: &Collected) -> Value {
    let mut ids: Vec<&str> = col.sensors.iter().filter_map(|(_, v)| match v.get("id") { Some(Value::Str(s)) => Some(s.as_str()), _ => None }).collect();
    ids.sort();
    let names = |group: &str, list: &str| Value::Arr(match col.stats.get(group).and_then(|g| g.get(list)) {
        Some(Value::Arr(items)) => items.iter().filter_map(|x| x.get("name").cloned()).collect(),
        _ => vec![],
    });
    let mut v = Value::obj();
    v.set("source_ids", crate::sha256::hex(ids.join("\n").as_bytes())[..16].into());
    v.set("interfaces", names("network", "interfaces"));
    v.set("disks", names("disk_io", "disks"));
    let pressure = match col.stats.get("pressure").and_then(|p| p.get("resources")) {
        Some(Value::Obj(items)) => items.iter().map(|(k, _)| Value::Str(k.clone())).collect(),
        _ => vec![],
    };
    v.set("pressure", Value::Arr(pressure));
    v
}

#[cfg(target_os = "linux")]
impl Stats {
    fn add(&mut self, col: &Collected) {
        if let Some(Value::Obj(groups)) = col.stats.get("collectors") {
            for (name, status) in groups {
                let state = match status.get("state") { Some(Value::Str(s)) => s.clone(), _ => "?".into() };
                let pos = self.states.iter().position(|(n, _)| n == name).unwrap_or_else(|| {
                    self.states.push((name.clone(), vec![]));
                    self.states.len() - 1
                });
                let counts = &mut self.states[pos].1;
                match counts.iter_mut().find(|(s, _)| *s == state) {
                    Some(c) => c.1 += 1,
                    None => counts.push((state, 1)),
                }
            }
        }
        self.sources = col.sensors.iter().filter(|(_, v)| matches!(v.get("id"), Some(Value::Str(_)))).count();
        let scope = read_scope(col);
        let key = scope.to_string(true);
        if !self.scopes.contains(&key) {
            self.scopes.push(key);
        }
        self.scope = scope;
    }

    fn value(&mut self) -> Value {
        let mut v = Value::obj();
        let mut e = self.elapsed_ms.clone();
        e.sort_by(f64::total_cmp);
        let pick = |q: f64| e.get(((e.len() as f64 - 1.0) * q).round() as usize).copied().map(|x| json::round_to(x, 3));
        let mut el = Value::obj();
        el.set("p50", pick(0.5).into());
        el.set("p95", pick(0.95).into());
        el.set("max", e.last().copied().map(|x| json::round_to(x, 3)).into());
        v.set("elapsed_ms", el);
        v.set("missed_deadlines", (self.missed as i64).into());
        v.set("rss_kib", Value::Arr(self.rss.iter().map(|r| (*r).into()).collect()));
        v.set("groups", Value::Obj(self.states.iter().map(|(n, c)| {
            (n.clone(), Value::Obj(c.iter().map(|(s, k)| (s.clone(), (*k as i64).into())).collect()))
        }).collect()));
        v.set("source_ids", (self.sources as i64).into());
        v.set("read_scopes", (self.scopes.len() as i64).into());
        v.set("read_scope", self.scope.clone());
        v
    }
}

/// replay-cost FILE [PAGES [OUT]]: what a replay answer costs, by stage, for the core publications in FILE (one
/// JSON publication per line, at most one page of them). Per record, in µs of elapsed time, the median of 7
/// rounds of PAGES pages: decoding the stored payloads, a page of records kept and then freed together;
/// writing records decoded beforehand, the page of texts kept and then freed together; a whole
/// Replay.view (decoding, ages and stale, the records and the envelope, writing), as the Python
/// service's was measured before its removal. One JSON line; OUT gets the page itself.
pub fn replay_cost(args: &[String]) {
    let Some(path) = args.first() else { eprintln!("replay-cost FILE [PAGES [OUT]]"); std::process::exit(2) };
    let pages: usize = args.get(1).and_then(|v| v.parse().ok()).unwrap_or(20);
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| { eprintln!("{}: {}", path, e); std::process::exit(1) });
    let data: Vec<Value> = text.lines().filter(|l| !l.is_empty())
        .map(|l| json::parse(l).unwrap_or_else(|e| { eprintln!("{}: {}", path, e); std::process::exit(1) })).collect();
    let n = data.len();
    let r = replay::Replay::new("cost".into(), "monotonic", &[], 1e6, 1 << 30);
    for (i, d) in data.iter().enumerate() {
        let t = (i as i64 + 1) * 1_000_000_000;
        r.append("core", i as i64 + 1, t, t, d, false, || t).unwrap_or_else(|e| { eprintln!("{}", e); std::process::exit(1) });
    }
    let now = (n as i64 + 1) * 1_000_000_000;
    let page = || r.view("core", None, 0, n as i64, || now).unwrap_or_else(|e| { eprintln!("{:?}", e); std::process::exit(1) });
    let got = json::parse(&page()).map(|p| match p.get("records") { Some(Value::Arr(a)) => a.len(), _ => 0 });
    if got != Ok(n) {
        eprintln!("one page holds {:?} of the {} records", got, n);
        std::process::exit(1);
    }
    if let Some(out) = args.get(2) {
        std::fs::write(out, page()).unwrap_or_else(|e| { eprintln!("{}: {}", out, e); std::process::exit(1) });
    }
    let payloads: Vec<String> = data.iter().map(Value::to_py_compact).collect();
    let median = |work: &dyn Fn() -> usize| {
        let mut rounds: Vec<f64> = (0..7).map(|_| {
            let t = std::time::Instant::now();
            let mut sink = 0;
            for _ in 0..pages {
                sink += work();
            }
            std::hint::black_box(sink);
            t.elapsed().as_secs_f64() * 1e6 / (pages * n) as f64
        }).collect();
        rounds.sort_by(f64::total_cmp);
        rounds[3]
    };
    let decoded: Vec<Value> = payloads.iter().map(|p| json::parse(p).unwrap()).collect();
    let decode = median(&|| payloads.iter().map(|p| json::parse(p).unwrap()).collect::<Vec<Value>>().len());
    let write = median(&|| decoded.iter().map(Value::to_py_compact).collect::<Vec<String>>().iter().map(String::len).sum());
    let whole = median(&|| page().len());
    let mut out = Value::obj();
    out.set("allocator", crate::ALLOCATOR.into());
    out.set("records", (n as i64).into());
    out.set("payload_bytes", ((payloads.iter().map(String::len).sum::<usize>() / n.max(1)) as i64).into());
    out.set("pages", (pages as i64).into());
    out.set("decode_us", json::round_to(decode, 3).into());
    out.set("write_us", json::round_to(write, 3).into());
    out.set("page_us", json::round_to(whole, 3).into());
    println!("{}", out.to_string(false));
}

/// collect, script and loop: the commands that collect on this host (collect only, elsewhere).
pub fn collect_command(args: &[String]) {
    let mut c = Collector::new(5.0, [true; 3]); // the service's at its default 1 s interval
    match args.get(1).map(String::as_str) {
        Some("collect") => {
            let _ = c.collect(); // the CPU baseline, as the service's first collection
            std::thread::sleep(std::time::Duration::from_secs(1));
            let col = c.collect().unwrap_or_else(|e| { eprintln!("collection failed: {}", e); std::process::exit(1) });
            let mut out = col.to_value();
            out.set("clock", metadata::clock_source().into());
            println!("{}", out.to_string(false));
        }
        #[cfg(target_os = "linux")]
        Some("script") => {
            use std::io::BufRead;
            for line in std::io::stdin().lock().lines() {
                let line = line.unwrap_or_default();
                let mut it = line.split_whitespace().map(|x| x.parse::<i64>());
                let (Some(Ok(base)), Some(Ok(wall))) = (it.next(), it.next()) else {
                    eprintln!("script: expected BASE_NS WALL_US, got {:?}", line);
                    std::process::exit(2)
                };
                metadata::set_clocks(base, wall);
                let out = match c.collect() {
                    Ok(col) => col.to_value(),
                    Err(e) => { let mut v = Value::obj(); v.set("error", e.into()); v }
                };
                println!("{}", out.to_string(false));
            }
        }
        #[cfg(target_os = "linux")]
        Some("loop") => {
            let seconds: f64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(60.0);
            let warmup: f64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(3.0);
            let w0 = std::time::Instant::now();
            while w0.elapsed().as_secs_f64() < warmup {
                let _ = c.collect();
                std::thread::sleep(std::time::Duration::from_secs(1));
            }
            let mut st = Stats::default();
            let (u0, s0) = usage();
            let t0 = std::time::Instant::now();
            let (mut n, mut failed, mut deadline) = (0u32, 0u32, t0);
            while t0.elapsed().as_secs_f64() < seconds {
                let start = std::time::Instant::now();
                match c.collect() {
                    Ok(col) => { st.elapsed_ms.push(start.elapsed().as_secs_f64() * 1e3); st.add(&col); }
                    Err(_) => failed += 1,
                }
                if n % 30 == 0 {
                    st.rss.push(rss_kib().0);
                }
                n += 1;
                deadline += std::time::Duration::from_secs(1);
                match deadline.checked_duration_since(std::time::Instant::now()) {
                    Some(d) => std::thread::sleep(d),
                    None => st.missed += 1,
                }
            }
            let wall = t0.elapsed().as_secs_f64();
            let (u1, s1) = usage();
            st.rss.push(rss_kib().0);
            let mut v = Value::obj();
            v.set("variant", if cfg!(feature = "cache-alloc") { "rust-collector-cache-alloc" } else { "rust-collector" }.into());
            v.set("collections", (n as i64).into());
            v.set("failed", (failed as i64).into());
            v.set("wall_s", json::round_to(wall, 3).into());
            v.set("user_pct", json::round_to(100.0 * (u1 - u0) / wall, 4).into());
            v.set("sys_pct", json::round_to(100.0 * (s1 - s0) / wall, 4).into());
            v.set("maxrss_kib", rss_kib().1.into());
            if let Value::Obj(items) = st.value() {
                for (k, x) in items { v.set(&k, x); }
            }
            println!("{}", v.to_string(false));
        }
        _ => {
            eprintln!("platmon-native: {} is Linux only (--help)", args[1]);
            std::process::exit(2);
        }
    }
}

#[cfg(target_os = "linux")]
/// observe-script TARGETS TIMEOUT (tools/equivalence/fixture.py, the slow groups): for each stdin line, one observation
/// of storage, wifi, battery and probe (with those [probe] targets and timeout), printed as one JSON line
/// {"observations": {group: {"collector", "data"}}}.
pub fn observe_script(args: &[String]) {
    use std::io::BufRead;
    let targets = crate::config::parse_targets(args.first().map_or("", String::as_str)).unwrap_or_else(|e| { eprintln!("observe-script: {}", e); std::process::exit(2) });
    let timeout: f64 = args.get(1).and_then(|t| t.parse().ok()).unwrap_or(2.0);
    let mut probe = crate::collect::probe::Probe::new(targets, timeout);
    let mut logged: Vec<Vec<(String, Vec<String>)>> = vec![vec![]; 4];
    for _ in std::io::stdin().lock().lines() {
        let mut groups = Value::obj();
        for (i, name) in observe::GROUPS.iter().enumerate() {
            let mut g = metadata::Group::new(name, false);
            let data = match *name {
                "storage" => observe::storage(&mut g),
                "wifi" => observe::wifi(&mut g),
                "battery" => observe::battery(&mut g),
                _ => probe.observe(&mut g),
            };
            let collector = metadata::summarize(std::slice::from_ref(&g), &mut logged[i]).get(name).cloned().unwrap_or_default();
            let mut entry = Value::obj();
            entry.set("collector", collector);
            entry.set("data", data);
            groups.set(name, entry);
        }
        let mut out = Value::obj();
        out.set("observations", groups);
        println!("{}", out.to_string(false));
    }
}

fn number(op: &Value, key: &str) -> f64 {
    match op.get(key) {
        Some(Value::Int(i)) => *i as f64,
        Some(Value::Float(f)) => *f,
        _ => { eprintln!("runtime-script: {} must be a number", key); std::process::exit(2) }
    }
}

fn integer(op: &Value, key: &str) -> i64 {
    match op.get(key) {
        Some(Value::Int(i)) => *i,
        _ => { eprintln!("runtime-script: {} must be an integer", key); std::process::exit(2) }
    }
}

fn text<'a>(op: &'a Value, key: &str) -> &'a str {
    match op.get(key) {
        Some(Value::Str(s)) => s,
        _ => { eprintln!("runtime-script: {} must be a string", key); std::process::exit(2) }
    }
}

static SCRIPT_NOW: std::sync::atomic::AtomicI64 = std::sync::atomic::AtomicI64::new(0);

/// The runtime script's elapsed clock: the time its last operation gave.
fn script_now() -> i64 {
    SCRIPT_NOW.load(std::sync::atomic::Ordering::SeqCst)
}

/// The runtime script (tools/equivalence/runtime_fixture.py, against the Python service's recorded answers). One JSON
/// operation per line, one JSON line out for each; every "now" also sets the clock that history, replay
/// and the HTTP answers read:
///   {"op": "setup", "interval", "instance_id", "clock", "slow": [[name, interval], ...], "levels",
///    "history": {"retention"} or null, "replay": {"retention", "max_bytes"} or null}
///                                             also starts the HTTP server on 127.0.0.1 (web off)
///   {"op": "begin", "wall", "now"}            a collection starts
///   {"op": "finish", "wall", "now", "result", "clock"}  it ends: result is Collected.to_value()'s shape or
///                                             {"error": ...}; clock "other": its spans are on another clock
///   {"op": "view", "now"}                     -> {"stats": the /api/stats JSON or null, "status": /api/status's}
///   {"op": "slow_begin", "group", "wall", "now"}
///   {"op": "slow_finish", "group", "now", "wall", "data", "notes": [[target, reason]], "values", "raise"}
///   {"op": "observations", "now"}             -> {"observations": the /api/observations JSON}
///   {"op": "http", "now", "request"}          -> {"response": the raw answer to that raw request}
pub fn runtime_script() {
    use std::io::{BufRead, Read, Write};
    use std::sync::Arc;
    let mut sampler: Option<Arc<runtime::Sampler>> = None;
    let mut groups: Vec<Arc<slow::Slow>> = vec![];
    let mut obs: Option<Arc<slow::Observations>> = None;
    let mut port = 0;
    let (mut begun, mut slow_begun) = ((0.0, 0), vec![]);
    let set_now = |op: &Value| SCRIPT_NOW.store(integer(op, "now"), std::sync::atomic::Ordering::SeqCst);
    for line in std::io::stdin().lock().lines() {
        let line = line.unwrap_or_default();
        let op = json::parse(&line).unwrap_or_else(|e| { eprintln!("runtime-script: {}: {:?}", e, line); std::process::exit(2) });
        let group = |op: &Value| groups.iter().position(|g| g.name == text(op, "group"))
            .unwrap_or_else(|| { eprintln!("runtime-script: no group {:?}", op.get("group")); std::process::exit(2) });
        let mut out = Value::obj();
        if op.contains("now") {
            set_now(&op);
        }
        match text(&op, "op") {
            "setup" => {
                let source: &'static str = if text(&op, "clock") == "boottime" { "boottime" } else { "monotonic" };
                let id: String = text(&op, "instance_id").into();
                let interval = number(&op, "interval");
                let names: Vec<(String, f64)> = match op.get("slow") {
                    Some(Value::Arr(items)) => items.iter().map(|g| match (g.get_index(0), g.get_index(1)) {
                        (Some(Value::Str(name)), Some(Value::Int(i))) => (name.clone(), *i as f64),
                        (Some(Value::Str(name)), Some(Value::Float(f))) => (name.clone(), *f),
                        _ => { eprintln!("runtime-script: slow is [[name, interval], ...]"); std::process::exit(2) }
                    }).collect(),
                    _ => vec![],
                };
                let history = op.get("history").filter(|h| !h.is_null())
                    .map(|h| Arc::new(history::History::new(number(h, "retention"), interval)));
                let group_names: Vec<&str> = names.iter().map(|(n, _)| n.as_str()).collect();
                let replay = op.get("replay").filter(|r| !r.is_null()).map(|r| Arc::new(replay::Replay::new(
                    id.clone(), source, &group_names, number(r, "retention"), integer(r, "max_bytes") as usize)));
                let mut s = runtime::Sampler::new(interval, None, id.clone(), source);
                s.clock = Box::new(script_now);
                s.history = history.clone();
                s.replay = replay.clone();
                let s = Arc::new(s);
                groups = names.iter().map(|(name, interval)| {
                    let mut g = slow::Slow::new(name, *interval);
                    g.clock = Box::new(script_now);
                    g.history = history.clone();
                    g.replay = replay.clone();
                    Arc::new(g)
                }).collect();
                slow_begun = vec![(0.0, 0); groups.len()];
                let o = Arc::new(slow::Observations { groups: groups.clone(), instance_id: id, clock_source: source });
                let mut srv = http::Server::new(s.clone(), o.clone(), None, "0".into(), op.get("levels").cloned().unwrap_or_default());
                srv.clock = script_now;
                let listener = http::bind("127.0.0.1", 0).unwrap_or_else(|e| { eprintln!("runtime-script: {}", e); std::process::exit(1) });
                port = listener.local_addr().unwrap().port();
                std::thread::spawn(move || Arc::new(srv).serve(listener));
                sampler = Some(s);
                obs = Some(o);
            }
            name => {
                let Some(s) = sampler.as_ref() else { eprintln!("runtime-script: setup first"); std::process::exit(2) };
                match name {
                    "begin" => {
                        begun = (number(&op, "wall"), integer(&op, "now"));
                        s.begin(begun.1);
                    }
                    "finish" => {
                        let result = op.get("result").cloned().unwrap_or_default();
                        let result = match result.get("error") {
                            Some(e) => Err(e.to_string(false)),
                            None => Ok(result),
                        };
                        let same = op.get("clock").map_or(true, |c| *c != Value::from("other"));
                        s.finish(result, same, begun.0, begun.1, number(&op, "wall"), integer(&op, "now"));
                    }
                    "view" => {
                        let now = integer(&op, "now");
                        let (stats, status) = s.view(|| now);
                        out.set("stats", stats.into());
                        out.set("status", status.to_py().into());
                    }
                    "slow_begin" => {
                        let i = group(&op);
                        slow_begun[i] = (number(&op, "wall"), integer(&op, "now"));
                        groups[i].begin(slow_begun[i].1);
                    }
                    "slow_finish" => {
                        let i = group(&op);
                        let mut g = metadata::Group::new(&groups[i].name, false);
                        if let Some(Value::Arr(notes)) = op.get("notes") {
                            for n in notes {
                                let (Some(Value::Str(target)), Some(Value::Str(reason))) = (n.get_index(0), n.get_index(1)) else { continue };
                                let reason: &'static str = metadata::PROBLEMS.iter().chain(metadata::ABSENT.iter())
                                    .find(|r| **r == reason.as_str()).copied().unwrap_or_else(|| Box::leak(reason.clone().into_boxed_str()));
                                g.note(target, reason, None);
                            }
                        }
                        for _ in 0..integer(&op, "values") {
                            g.got(());
                        }
                        let data = match op.get("raise") {
                            Some(Value::Str(e)) => Err(e.clone()),
                            _ => Ok(op.get("data").cloned().unwrap_or_default()),
                        };
                        let wall = number(&op, "wall");
                        groups[i].finish(g, data, slow_begun[i].0, slow_begun[i].1, integer(&op, "now"), || wall);
                    }
                    "observations" => {
                        let now = integer(&op, "now");
                        out.set("observations", obs.as_ref().unwrap().view(|| now).to_py().into());
                    }
                    "http" => {
                        let mut c = std::net::TcpStream::connect(("127.0.0.1", port)).expect("runtime-script: connect");
                        c.write_all(text(&op, "request").as_bytes()).expect("runtime-script: send");
                        let mut answer = Vec::new();
                        let _ = c.read_to_end(&mut answer);
                        out.set("response", String::from_utf8_lossy(&answer).into_owned().into());
                    }
                    other => { eprintln!("runtime-script: unknown op {:?}", other); std::process::exit(2) }
                }
            }
        }
        println!("{}", out.to_string(false));
    }
}
