//! The processes slow group: the busiest processes by CPU and by memory, from /proc/<pid>/stat, and the
//! host's process counts. Contract: docs/reference/processes.md; design and measurements:
//! docs/development/processes-design.md.
//!
//! One observation reads every process's stat (nothing else per process) without allocating per process:
//! the listing is getdents64 into a reused buffer (sysfs::pids), the parse keeps byte offsets, names are
//! kept inline, and the vectors and maps are reused. The cgroup is read for the selected rows only.
//! Never read: command lines, environment, owners, per-process I/O or PSS.
use crate::collect::linux::sysfs;
use crate::json::{round_to, Value};
use crate::metadata::Group;
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};
use super::obj;

/// The PID namespace of the initial process (PROC_PID_INIT_INO): the host's, as /proc/self/ns/pid names it.
const INIT_PID_NS: &str = "pid:[4026531836]";
/// PF_KTHREAD in the flags field of stat.
const PF_KTHREAD: u64 = 0x0020_0000;
const COMM: usize = 64; // bytes of a name kept; the kernel's are at most 63 (kernel threads), usually 15

/// A small multiplicative hash: the keys are (pid, start time) numbers chosen by the kernel, not by a
/// client, so SipHash's flooding protection buys nothing here and costs a third of the map's time.
#[derive(Default)]
struct Mix(u64);
impl Hasher for Mix {
    fn finish(&self) -> u64 { self.0 }
    fn write(&mut self, bytes: &[u8]) { for &b in bytes { self.write_u64(b as u64); } }
    fn write_u32(&mut self, x: u32) { self.write_u64(x as u64); }
    fn write_u64(&mut self, x: u64) { self.0 = (self.0.rotate_left(5) ^ x).wrapping_mul(0x517c_c1b7_2722_0a95); }
}
type Ticks = HashMap<(u32, u64), u64, BuildHasherDefault<Mix>>;

#[derive(Clone, Copy)]
struct Row {
    pid: u32,
    name: [u8; COMM],
    name_len: u8,
    state: u8,
    kernel: bool,
    ticks: u64,
    threads: u64,
    start: u64,
    rss_pages: u64,
    cpu: Option<f64>,
}

fn digits(b: &[u8]) -> Option<u64> {
    if b.is_empty() || b.len() > 20 { return None; }
    b.iter().try_fold(0u64, |v, &c| if c.is_ascii_digit() { v.checked_mul(10)?.checked_add((c - b'0') as u64) } else { None })
}

/// One /proc/<pid>/stat (proc(5)): the name is between the first '(' and the last ')' (it may hold spaces
/// and parentheses), then fields 3 on separated by single spaces. None when it is not that shape.
fn parse(pid: u32, b: &[u8]) -> Option<Row> {
    let open = b.iter().position(|&c| c == b'(')?;
    let close = b.iter().rposition(|&c| c == b')')?;
    if close < open { return None; }
    let name = &b[open + 1..close];
    let mut row = Row { pid, name: [0; COMM], name_len: name.len().min(COMM) as u8, state: 0, kernel: false, ticks: 0,
                        threads: 0, start: 0, rss_pages: 0, cpu: None };
    row.name[..row.name_len as usize].copy_from_slice(&name[..row.name_len as usize]);
    let rest = b.get(close + 1..)?;
    let rest = rest.strip_prefix(b" ")?;
    let rest = rest.strip_suffix(b"\n").unwrap_or(rest);
    for (i, tok) in rest.split(|&c| c == b' ').enumerate() {
        let field = i + 3;
        match field {
            3 => { if tok.len() != 1 || !tok[0].is_ascii_alphabetic() { return None; } row.state = tok[0]; }
            9 => row.kernel = digits(tok)? & PF_KTHREAD != 0,
            14 | 15 => row.ticks = row.ticks.checked_add(digits(tok)?)?,
            20 => row.threads = digits(tok)?,
            22 => row.start = digits(tok)?,
            24 => { row.rss_pages = digits(tok)?; return Some(row); }
            _ => {}
        }
    }
    None
}

/// The last component of the unified (cgroup v2) hierarchy's path, e.g. "ssh.service"; None for the root
/// ("/", also a container's own root), without a unified line (cgroup v1 only) or when unreadable.
fn unit(cgroup: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(cgroup).ok()?;
    let path = text.lines().find_map(|l| l.strip_prefix("0::"))?;
    let last = path.trim_end_matches('/').rsplit('/').next()?;
    (!last.is_empty()).then(|| last.chars().take(256).collect())
}

/// VmRSS of /proc/<pid>/status in bytes; None without the line (kernel threads) or when it is not a number.
fn vm_rss(status: &[u8]) -> Option<u64> {
    let text = std::str::from_utf8(status).ok()?;
    let kb = text.lines().find_map(|l| l.strip_prefix("VmRSS:"))?.trim().strip_suffix("kB")?.trim();
    digits(kb.as_bytes())?.checked_mul(1024)
}

/// "hidepid=…" of the /proc mount this process sees, when it hides other users' processes.
fn hidepid(mountinfo: &str) -> Option<String> {
    mountinfo.lines().find_map(|l| {
        let (left, right) = l.split_once(" - ")?;
        if left.split(' ').nth(4)? != "/proc" || right.split(' ').next()? != "proc" { return None; }
        right.split(' ').nth(2)?.split(',').find_map(|o| o.strip_prefix("hidepid="))
            .filter(|v| !matches!(*v, "0" | "off")).map(|v| format!("hidepid={}", v))
    })
}

/// The reader's state between observations: the previous observation's ticks per process and its time,
/// and buffers reused from one observation to the next. prev holds only the processes the last
/// observation listed (next is filled and swapped in each time), so it cannot grow with process churn.
pub struct Processes {
    top: usize,
    hz: f64,
    page: u64,
    prev: Ticks,
    next: Ticks,
    prev_ns: Option<i64>,
    pids: Vec<u32>,
    rows: Vec<Row>,
    order: Vec<usize>,
    pick: Vec<usize>,
}

impl Processes {
    pub fn new(top: usize) -> Processes {
        let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
        let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
        Processes { top, hz: if hz > 0 { hz as f64 } else { 100.0 }, page: if page > 0 { page as u64 } else { 4096 },
                    prev: Ticks::default(), next: Ticks::default(), prev_ns: None, pids: Vec::new(), rows: Vec::new(),
                    order: Vec::new(), pick: Vec::new() }
    }

    /// One observation: the group's data; problems are noted in g.
    pub fn observe(&mut self, g: &mut Group) -> Value {
        let now_ns = crate::metadata::clock();
        let scope = match std::fs::read_link(&*sysfs::at("/proc/self/ns/pid")) {
            Ok(l) if l.to_str() == Some(INIT_PID_NS) => Value::from("host"),
            Ok(_) => Value::from("container"),
            Err(_) => Value::Null,
        };
        if let Ok(text) = sysfs::read_utf8("/proc/self/mountinfo") {
            if let Some(h) = hidepid(&text) {
                g.note("/proc", "permission_denied", Some(format!("/proc is mounted with {}: only processes this user may see are listed", h)));
            }
        }
        if let Err(e) = sysfs::pids("/proc", &mut self.pids) {
            g.note("/proc", sysfs::reason_of(&e), Some(format!("/proc: {}", e)));
            self.prev.clear();
            self.prev_ns = None;
            return Value::Null;
        }
        let uptime = sysfs::read_utf8("/proc/uptime").ok()
            .and_then(|t| t.split_whitespace().next().and_then(|s| s.parse::<f64>().ok()));
        self.rows.clear();
        let mut path = String::with_capacity(32);
        for &pid in &self.pids {
            path.clear();
            use std::fmt::Write;
            let _ = write!(path, "/proc/{}/stat", pid);
            match sysfs::with_file(&path, |b| parse(pid, b)) {
                Ok(Some(row)) => self.rows.push(row),
                Ok(None) => g.note(&format!("{}/stat", pid), "invalid_data", Some(path.clone())),
                Err(e) if matches!(e.raw_os_error(), Some(libc::ENOENT) | Some(libc::ESRCH)) => {} // exited meanwhile
                Err(e) => g.note(&format!("{}/stat", pid), sysfs::reason_of(&e), Some(format!("{}: {}", path, e))),
            }
        }
        // CPU: ticks since the previous observation over the time since it; a process that observation did
        // not list: its ticks since it started over its age. None in the first observation.
        let elapsed = self.prev_ns.map(|p| (now_ns - p) as f64 / 1e9).filter(|s| *s > 0.0);
        self.next.clear();
        let (mut running, mut blocked, mut zombie, mut threads) = (0i64, 0i64, 0i64, 0u64);
        for r in self.rows.iter_mut() {
            if let Some(dt) = elapsed {
                r.cpu = Some(match self.prev.get(&(r.pid, r.start)) {
                    Some(before) => r.ticks.saturating_sub(*before) as f64 / self.hz / dt * 100.0,
                    None => {
                        let age = uptime.map(|u| u - r.start as f64 / self.hz).unwrap_or(dt);
                        r.ticks as f64 / self.hz / age.clamp(1.0 / self.hz, dt.max(1.0 / self.hz)) * 100.0
                    }
                });
            }
            self.next.insert((r.pid, r.start), r.ticks);
            match r.state { b'R' => running += 1, b'D' => blocked += 1, b'Z' => zombie += 1, _ => {} }
            threads += r.threads;
        }
        std::mem::swap(&mut self.prev, &mut self.next);
        self.prev_ns = Some(now_ns);
        let rows = &self.rows;
        let by_cpu = |&a: &usize, &b: &usize| {
            let (x, y) = (&rows[a], &rows[b]);
            y.cpu.unwrap_or(0.0).total_cmp(&x.cpu.unwrap_or(0.0)).then(y.rss_pages.cmp(&x.rss_pages)).then(x.pid.cmp(&y.pid))
        };
        let by_rss = |&a: &usize, &b: &usize| {
            let (x, y) = (&rows[a], &rows[b]);
            y.rss_pages.cmp(&x.rss_pages).then(x.pid.cmp(&y.pid))
        };
        self.pick.clear();
        let k = self.top.min(rows.len());
        if k > 0 {
            self.order.clear();
            self.order.extend(0..rows.len());
            self.order.select_nth_unstable_by(k - 1, by_cpu);
            self.pick.extend_from_slice(&self.order[..k]);
            self.order.select_nth_unstable_by(k - 1, by_rss);
            for j in 0..k {
                let c = self.order[j];
                if !self.pick.contains(&c) { self.pick.push(c); }
            }
            self.pick.sort_unstable_by(by_cpu);
        }
        let mut list = Vec::with_capacity(self.pick.len());
        for &c in &self.pick {
            let r = &rows[c];
            use std::fmt::Write;
            // stat's rss is the kernel's approximate counter (off by megabytes on recent kernels, which batch
            // it per CPU); status's VmRSS is the exact sum, as ps shows. Ranked by the first, shown as the second.
            path.clear();
            let _ = write!(path, "/proc/{}/status", r.pid);
            let rss = sysfs::with_file(&path, vm_rss).ok().flatten().unwrap_or(r.rss_pages.saturating_mul(self.page));
            path.clear();
            let _ = write!(path, "/proc/{}/cgroup", r.pid);
            let unit = match sysfs::with_file(&path, unit) {
                Ok(u) => u.map(Value::from).unwrap_or(Value::Null),
                Err(e) if matches!(e.raw_os_error(), Some(libc::ENOENT) | Some(libc::ESRCH)) => Value::Null,
                Err(e) => { g.note(&format!("{}/cgroup", r.pid), sysfs::reason_of(&e), Some(format!("{}: {}", path, e))); Value::Null }
            };
            list.push(obj(vec![
                ("pid", (r.pid as i64).into()),
                ("name", String::from_utf8_lossy(&r.name[..r.name_len as usize]).into_owned().into()),
                ("kernel", Value::Bool(r.kernel)),
                ("state", ((r.state as char).to_string()).into()),
                ("cpu_percent", r.cpu.map(|c| Value::from(round_to(c, 1))).unwrap_or(Value::Null)),
                ("rss_bytes", (rss as i64).into()),
                ("threads", (r.threads as i64).into()),
                ("unit", unit),
            ]));
        }
        g.got(());
        obj(vec![
            ("scope", scope),
            ("total", (rows.len() as i64).into()),
            ("running", running.into()),
            ("blocked", blocked.into()),
            ("zombie", zombie.into()),
            ("threads", (threads as i64).into()),
            ("top", (self.top as i64).into()),
            ("processes", Value::Arr(list)),
        ])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collect::linux::sysfs::fake::Root;

    const S: i64 = 1_000_000_000;

    /// A stat line as the kernel writes it: fields 1-52, with the ones this reader uses set.
    fn stat(pid: u32, comm: &str, state: char, flags: u64, utime: u64, stime: u64, threads: u64, start: u64, rss: u64) -> String {
        let mut f: Vec<String> = (3..=52).map(|_| "0".to_string()).collect();
        let set = |f: &mut Vec<String>, n: usize, v: String| f[n - 3] = v;
        set(&mut f, 3, state.to_string());
        set(&mut f, 9, flags.to_string());
        set(&mut f, 14, utime.to_string());
        set(&mut f, 15, stime.to_string());
        set(&mut f, 20, threads.to_string());
        set(&mut f, 22, start.to_string());
        set(&mut f, 24, rss.to_string());
        format!("{} ({}) {}\n", pid, comm, f.join(" "))
    }

    fn proc_tree(root: &Root, procs: &[(u32, String, &str)]) {
        root.put("/proc/uptime", "1000.00 4000.00\n");
        root.put("/proc/self/mountinfo", "22 28 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw\n");
        root.link("/proc/self/ns/pid", INIT_PID_NS);
        for (pid, line, cgroup) in procs {
            root.put(&format!("/proc/{}/stat", pid), line);
            root.put(&format!("/proc/{}/cgroup", pid), cgroup);
        }
    }

    fn names(data: &Value) -> Vec<String> {
        match data.get("processes") {
            Some(Value::Arr(a)) => a.iter().map(|p| match p.get("name") { Some(Value::Str(s)) => s.clone(), _ => String::new() }).collect(),
            _ => vec![],
        }
    }

    fn field<'a>(data: &'a Value, name: &str, key: &str) -> &'a Value {
        let Some(Value::Arr(a)) = data.get("processes") else { panic!("no processes") };
        a.iter().find(|p| p.get("name") == Some(&Value::from(name))).and_then(|p| p.get(key)).unwrap()
    }

    #[test]
    fn parses_names_with_spaces_and_parentheses() {
        let line = stat(7, "a) (b", 'S', 0, 3, 4, 2, 50, 10);
        let r = parse(7, line.as_bytes()).unwrap();
        assert_eq!(&r.name[..r.name_len as usize], b"a) (b");
        assert_eq!((r.state, r.ticks, r.threads, r.start, r.rss_pages, r.kernel), (b'S', 7, 2, 50, 10, false));
        assert!(parse(7, stat(7, "k", 'I', PF_KTHREAD, 0, 0, 1, 2, 0).as_bytes()).unwrap().kernel);
        for bad in ["7 (x) S 1 2", "7 x S 0", "", "7 (x)", "7 (x) SS 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"] {
            assert!(parse(7, bad.as_bytes()).is_none(), "{:?}", bad);
        }
        let mut line = stat(7, "x", 'S', 0, 1, 1, 1, 1, 1);
        line = line.replacen(" 1 1 ", " 99999999999999999999999 1 ", 1); // utime overflows u64
        assert!(parse(7, line.as_bytes()).is_none());
    }

    #[test]
    fn units_and_hidepid() {
        assert_eq!(unit(b"0::/system.slice/ssh.service\n"), Some("ssh.service".into()));
        assert_eq!(unit(b"12:memory:/x\n0::/user.slice/user-1000.slice/session-3.scope\n"), Some("session-3.scope".into()));
        assert_eq!(unit(b"0::/\n"), None);
        assert_eq!(unit(b"4:cpu:/docker/abc\n"), None);
        assert_eq!(vm_rss(b"Name:\tx\nVmHWM:\t  9 kB\nVmRSS:\t    2000 kB\nRssAnon: 1 kB\n"), Some(2_048_000));
        assert_eq!(vm_rss(b"Name:\tkworker/0:1\nState:\tI (idle)\n"), None);
        assert_eq!(vm_rss(b"VmRSS:\t  x kB\n"), None);
        assert_eq!(hidepid("22 28 0:21 / /proc rw,relatime - proc proc rw,hidepid=invisible\n"), Some("hidepid=invisible".into()));
        assert_eq!(hidepid("22 28 0:21 / /proc rw,relatime - proc proc rw,hidepid=0\n"), None);
        assert_eq!(hidepid("22 28 0:21 / /proc rw shared:1 - proc proc rw\n"), None);
        assert_eq!(hidepid("30 28 0:40 / /host/proc rw - proc proc rw,hidepid=2\n"), None);
    }

    #[test]
    fn first_observation_has_no_cpu_then_deltas_and_selection() {
        let root = Root::new(10 * S);
        proc_tree(&root, &[
            (1, stat(1, "init", 'S', 0, 100, 50, 1, 1, 3000), "0::/init.scope\n"),
            (20, stat(20, "kworker/0:1", 'I', PF_KTHREAD, 0, 10, 1, 5, 0), "0::/\n"),
            (300, stat(300, "busy loop", 'R', 0, 1000, 0, 4, 90000, 500), "0::/system.slice/busy.service\n"),
            (301, stat(301, "big", 'S', 0, 0, 0, 2, 90000, 900000), "0::/system.slice/big.service\n"),
            (302, stat(302, "stuck", 'D', 0, 0, 0, 1, 90000, 10), "0::/system.slice/disk.service\n"),
            (303, stat(303, "gone", 'Z', 0, 0, 0, 1, 90000, 0), "0::/system.slice/gone.service\n")]);
        let mut p = Processes::new(2);
        let mut g = Group::new("processes", false);
        let first = p.observe(&mut g);
        assert_eq!(g.status().get("state"), Some(&Value::from("ok")));
        assert_eq!(first.get("scope"), Some(&Value::from("host")));
        for (k, v) in [("total", 6), ("running", 1), ("blocked", 1), ("zombie", 1), ("threads", 10), ("top", 2)] {
            assert_eq!(first.get(k), Some(&Value::Int(v)), "{}", k);
        }
        assert_eq!(*field(&first, "big", "cpu_percent"), Value::Null);
        // no CPU yet: ranked by memory for both rankings, so two rows
        assert_eq!(names(&first), ["big", "init"]);
        // 5 s later: busy loop used 4 s of CPU (400 ticks at 100 Hz), init 0.5 s; a new process appeared
        root.at(15 * S);
        root.put("/proc/300/stat", stat(300, "busy loop", 'R', 0, 1400, 0, 4, 90000, 500));
        root.put("/proc/1/stat", stat(1, "init", 'S', 0, 140, 60, 1, 1, 3000));
        root.put("/proc/400/stat", stat(400, "new", 'R', 0, 50, 50, 1, 99900, 100)); // started 1 s ago, 1 s of CPU
        root.put("/proc/400/cgroup", "0::/user.slice/x.scope\n");
        root.remove("/proc/303");
        let mut g = Group::new("processes", false);
        let second = p.observe(&mut g);
        let hz = p.hz;
        assert_eq!(hz, 100.0, "the tests assume USER_HZ 100");
        assert_eq!(names(&second), ["new", "busy loop", "init", "big"]); // top 2 by CPU, then top 2 by RSS, by CPU
        assert_eq!(*field(&second, "busy loop", "cpu_percent"), Value::Float(80.0));
        assert_eq!(*field(&second, "init", "cpu_percent"), Value::Float(10.0));
        assert_eq!(*field(&second, "new", "cpu_percent"), Value::Float(100.0));
        assert_eq!(*field(&second, "busy loop", "unit"), Value::from("busy.service"));
        assert_eq!(*field(&second, "big", "rss_bytes"), Value::Int(900000 * p.page as i64)); // no status: stat's
        root.put("/proc/301/status", "Name:\tbig\nVmRSS:\t 3700000 kB\n");
        root.at(20 * S);
        let third = p.observe(&mut Group::new("processes", false));
        assert_eq!(*field(&third, "big", "rss_bytes"), Value::Int(3_700_000 * 1024)); // status's exact VmRSS
        assert_eq!(second.get("total"), Some(&Value::Int(6)));
        assert_eq!(second.get("zombie"), Some(&Value::Int(0)));
    }

    #[test]
    fn pid_reuse_is_a_new_process() {
        let root = Root::new(10 * S);
        proc_tree(&root, &[(50, stat(50, "old", 'S', 0, 5000, 0, 1, 10, 1), "0::/a.service\n")]);
        let mut p = Processes::new(5);
        p.observe(&mut Group::new("processes", false));
        root.at(15 * S);
        // the same pid, a later start time and fewer ticks: not a negative delta from "old"
        root.put("/proc/50/stat", stat(50, "reused", 'S', 0, 20, 0, 1, 99800, 1)); // 2 s old, 0.2 s of CPU
        let data = p.observe(&mut Group::new("processes", false));
        assert_eq!(*field(&data, "reused", "cpu_percent"), Value::Float(10.0));
    }

    #[test]
    fn exits_while_reading_are_not_problems_but_other_errors_are() {
        let root = Root::new(10 * S);
        proc_tree(&root, &[(1, stat(1, "init", 'S', 0, 1, 1, 1, 1, 1), "0::/init.scope\n"),
                           (2, stat(2, "two", 'S', 0, 1, 1, 1, 1, 1), "0::/two.scope\n")]);
        crate::collect::linux::sysfs::fake::answers("/proc/2/stat", vec![Err(libc::ESRCH)]);
        let mut p = Processes::new(5);
        let mut g = Group::new("processes", false);
        let data = p.observe(&mut g);
        assert_eq!(g.status().get("state"), Some(&Value::from("ok")));
        assert_eq!(names(&data), ["init"]);
        crate::collect::linux::sysfs::fake::answers("/proc/2/stat", vec![Err(libc::EACCES)]);
        crate::collect::linux::sysfs::fake::answers("/proc/1/stat", vec![Ok("garbage")]);
        let mut g = Group::new("processes", false);
        p.observe(&mut g);
        let status = g.status();
        assert_eq!(status.get("state"), Some(&Value::from("partial")));
        let Some(Value::Arr(issues)) = status.get("issues") else { panic!() };
        let mut reasons: Vec<String> = issues.iter().map(|i| format!("{:?}", i.get("reason").unwrap())).collect();
        reasons.sort(); // the listing's order is the directory's
        assert_eq!(reasons, [format!("{:?}", Value::from("invalid_data")), format!("{:?}", Value::from("permission_denied"))]);
    }

    #[test]
    fn unlistable_proc_is_an_error_and_resets_the_deltas() {
        let root = Root::new(10 * S);
        proc_tree(&root, &[(1, stat(1, "init", 'S', 0, 1, 1, 1, 1, 1), "0::/init.scope\n")]);
        let mut p = Processes::new(5);
        p.observe(&mut Group::new("processes", false));
        crate::collect::linux::sysfs::fake::unlistable("/proc", libc::EACCES);
        let mut g = Group::new("processes", false);
        assert_eq!(p.observe(&mut g), Value::Null);
        assert_eq!(g.status().get("state"), Some(&Value::from("error")));
        crate::collect::linux::sysfs::fake::listable("/proc");
        root.at(20 * S);
        let data = p.observe(&mut Group::new("processes", false));
        assert_eq!(*field(&data, "init", "cpu_percent"), Value::Null); // a first observation again
    }

    #[test]
    fn container_scope_and_hidepid() {
        let root = Root::new(10 * S);
        proc_tree(&root, &[(1, stat(1, "sh", 'S', 0, 1, 1, 1, 1, 1), "0::/\n")]);
        root.remove("/proc/self/ns/pid");
        root.link("/proc/self/ns/pid", "pid:[4026532999]");
        root.put("/proc/self/mountinfo", "22 28 0:21 / /proc rw - proc proc rw,hidepid=invisible\n");
        let mut g = Group::new("processes", false);
        let data = Processes::new(5).observe(&mut g);
        assert_eq!(data.get("scope"), Some(&Value::from("container")));
        assert_eq!(*field(&data, "sh", "unit"), Value::Null);
        assert_eq!(g.status().get("state"), Some(&Value::from("partial")));
    }

    #[test]
    fn previous_ticks_hold_only_listed_processes() {
        let root = Root::new(10 * S);
        proc_tree(&root, &[(1, stat(1, "init", 'S', 0, 1, 1, 1, 1, 1), "0::/init.scope\n")]);
        let mut p = Processes::new(5);
        for n in 0..50u32 {
            let pid = 1000 + n;
            root.put(&format!("/proc/{}/stat", pid), stat(pid, "short", 'R', 0, 1, 1, 1, 99000, 1));
            root.at((11 + n as i64) * S);
            p.observe(&mut Group::new("processes", false));
            root.remove(&format!("/proc/{}", pid));
            assert!(p.prev.len() <= 2, "{}", p.prev.len());
        }
    }

    #[test]
    fn the_answer_never_names_command_lines_users_or_environment() {
        let root = Root::new(10 * S);
        proc_tree(&root, &[(1, stat(1, "init", 'S', 0, 1, 1, 1, 1, 1), "0::/init.scope\n")]);
        root.put("/proc/1/cmdline", "init\0--password=secret\0");
        root.put("/proc/1/environ", "TOKEN=secret\0");
        let data = Processes::new(5).observe(&mut Group::new("processes", false));
        let text = data.to_py_compact();
        assert!(!text.contains("secret"), "{}", text);
        let Some(Value::Arr(rows)) = data.get("processes") else { panic!() };
        let Value::Obj(items) = &rows[0] else { panic!() };
        let keys: Vec<&str> = items.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(keys, ["pid", "name", "kernel", "state", "cpu_percent", "rss_bytes", "threads", "unit"]);
    }
}
