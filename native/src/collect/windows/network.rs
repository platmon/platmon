//! Network on Windows: the counters of every interface GetIfTable2 lists, except the filter drivers' layers
//! (bindings, not interfaces), with the rates and reasons of docs/reference/network.md. Windows has no
//! network namespace: the scope is the host, and an interface is matched by its index and name.
use crate::collect::{rate, reason_value, window_ms};
use crate::metadata::{clock, Group, Span};
use crate::json::Value;
use windows_sys::Win32::NetworkManagement::IpHelper::{FreeMibTable, GetIfTable2, MIB_IF_ROW2, MIB_IF_TABLE2};

const COUNTERS: [&str; 4] = ["bytes", "packets", "errors", "dropped"];
/// InterfaceAndOperStatusFlags: a filter driver's layer over an interface.
const FILTER_INTERFACE: u8 = 1 << 1;

/// rx bytes, packets, errors, dropped, then tx the same.
type Row = [u64; 8];

fn row(r: &MIB_IF_ROW2) -> Row {
    [r.InOctets, r.InUcastPkts.wrapping_add(r.InNUcastPkts), r.InErrors, r.InDiscards,
     r.OutOctets, r.OutUcastPkts.wrapping_add(r.OutNUcastPkts), r.OutErrors, r.OutDiscards]
}

fn name(r: &MIB_IF_ROW2) -> String {
    let n = r.Alias.iter().position(|c| *c == 0).unwrap_or(r.Alias.len());
    String::from_utf16_lossy(&r.Alias[..n])
}

/// (index, name, counters) of the interfaces, or the error code.
fn read() -> Result<Vec<(u32, String, Row)>, u32> {
    let mut table: *mut MIB_IF_TABLE2 = std::ptr::null_mut();
    let rc = unsafe { GetIfTable2(&mut table) };
    if rc != 0 {
        return Err(rc);
    }
    let rows = unsafe { std::slice::from_raw_parts((*table).Table.as_ptr(), (*table).NumEntries as usize) };
    let out = rows.iter().filter(|r| r.InterfaceAndOperStatusFlags._bitfield & FILTER_INTERFACE == 0)
        .map(|r| (r.InterfaceIndex, name(r), row(r))).collect();
    unsafe { FreeMibTable(table.cast()) };
    Ok(out)
}

/// The network counters, kept between collections (network.NetworkCounters without a namespace).
pub struct Network {
    max_gap: Option<i64>,
    last: Option<(i64, Vec<((u32, String), Row)>)>,
}

impl Network {
    pub fn new(max_gap: Option<f64>) -> Network {
        Network { max_gap: max_gap.map(|g| (g * 1e9).round() as i64), last: None }
    }

    fn output(interfaces: Vec<Value>) -> Value {
        let mut scope = Value::obj();
        scope.set("kind", "host".into());
        scope.set("id", Value::Null);
        let mut out = Value::obj();
        out.set("scope", scope);
        out.set("provider", "if_table2".into());
        out.set("interfaces", Value::Arr(interfaces));
        out.set("read", Value::Null);
        out
    }

    pub fn sample(&mut self, group: &mut Group) -> (Value, Span) {
        let start = clock();
        let mut rows = match read() {
            Ok(rows) => rows,
            Err(rc) => {
                self.last = None;
                let reason = if rc == 5 { "permission_denied" } else { "io_error" }; // ERROR_ACCESS_DENIED
                group.note("if_table", reason, Some(format!("GetIfTable2: {}", std::io::Error::from_raw_os_error(rc as i32))));
                return (Network::output(vec![]), None);
            }
        };
        let end = clock();
        // a name on several rows is ambiguous, as on Linux: all of them are left out
        let mut names: Vec<String> = rows.iter().map(|r| r.1.clone()).collect();
        names.sort();
        let twice: Vec<String> = names.windows(2).filter(|w| w[0] == w[1]).map(|w| w[0].clone()).collect();
        for n in &twice {
            group.note(n, "invalid_data", Some("an interface name on several rows".into()));
        }
        rows.retain(|r| !r.1.is_empty() && !twice.contains(&r.1));
        rows.sort_by(|a, b| a.1.cmp(&b.1));
        if rows.is_empty() {
            group.note("if_table", "not_detected", None);
        }

        let last = self.last.take();
        let window = last.as_ref().map(|l| start - l.0);
        let mut kept = Vec::new();
        let mut interfaces = Vec::new();
        for (index, name, row) in rows {
            let before = last.as_ref().and_then(|l| l.1.iter().find(|(k, _)| k.0 == index && k.1 == name)).map(|(_, r)| r);
            let mut rates = Value::Null;
            let reason = match (before, window) {
                (None, _) | (_, None) => Some("warmup"),
                (_, Some(w)) if w <= 0 => Some("invalid_interval"),
                (_, Some(w)) if self.max_gap.map_or(false, |g| w > g) => Some("gap"),
                (Some(b), _) if row.iter().zip(b).any(|(a, b)| a < b) => Some("counter_regressed"),
                (Some(b), Some(w)) => {
                    let d: Vec<u128> = row.iter().zip(b).map(|(a, b)| (a - b) as u128).collect();
                    let mut r = Value::obj();
                    r.set("rx_bytes_per_s", rate(d[0], w).into());
                    r.set("tx_bytes_per_s", rate(d[4], w).into());
                    r.set("rx_packets_per_s", rate(d[1], w).into());
                    r.set("tx_packets_per_s", rate(d[5], w).into());
                    rates = r;
                    None
                }
            };
            let dir = |v: &[u64]| {
                let mut o = Value::obj();
                for (k, x) in COUNTERS.iter().zip(v) {
                    o.set(k, (*x).into());
                }
                o
            };
            let mut v = Value::obj();
            v.set("name", name.as_str().into());
            v.set("ifindex", (index as i64).into());
            v.set("rx", dir(&row[..4]));
            v.set("tx", dir(&row[4..]));
            v.set("rates", rates);
            v.set("window_ms", window_ms(window, reason));
            v.set("reason", reason_value(reason));
            interfaces.push(group.got(v));
            kept.push(((index, name), row));
        }
        self.last = Some((start, kept));
        (Network::output(interfaces), Some((start, end)))
    }
}
