"""A native server build as a candidate Windows service, the Windows counterpart of deploy_check.py: one
service (platmon-candidate, LocalSystem, the recovery actions of docs/guides/getting-started.md), one folder
(C:\\Program Files\\platmon-candidate, writable by administrators only), one INI there (bind 127.0.0.1, port
19797), one port. Only that service is started, stopped or killed; the platmon service is only read (its
state, an answer on 9797). Run it in an administrator shell: the service control manager, taskkill on a
LocalSystem process and the process's CPU time need one. One JSON line per check or hour.

  python windows_check.py install BINARY                    # the folder, the INI and the service (stopped)
  python windows_check.py checks --out OUT.jsonl [--previous OLD_BINARY]  # start, recovery, stopping, upgrade
  python windows_check.py soak --out OUT.jsonl [--hours 24]  # the long run
  python windows_check.py report OUT.jsonl [--settled-mib N]  # deploy_check.py's judgement
  python windows_check.py uninstall

checks, in order; a check passes with no problem in its line:
  ready         the candidate service stopped, the port free, the platmon service running and answering
  start         sc.exe start: ready, as LocalSystem; the INI unchanged
  restart       sc.exe stop and start: ready, a new instance_id, 409 for the previous one
  crash         taskkill /F (kill()): the recovery actions bring it back (a new process and instance_id), the old
                process gone, 409 for the previous instance
  failed_start  the port held by this tool: the service cannot listen, logs why (event 1) and is restarted
                (sc.exe failureflag); released, it serves again without anyone starting it
  From start to upgrade the recovery actions restart after 5 s every time, so that failures of an earlier
  run (the service counts them for a day) do not stretch the waits to the guide's 60 s; end sets the
  guide's back.
  stop_collect  stop at 7 offsets from a collection's start (0 to 750 ms): each stop within 2 s, exit code 0,
                the process gone, the port free
  stop_replay   stop while 4 clients fetch replay pages: a page answers whole or the client sees an error,
                never a hang; then as above
  upgrade       with --previous: the service on that binary, then stopped, the candidate copied over it and
                started: ready as a new instance from the candidate's bytes, the INI unchanged. Not run without
  end           the candidate service stopped, the port free, the platmon service running and answering, the
                INI unchanged
deploy_check.py's stop_nvsmi has no Windows counterpart (nvidia-smi is a Linux collector's fallback).

soak: deploy_check.py's, with the same phases and one restart, through service_ab.py's Hub, concurrent and
retained on a Windows Proc. Its lines have deploy_check.py's keys so that its report judges them by the same
rules, except that a settled thread or handle count may stray from its segment's first reading by SLACK
(Windows' thread pool); on Windows "pss" is the process's private bytes (commit) and "fds" its handles; a
service has one process and no socket count. "NRestarts" is how often the process changed in the hour
without this tool restarting it."""
import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path[:0] = [HERE, TOOLS, os.path.join(TOOLS, "perf")]
import deploy_check as dc  # noqa: E402
import service_ab as ab  # noqa: E402
import walk  # noqa: E402
import worker  # noqa: E402

PORT = dc.PORT
NAME = "platmon-candidate"
DIR = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), NAME)
BINARY, INI, WEB = os.path.join(DIR, "platmon-server.exe"), os.path.join(DIR, "platmon.ini"), os.path.join(DIR, "web")
REPO = os.path.dirname(os.path.dirname(TOOLS))
# How far a settled thread or handle count may stray from its segment's first reading (deploy_check.py
# allows none). Windows' thread pool, which system DLLs use, starts and ends workers on its own: idle, the
# service read 9 or 10 threads with 208 or 210 handles (2026-11-16). Set before the first 24 h run; growth is
# still caught by the private bytes rules.
SLACK = {"threads": 3, "fds": 30}
# docs/guides/getting-started.md, "Windows"
RECOVERY = (["failure", NAME, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/60000"],
            ["failureflag", NAME, "1"])

k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
QUERY, STILL_ACTIVE = 0x1000 | 0x00100000, 259  # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE


class Counters(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
        (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                       "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                                       "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class Thread(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG), ("dwFlags", wt.DWORD)]


if k32:
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = (wt.DWORD, wt.DWORD)
    for f in ("GetExitCodeProcess", "GetProcessHandleCount"):
        getattr(k32, f).argtypes = (wt.HANDLE, ctypes.POINTER(wt.DWORD))
    k32.GetProcessTimes.argtypes = (wt.HANDLE,) + (ctypes.POINTER(ctypes.c_ulonglong),) * 4
    k32.K32GetProcessMemoryInfo.argtypes = (wt.HANDLE, ctypes.POINTER(Counters), wt.DWORD)
    k32.Thread32First.argtypes = k32.Thread32Next.argtypes = (wt.HANDLE, ctypes.POINTER(Thread))
    k32.CloseHandle.argtypes = (wt.HANDLE,)
    k32.TerminateProcess.argtypes = (wt.HANDLE, wt.UINT)


def kill(pid):
    """taskkill /F on pid: {"method", "code", "output"}. Elevated runs have had the launch of taskkill itself
    refused (PermissionError, WinError 5) with no cause found (2026-11-16 and 17, 4 of 7 checks runs, never
    without elevation): then the process ends by TerminateProcess, exit code 1, as taskkill /F does, and the
    refused launch is kept in "launch"."""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
    try:
        k = subprocess.run([exe, "/F", "/PID", str(pid)], capture_output=True, text=True, timeout=60)
        return {"method": "taskkill", "code": k.returncode, "output": (k.stdout + k.stderr).strip()}
    except PermissionError as e:
        launch = f"{type(e).__name__}: {e}"
    h = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ok = k32.TerminateProcess(h, 1)
        err = ctypes.get_last_error()
    finally:
        k32.CloseHandle(h)
    if not ok:
        raise ctypes.WinError(err)
    return {"method": "TerminateProcess", "code": 0, "output": "", "launch": launch}


class Proc:
    """service_ab.Proc's readings of a Windows process: CPU time in 10 ms ticks (service_ab.TICK = 100), the
    working set as VmRSS, private bytes as Pss, handles as fds. OSError once the process has exited."""
    def __init__(self, pid):
        self.pid = pid
        self.handle = k32.OpenProcess(QUERY, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def alive(self):
        code = wt.DWORD()
        return bool(k32.GetExitCodeProcess(self.handle, ctypes.byref(code))) and code.value == STILL_ACTIVE

    def _check(self):
        if not self.alive():
            raise ProcessLookupError(f"process {self.pid} exited")

    def ticks(self):
        self._check()
        t = [ctypes.c_ulonglong() for _ in range(4)]
        if not k32.GetProcessTimes(self.handle, *map(ctypes.byref, t)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (t[2].value + t[3].value) // 100_000  # kernel + user, 100 ns units

    def counters(self):
        self._check()
        c = Counters()
        c.cb = ctypes.sizeof(c)
        if not k32.K32GetProcessMemoryInfo(self.handle, ctypes.byref(c), c.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return c

    def threads(self):
        snap = k32.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snap in (None, wt.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            e, n = Thread(), 0
            e.dwSize = ctypes.sizeof(e)
            ok = k32.Thread32First(snap, ctypes.byref(e))
            while ok:
                n += e.th32OwnerProcessID == self.pid
                ok = k32.Thread32Next(snap, ctypes.byref(e))
            return n
        finally:
            k32.CloseHandle(snap)

    def status(self):
        c = self.counters()
        return {"VmRSS": c.WorkingSetSize // 1024, "VmHWM": c.PeakWorkingSetSize // 1024, "Threads": self.threads()}

    def rollup(self):
        c = self.counters()
        return {"Rss": c.WorkingSetSize // 1024, "Pss": c.PrivateUsage // 1024}

    def fds(self):
        self._check()
        n = wt.DWORD()
        k32.GetProcessHandleCount(self.handle, ctypes.byref(n))
        return n.value

    def measure(self):
        """deploy_check.py's measure() keys for the settled reading."""
        c = self.counters()
        return {"pid": self.pid, "pss": c.PrivateUsage // 1024, "rss": c.WorkingSetSize // 1024,
                "threads": self.threads(), "fds": self.fds(), "sockets": None, "processes": 1}


def sc(*args):
    r = subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def query(name=NAME):
    """sc.exe queryex: state, pid, exit codes (the labels sc.exe keeps in English)."""
    out = sc("queryex", name)[1]
    num = lambda k: int(m.group(1)) if (m := re.search(rf"{k}\s*:\s*(\d+)", out)) else None
    state = re.search(r":\s*\d+\s+(STOPPED|START_PENDING|STOP_PENDING|RUNNING|CONTINUE_PENDING|PAUSE_PENDING|PAUSED)", out)
    return {"state": state.group(1) if state else None, "pid": num("PID"), "win32_exit": num("WIN32_EXIT_CODE"),
            "service_exit": num("SERVICE_EXIT_CODE")}


def wait_state(want, timeout=30):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        q = query()
        if q["state"] == want:
            return round(time.monotonic() - t, 3), q
        time.sleep(0.05)
    return None, query()


def ctl(verb):
    """start or stop, then the wait for RUNNING or STOPPED; (exit code, output, seconds)."""
    t = time.monotonic()
    code, out = sc(verb, NAME)
    wait_state("RUNNING" if verb == "start" else "STOPPED")
    return code, out[-300:], round(time.monotonic() - t, 3)


def start(other_than=None):
    code, out, took = ctl("start")
    secs, s = dc.ready(other_than=other_than)
    return {"start": [code, out, took], "ready_s": secs, "pid": query()["pid"]}, s


def stopped(pid, took, limit=2.0):
    """What a stop must leave: STOPPED with exit code 0, the process gone, the port free."""
    p, q = [], query()
    if q["state"] != "STOPPED" or q["win32_exit"] != 0:
        p.append(f"after stop: {q}")
    if took > limit:
        p.append(f"stop took {took} s")
    if pid and process_alive(pid):
        p.append(f"process {pid} left behind")
    if not dc.port_free():
        p.append("port still taken")
    return p, q


def process_alive(pid):
    try:
        return Proc(pid).alive()
    except OSError:
        return False


def production():
    """The platmon service, read only: dc.production()'s keys."""
    try:
        code = ab.get(9797, "/api/status", 5)[0]
    except OSError as e:
        code = type(e).__name__
    return {"active": "active" if query("platmon")["state"] == "RUNNING" else query("platmon")["state"], "answer": code}


def events(since, text):
    """The platmon source's error events after `since` (epoch seconds) whose text has `text`."""
    ms = max(1000, int((time.time() - since) * 1000) + 1000)
    out = subprocess.run(["wevtutil", "qe", "Application", "/rd:true", "/f:xml",
                          f"/q:*[System[Provider[@Name='platmon'] and TimeCreated[timediff(@SystemTime) <= {ms}]]]"],
                         capture_output=True, text=True, timeout=60).stdout
    return [d for d in re.findall(r"<Data>(.*?)</Data>", out) if text in d]


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def own():
    return Proc(os.getpid()).status()


def setup(kind):
    return {"kind": kind, "host": socket.gethostname(), "machine": os.environ.get("PROCESSOR_ARCHITECTURE"),
            "windows": sys.getwindowsversion()[:3], "python": sys.version.split()[0], "binary_sha256": sha(BINARY),
            "ini_sha256": sha(INI), "service": sc("qc", NAME)[1][-600:], "recovery": sc("qfailure", NAME)[1][-400:],
            "tools_sha256": {os.path.basename(f): sha(f) for f in (__file__, dc.__file__, ab.__file__, walk.__file__,
                                                                  worker.__file__)},
            "production": production()}


def install(args):
    if query()["state"] is not None:
        sys.exit(f"{NAME} exists: uninstall first")
    os.makedirs(DIR, exist_ok=False)
    shutil.copy2(args.binary, BINARY)
    shutil.copytree(os.path.join(REPO, "frontends", "web"), WEB)
    with open(INI, "w") as f:
        f.write(f"[core]\ninterval = 1.0\n\n[http]\nbind = 127.0.0.1\nport = {PORT}\n")
    code, out = sc("create", NAME, "start=", "demand", "binPath=", f'"{BINARY}" service "{INI}" --web "{WEB}"')
    if code:
        sys.exit(out)
    for a in RECOVERY:
        code, out = sc(*a)
        if code:
            sys.exit(out)
    print(f"{NAME}: {BINARY} ({sha(BINARY)}), stopped")


def uninstall(_):
    if query()["state"] not in (None, "STOPPED"):
        ctl("stop")
    sc("delete", NAME)
    shutil.rmtree(DIR, ignore_errors=True)
    print(f"{NAME}: removed")


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
            return line(name, problems=[f"raised {type(e).__name__}: {e}"], traceback=traceback.format_exc())

    def ready_check():
        nonlocal sha0
        sha0, q, prod = sha(INI), query(), production()
        p = [] if q["state"] == "STOPPED" else [f"{NAME} {q}"]
        p += [] if dc.port_free() else ["port taken"]
        p += [] if prod == {"active": "active", "answer": 200} else [f"platmon service {prod}"]
        return {"ini_sha256": sha0, "production": prod, "problems": p}

    def start_check():
        r, s = start()
        res["native"] = s
        account = re.search(r"SERVICE_START_NAME\s*:\s*(\S+)", sc("qc", NAME)[1])
        p = [] if s else ["not ready"]
        p += [] if account and account.group(1) == "LocalSystem" else [f"account {account and account.group(1)}"]
        p += [] if sha(INI) == sha0 else ["the INI changed"]
        time.sleep(20)  # some records to keep a cursor in
        return {**r, "instance": s and s["instance_id"], "account": account and account.group(1), "problems": p}

    def restart():
        old = res["native"]
        stop, begin = ctl("stop"), ctl("start")
        secs, s = dc.ready(other_than=old["instance_id"])
        res["native"] = s
        p = [] if s else ["not ready with a new instance"]
        oc = dc.old_cursor(old["instance_id"])
        p += [] if oc == 409 else [f"the previous cursor: {oc}, not 409"]
        return {"stop": stop, "start": begin, "ready_s": secs, "old_cursor": oc, "problems": p}

    def crash():
        old, pid0 = res["native"], query()["pid"]
        k = kill(pid0)
        secs, s = dc.ready(timeout=30, other_than=old["instance_id"])
        res["native"] = s
        pid1 = query()["pid"]
        p = [] if s else ["not back within 30 s"]
        p += [] if pid1 and pid1 != pid0 else [f"pid {pid0} -> {pid1}"]
        p += [] if not process_alive(pid0) else [f"the killed process {pid0} still there"]
        oc = dc.old_cursor(old["instance_id"]) if s else None
        p += [] if oc == 409 else [f"the previous cursor: {oc}, not 409"]
        return {"kill": k, "back_s": secs, "pids": [pid0, pid1],
                "old_cursor": oc, "problems": p}

    def failed_start():
        p = []
        ctl("stop")
        holder = socket.socket()
        holder.bind(("127.0.0.1", PORT))
        holder.listen(1)
        t0 = time.time()
        try:
            code, outp = sc("start", NAME)
            time.sleep(8)  # at least one exit and one restart (5 s)
            logged = events(t0, f"cannot listen on 127.0.0.1:{PORT}")
            during = query()
            if len(logged) < 2:
                p.append(f"{len(logged)} 'cannot listen' events while the port was held, not 2 or more")
        finally:
            holder.close()
        secs, s = dc.ready(timeout=90)
        res["native"] = s
        p += [] if s else ["not serving 90 s after the port was released"]
        return {"start": [code, outp[-300:]], "while_held": during, "events": logged[:3], "serving_after_s": secs,
                "problems": p}

    def stop_collect():
        p, runs = [], []
        if query()["state"] != "STOPPED":
            ctl("stop")
        for off in (0.0, 0.001, 0.002, 0.004, 0.25, 0.5, 0.75):
            r, s = start()
            if not s:
                p.append(f"offset {off}: not ready")
                continue
            time.sleep(2)
            body = json.loads(ab.get(PORT, "/api/stats")[1])
            started = body["sample"]["started_at"]
            nxt = started + 1.0 * (int(time.time() - started) + 1)  # interval = 1.0
            time.sleep(max(0.0, nxt + off - time.time()))
            stop = ctl("stop")
            q, st = stopped(r["pid"], stop[2])
            p += [f"offset {off}: {x}" for x in q]
            runs.append({"offset_s": off, "stop": stop, "after": st})
        return {"runs": runs, "problems": p}

    def stop_replay():
        p, tries = [], []
        for attempt in range(3):
            r, s = start()
            if not s:
                return {"problems": ["not ready"]}
            time.sleep(150)  # 150 core records: pages of 100 with something to write
            inst, halt, got = s["instance_id"], threading.Event(), []

            def fetch():
                while not halt.is_set():
                    t0 = time.monotonic()
                    try:
                        code, body, _ = ab.get(PORT, f"/api/samples?instance_id={inst}&after=0&limit=100", 10)
                        json.loads(body)
                        got.append((t0, time.monotonic(), code))
                    except socket.timeout:
                        got.append((t0, time.monotonic(), "timeout"))
                    except (OSError, ValueError, ab.http.client.HTTPException) as e:
                        got.append((t0, time.monotonic(), type(e).__name__))
                        time.sleep(0.05)
            ws = [worker.Worker(f"fetch {i}", fetch) for i in range(4)]
            for w in ws:
                w.start()
            time.sleep(1.0)
            t_stop = time.monotonic()
            stop = ctl("stop")
            time.sleep(1.0)
            halt.set()
            states = [w.finish(15) for w in ws]
            in_flight = sum(1 for a, b, _ in got if a < t_stop < b)
            outcomes = {}
            for _, _, c in got:
                outcomes[str(c)] = outcomes.get(str(c), 0) + 1
            q, st = stopped(r["pid"], stop[2])
            q += [x for i, w in enumerate(states) for x in worker.problems(f"fetch {i}", w)]
            q += ["a client hung (timeout)"] if "timeout" in outcomes else []
            tries.append({"stop": stop, "in_flight_at_stop": in_flight, "outcomes": outcomes, "after": st, "problems": q})
            if in_flight:
                p += q
                break
        else:
            p.append("no answer in flight at the stop in 3 tries")
        return {"tries": tries, "problems": p}

    def upgrade():
        if not args.previous:
            return {"run": False, "problems": []}
        candidate = sha(BINARY)
        keep = BINARY + ".candidate"
        shutil.copy2(BINARY, keep)
        try:
            if query()["state"] != "STOPPED":
                ctl("stop")
            shutil.copy2(args.previous, BINARY)
            r0, s0 = start()
            p = [] if s0 else ["not ready on the previous binary"]
            stop = ctl("stop")
            shutil.copy2(keep, BINARY)
            r1, s1 = start(other_than=s0 and s0["instance_id"])
            p += [] if s1 else ["not ready after the upgrade"]
            p += [] if sha(BINARY) == candidate else ["the binary is not the candidate's"]
            p += [] if not process_alive(r0["pid"]) else [f"the previous process {r0['pid']} still there"]
            p += [] if sha(INI) == sha0 else ["the INI changed"]
            ctl("stop")
            return {"run": True, "previous_sha256": sha(args.previous), "candidate_sha256": candidate,
                    "previous": r0, "stop": stop, "upgraded": r1, "problems": p}
        finally:
            if sha(BINARY) != candidate and query()["state"] == "STOPPED":
                shutil.copy2(keep, BINARY)
            os.remove(keep)

    def end():
        if query()["state"] != "STOPPED":
            ctl("stop")
        q, prod = query(), production()
        p = [] if q["state"] == "STOPPED" else [f"{NAME} {q}"]
        p += [] if dc.port_free() else ["port taken"]
        p += [] if prod == {"active": "active", "answer": 200} else [f"platmon service {prod}"]
        p += [] if sha(INI) == sha0 else ["the INI changed"]
        return {"production": prod, "problems": p}

    with open(out, "a") as f:
        f.write(json.dumps({"setup": setup("checks")}) + "\n")
    first = step("ready", ready_check)
    if first["problems"]:
        return
    # the service keeps its failure count for a day (reset= 86400), so the guide's actions wait 60 s from an
    # earlier run's third failure on; restarting after 5 s every time (the last action repeats) keeps the
    # crash and failed_start waits the same in every run. The guide's actions are set back at the end.
    sc("failure", NAME, "reset=", "86400", "actions=", "restart/5000")
    try:
        for name, fn in (("start", start_check), ("restart", restart), ("crash", crash),
                         ("failed_start", failed_start), ("stop_collect", stop_collect), ("stop_replay", stop_replay),
                         ("upgrade", upgrade)):
            step(name, fn)
    finally:
        sc(*RECOVERY[0])
    step("end", end)


def soak(args):
    """deploy_check.soak on the candidate service; see the module docstring."""
    ph = dict(dc.QUICK if args.quick else dc.ACCEL if args.accel else dc.SOAK)
    out = args.out

    def write(rec):
        with open(out, "a") as f:
            f.write(json.dumps(rec) + "\n")

    write({"setup": setup("soak"), "phases": ph, "hours": args.hours})
    r, s = start()
    if not s:
        write({"failed": "not ready", **r})
        return
    clock = ab.Clock()
    proc = Proc(r["pid"])
    rss = ab.Rss(proc, clock)
    rw = worker.Worker("rss", rss.run)
    rw.start()
    hub = ab.Hub(PORT, clock)
    hub.mono0, hub.wall0 = clock.now(), time.time()
    marks = {"losses": 0, "problems": 0, "ticks": proc.ticks(), "t": clock.now(), "pid": proc.pid}
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
        st = dc.status()
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
                            "cursor": cursor, "missing": {g: hub.walks[g].missing[missing0[g]:] for g in target}}
        for g in target:
            hub.walks[g].target = None
            hub.first[g] = False
        held = ab.retained(PORT, proc).get("replay", {})
        rec["reconnect"]["oldest_after"] = {g: v[0] for g, v in held.items()}
        cur = {g: max(cursor[g], held[g][0] - 1 + (held[g][1] - held[g][0]) // 5) if expire and g in held
               else cursor[g] for g in cursor}
        conc = ab.concurrent(PORT, clock, proc, rss, hub, cur, ph)
        rec["concurrent"] = {k: conc[k] for k in ("backlog", "done", "seconds", "cpu_s_each", "start_pss_kib",
                                                  "peak_rss_kib", "peak_pss_kib", "losses", "problems",
                                                  "requests", "errors", "records", "bytes", "response_ms")}
        rec["concurrent"]["workers"] = [w for r_ in conc["rounds"] for w in r_["workers"]]
        time.sleep(ph["settle"])
        rec["settled"] = {**ab.retained(PORT, proc), **proc.measure()}
        q = query()
        rec["unit"] = {"ActiveState": "active" if q["state"] == "RUNNING" else str(q["state"]), "SubState": q["state"],
                       "NRestarts": "0" if q["pid"] == marks["pid"] else "1", "MainPID": str(q["pid"])}
        if h + 1 == ph["restart_at"]:
            old = dc.status()["instance_id"]
            stop_, start_ = ctl("stop"), ctl("start")
            secs, s = dc.ready(other_than=old)
            rss.end()
            rw.finish()
            proc = Proc(query()["pid"])
            rss = ab.Rss(proc, clock)
            rw = worker.Worker("rss", rss.run)
            rw.start()
            hub.walks = {g: walk.Walk(g) for g in ab.STREAMS}
            hub.first = {g: True for g in ab.STREAMS}
            hub.supported = {g: None for g in ab.STREAMS}
            marks["ticks"] = proc.ticks()
            restarts.append(h)
            rec["restart"] = {"restart": [stop_, start_], "ready_s": secs, "old_cursor": dc.old_cursor(old),
                              "pid": proc.pid}
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
        rec["vmrss_max_kib"] = max([v for t, v in rss.trace if t >= span[0]], default=None)
        rec["tool"] = own()
        write(rec)
        print(f"hour {h}: settled private {rec['settled'].get('pss')} KiB threads {rec['settled'].get('threads')} "
              f"handles {rec['settled'].get('fds')} losses {len(rec['hub']['losses'])} "
              f"problems {len(rec['hub']['problems'])}", file=sys.stderr, flush=True)
        marks = {"losses": len(hub.losses), "problems": len(hub.problems), "ticks": proc.ticks(), "t": clock.now(),
                 "pid": proc.pid}
        hub.core.clear()
        hub.requests.clear()
        rss.trace.clear()
        if h + 1 < args.hours:
            state = poll((h + 1) * ph["hour"])
            if worker.problems("hub", state):
                write({"hour": h, "between_hours": state})
    rss.end()
    end = {"end": True, "rss_worker": rw.finish(), "restarts": restarts, "measure": proc.measure()}
    pid = proc.pid
    stop = ctl("stop")
    end["stop"] = stop
    end["stopped"] = stopped(pid, stop[2])
    end["production"] = production()
    end["tool"] = own()
    write(end)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("binary")
    sub.add_parser("uninstall")
    c = sub.add_parser("checks")
    c.add_argument("--out", required=True)
    c.add_argument("--previous", help="an earlier platmon-server.exe for the upgrade check")
    s = sub.add_parser("soak")
    s.add_argument("--out", required=True)
    s.add_argument("--hours", type=int, default=24)
    s.add_argument("--accel", action="store_true", help="deploy_check.py's accelerated phases")
    s.add_argument("--quick", action="store_true", help="deploy_check.py's quick phases (a dry run)")
    r = sub.add_parser("report")
    r.add_argument("path")
    r.add_argument("--settled-mib", type=float)
    r.add_argument("--peak-mib", type=float)
    args = ap.parse_args()
    if args.cmd == "report":
        args.slack = SLACK
        sys.exit(0 if dc.report(args) else 1)
    {"install": install, "uninstall": uninstall, "checks": checks, "soak": soak}[args.cmd](args)


if __name__ == "__main__":
    main()
