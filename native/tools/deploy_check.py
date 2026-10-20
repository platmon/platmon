"""The deployed form of candidate 1 (native/CANDIDATE.md) on a host where native/tools/deploy/install.sh ran:
three units with the product unit's hardening (DynamicUser, ProtectSystem=strict, ProtectHome, NoNewPrivileges,
Restart=on-failure), one INI (/etc/platmon/platmon.ini with bind 127.0.0.1 and port 19797), one port. Only those
units are started, stopped or killed, through the sudo rules install.sh wrote; platmon.service is only read
(is-active, an answer on 9797). Memory and descriptors of a DynamicUser process come from the root `measure`
script; CPU, VmRSS and threads from /proc, which anyone may read. One JSON line per check or hour.

  python3 deploy_check.py checks --out OUT.jsonl            # install, swap, recovery, stopping (about 15 min)
  python3 deploy_check.py soak --out OUT.jsonl [--hours 24]  # the long run, platmon-native-candidate
  python3 deploy_check.py report OUT.jsonl [--settled-mib N]  # the judgement, set before the runs

checks, in order; a check passes with no problem in its line:
  ready         the three candidate units inactive, the port free, platmon.service active and answering
  python        the Python unit starts on the INI and becomes ready
  swap          starting the native unit stops the Python one (Conflicts=); ready on the same port; a new
                instance_id; the Python instance's cursor gets 409; /api/status the same but for its
                moment (the same INI); the INI unchanged
  restart       systemctl restart: ready, a new instance_id, 409 for the previous one
  crash         SIGKILL: Restart=on-failure brings it back (NRestarts + 1), a new instance_id, the old
                process gone
  failed_start  the port held by this tool: the unit cannot serve and keeps restarting; released, it serves
                again without anyone starting it
  rollback      starting the Python unit stops the native one; ready; 409 for the native instance; the INI
                unchanged
  stop_collect  stop at 7 offsets from a collection's start (0 to 750 ms): each stop within 2 s, Result
                success, no process of the unit left, the port free
  stop_replay   stop while 4 clients fetch replay pages: a page answers whole or the client sees an error,
                never a hang; then as above
  stop_nvsmi    the unit with the slow fake nvidia-smi first on PATH: stopped 3 times while the command runs
                (the unit has more than one process); then as above. On a host whose collector does not
                reach the fallback, recorded as not reached
  end           the candidate units inactive, the port free, platmon.service active and answering, the INI
                unchanged

soak, one line per hour (HOUR seconds), the native unit started once (and restarted once, at RESTART_AT):
  0 .. POLL       the Hub-like poller of service_ab.py (one node as platmon-hub's Fleet polls it)
  POLL ..         no client for DISCONNECT seconds: 480 (within the 600 s replay retention), or 720 in odd hours
                  (the oldest records expire: the first page's missing range is expected, nothing else)
                  then the reconnect from the poller's cursors, one round of 4 pollers fetching the same
                  backlog at once (service_ab.concurrent), 120 s with no client, a reading (settled)
                  then polling again until the next hour
Each hour: CPU, the poller's requests and errors, losses and walk problems, the collections it replayed
(steps between consecutive ids, gap_before), the reconnect and the concurrent round, the settled reading (PSS,
threads, descriptors, sockets, processes, what is retained), NRestarts and the unit's state, and this tool's
own VmRSS and threads."""
import argparse
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time

import service_ab as ab
import walk
import worker

PORT = 19797
NATIVE, FAKE, PYTHON = ("platmon-native-candidate.service", "platmon-native-candidate-fakesmi.service",
                        "platmon-python-candidate.service")
UNITS = (NATIVE, FAKE, PYTHON)
INI = "/etc/platmon-candidate/platmon.ini"
SYSTEMCTL = "/usr/bin/systemctl"
MOMENT = ("instance_id", "state", "ready", "sample", "last_attempt", "collecting_for_ms")  # /api/status, per answer
SOAK = {"hour": 3600, "poll": 2400, "disconnect": 480, "expire": 720, "settle": 120, "restart_at": 12,
        "concurrent_rounds": 1, "concurrent_pollers": 4, "reconnect_limit": 300}
QUICK = {"hour": 420, "poll": 120, "disconnect": 60, "expire": 60, "settle": 30, "restart_at": 1,
         "concurrent_rounds": 1, "concurrent_pollers": 4, "reconnect_limit": 60}


def sudo(*args):
    r = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def ctl(verb, unit):
    """start, stop or restart, as the sudo rule names it; (exit code, output, seconds)."""
    t = time.monotonic()
    code, out = sudo(SYSTEMCTL, verb, unit)
    return code, out, round(time.monotonic() - t, 3)


def show(unit, *props):
    out = subprocess.run([SYSTEMCTL, "show", "-p", ",".join(props), unit], capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def journal(unit, n=15):
    return subprocess.run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-monotonic"],
                          capture_output=True, text=True).stdout.splitlines()


def measure():
    """{unit: {pid, rss, pss, vmrss, threads, fds, sockets, processes}} for the running candidate units."""
    code, out = sudo("/usr/lib/platmon-candidate/measure")
    if code:
        raise OSError(f"measure: {out}")
    units, cur = {}, None
    for line in out.splitlines():
        t = line.split()
        if t[0] == "unit":
            cur = units[t[1] + ".service"] = {"pid": int(t[3])}
        elif t[0] in ("Rss:", "Pss:", "VmRSS:", "Threads:"):
            cur[t[0][:-1].lower()] = int(t[1])
        elif t[0] == "fds":
            cur["fds"], cur["sockets"] = int(t[1]), int(t[3])
        elif t[0] == "processes":
            cur["processes"] = int(t[1])
    return units


class UnitProc(ab.Proc):
    """service_ab.Proc for a unit's main process: smaps_rollup and descriptors through measure."""
    def __init__(self, unit, pid):
        super().__init__(pid)
        self.unit = unit

    def m(self):
        got = measure().get(self.unit)
        if not got or got["pid"] != self.pid:
            raise OSError(f"{self.unit}: pid {self.pid} is not its main process")
        return got

    def fds(self):
        return self.m()["fds"]

    def rollup(self):
        m = self.m()
        return {"Rss": m["rss"], "Pss": m["pss"]}


class VmRss(ab.Rss):
    """VmRSS samples only: PSS needs measure (sudo), read before and after the rounds, not every 50 ms."""
    def speed(self, every, pss=False):
        super().speed(every, False)


def procs(unit):
    try:
        with open(f"/sys/fs/cgroup/system.slice/{unit}/cgroup.procs") as f:
            return [int(x) for x in f.read().split()]
    except OSError:
        return []


def owned(uid):
    """Processes running as uid, or from the candidate's directory: what a stopped unit must not leave."""
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/status") as f:
                u = next(int(line.split()[1]) for line in f if line.startswith("Uid:"))
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except (OSError, StopIteration):
            continue
        if u == uid or "/usr/lib/platmon-candidate/" in cmd:
            out.append((int(pid), u, cmd[:120]))
    return out


def uid_of(pid):
    with open(f"/proc/{pid}/status") as f:
        return next(int(line.split()[1]) for line in f if line.startswith("Uid:"))


def port_free():
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", PORT))
        return False
    except OSError:
        return True
    finally:
        s.close()


def status(timeout=5):
    code, body, _ = ab.get(PORT, "/api/status", timeout)
    return json.loads(body) if code == 200 else None


def ready(timeout=20, other_than=None):
    """Seconds until /api/status says ready (with an instance_id other than other_than), and the status."""
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        try:
            s = status(2)
            if s and s.get("ready") and s["instance_id"] != other_than:
                return round(time.monotonic() - t, 2), s
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(0.1)
    return None, None


def old_cursor(instance):
    return ab.get(PORT, f"/api/samples?instance_id={instance}&after=0&limit=1")[0]


def production():
    active = subprocess.run([SYSTEMCTL, "is-active", "platmon.service"], capture_output=True, text=True).stdout.strip()
    try:
        code = ab.get(9797, "/api/status", 5)[0]
    except OSError as e:
        code = type(e).__name__
    return {"active": active, "answer": code}


def ini_sha():
    with open(INI, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def same_status(a, b):
    """The keys of /api/status that differ between two services on the same INI, but for the moment's."""
    keys = (set(a) | set(b)) - set(MOMENT)
    return sorted(k for k in keys if a.get(k) != b.get(k))


def stopped(unit, uid, took, limit=2.0):
    """What a stop must leave: the unit inactive with Result success, no process, the port free."""
    p = []
    st = show(unit, "ActiveState", "Result", "ExecMainCode", "ExecMainStatus")
    if st.get("ActiveState") != "inactive" or st.get("Result") != "success":
        p.append(f"{unit} after stop: {st}")
    if took > limit:
        p.append(f"stop took {took} s")
    left = procs(unit) + [x[0] for x in owned(uid)]
    if left:
        p.append(f"left behind: {owned(uid) or left}")
    if not port_free():
        p.append("port still taken")
    return p, st


def start(unit, other_than=None):
    code, out, took = ctl("start", unit)
    secs, s = ready(other_than=other_than)
    pid = int(show(unit, "MainPID")["MainPID"])
    return {"start": [code, out, took], "ready_s": secs, "pid": pid}, s


def checks(args):
    out, sha0, res = args.out, None, {}

    def line(name, **kw):
        kw["problems"] = kw.get("problems", [])
        with open(out, "a") as f:
            f.write(json.dumps({"check": name, "at": time.time(), **kw}) + "\n")
        print(f"{name}: {'ok' if not kw['problems'] else kw['problems']}", file=sys.stderr, flush=True)
        return kw

    def step(name, fn):
        try:
            return line(name, **fn())
        except Exception as e:  # the check ends here; the next ones still run
            return line(name, problems=[f"raised {type(e).__name__}: {e}"])

    def ready_check():
        nonlocal sha0
        sha0 = ini_sha()
        p = [f"{u} {show(u, 'ActiveState')['ActiveState']}" for u in UNITS
             if show(u, "ActiveState")["ActiveState"] != "inactive"]
        prod = production()
        p += [] if port_free() else ["port taken"]
        p += [] if prod == {"active": "active", "answer": 200} else [f"platmon.service {prod}"]
        return {"ini_sha256": sha0, "production": prod, "problems": p}

    def python():
        r, s = start(PYTHON)
        res["python"] = s
        p = [] if s else ["not ready"]
        time.sleep(20)  # some records to keep a cursor in
        return {**r, "instance": s and s["instance_id"], "problems": p}

    def swap():
        old = res["python"]
        r, s = start(NATIVE, other_than=old["instance_id"])
        res["native"] = s
        p = [] if s else ["not ready"]
        if show(PYTHON, "ActiveState")["ActiveState"] != "inactive":
            p.append("the Python unit still active")
        code = old_cursor(old["instance_id"])
        p += [] if code == 409 else [f"the Python instance's cursor: {code}, not 409"]
        diff = same_status(old, s) if s else ["?"]
        p += [f"/api/status differs: {diff}"] if diff else []
        p += [] if ini_sha() == sha0 else ["the INI changed"]
        return {**r, "instance": s and s["instance_id"], "old_cursor": code, "status_keys": sorted(s or {}),
                "uid": uid_of(r["pid"]), "problems": p}

    def restart():
        old, n0 = res["native"], show(NATIVE, "NRestarts")["NRestarts"]
        code, outp, took = ctl("restart", NATIVE)
        secs, s = ready(other_than=old["instance_id"])
        res["native"] = s
        p = [] if s else ["not ready with a new instance"]
        oc = old_cursor(old["instance_id"])
        p += [] if oc == 409 else [f"the previous cursor: {oc}, not 409"]
        n1 = show(NATIVE, "NRestarts")["NRestarts"]
        return {"restart": [code, outp, took], "ready_s": secs, "old_cursor": oc, "nrestarts": [n0, n1],
                "problems": p}

    def crash():
        old = res["native"]
        pid0, n0 = int(show(NATIVE, "MainPID")["MainPID"]), int(show(NATIVE, "NRestarts")["NRestarts"])
        code, outp = sudo(SYSTEMCTL, "kill", "--signal=SIGKILL", NATIVE)
        secs, s = ready(timeout=30, other_than=old["instance_id"])
        res["native"] = s
        st = show(NATIVE, "MainPID", "NRestarts", "ActiveState")
        p = [] if s else ["not back within 30 s"]
        p += [] if int(st["NRestarts"]) == n0 + 1 else [f"NRestarts {n0} -> {st['NRestarts']}"]
        p += [] if not os.path.exists(f"/proc/{pid0}") else [f"the killed process {pid0} still there"]
        oc = old_cursor(old["instance_id"])
        p += [] if oc == 409 else [f"the previous cursor: {oc}, not 409"]
        return {"kill": [code, outp], "back_s": secs, "pids": [pid0, int(st["MainPID"])], "old_cursor": oc,
                "nrestarts": [n0, int(st["NRestarts"])], "problems": p}

    def failed_start():
        p = []
        ctl("stop", NATIVE)
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", PORT))
        holder.listen(1)
        try:
            n0 = int(show(NATIVE, "NRestarts")["NRestarts"])
            code, outp, took = ctl("start", NATIVE)
            time.sleep(8)  # at least one exit and one restart (RestartSec=5)
            during = show(NATIVE, "ActiveState", "SubState", "Result", "NRestarts", "ExecMainStatus")
            if int(during["NRestarts"]) <= n0:
                p.append(f"no restart while the port was held: {during}")
            log = journal(NATIVE, 8)
        finally:
            holder.close()
        secs, s = ready(timeout=20)
        res["native"] = s
        p += [] if s else ["not serving 20 s after the port was released"]
        return {"start": [code, outp, took], "while_held": during, "journal": log, "serving_after_s": secs,
                "problems": p}

    def rollback():
        old = res["native"]
        r, s = start(PYTHON, other_than=old["instance_id"])
        p = [] if s else ["not ready"]
        if show(NATIVE, "ActiveState")["ActiveState"] != "inactive":
            p.append("the native unit still active")
        oc = old_cursor(old["instance_id"])
        p += [] if oc == 409 else [f"the native instance's cursor: {oc}, not 409"]
        diff = same_status(old, s) if s else ["?"]
        p += [f"/api/status differs: {diff}"] if diff else []
        p += [] if ini_sha() == sha0 else ["the INI changed"]
        return {**r, "old_cursor": oc, "problems": p}

    def stop_collect():
        p, runs = [], []
        for off in (0.0, 0.001, 0.002, 0.004, 0.25, 0.5, 0.75):
            r, s = start(NATIVE)
            if not s:
                p.append(f"offset {off}: not ready")
                continue
            uid = uid_of(r["pid"])
            time.sleep(2)
            body = json.loads(ab.get(PORT, "/api/stats")[1])
            started = body["sample"]["started_at"]
            nxt = started + 1.0 * (int(time.time() - started) + 1)  # interval = 1.0
            time.sleep(max(0.0, nxt + off - time.time()))
            code, outp, took = ctl("stop", NATIVE)
            q, st = stopped(NATIVE, uid, took)
            p += [f"offset {off}: {x}" for x in q]
            runs.append({"offset_s": off, "stop": [code, outp, took], "after": st})
        return {"runs": runs, "problems": p}

    def stop_replay():
        p, tries = [], []
        for attempt in range(3):
            r, s = start(NATIVE)
            if not s:
                return {"problems": ["not ready"]}
            uid = uid_of(r["pid"])
            time.sleep(150)  # 150 core records: pages of 100 with something to write
            inst, stop, got = s["instance_id"], threading.Event(), []

            def fetch():
                while not stop.is_set():
                    t0 = time.monotonic()
                    try:
                        code, body, _ = ab.get(PORT, f"/api/samples?instance_id={inst}&after=0&limit=100", 10)
                        json.loads(body)
                        got.append((t0, time.monotonic(), code))
                    except socket.timeout:
                        got.append((t0, time.monotonic(), "timeout"))
                    except (OSError, ValueError, http.client.HTTPException) as e:
                        got.append((t0, time.monotonic(), type(e).__name__))
                        time.sleep(0.05)

            ws = [worker.Worker(f"fetch {i}", fetch) for i in range(4)]
            for w in ws:
                w.start()
            time.sleep(1.0)
            t_stop = time.monotonic()
            code, outp, took = ctl("stop", NATIVE)
            time.sleep(1.0)
            stop.set()
            states = [w.finish(15) for w in ws]
            in_flight = sum(1 for a, b, _ in got if a < t_stop < b)
            outcomes = {}
            for _, _, c in got:
                outcomes[str(c)] = outcomes.get(str(c), 0) + 1
            q, st = stopped(NATIVE, uid, took)
            q += [x for i, w in enumerate(states) for x in worker.problems(f"fetch {i}", w)]
            q += ["a client hung (timeout)"] if "timeout" in outcomes else []
            tries.append({"stop": [code, outp, took], "in_flight_at_stop": in_flight, "outcomes": outcomes,
                          "after": st, "problems": q})
            if in_flight:
                p += q
                break
        else:
            p.append("no answer in flight at the stop in 3 tries")
        return {"tries": tries, "problems": p}

    def stop_nvsmi():
        p, runs = [], []
        r, s = start(FAKE)
        if not s:
            return {"problems": ["not ready"]}
        time.sleep(3)
        stats = json.loads(ab.get(PORT, "/api/stats")[1])
        gpu, col = stats.get("gpu"), (stats.get("collectors") or {}).get("gpu")
        ctl("stop", FAKE)
        for i in range(3):
            r, s = start(FAKE)
            uid = uid_of(r["pid"])
            t, seen = time.monotonic(), None
            while time.monotonic() - t < 5:
                ps = procs(FAKE)
                if len(ps) > 1:
                    seen = ps
                    break
                time.sleep(0.01)
            if not seen:
                code, outp, took = ctl("stop", FAKE)
                runs.append({"reached": False, "stop": [code, outp, took]})
                if i == 0:
                    break
                continue
            code, outp, took = ctl("stop", FAKE)
            q, st = stopped(FAKE, uid, took)
            p += [f"run {i}: {x}" for x in q]
            runs.append({"reached": True, "processes_at_stop": len(seen), "stop": [code, outp, took], "after": st,
                         "journal": journal(FAKE, 6)})
        reached = any(x["reached"] for x in runs)
        return {"gpu": gpu, "collector": col, "reached": reached, "runs": runs, "problems": p}

    def end():
        for u in UNITS:
            if show(u, "ActiveState")["ActiveState"] != "inactive":
                ctl("stop", u)
        sudo(SYSTEMCTL, "reset-failed", *UNITS)
        p = [f"{u} {show(u, 'ActiveState')['ActiveState']}" for u in UNITS
             if show(u, "ActiveState")["ActiveState"] != "inactive"]
        prod = production()
        p += [] if port_free() else ["port taken"]
        p += [] if prod == {"active": "active", "answer": 200} else [f"platmon.service {prod}"]
        p += [] if ini_sha() == sha0 else ["the INI changed"]
        return {"production": prod, "problems": p}

    with open(out, "a") as f:
        f.write(json.dumps({"setup": setup("checks")}) + "\n")
    first = step("ready", ready_check)
    if first["problems"]:
        return
    for name, fn in (("python", python), ("swap", swap), ("restart", restart), ("crash", crash),
                     ("failed_start", failed_start), ("rollback", rollback), ("stop_collect", stop_collect),
                     ("stop_replay", stop_replay), ("stop_nvsmi", stop_nvsmi)):
        step(name, fn)
    step("end", end)


def own():
    with open("/proc/self/status") as f:
        s = dict(line.split(":", 1) for line in f)
    return {k: int(s[k].split()[0]) for k in ("VmRSS", "Threads")}


def soak(args):
    ph = dict(QUICK if args.quick else SOAK)
    out = args.out

    def write(rec):
        with open(out, "a") as f:
            f.write(json.dumps(rec) + "\n")

    write({"setup": setup("soak"), "phases": ph, "hours": args.hours})
    for u in (PYTHON, FAKE):
        if show(u, "ActiveState")["ActiveState"] != "inactive":
            ctl("stop", u)
    r, s = start(NATIVE)
    if not s:
        write({"failed": "not ready", **r})
        return
    clock = ab.Clock()
    proc = UnitProc(NATIVE, r["pid"])
    rss = VmRss(proc, clock)
    rw = worker.Worker("rss", rss.run)
    rw.start()
    hub = ab.Hub(PORT, clock)
    hub.mono0, hub.wall0 = clock.now(), time.time()
    marks = {"losses": 0, "problems": 0, "ticks": proc.ticks(), "t": clock.now()}
    restarts = []

    def poll(until):
        stop = threading.Event()
        w = worker.Worker("hub", hub.run, stop)
        w.start()
        clock.at(until)
        stop.set()
        return w.finish()

    for h in range(args.hours):
        base = h * ph["hour"]
        rec = {"hour": h, "started": time.time()}
        rec["poll"] = poll(base + ph["poll"])
        expire = h % 2 == 1
        clock.at(base + ph["poll"] + (ph["expire"] if expire else ph["disconnect"]))
        before = ab.retained(PORT, proc)
        st = status()
        obs = json.loads(ab.get(PORT, "/api/observations")[1])
        target = {"core": st["sample"]["sequence"]}
        for g in ab.STREAMS[1:]:
            o = (obs.get("groups", {}).get(g) or {}).get("observation") or {}
            if hub.supported[g] and o.get("id") is not None:
                target[g] = o["id"]
        cursor = {g: hub.walks[g].after for g in target}
        for g, t in target.items():
            hub.walks[g].target = t
            hub.first[g] = expire  # an expired range is expected only on the first page of an odd hour
        missing0 = {g: len(hub.walks[g].missing) for g in target}
        stop = threading.Event()
        limit = threading.Timer(ph["reconnect_limit"], stop.set)
        limit.start()
        c0, t0 = proc.ticks(), time.monotonic()
        hub.run(stop, done=lambda: all(hub.walks[g].after >= t for g, t in target.items()))
        c1, t1 = proc.ticks(), time.monotonic()
        limit.cancel()
        rec["reconnect"] = {"expire": expire, "backlog": {g: t - cursor[g] for g, t in target.items()},
                            "seconds": round(t1 - t0, 3), "cpu_s": (c1 - c0) / ab.TICK,
                            "done": all(hub.walks[g].after >= t for g, t in target.items()),
                            "oldest_before": {g: v[0] for g, v in before.get("replay", {}).items()},
                            "cursor": cursor,
                            "missing": {g: hub.walks[g].missing[missing0[g]:] for g in target}}
        for g in target:
            hub.walks[g].target = None
            hub.first[g] = False
        now = ab.retained(PORT, proc)
        held = now.get("replay", {})
        rec["reconnect"]["oldest_after"] = {g: v[0] for g, v in held.items()}
        # the 4 pollers start within the retention: in an odd hour a fifth into what is held (about 120 s
        # before the oldest record expires), so that nothing expires under them
        cur = {g: max(cursor[g], held[g][0] - 1 + (held[g][1] - held[g][0]) // 5) if expire and g in held
               else cursor[g] for g in cursor}
        conc = ab.concurrent(PORT, clock, proc, rss, hub, cur, ph)
        rec["concurrent"] = {k: conc[k] for k in ("backlog", "done", "seconds", "cpu_s_each", "start_pss_kib",
                                                  "peak_rss_kib", "peak_pss_kib", "losses", "problems",
                                                  "requests", "errors", "records", "bytes", "response_ms")}
        rec["concurrent"]["workers"] = [w for r_ in conc["rounds"] for w in r_["workers"]]
        time.sleep(ph["settle"])
        rec["settled"] = {**ab.retained(PORT, proc), **measure().get(NATIVE, {})}
        if h + 1 == ph["restart_at"]:
            old = status()["instance_id"]
            code, outp, took = ctl("restart", NATIVE)
            secs, s = ready(other_than=old)
            pid = int(show(NATIVE, "MainPID")["MainPID"])
            rss.end()
            rw.finish()
            proc = UnitProc(NATIVE, pid)
            rss = VmRss(proc, clock)
            rw = worker.Worker("rss", rss.run)
            rw.start()
            hub.walks = {g: walk.Walk(g) for g in ab.STREAMS}
            hub.first = {g: True for g in ab.STREAMS}
            hub.supported = {g: None for g in ab.STREAMS}
            marks["ticks"] = proc.ticks()
            restarts.append(h)
            rec["restart"] = {"restart": [code, outp, took], "ready_s": secs, "old_cursor": old_cursor(old),
                              "pid": pid}
        span = [marks["t"], clock.now()]
        ticks = proc.ticks()
        rec["cpu_pct"] = None if "restart" in rec else round(100 * (ticks - marks["ticks"]) / ab.TICK /
                                                             (span[1] - span[0]), 3)
        rows = sorted(hub.core.items())
        steps = [v2[0] - v1[0] for (i1, v1), (i2, v2) in zip(rows, rows[1:]) if i2 == i1 + 1]
        rec["collected"] = {"records": len(rows), "max_step_s": round(max(steps, default=0), 3),
                            "steps_over_1_5": sum(1 for x in steps if x > 1.5),
                            "gap_before": sum(1 for _, v in rows if v[2]),
                            "first_last": [rows[0][0], rows[-1][0]] if rows else None}
        rec["hub"] = {**hub.window(span[0], span[1]), "losses": hub.losses[marks["losses"]:],
                      "problems": hub.problems[marks["problems"]:]}
        rec["unit"] = show(NATIVE, "ActiveState", "SubState", "NRestarts", "MainPID")
        rec["vmrss_max_kib"] = max([v for t, v in rss.trace if t >= span[0]], default=None)
        rec["tool"] = own()
        write(rec)
        print(f"hour {h}: settled pss {rec['settled'].get('pss')} threads {rec['settled'].get('threads')} "
              f"fds {rec['settled'].get('fds')} losses {len(rec['hub']['losses'])} "
              f"problems {len(rec['hub']['problems'])}", file=sys.stderr, flush=True)
        marks = {"losses": len(hub.losses), "problems": len(hub.problems), "ticks": proc.ticks(), "t": clock.now()}
        hub.core.clear()
        hub.requests.clear()
        rss.trace.clear()
        if h + 1 < args.hours:
            rec_poll_until = (h + 1) * ph["hour"]
            rec_state = poll(rec_poll_until)
            if worker.problems("hub", rec_state):
                write({"hour": h, "between_hours": rec_state})
    rss.end()
    end = {"end": True, "rss_worker": rw.finish(), "restarts": restarts, "measure": measure().get(NATIVE)}
    uid = uid_of(proc.pid)
    code, outp, took = ctl("stop", NATIVE)
    end["stop"] = [code, outp, took]
    end["stopped"] = stopped(NATIVE, uid, took)
    end["production"] = production()
    end["tool"] = own()
    write(end)


def setup(kind):
    def sha(path):
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    here = os.path.dirname(os.path.abspath(__file__))
    return {"kind": kind, "host": socket.gethostname(), "machine": os.uname().machine, "python": sys.version.split()[0],
            "user": os.getuid(), "binary_sha256": sha("/usr/lib/platmon-candidate/platmon-native"),
            "ini_sha256": ini_sha(), "units": {u: show(u, "FragmentPath", "User", "DynamicUser")
                                               for u in UNITS},
            "tools_sha256": {f: sha(os.path.join(here, f))
                             for f in ("deploy_check.py", "service_ab.py", "walk.py", "worker.py")},
            "production": production()}


def report(args):
    """Every hour judged on its own; see the README, "The deployed form, set before it was run"."""
    rows = [json.loads(x) for x in open(args.path)]
    hours = [r for r in rows if "hour" in r and "settled" in r]
    end = next((r for r in rows if r.get("end")), None)
    bad, ref = [], None
    for r in rows:
        if "between_hours" in r:
            bad += [f"after hour {r['hour']}: {x}" for x in worker.problems("hub", r["between_hours"])]
    for r in hours:
        h, p = r["hour"], []
        p += [f"poll: {x}" for x in worker.problems("hub", r["poll"])]
        p += [f"loss {x}" for x in r["hub"]["losses"]] + [f"walk {x}" for x in r["hub"]["problems"]]
        p += [f"{r['hub']['errors']} HTTP errors"] if r["hub"]["errors"] else []
        rc, cc = r["reconnect"], r["concurrent"]
        p += [] if rc["done"] else ["reconnect not done"]
        for g, ms in rc["missing"].items():
            for a, z in ms:
                if not rc["expire"] or a != rc["cursor"][g] + 1 or z >= rc["oldest_after"].get(g, 0):
                    p.append(f"reconnect {g} missing {a}..{z} (cursor {rc['cursor'][g]})")
        p += [] if cc["done"] else ["concurrent not done"]
        p += [f"concurrent {x}" for x in cc["losses"] + cc["problems"]] + ([f"concurrent {cc['errors']} errors"]
                                                                          if cc["errors"] else [])
        p += [x for w in cc["workers"] for x in worker.problems("poller", w)]
        col = r["collected"]
        p += [f"collections: {col}"] if col["steps_over_1_5"] or col["gap_before"] else []
        u = r["unit"]
        p += [] if u["ActiveState"] == "active" and u["NRestarts"] == "0" else [f"unit {u}"]
        if "restart" in r:
            p += [] if r["restart"]["old_cursor"] == 409 else [f"restart: old cursor {r['restart']['old_cursor']}"]
        s = r["settled"]
        if ref is None:
            ref = s
        else:
            for k in ("threads", "fds", "sockets", "processes"):
                if s.get(k) != ref.get(k):
                    p.append(f"settled {k} {s.get(k)}, hour 0 {ref.get(k)}")
            limit = max(ref["pss"] * 1.10, ref["pss"] + 1024)
            if s["pss"] > limit:
                p.append(f"settled PSS {s['pss']} KiB, hour 0 {ref['pss']} (limit {round(limit)})")
        if args.settled_mib and s["pss"] > args.settled_mib * 1024:
            p.append(f"settled PSS {s['pss']} KiB over the budget {args.settled_mib} MiB")
        if args.peak_mib and cc["peak_pss_kib"] > args.peak_mib * 1024:
            p.append(f"concurrent PSS {cc['peak_pss_kib']} KiB (round-end reading) over {args.peak_mib} MiB")
        print(f"hour {h:2}: cpu {r['cpu_pct']} pss {s['pss']} threads {s['threads']} fds {s['fds']} "
              f"conc peak pss {cc['peak_pss_kib']} records {col['records']} max step {col['max_step_s']} "
              f"tool rss {r['tool']['VmRSS']} {'ok' if not p else p}")
        bad += p
    if not end:
        bad.append("no end line: the run did not finish")
    else:
        q = end["stopped"][0] + worker.problems("rss", end["rss_worker"])
        q += [] if end["production"] == {"active": "active", "answer": 200} else [f"platmon.service {end['production']}"]
        print(f"end: {end['stop']} {q or 'ok'}; tool {end['tool']}")
        bad += q
    print(f"{len(hours)} hours: {'PASS' if not bad else 'FAIL'}")
    return not bad


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("checks")
    c.add_argument("--out", required=True)
    s = sub.add_parser("soak")
    s.add_argument("--out", required=True)
    s.add_argument("--hours", type=int, default=24)
    s.add_argument("--quick", action="store_true",
                   help="short hours, to try the tool: no expiry (60 s away), readings before the buffers are full")
    r = sub.add_parser("report")
    r.add_argument("path")
    r.add_argument("--settled-mib", type=float)
    r.add_argument("--peak-mib", type=float)
    args = ap.parse_args()
    if args.cmd == "checks":
        checks(args)
    elif args.cmd == "soak":
        soak(args)
    else:
        sys.exit(0 if report(args) else 1)


if __name__ == "__main__":
    main()
