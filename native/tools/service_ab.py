"""Step 3C: the whole Python service (A) against `platmon-native serve` (B) on one host, A B B A, the same
platmon.ini (the defaults, 1 s collection, 127.0.0.1:PORT), the same user, one service at a time. Only the
processes this tool starts are stopped, by their own handles. One JSON line per run, and a first line with
the host and what was run.

  python3 service_ab.py --tree PYTHON_TREE --binary platmon-native --out OUT.jsonl [--port 19797]
  python3 service_ab.py ... --variant S=SYSTEM_BINARY --variant M=MIMALLOC_BINARY --order "S M M S"

Each run, in seconds from the service's start (docs/performance/budget.md for the first window):

  0-30      warmup            the budget client: GET /api/stats every 1 s, from the start
  30-150    w1                release window, buffers still filling: CPU, response times, cadence
  150-1290  fill              the budget client goes on; history and replay (600 s) fill and expire
  1290-1410 w2                the same release window with full buffers
  1410-1440 hub warmup        a Hub-like poller from no cursor (it catches up the buffers)
  1440-1560 w3                everyday Hub polling: CPU, requests done, records and bytes, data lag
  1560-2040 disconnected      no client; 1590-1710 is an idle window
  2040-     reconnect         the poller again, from its cursors: the backlog up to each stream's latest id
                              when it reconnects (480 s of it), five times from the same cursors, 2 s apart;
           concurrent         then four pollers fetch the same backlog (from the same cursors to the latest
                              ids) at once, three times, 2 s apart; VmRSS and PSS every 50 ms meanwhile
           settle             120 s with no client
           after              then 120 s more polling

The poller does what platmon-hub's Fleet does for one node (hub/runtime.py, hub/replay.py): one request at
a time; /api/status + /api/stats every 5 s; /api/observations every 10 s; replay turns round robin over
core, storage, wifi, battery and probe, each /api/status then one page of limit=100, the stream due again
0.05 s later while has_more, otherwise 5 s (core) or 10 s; a 404 stream again after 60 s. Every page is
checked by walk.Walk; a missing range is expected only on a stream's first page (what expired before the
poller came), anywhere else it is a loss.

The reconnect is judged by: the time until every stream's cursor reached its target, the process CPU
seconds over that time (the mean of the five rounds: a round is too short for one 10 ms tick to be
precise), the peak VmRSS (sampled every 50 ms and after each round) and VmHWM before and after, and the
collection cadence meanwhile (the replayed core records that started then). A backlog with an
unexpected missing range is not a success: the data is still within the retention.

CPU is /proc/PID/stat utime + stime + cutime + cstime, in clock ticks (SC_CLK_TCK); % of one core over a
window. VmRSS every 5 s through the run; smaps_rollup Rss/Pss/Pss_Anon, threads and open descriptors at the
main points, with what is retained then (replay oldest..latest per stream from an empty page, history series
and points)."""
import argparse
import http.client
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time

import walk
import worker

TICK = os.sysconf("SC_CLK_TCK")
STREAMS = ("core", "storage", "wifi", "battery", "probe")
PHASES = {"warmup": 30, "window": 120, "w2_at": 1290, "hub_warmup": 30, "disconnect": 480, "idle_at": 30,
          "after": 120, "reconnect_limit": 300, "repeats": 5, "concurrent_rounds": 3, "concurrent_pollers": 4,
          "settle": 120}


def get(port, path, timeout=10):
    """(code, body, seconds); the time is from connecting to the whole body."""
    t = time.monotonic()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        return r.status, body, time.monotonic() - t
    finally:
        c.close()


def pct(values, q):
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 3) if v else None


def spread(values):
    return {"n": len(values), "p50": pct(values, 0.5), "p95": pct(values, 0.95), "max": pct(values, 1.0)}


class Proc:
    def __init__(self, pid):
        self.pid = pid

    def ticks(self):
        with open(f"/proc/{self.pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return sum(int(x) for x in fields[11:15])  # utime stime cutime cstime

    def status(self):
        with open(f"/proc/{self.pid}/status") as f:
            s = dict(line.split(":", 1) for line in f)
        return {k: int(s[k].split()[0]) for k in ("VmRSS", "VmHWM", "Threads")}

    def fds(self):
        return len(os.listdir(f"/proc/{self.pid}/fd"))

    def rollup(self):
        with open(f"/proc/{self.pid}/smaps_rollup") as f:
            s = dict(line.split(":", 1) for line in f if ":" in line and not line.startswith("0"))
        return {k: int(s[k].split()[0]) for k in ("Rss", "Pss", "Pss_Anon", "Pss_File", "Anonymous") if k in s}


class Clock:
    def __init__(self):
        self.t0 = time.monotonic()

    def at(self, s):
        """Sleeps until s seconds after the start."""
        time.sleep(max(0.0, self.t0 + s - time.monotonic()))

    def now(self):
        return time.monotonic() - self.t0


class Rss:
    def __init__(self, proc, clock):
        self.proc, self.clock, self.every, self.trace, self.stop = proc, clock, 5.0, [], threading.Event()
        self.gone, self.pss, self.pss_trace, self.wake = None, False, [], threading.Event()

    def speed(self, every, pss=False):
        """Samples every `every` s from now on (not after the wait under way), with PSS if asked."""
        self.every, self.pss = every, pss
        self.wake.set()

    def end(self):
        self.stop.set()
        self.wake.set()

    def run(self):
        while True:
            self.wake.wait(self.every)
            self.wake.clear()
            if self.stop.is_set():
                return
            try:
                t = round(self.clock.now(), 2)
                self.trace.append((t, self.proc.status()["VmRSS"]))
                if self.pss:  # smaps_rollup as well, while a peak is looked for
                    self.pss_trace.append((t, self.proc.rollup()["Pss"]))
            except (OSError, KeyError) as e:  # gone, or a zombie without VmRSS: the trace ends here
                self.gone = (round(self.clock.now(), 2), type(e).__name__)
                return


class Budget:
    """GET /api/stats every 1 s on a fixed grid: response times, codes, the published sample."""
    def __init__(self, port, clock):
        self.port, self.clock, self.answers, self.stop = port, clock, [], threading.Event()

    def run(self):
        n = 0
        while not self.stop.is_set():
            self.clock.at(n)
            n += 1
            t = self.clock.now()
            try:
                code, body, s = get(self.port, "/api/stats")
                sample = json.loads(body).get("sample", {}) if code == 200 else {}
                self.answers.append((t, code, s, sample.get("sequence"), sample.get("started_at"),
                                     sample.get("duration_ms")))
            except (OSError, ValueError, http.client.HTTPException) as e:
                self.answers.append((t, type(e).__name__, None, None, None, None))

    def window(self, a, b):
        got = [x for x in self.answers if a <= x[0] < b]
        ok = [x for x in got if x[1] == 200 and x[3] is not None]
        seqs = sorted({x[3] for x in ok})
        starts = sorted({(x[3], x[4]) for x in ok})
        gaps = [(s2 - s1) / (q2 - q1) for (q1, s1), (q2, s2) in zip(starts, starts[1:])]
        return {"requests": len(got), "errors": len(got) - len(ok), "response_ms": spread([x[2] * 1e3 for x in ok]),
                "publications": seqs[-1] - seqs[0] if seqs else 0, "interval_s": spread(gaps),
                "collection_ms": spread([x[5] for x in ok if x[5] is not None])}


class Hub:
    """One node as platmon-hub's Fleet polls it; see the module docstring."""
    def __init__(self, port, clock):
        self.port, self.clock = port, clock
        self.walks = {s: walk.Walk(s) for s in STREAMS}
        self.first = {s: True for s in STREAMS}
        self.supported = {s: None for s in STREAMS}
        self.requests, self.core, self.losses, self.problems = [], {}, [], []

    def ask(self, kind, path):
        t = self.clock.now()
        try:
            code, body, s = get(self.port, path)
        except (OSError, http.client.HTTPException) as e:
            self.requests.append((t, kind, type(e).__name__, None, 0, 0))
            return None, None
        self.requests.append((t, kind, code, s, len(body), 0))
        return code, body

    def turn(self, stream):
        """One replay turn: status, then one page. The seconds until the stream is due again."""
        code, body = self.ask("status", "/api/status")
        if code != 200:
            return 5
        instance = json.loads(body)["instance_id"]
        w = self.walks[stream]
        if w.instance not in (None, instance):
            self.problems.append(f"{stream}: instance {instance} after {w.instance}")
            return 60
        path = "/api/samples" if stream == "core" else f"/api/observation-samples?group={stream}"
        path += ("&" if "?" in path else "?") + f"instance_id={instance}&after={w.after}&limit=100"
        code, body = self.ask("replay", path)
        if code == 404 and stream != "core":
            self.supported[stream] = False
            return 60
        if code != 200:
            self.problems.append(f"{stream}: HTTP {code} for {path}")
            return 5
        received = time.time()
        page = json.loads(body)
        self.supported[stream] = True
        before = (len(w.problems), w.after)
        w.page(page)
        if len(w.problems) > before[0]:
            self.problems += w.problems[before[0]:]
        if page["missing_ranges"] and not self.first[stream]:
            self.losses.append((round(self.clock.now(), 2), stream, page["missing_ranges"]))
        self.first[stream] = False
        t, kind, c, s, size, _ = self.requests[-1]
        self.requests[-1] = (t, kind, c, s, size, len(page["records"]))
        for r in page["records"]:
            if stream == "core":
                d = r["data"]["sample"]
                self.core[r["id"]] = (d["started_at"], d["duration_ms"], r["gap_before"], received - d["completed_at"])
        return 0.05 if page["has_more"] else 5 if stream == "core" else 10

    def run(self, stop, done=None):
        """Polls until `stop` is set, or until done() says so after a turn."""
        now = self.clock.now()
        due = {"poll": now, "observations": now, **{s: now for s in STREAMS}}  # poll: status and stats
        rr = 0
        while not stop.is_set():
            now = self.clock.now()
            if due["poll"] <= now:
                self.ask("status", "/api/status")
                self.ask("stats", "/api/stats")
                due["poll"] = now + 5
            elif due["observations"] <= now:
                self.ask("observations", "/api/observations")
                due["observations"] = now + 10
            else:
                ready = [s for s in STREAMS[rr:] + STREAMS[:rr] if due[s] <= now]
                if ready:
                    s = ready[0]
                    rr = (STREAMS.index(s) + 1) % len(STREAMS)
                    due[s] = self.clock.now() + self.turn(s)
                    if done and done():
                        return
                else:
                    stop.wait(max(0.0, min(due.values()) - self.clock.now()))

    def window(self, a, b):
        got = [x for x in self.requests if a <= x[0] < b]
        ok = [x for x in got if x[2] == 200 or x[2] == 404 and x[1] == "replay"]
        lag = [v[3] * 1e3 for v in self.core.values() if a <= v[0] - self.wall0 + self.mono0 < b]
        return {"requests": len(got), "errors": len(got) - len(ok),
                "by_kind": {k: sum(1 for x in got if x[1] == k) for k in ("status", "stats", "observations", "replay")},
                "records": sum(x[5] for x in got), "bytes": sum(x[4] for x in got),
                "response_ms": {k: spread([x[3] * 1e3 for x in ok if x[1] == k]) for k in ("stats", "replay")},
                "core_lag_ms": spread(lag)}

    def cadence(self, a, b):
        """The collections that started between a and b (seconds from the start), from replayed records."""
        rows = sorted((i, v) for i, v in self.core.items() if a <= v[0] - self.wall0 + self.mono0 < b)
        gaps = [(v2[0] - v1[0]) / (i2 - i1) for (i1, v1), (i2, v2) in zip(rows, rows[1:])]
        missing = sum(i2 - i1 - 1 for (i1, _), (i2, _) in zip(rows, rows[1:]))
        return {"collections": len(rows), "missing_ids": missing, "gap_before": sum(1 for _, v in rows if v[2]),
                "interval_s": spread(gaps), "max_step_s": round(max((v2[0] - v1[0] for (_, v1), (_, v2) in zip(rows, rows[1:])),
                                                                  default=0), 3),
                "collection_ms": spread([v[1] for _, v in rows])}


def cpu_window(proc, clock, a, b):
    clock.at(a)
    c0, t0 = proc.ticks(), time.monotonic()
    clock.at(b)
    c1, t1 = proc.ticks(), time.monotonic()
    return {"from": a, "to": b, "cpu_s": (c1 - c0) / TICK, "cpu_pct": round(100 * (c1 - c0) / TICK / (t1 - t0), 3)}


def retained(port, proc):
    """What the service holds now, and its memory: smaps_rollup, replay oldest..latest, history."""
    out = {"rollup_kib": proc.rollup(), "fds": proc.fds(),
           **{k.lower() + "_kib" if k != "Threads" else "threads": v for k, v in proc.status().items()}}
    status = json.loads(get(port, "/api/status")[1])
    obs = json.loads(get(port, "/api/observations")[1])
    latest = {"core": status["sample"]["sequence"],
              **{g: (o.get("observation") or {}).get("id") for g, o in obs.get("groups", {}).items()}}
    out["replay"] = {}
    for s, last in latest.items():
        if last is None:
            continue
        path = "/api/samples" if s == "core" else f"/api/observation-samples?group={s}"
        code, body, _ = get(port, path + ("&" if "?" in path else "?") +
                            f"instance_id={status['instance_id']}&after={last}&limit=1")
        if code == 200:
            b = json.loads(body)
            out["replay"][s] = [b["oldest_id"], b["latest_id"]]
    code, body, _ = get(port, "/api/history")
    if code == 200:
        h = json.loads(body)
        out["history"] = {"series": len(h["series"]), "points": sum(len(p) for p in h["series"].values()),
                          "dropped_series": h["dropped_series"]}
    return out


def config(port):
    path = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
    path.write(f"[core]\ninterval = 1.0\n\n[http]\nbind = 127.0.0.1\nport = {port}\n")
    path.close()
    return path.name


def command(variant, args, ini):
    """A: the Python service; B: --binary; any other name: its --variant binary."""
    if variant == "A":
        return [sys.executable, f"{args.tree}/platmon.py", ini]
    binary = args.binary if variant == "B" else args.variants[variant]
    return [binary, "serve", ini, "--web", f"{args.tree}/frontends/web", "--app-version", args.app_version]


def concurrent(port, clock, proc, rss, hub, cursor, ph):
    """concurrent_pollers pollers fetch the same backlog at once, from the same cursors to the latest ids,
    concurrent_rounds times, 2 s apart: the seconds until all are done, the CPU seconds, the pages' times,
    the peak VmRSS and PSS. Every missing range is a loss (the cursors are within the retention). The pollers
    stop at the ids they started for, so the collections meanwhile come from the main poller's after walk
    (run(): "collected")."""
    status = json.loads(get(port, "/api/status")[1])
    obs = json.loads(get(port, "/api/observations")[1])
    target = {"core": status["sample"]["sequence"]}
    for s in cursor:
        o = (obs.get("groups", {}).get(s) or {}).get("observation") or {}
        if s != "core" and o.get("id") is not None:
            target[s] = o["id"]
    rounds, hubs, first = [], [], clock.now()
    start = {"rss_kib": proc.status()["VmRSS"], "pss_kib": proc.rollup()["Pss"]}  # a reading before round 1
    rss.speed(0.05, pss=True)
    for _ in range(ph["concurrent_rounds"]):
        group = []
        for _ in range(ph["concurrent_pollers"]):
            h = Hub(port, clock)
            h.mono0, h.wall0 = hub.mono0, hub.wall0
            for s in target:
                h.walks[s].after, h.walks[s].target, h.walks[s].instance = cursor[s], target[s], status["instance_id"]
                h.first[s], h.supported[s] = False, True
            for s in STREAMS:
                if s not in target:
                    h.supported[s] = hub.supported[s]
            group.append(h)
        stop = threading.Event()
        limit = threading.Timer(ph["reconnect_limit"], stop.set)
        threads = [worker.Worker(f"concurrent {i}", h.run, stop,
                                 lambda h=h: all(h.walks[s].after >= t for s, t in target.items()))
                   for i, h in enumerate(group)]
        limit.start()
        c0, t0 = proc.ticks(), time.monotonic()
        for t in threads:
            t.start()
        states = [t.finish(ph["reconnect_limit"] + 10) for t in threads]
        c1, t1 = proc.ticks(), time.monotonic()
        limit.cancel()
        rounds.append({"seconds": round(t1 - t0, 3), "cpu_s": (c1 - c0) / TICK,
                       "rss_kib": proc.status()["VmRSS"], "pss_kib": proc.rollup()["Pss"],
                       "done": all(h.walks[s].after >= t for h in group for s, t in target.items()),
                       "workers": states})
        hubs += group
        time.sleep(2)
    last = clock.now()
    rss.speed(5.0)
    merged = Hub(port, clock)
    merged.mono0, merged.wall0 = hub.mono0, hub.wall0
    for h in hubs:
        merged.requests += h.requests
        merged.core.update(h.core)
    out = {"target": target, "backlog": {s: t - cursor[s] for s, t in target.items()}, "rounds": rounds,
           "done": all(r["done"] for r in rounds), "from": first, "to": last, "span_s": round(last - first, 2),
           "seconds": spread([r["seconds"] for r in rounds]),
           "cpu_s_each": round(sum(r["cpu_s"] for r in rounds) / len(rounds), 4),
           # the peak is the highest reading: the one before round 1, the samples every 50 ms, the one after each round
           "start_rss_kib": start["rss_kib"], "start_pss_kib": start["pss_kib"],
           "peak_rss_kib": max([v for t, v in rss.trace if first <= t <= last] + [r["rss_kib"] for r in rounds] + [start["rss_kib"]]),
           "peak_pss_kib": max([v for t, v in rss.pss_trace if first <= t <= last] + [r["pss_kib"] for r in rounds] + [start["pss_kib"]]),
           "samples": sum(1 for t, _ in rss.pss_trace if first <= t <= last),
           "losses": [x for h in hubs for x in h.losses], "problems": [x for h in hubs for x in h.problems]}
    out.update(merged.window(first, last))
    out["cadence"] = merged.cadence(first - 1, last + 1)  # the pollers' own: they stop at the ids they started for
    return out


def run(variant, args, ini, ph):
    log = tempfile.TemporaryFile("w+")
    env = {**os.environ, "PYTHONPATH": args.tree} if variant == "A" else dict(os.environ)
    p = subprocess.Popen(command(variant, args, ini), cwd=args.tree, stdout=log, stderr=log, env=env)
    clock, proc = Clock(), Proc(p.pid)
    res = {"variant": variant, "pid": p.pid, "started": time.time(), "phases": ph}
    budget, rss = Budget(args.port, clock), Rss(proc, clock)
    workers = {"budget": worker.Worker("budget", budget.run), "rss": worker.Worker("rss", rss.run)}
    res["workers"] = {}
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if get(args.port, "/api/status", 2)[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline or p.poll() is not None:
                raise RuntimeError(f"{variant} not ready")
            time.sleep(0.1)
        res["ready_s"] = round(clock.now(), 2)
        workers["budget"].start()
        workers["rss"].start()
        w, ww = ph["warmup"], ph["window"]
        res["w1"] = cpu_window(proc, clock, w, w + ww)
        res["w1"].update(budget.window(w, w + ww))
        res["points"] = {"w1_end": retained(args.port, proc)}
        res["w2"] = cpu_window(proc, clock, ph["w2_at"], ph["w2_at"] + ww)
        res["w2"].update(budget.window(ph["w2_at"], ph["w2_at"] + ww))
        budget.stop.set()
        res["workers"]["budget"] = workers["budget"].finish()
        res["points"]["filled"] = retained(args.port, proc)
        hub, stop = Hub(args.port, clock), threading.Event()
        hub.mono0, hub.wall0 = clock.now(), time.time()
        th = worker.Worker("hub w3", hub.run, stop)
        h0 = ph["w2_at"] + ww
        th.start()
        w3 = h0 + ph["hub_warmup"]
        res["w3"] = cpu_window(proc, clock, w3, w3 + ww)
        stop.set()
        res["workers"]["hub w3"] = th.finish()
        res["w3"].update(hub.window(w3, w3 + ww))
        res["w3"]["cadence"] = hub.cadence(w3, w3 + ww)
        res["hub_catch_up"] = {s: hub.walks[s].pages for s in STREAMS}
        res["points"]["hub_end"] = retained(args.port, proc)
        off = w3 + ww
        res["idle"] = cpu_window(proc, clock, off + ph["idle_at"], off + ph["idle_at"] + ww)
        clock.at(off + ph["disconnect"])
        res["points"]["disconnected"] = retained(args.port, proc)
        status = json.loads(get(args.port, "/api/status")[1])
        obs = json.loads(get(args.port, "/api/observations")[1])
        target = {"core": status["sample"]["sequence"]}
        for s in STREAMS[1:]:
            o = (obs.get("groups", {}).get(s) or {}).get("observation") or {}
            if hub.supported[s] and o.get("id") is not None:
                target[s] = o["id"]
        cursor = {s: hub.walks[s].after for s in target}
        losses0, problems0 = len(hub.losses), len(hub.problems)
        hwm0 = proc.status()["VmHWM"]
        rss.speed(0.05)
        rounds, first = [], clock.now()
        for _ in range(ph["repeats"]):  # the same backlog again: CPU seconds above the tick's resolution
            for s, t in target.items():
                hub.walks[s].after, hub.walks[s].target = cursor[s], t
            stop = threading.Event()
            limit = threading.Timer(ph["reconnect_limit"], stop.set)
            limit.start()
            r0, c0, t0 = clock.now(), proc.ticks(), time.monotonic()
            hub.run(stop, done=lambda: all(hub.walks[s].after >= t for s, t in target.items()))
            c1, t1, r1 = proc.ticks(), time.monotonic(), clock.now()
            limit.cancel()
            st = proc.status()
            rounds.append({"seconds": round(t1 - t0, 3), "cpu_s": (c1 - c0) / TICK, "rss_kib": st["VmRSS"],
                           "done": all(hub.walks[s].after >= t for s, t in target.items()),
                           "cursors": {s: hub.walks[s].after for s in target}})
            time.sleep(2)
        last = clock.now()
        rss.speed(5.0)
        peak = max([v for t, v in rss.trace if first <= t <= last] + [r["rss_kib"] for r in rounds])
        res["reconnect"] = {"target": target, "backlog": {s: t - cursor[s] for s, t in target.items()},
                            "rounds": rounds, "done": all(r["done"] for r in rounds),
                            "seconds": spread([r["seconds"] for r in rounds]),
                            "cpu_s_each": round(sum(r["cpu_s"] for r in rounds) / len(rounds), 4),
                            "peak_rss_kib": peak, "vmhwm_kib": [hwm0, proc.status()["VmHWM"]],
                            "losses": hub.losses[losses0:], "problems": hub.problems[problems0:]}
        res["reconnect"]["span_s"] = round(last - first, 2)
        res["reconnect"].update(hub.window(first, last))
        res["reconnect"]["cadence"] = hub.cadence(first - 1, last + 1)
        r1 = last
        res["points"]["reconnected"] = retained(args.port, proc)
        if ph.get("concurrent_rounds"):
            res["concurrent"] = concurrent(args.port, clock, proc, rss, hub, cursor, ph)
            res["points"]["concurrent"] = retained(args.port, proc)
            time.sleep(ph["settle"])  # no client: threads, descriptors and memory settle
            res["points"]["settled"] = retained(args.port, proc)
            r1 = clock.now()
        for s in target:
            hub.walks[s].target = None
        stop = threading.Event()
        th = worker.Worker("hub after", hub.run, stop)
        th.start()
        clock.at(r1 + ph["after"])
        stop.set()
        res["workers"]["hub after"] = th.finish()
        res["after"] = {"cadence": hub.cadence(r1, r1 + ph["after"])}
        if "concurrent" in res:  # the collections during the rounds, which the after walk replayed
            res["concurrent"]["collected"] = hub.cadence(res["concurrent"]["from"] - 1, res["concurrent"]["to"] + 1)
        res["points"]["after"] = retained(args.port, proc)
        res["hub"] = {"losses": hub.losses, "problems": hub.problems, "supported": hub.supported,
                      "pages": {s: hub.walks[s].pages for s in STREAMS}}
    except Exception as e:
        res["failed"] = f"{type(e).__name__}: {e}"
    finally:
        budget.stop.set()
        rss.end()
        for name, w in workers.items():
            if w.ident is not None and name not in res["workers"]:
                res["workers"][name] = w.finish()
        res["rss_gone"] = rss.gone
        res["exited_by_itself"] = p.poll()
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    res["rss_trace"] = rss.trace if len(rss.trace) < 2000 else rss.trace[::len(rss.trace) // 2000 + 1]
    log.seek(0)
    res["log"] = log.read().splitlines()[-20:]
    return res


def self_test():
    """The poller in its Worker against a synthetic node: answering well it ends when stopped, with no
    problem; answering a page it cannot decode, the thread's end is in the result instead of a quiet window."""
    global get
    real, passed = get, True

    def node(broken):
        n = [0]

        def fake(port, path, timeout=10):
            if path == "/api/status":
                return 200, b'{"instance_id": "i", "sample": {"sequence": 3}}', 0.001
            if not path.startswith("/api/samples"):
                return (404, b"{}", 0.001) if "observation-samples" in path else (200, b'{"groups": {}}', 0.001)
            n[0] += 1
            if broken and n[0] > 1:
                return 200, b'{"records": [', 0.001
            first = n[0] == 1
            recs = [{"id": i, "gap_before": False,
                     "data": {"sample": {"started_at": time.time(), "duration_ms": 1.0, "completed_at": time.time()}}}
                    for i in ((1, 2) if first else (3,) if n[0] == 2 else ())]
            body = {"instance_id": "i", "stream": "core", "records": recs, "missing_ranges": [],
                    "next_after": 2 if first else 3, "has_more": first, "latest_id": 3}
            return 200, json.dumps(body).encode(), 0.001
        return fake

    for name, broken, want in (("answers well", False, []), ("an undecodable page", True, ["raised JSONDecodeError"])):
        get = node(broken)
        hub, stop = Hub(0, Clock()), threading.Event()
        w = worker.Worker("hub", hub.run, stop)
        w.start()
        time.sleep(0.5)
        stop.set()
        got = worker.problems("hub", w.finish(5)) + hub.problems
        good = len(got) == len(want) and all(x in g for x, g in zip(want, got)) and hub.walks["core"].pages >= 1
        print(f"service_ab {'ok  ' if good else 'FAIL'} {name}: {got}", file=sys.stderr)
        passed &= good
    get = real
    return passed


def sha256(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def host(args, ini):
    def read(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None
    def run_(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except OSError:
            return None
    with open(ini) as f:
        cfg = f.read()
    return {"host": socket.gethostname(), "kernel": platform.release(), "machine": platform.machine(),
            "cpus": os.cpu_count(), "python": sys.version.split()[0], "user": os.getuid(),
            "governor": read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
            "nvpmodel": run_(["nvpmodel", "-q"]) if os.path.exists("/usr/sbin/nvpmodel") else None,
            "loadavg": read("/proc/loadavg"), "clk_tck": TICK,
            "tree": args.tree, "tree_ref": args.tree_ref, "binary": args.binary, "binary_sha256": sha256(args.binary),
            "tools_sha256": {os.path.basename(f): sha256(f) for f in (__file__, walk.__file__, worker.__file__)},
            "variants_sha256": {n: sha256(b) for n, b in args.variants.items()}, "order": args.order.split(),
            "commands": {v: command(v, args, ini) for v in dict.fromkeys(["A", "B", *args.variants])}, "config": cfg}


def main():
    ap = argparse.ArgumentParser()
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(0 if self_test() & worker.self_test() & walk.self_test() else 1)
    if not (self_test() and worker.self_test() and walk.self_test()):
        sys.exit("the self-test failed")
    ap.add_argument("--tree", required=True)
    ap.add_argument("--tree-ref", default=None)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--variant", action="append", default=[], metavar="NAME=BINARY",
                    help="another binary to run as variant NAME (e.g. S=system M=mimalloc)")
    ap.add_argument("--app-version", default="1.3.0")
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=19797)
    ap.add_argument("--order", default="A B B A")
    ap.add_argument("--quick", action="store_true", help="short phases, to try the tool")
    args = ap.parse_args()
    args.tree, args.binary = os.path.abspath(args.tree), os.path.abspath(args.binary)
    args.variants = {n: os.path.abspath(b) for n, b in (v.split("=", 1) for v in args.variant)}
    ph = dict(PHASES)
    if args.quick:
        ph.update(warmup=5, window=15, w2_at=40, hub_warmup=10, disconnect=40, idle_at=5, after=10, reconnect_limit=30, repeats=3,
                  concurrent_rounds=2, settle=10)
    ini = config(args.port)
    with open(args.out, "a") as f:
        f.write(json.dumps({"setup": host(args, ini)}) + "\n")
    for v in args.order.split():
        r = run(v, args, ini, ph)
        with open(args.out, "a") as f:
            f.write(json.dumps(r) + "\n")
        print(f"{v}: w1 {r.get('w1', {}).get('cpu_pct')} w2 {r.get('w2', {}).get('cpu_pct')} "
              f"w3 {r.get('w3', {}).get('cpu_pct')} reconnect {r.get('reconnect', {}).get('seconds', {}).get('p50')} s "
              f"{r.get('reconnect', {}).get('cpu_s_each')} cpu s {r.get('failed', '')}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
