//! The probe slow group (collector/probe.py): one TCP connection attempt per target, with each OS's
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
