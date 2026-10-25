//! The GPU on Windows: the first hardware display adapter, in D3DKMTEnumAdapters2's order. usage is Task
//! Manager's: its busiest engine's "Utilization Percentage" (PDH "GPU Engine", by the counter's English
//! name), the processes' shares of one engine added up, an average since the last collection; freq and
//! max_freq are its 3D engine's clocks from the node performance data (D3DKMT_NODE_PERFDATA), where the
//! driver reports them.
use super::temperature::{adapters, query, until_nul, SOFTWARE_DEVICE};
use crate::json::{round_to, Value};
use crate::metadata::{clock, source, Group, Slot};
use windows_sys::Wdk::Graphics::Direct3D::{D3DKMTCloseAdapter, D3DKMT_ADAPTERREGISTRYINFO, D3DKMT_ADAPTERTYPE,
                                           D3DKMT_CLOSEADAPTER, D3DKMT_NODEMETADATA, D3DKMT_NODE_PERFDATA,
                                           DXGK_ENGINE_TYPE_3D, KMTQAITYPE_ADAPTERREGISTRYINFO, KMTQAITYPE_ADAPTERTYPE,
                                           KMTQAITYPE_NODEMETADATA, KMTQAITYPE_NODEPERFDATA};
use windows_sys::Win32::Foundation::{STATUS_ACCESS_DENIED, STATUS_INVALID_PARAMETER, STATUS_NOT_IMPLEMENTED,
                                     STATUS_NOT_SUPPORTED};
use windows_sys::Win32::System::Performance::{PdhAddEnglishCounterW, PdhCloseQuery, PdhCollectQueryData,
                                              PdhGetFormattedCounterArrayW, PdhOpenQueryW, PDH_CSTATUS_NEW_DATA,
                                              PDH_CSTATUS_NO_COUNTER, PDH_CSTATUS_NO_OBJECT, PDH_CSTATUS_VALID_DATA,
                                              PDH_FMT_COUNTERVALUE_ITEM_W, PDH_FMT_DOUBLE, PDH_HCOUNTER, PDH_HQUERY,
                                              PDH_MORE_DATA};

const COUNTER: &str = "\\GPU Engine(*)\\Utilization Percentage";
/// PDH_FMT_NOCAP100 (pdh.h): the shares of one engine are added up before they are capped.
const NOCAP100: u32 = 0x8000;
/// The engines an adapter's node ordinals are tried up to.
const MAX_NODES: u32 = 64;

/// PDH's open query, or why it could not be opened.
pub struct Gpu {
    pdh: Result<(PDH_HQUERY, PDH_HCOUNTER), (&'static str, String)>,
    /// the adapters (LUID tags) that have reported their clocks at least once
    clocked: Vec<String>,
}

// the query's handles may be used from any thread, one at a time
unsafe impl Send for Gpu {}

impl Drop for Gpu {
    fn drop(&mut self) {
        if let Ok((q, _)) = self.pdh {
            unsafe { PdhCloseQuery(q) };
        }
    }
}

fn pdh_reason(status: u32) -> &'static str {
    if status == PDH_CSTATUS_NO_OBJECT || status == PDH_CSTATUS_NO_COUNTER { "not_exposed" } else { "io_error" }
}

fn d3dkmt_reason(status: i32) -> &'static str {
    match status {
        STATUS_INVALID_PARAMETER | STATUS_NOT_SUPPORTED | STATUS_NOT_IMPLEMENTED => "not_exposed",
        STATUS_ACCESS_DENIED => "permission_denied",
        _ => "io_error",
    }
}

/// The "luid_0x…_0x…" an engine instance of the adapter with this LUID names.
fn luid_tag(high: i32, low: u32) -> String {
    format!("luid_0x{:08X}_0x{:08X}", high as u32, low)
}

/// Task Manager's usage from "GPU Engine" instances (name, %): for the adapter tagged luid, each engine's
/// ("phys_P_eng_E") shares added up, the busiest engine's, at most 100; None if the adapter has none.
pub fn busiest(items: &[(String, f64)], luid: &str) -> Option<f64> {
    let mut engines: Vec<(&str, f64)> = Vec::new();
    for (name, share) in items {
        let Some(rest) = name.split_once(luid).map(|(_, r)| r) else { continue };
        let engine = rest.split_once("_engtype_").map_or(rest, |(e, _)| e);
        match engines.iter_mut().find(|(e, _)| *e == engine) {
            Some(slot) => slot.1 += share,
            None => engines.push((engine, *share)),
        }
    }
    engines.iter().map(|(_, s)| *s).reduce(f64::max).map(|s| round_to(s.clamp(0.0, 100.0), 1))
}

impl Gpu {
    /// Opens the query and takes its first reading, so the first collection has an average.
    pub fn new() -> Gpu {
        let mut q: PDH_HQUERY = std::ptr::null_mut();
        let mut c: PDH_HCOUNTER = std::ptr::null_mut();
        let status = unsafe { PdhOpenQueryW(std::ptr::null(), 0, &mut q) };
        let pdh = if status != 0 {
            Err((pdh_reason(status), format!("PdhOpenQueryW: {:#x}", status)))
        } else {
            let path: Vec<u16> = COUNTER.encode_utf16().chain(Some(0)).collect();
            match unsafe { PdhAddEnglishCounterW(q, path.as_ptr(), 0, &mut c) } {
                0 => {
                    unsafe { PdhCollectQueryData(q) };
                    Ok((q, c))
                }
                status => {
                    unsafe { PdhCloseQuery(q) };
                    Err((pdh_reason(status), format!("{}: {:#x}", COUNTER, status)))
                }
            }
        };
        Gpu { pdh, clocked: Vec::new() }
    }

    /// The engine instances with a current value: [(instance name, %)].
    fn shares(&self) -> Result<Vec<(String, f64)>, (&'static str, String)> {
        let (q, counter) = self.pdh.as_ref().map_err(Clone::clone)?;
        match unsafe { PdhCollectQueryData(*q) } {
            0 => {}
            s => return Err((pdh_reason(s), format!("PdhCollectQueryData: {:#x}", s))),
        }
        let mut buf: Vec<u64> = Vec::new();
        for _ in 0..4 { // more instances can appear between the size and the read
            let (mut size, mut count) = ((buf.len() * 8) as u32, 0u32);
            let status = unsafe { PdhGetFormattedCounterArrayW(*counter, PDH_FMT_DOUBLE | NOCAP100, &mut size, &mut count,
                                                               buf.as_mut_ptr().cast()) };
            if status == PDH_MORE_DATA {
                buf.resize((size as usize).div_ceil(8), 0);
                continue;
            }
            if status != 0 {
                return Err((pdh_reason(status), format!("{}: {:#x}", COUNTER, status)));
            }
            let items = unsafe { std::slice::from_raw_parts(buf.as_ptr().cast::<PDH_FMT_COUNTERVALUE_ITEM_W>(), count as usize) };
            return Ok(items.iter().filter(|i| [PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA].contains(&i.FmtValue.CStatus))
                .filter(|i| !i.szName.is_null())
                .map(|i| unsafe {
                    let n = (0..).take_while(|k| *i.szName.add(*k) != 0).count();
                    (String::from_utf16_lossy(std::slice::from_raw_parts(i.szName, n)), i.FmtValue.Anonymous.doubleValue)
                }).collect());
        }
        Err(("io_error", format!("{}: {:#x}", COUNTER, PDH_MORE_DATA)))
    }

    /// The gpu entry ({usage, freq, max_freq, driver, provider}), None without a hardware adapter.
    pub fn sample(&mut self, g: &mut Group) -> Option<Value> {
        let list = match adapters() {
            Ok(list) => list,
            Err(status) => {
                let reason = if status == STATUS_ACCESS_DENIED { "permission_denied" } else { "io_error" };
                g.note("adapters", reason, Some(format!("D3DKMTEnumAdapters2: NTSTATUS {:#x}", status)));
                return None;
            }
        };
        let hardware = list.iter().find(|a| {
            let mut kind = D3DKMT_ADAPTERTYPE::default();
            !(query(a.hAdapter, KMTQAITYPE_ADAPTERTYPE, &mut kind) >= 0 && unsafe { kind.Anonymous.Value } & SOFTWARE_DEVICE != 0)
        });
        let out = hardware.map(|a| {
            let mut info = D3DKMT_ADAPTERREGISTRYINFO::default();
            let name = if query(a.hAdapter, KMTQAITYPE_ADAPTERREGISTRYINFO, &mut info) >= 0 { until_nul(&info.AdapterString) } else { String::new() };
            let luid = luid_tag(a.AdapterLuid.HighPart, a.AdapterLuid.LowPart);

            let start = clock();
            let read = self.shares();
            let span = Some((start, clock()));
            let usage = match read {
                Ok(items) => busiest(&items, &luid).or(Some(0.0)), // no engine with work: idle
                Err((reason, detail)) => { g.note("usage", reason, Some(detail)); None }
            };
            g.sensor(Slot::Key("usage".into()), Some(source("pdh", "percent", "device_channel",
                vec![("adapter", luid.as_str().into()), ("attr", "Utilization Percentage".into())])), usage.and(span));
            let usage = usage.map(|u| g.got(u));

            // the 3D engine's node, else node 0
            let mut ordinal = 0u32;
            for n in 0..MAX_NODES {
                let mut meta = D3DKMT_NODEMETADATA { NodeOrdinalAndAdapterIndex: n, ..Default::default() };
                if query(a.hAdapter, KMTQAITYPE_NODEMETADATA, &mut meta) < 0 {
                    break;
                }
                if { meta.NodeData.EngineType } == DXGK_ENGINE_TYPE_3D {
                    ordinal = n;
                    break;
                }
            }
            let mut perf = D3DKMT_NODE_PERFDATA { NodeOrdinal: ordinal, PhysicalAdapterIndex: 0, ..Default::default() };
            let start = clock();
            let status = query(a.hAdapter, KMTQAITYPE_NODEPERFDATA, &mut perf);
            let span = Some((start, clock()));
            // both 0: no clocks from this driver, or none at this moment (Intel's while the GPU sleeps)
            let clocks = match status {
                s if s >= 0 && perf.MaxFrequency > 0 => {
                    if !self.clocked.contains(&luid) {
                        self.clocked.push(luid.clone());
                    }
                    Some((perf.Frequency as i64, perf.MaxFrequency as i64))
                }
                s if s >= 0 => {
                    g.note("freq", if self.clocked.contains(&luid) { "no_data" } else { "not_exposed" }, None);
                    None
                }
                s => {
                    let reason = d3dkmt_reason(s);
                    g.note("freq", reason, (reason == "io_error").then(|| format!("node performance data: NTSTATUS {:#x}", s)));
                    None
                }
            };
            for (key, attr) in [("freq", "Frequency"), ("max_freq", "MaxFrequency")] {
                g.sensor(Slot::Key(key.into()), Some(source("d3dkmt", "hertz", "device_channel",
                    vec![("adapter", luid.as_str().into()), ("node", (ordinal as i64).into()), ("attr", attr.into())])),
                    clocks.and(span));
            }
            let (freq, max_freq) = match clocks {
                Some((f, m)) => (Some(g.got(f)), Some(g.got(m))),
                None => (None, None),
            };
            let mut v = Value::obj();
            v.set("usage", usage.into());
            v.set("freq", freq.into());
            v.set("max_freq", max_freq.into());
            v.set("driver", (!name.is_empty()).then_some(name).into());
            v.set("provider", "pdh_gpu_engine".into());
            v
        });
        for a in &list {
            unsafe { D3DKMTCloseAdapter(&D3DKMT_CLOSEADAPTER { hAdapter: a.hAdapter }) };
        }
        if out.is_none() {
            g.note("gpu", "not_detected", None);
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn busiest_adds_up_one_engines_processes_and_takes_the_busiest_engine() {
        let tag = luid_tag(0, 0x15BDA);
        assert_eq!(tag, "luid_0x00000000_0x00015BDA");
        let items: Vec<(String, f64)> = [
            ("pid_1_luid_0x00000000_0x00015BDA_phys_0_eng_0_engtype_3D", 20.0),
            ("pid_2_luid_0x00000000_0x00015BDA_phys_0_eng_0_engtype_3D", 15.5),
            ("pid_1_luid_0x00000000_0x00015BDA_phys_0_eng_3_engtype_VideoDecode", 30.0),
            ("pid_3_luid_0x00000000_0x0000AAAA_phys_0_eng_0_engtype_3D", 99.0), // another adapter
        ].iter().map(|(n, v)| (n.to_string(), *v)).collect();
        assert_eq!(busiest(&items, &tag), Some(35.5));
        assert_eq!(busiest(&items, &luid_tag(0, 0xBBBB)), None);
        let over = vec![("pid_1_luid_0x00000000_0x00015BDA_phys_0_eng_0_engtype_3D".to_string(), 70.0),
                        ("pid_2_luid_0x00000000_0x00015BDA_phys_0_eng_0_engtype_3D".to_string(), 40.0)];
        assert_eq!(busiest(&over, &tag), Some(100.0));
    }
}
