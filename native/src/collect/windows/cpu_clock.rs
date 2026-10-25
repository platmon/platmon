//! CPU clocks on Windows: each processor's "% Processor Performance" (PDH, by the counter's English name)
//! times its base clock (~MHz in the registry), the speed Task Manager shows. The share is an average over
//! the time since the last collection, so it moves with turbo and power saving; processor group 0 only.
use super::registry_dword;
use crate::metadata::{clock, source, Group, Slot};
use std::collections::BTreeMap;
use windows_sys::Win32::System::Performance::{PdhAddEnglishCounterW, PdhCloseQuery, PdhCollectQueryData,
                                              PdhGetFormattedCounterArrayW, PdhOpenQueryW, PDH_CSTATUS_NEW_DATA,
                                              PDH_CSTATUS_NO_COUNTER, PDH_CSTATUS_NO_OBJECT, PDH_CSTATUS_VALID_DATA,
                                              PDH_FMT_COUNTERVALUE_ITEM_W, PDH_FMT_DOUBLE, PDH_HCOUNTER, PDH_HQUERY,
                                              PDH_MORE_DATA};

const COUNTER: &str = "\\Processor Information(*)\\% Processor Performance";
/// PDH_FMT_NOCAP100 (pdh.h; windows-sys leaves it out): a turbo clock reads above 100 %.
const NOCAP100: u32 = 0x8000;

/// PDH's open query, or why it could not be opened.
pub struct Clocks {
    pdh: Result<(PDH_HQUERY, PDH_HCOUNTER), (&'static str, String)>,
    /// MHz by CPU id, read on first use
    base: BTreeMap<i64, Option<i64>>,
}

// the query's handles may be used from any thread, one at a time
unsafe impl Send for Clocks {}

impl Drop for Clocks {
    fn drop(&mut self) {
        if let Ok((q, _)) = self.pdh {
            unsafe { PdhCloseQuery(q) };
        }
    }
}

fn reason(status: u32) -> &'static str {
    if status == PDH_CSTATUS_NO_OBJECT || status == PDH_CSTATUS_NO_COUNTER { "not_exposed" } else { "io_error" }
}

impl Clocks {
    /// Opens the query and takes its first reading, so the first collection has an average.
    pub fn new() -> Clocks {
        let mut q: PDH_HQUERY = std::ptr::null_mut();
        let mut c: PDH_HCOUNTER = std::ptr::null_mut();
        let status = unsafe { PdhOpenQueryW(std::ptr::null(), 0, &mut q) };
        let pdh = if status != 0 {
            Err((reason(status), format!("PdhOpenQueryW: {:#x}", status)))
        } else {
            let path: Vec<u16> = COUNTER.encode_utf16().chain(Some(0)).collect();
            match unsafe { PdhAddEnglishCounterW(q, path.as_ptr(), 0, &mut c) } {
                0 => {
                    unsafe { PdhCollectQueryData(q) };
                    Ok((q, c))
                }
                status => {
                    unsafe { PdhCloseQuery(q) };
                    Err((reason(status), format!("{}: {:#x}", COUNTER, status)))
                }
            }
        };
        Clocks { pdh, base: BTreeMap::new() }
    }

    /// {group 0 CPU number: % of the base clock} from the counter's instances ("0,3" is CPU 3 of group 0).
    fn shares(counter: PDH_HCOUNTER) -> Result<BTreeMap<i64, f64>, u32> {
        let mut buf: Vec<u64> = Vec::new();
        for _ in 0..4 { // more instances can appear between the size and the read
            let (mut size, mut count) = ((buf.len() * 8) as u32, 0u32);
            let status = unsafe { PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE | NOCAP100, &mut size, &mut count,
                                                               buf.as_mut_ptr().cast()) };
            if status == PDH_MORE_DATA {
                buf.resize((size as usize).div_ceil(8), 0);
                continue;
            }
            if status != 0 {
                return Err(status);
            }
            let items = unsafe { std::slice::from_raw_parts(buf.as_ptr().cast::<PDH_FMT_COUNTERVALUE_ITEM_W>(), count as usize) };
            let mut out = BTreeMap::new();
            for item in items {
                let name = if item.szName.is_null() { String::new() } else { unsafe {
                    let n = (0..).take_while(|i| *item.szName.add(*i) != 0).count();
                    String::from_utf16_lossy(std::slice::from_raw_parts(item.szName, n))
                } };
                let cpu = name.strip_prefix("0,").and_then(|n| n.parse::<i64>().ok());
                let valid = [PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA].contains(&item.FmtValue.CStatus);
                if let (Some(cpu), true) = (cpu, valid) {
                    out.insert(cpu, unsafe { item.FmtValue.Anonymous.doubleValue });
                }
            }
            return Ok(out);
        }
        Err(PDH_MORE_DATA)
    }

    /// [(id, Hz)] for the CPUs asked for, the reads noted in g.
    pub fn sample(&mut self, ids: &[i64], g: &mut Group) -> Vec<(i64, Option<i64>)> {
        if ids.is_empty() {
            g.note("cpu", "no_data", None);
        }
        let start = clock();
        let read = match &self.pdh {
            Ok((q, c)) => match unsafe { PdhCollectQueryData(*q) } {
                0 => Clocks::shares(*c).map_err(|s| (reason(s), format!("{}: {:#x}", COUNTER, s))),
                s => Err((reason(s), format!("PdhCollectQueryData: {:#x}", s))),
            },
            Err(e) => Err(e.clone()),
        };
        let span = Some((start, clock()));
        let shares = match read {
            Ok(shares) => Some(shares),
            Err((reason, detail)) => {
                if !ids.is_empty() {
                    g.note("cpu", reason, Some(detail));
                }
                None
            }
        };
        let mut out = Vec::new();
        for id in ids {
            let base = *self.base.entry(*id).or_insert_with(|| {
                registry_dword(&format!("HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\{}", id), "~MHz").map(i64::from)
            });
            let target = format!("cpu{}", id);
            let hz = match (base.filter(|m| *m > 0), shares.as_ref().map(|s| s.get(id))) {
                (Some(mhz), Some(Some(share))) => Some(g.got((share * mhz as f64 / 100.0).round() as i64 * 1_000_000)),
                (None, _) => { g.note(&target, "not_exposed", None); None }
                (_, Some(None)) => { g.note(&target, "no_data", None); None } // no current value from PDH
                (_, None) => None, // the read failed, noted above
            };
            g.sensor(Slot::Cpu(*id), Some(source("pdh", "hertz", "device_channel",
                vec![("cpu", (*id).into()), ("attr", "% Processor Performance".into())])), hz.and(span));
            out.push((*id, hz));
        }
        out
    }
}
