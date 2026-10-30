//! The probe slow group: one TCP connection attempt per target, with each OS's
//! connect_ms. Contract: docs/reference/probe.md.
use crate::json::Value;
use crate::metadata::Group;
#[cfg(target_os = "linux")]
use super::linux::observe::connect_ms;
#[cfg(windows)]
use super::windows::connect_ms;
use std::collections::VecDeque;

fn obj(items: Vec<(&str, Value)>) -> Value {
    Value::Obj(items.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

/// probe.Probe: one attempt per target per observation, one after another, with the last 10 results of
/// each for its failure share.
pub struct Probe {
    targets: Vec<(String, u16)>,
    timeout: f64,
    recent: Vec<VecDeque<bool>>,
}

impl Probe {
    pub fn new(targets: Vec<(String, u16)>, timeout: f64) -> Probe {
        let recent = targets.iter().map(|_| VecDeque::new()).collect();
        Probe { targets, timeout, recent }
    }

    pub fn observe(&mut self, g: &mut Group) -> Value {
        let mut out = Vec::new();
        for (i, (address, port)) in self.targets.iter().enumerate() {
            let (ms, why) = connect_ms(address, *port, self.timeout);
            let recent = &mut self.recent[i];
            if recent.len() == 10 {
                recent.pop_front();
            }
            recent.push_back(ms.is_some());
            let failures = recent.iter().filter(|ok| !**ok).count();
            g.got(());
            out.push(obj(vec![("address", address.as_str().into()), ("port", (*port as i64).into()), ("connect_ms", ms.into()),
                              ("reason", why.into()), ("attempts", (recent.len() as i64).into()), ("failures", (failures as i64).into()),
                              ("failure_ratio", crate::json::round_to(failures as f64 / recent.len() as f64, 3).into())]));
        }
        obj(vec![("provider", "tcp_connect".into()), ("timeout_ms", (self.timeout * 1000.0).into()), ("targets", Value::Arr(out))])
    }
}

// tests/collector/test_probe.py, ported (docs/development/python-test-port.md): the observation; connect_ms
// is tested in linux/observe/probe.rs, target parsing in config.rs.
#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;

    fn targets(v: &Value) -> Vec<Value> {
        match v.get("targets") { Some(Value::Arr(t)) => t.clone(), _ => panic!("no targets") }
    }

    fn field(t: &Value, key: &str) -> String {
        t.get(key).unwrap().to_string(false)
    }

    #[test]
    fn observation_keeps_a_bounded_failure_window() {
        // the same port, listening on every other attempt: a connection, then a refusal, 24 times
        let first = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = first.local_addr().unwrap().port();
        drop(first);
        let mut p = Probe::new(vec![("127.0.0.1".into(), port)], 1.0);
        let mut last = Value::Null;
        for n in 1..25 {
            let listener = (n % 2 == 1).then(|| std::net::TcpListener::bind(("127.0.0.1", port)).unwrap());
            let mut g = Group::new("probe", false);
            last = targets(&p.observe(&mut g)).remove(0);
            drop(listener);
            assert_eq!(field(&last, "attempts"), n.min(10).to_string());
            assert_eq!(g.status().get("state").unwrap().to_string(false), r#""ok""#); // a failed attempt is a result
        }
        assert_eq!((field(&last, "connect_ms"), field(&last, "reason"), field(&last, "failures"), field(&last, "failure_ratio")),
                   ("null".into(), r#""refused""#.into(), "5".into(), "0.5".into()));
        let mut once = Probe::new(vec![("127.0.0.1".into(), port)], 1.0);
        let first = targets(&once.observe(&mut Group::new("probe", false))).remove(0);
        assert_eq!((field(&first, "connect_ms"), field(&first, "failures")), ("null".into(), "1".into())); // never 0 ms
    }

    #[test]
    fn targets_are_probed_one_after_another_and_a_socket_error_keeps_the_others() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        // an address no socket can be made for stands in for Python's failing socket()
        let mut p = Probe::new(vec![("127.0.0.1".into(), port), ("not an address".into(), port), ("127.0.0.1".into(), port)], 1.0);
        for attempt in 1..3 {
            let mut g = Group::new("probe", false);
            let out = targets(&p.observe(&mut g));
            assert_eq!(g.status().get("state").unwrap().to_string(false), r#""ok""#);
            assert_eq!(out.iter().map(|t| field(t, "address")).collect::<Vec<_>>(), [r#""127.0.0.1""#, r#""not an address""#, r#""127.0.0.1""#]);
            assert!(out[0].get("connect_ms").is_some_and(|v| !v.is_null()) && out[2].get("connect_ms").is_some_and(|v| !v.is_null()));
            assert_eq!((field(&out[1], "connect_ms"), field(&out[1], "reason")), ("null".into(), r#""error""#.into()));
            assert_eq!((field(&out[1], "attempts"), field(&out[1], "failures")), (attempt.to_string(), attempt.to_string()));
            assert_eq!(field(&out[1], "failure_ratio"), "1.0");
        }
    }
}
