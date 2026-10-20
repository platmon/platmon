"""Live check of the HTTP runtime on a real host: starts the Python service (platmon.py) and `platmon-native
serve` with the same platmon.ini (the defaults, each on its own local port), checks each the same way and
compares them. Only the processes it started are stopped (and restarted once), by their own handles.

  PYTHONPATH=<platmon tree> python3 live_http.py PATH/TO/platmon-native [--json OUT]

Per server:
- every path: status code, Content-Type, Cache-Control; the web page and its files: the same bytes; the
  /text screen: the same sections
- raw requests at the protocol's edges (HEAD, HTTP/0.9, versions, header and line limits): the same answer
  shape from its bytes (status line and headers or none, header names, a body or none)
- /api/stats, /api/status, /api/observations: the same keys, order and JSON types; values exact except
  those that move (compare.py's plausibility rules, sample identity and ages, read offsets, the slow
  groups' times and readings)
- /api/history: the same series ids, cadences, bounds and dropped count, every point [int, float, number or
  null]; /api/samples: the same envelope, records with /api/stats' keys and sample.stale
- replay from the start, page by page, as walk.py checks it: one instance and stream, ids rising strictly,
  every page explaining exactly the ids it moves the cursor over, the cursor moving while has_more and
  ending at latest_id; a walk that does not end within 50 pages fails
- 16 clients × 20 requests at once: all 320 answers verified (200, JSON, a sample), no client error, one
  instance, sequences never going back, and no more collections than intervals (requests do not collect).
  A self-test runs first (`--self-test` alone): fake answers that time out, are not JSON, have no sample,
  are a 503 or go back after a client's first answer must each fail the check; walk.py's own self-test too
- 40 idle connections, opened 20 ms apart: 32 held, the rest closed without an answer, a request refused
  while 32 are held and answered after they close
- a request line cut off: the connection closed after the 10 s request timeout
- 200 connections at once: handler threads stay within 32 more than before, the server answers again
  once they close, and its threads go back (which connections were refused is not compared: timing)
- a restart: the previous instance's cursor gets 409 instance_changed with the new instance id
Exit 1 on any difference or failed check."""
import concurrent.futures as concurrent_futures
import configparser
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import walk
import worker
from compare import kind, plausible

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PATHS = ["/api/stats", "/api/status", "/api/observations", "/api/history", "/api/samples",
         "/api/observation-samples?group=storage", "/api/stats?x=1", "/", "/index.html", "/app.js", "/styles.css",
         "/assets/brand/favicon.svg", "/assets/brand/platmon-logo-dark.svg", "/nothing", "/text", "/text?x"]
WEB = ["/", "/index.html", "/app.js", "/styles.css", "/assets/brand/favicon.svg", "/assets/brand/platmon-logo-dark.svg"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(port, path, timeout=10):
    """(code, headers, body)"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def wait_ready(port, deadline):
    while time.monotonic() < deadline:
        try:
            if json.loads(fetch(port, "/api/status", 2)[2])["ready"]:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def moving(ptr, a, b):
    """True where two servers' values may differ: what compare.py allows for two collections, the sample's
    identity and times, read offsets, and how long a running collection has taken."""
    if isinstance(a, (int, float)) and not isinstance(a, bool) and plausible("/stats" + ptr, a, b):
        return True
    parts = ptr.split("/")
    observed = ptr.startswith("/groups/") and len(parts) > 3
    return (ptr.startswith(("/sample/", "/last_attempt/completed_at", "/last_attempt/duration_ms"))
            or ptr in ("/instance_id", "/collecting_for_ms")
            or "read" in parts[:-1] and parts[-1] in ("started_offset_ms", "completed_offset_ms")
            # slow groups: their own times, and readings that move (bytes in use, a battery, a signal)
            or observed and (parts[3] in ("observation", "collecting_for_ms") or parts[3] == "data" and parts[2] in ("storage", "battery", "wifi")
                             and isinstance(a, (int, float)) and isinstance(b, (int, float))))


def diff(a, b, ptr, out):
    if ptr.endswith("/collecting_for_ms") and (a is None or b is None):
        return  # a collection may be running at one answer and not at the other
    if kind(a) != kind(b):
        out.append(f"{ptr}: {kind(a)} {a!r:.80} (python) != {kind(b)} {b!r:.80} (rust)")
    elif isinstance(a, dict):
        if list(a) != list(b):
            out.append(f"{ptr}: keys {list(a)} (python) != {list(b)} (rust)")
        for k in a:
            if k in b:
                diff(a[k], b[k], f"{ptr}/{k.replace('~', '~0').replace('/', '~1')}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{ptr}: length {len(a)} (python) != {len(b)} (rust)")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{ptr}/{i}", out)
    elif a != b and not moving(ptr, a, b):
        out.append(f"{ptr}: {a!r:.80} (python) != {b!r:.80} (rust)")


def endpoints(port):
    out = {}
    for path in PATHS:
        code, headers, body = fetch(port, path)
        out[path] = {"code": code, "type": headers.get("Content-Type"), "no_store": headers.get("Cache-Control") == "no-store",
                     "body": body}
    return out


def concurrent(port, interval, clients=16, requests=20, get=fetch):
    """clients × requests /api/stats at once. Each answer is verified (HTTP 200, JSON, a sample with an int
    sequence and an instance id, the sequence not going back for its client); anything a client raises is
    collected, never lost in its thread. Passes only with every answer verified and no error."""
    problems, planned = [], clients * requests
    first = json.loads(get(port, "/api/status")[2])["sample"]["sequence"]
    t0 = time.monotonic()

    def client():
        out = {"attempted": 0, "verified": 0, "errors": [], "ids": set()}
        last = 0
        for _ in range(requests):
            out["attempted"] += 1
            try:
                code, _, body = get(port, "/api/stats")
                if code != 200:
                    raise ValueError(f"HTTP {code}")
                sample = json.loads(body)["sample"]
                seq, iid = sample["sequence"], sample["instance_id"]
                if type(seq) is not int or not isinstance(iid, str):
                    raise ValueError(f"sample {sample!r:.80}")
            except Exception as e:  # a connection, read, decoding or structure error is a failed request
                out["errors"].append(f"{type(e).__name__}: {e}"[:120])
                continue
            if seq < last:
                out["errors"].append(f"sequence {seq} after {last}")
                continue
            last = seq
            out["ids"].add(iid)
            out["verified"] += 1
        return out

    with concurrent_futures.ThreadPoolExecutor(clients) as pool:
        futures = [pool.submit(client) for _ in range(clients)]
    results = []
    for f in futures:
        try:
            results.append(f.result())  # re-raises what the client raised outside its requests
        except Exception as e:
            problems.append(f"a client ended with {type(e).__name__}: {e}"[:160])
    elapsed = time.monotonic() - t0
    attempted, verified = sum(r["attempted"] for r in results), sum(r["verified"] for r in results)
    errors = [e for r in results for e in r["errors"]]
    ids = set().union(*(r["ids"] for r in results)) if results else set()
    if verified != planned:
        problems.append(f"{verified} of {planned} answers verified ({attempted} attempted, {len(errors)} failed)")
    problems += sorted(set(errors))[:5]
    if len(ids) > 1:
        problems.append(f"{len(ids)} instance ids")
    published = json.loads(get(port, "/api/status")[2])["sample"]["sequence"] - first
    if published > elapsed / interval + 2:
        problems.append(f"{published} snapshots in {elapsed:.1f} s: requests collected")
    return {"planned": planned, "attempted": attempted, "verified": verified, "failed": len(errors),
            "seconds": round(elapsed, 2), "published": published, "problems": problems}


def fake_get(fail):
    """A fetch for the self-test: answers like a server until a client's second request, then fails."""
    local = threading.local()

    def get(port, path, timeout=10):
        if path == "/api/status":
            return 200, {}, b'{"sample": {"sequence": 7}}'
        local.n = getattr(local, "n", 0) + 1
        good = b'{"sample": {"sequence": 7, "instance_id": "a"}}'
        if local.n == 1 or fail == "none":
            return 200, {}, good
        if fail == "timeout":
            raise socket.timeout("timed out")
        if fail == "garbage":
            return 200, {}, b"{not json"
        if fail == "no_sample":
            return 200, {}, b'{"cpu": []}'
        if fail == "http_503":
            return 503, {}, b"{}"
        if fail == "back":
            return 200, {}, b'{"sample": {"sequence": 6, "instance_id": "a"}}'
    return get


CONTROLS = [("none", True), ("timeout", False), ("garbage", False), ("no_sample", False), ("http_503", False),
            ("back", False)]


def self_test():
    """The concurrent check against fake answers: every failure after a client's first answer must fail it."""
    wrong = []
    for fail, passes in CONTROLS:
        r = concurrent(0, 1.0, get=fake_get(fail))
        if (not r["problems"]) != passes:
            wrong.append(f"  {fail}: {'failed' if passes else 'passed'}, must {'pass' if passes else 'fail'}: {r}")
    print(f"harness self-test: {len(CONTROLS) - len(wrong)}/{len(CONTROLS)} controls as expected", file=sys.stderr)
    for w in wrong:
        print(w, file=sys.stderr)
    return not wrong


def closed(sock, wait):
    """True if the server closed sock (EOF or reset) within wait seconds without answering."""
    sock.settimeout(wait)
    try:
        return sock.recv(1) == b""
    except ConnectionResetError:
        return True
    except socket.timeout:
        return False


def connection_limit(port, total=40):
    """Connections 20 ms apart: a burst meets the listen backlog of 5 first, and how its overflow is
    accepted later depends on timing, not on the limit (the Python server then answered a request while
    the client held 40 connections)."""
    socks = []
    for _ in range(total):
        socks.append(socket.create_connection(("127.0.0.1", port)))
        time.sleep(0.02)
    time.sleep(0.5)
    shut = sum(closed(s, 0.2) for s in socks)
    extra = socket.create_connection(("127.0.0.1", port))
    extra.sendall(b"GET /api/status HTTP/1.0\r\n\r\n")
    refused = closed(extra, 1.0)
    for s in socks + [extra]:
        s.close()
    time.sleep(0.5)
    after = fetch(port, "/api/status")[0]
    return {"connections": total, "closed": shut, "refused_while_full": refused, "after": after}


def request_timeout(port):
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(b"GET /api/sta")
    t0 = time.monotonic()
    s.settimeout(20)
    try:
        data = s.recv(1)
    except ConnectionResetError:
        data = b""
    s.close()
    return {"closed_after_s": round(time.monotonic() - t0, 2), "answered": data != b""}


RAW = {  # request -> what is compared of the answer: bare (no status line), status line, header names, body or not
    "HEAD": b"HEAD /api/status HTTP/1.0\r\n\r\n",
    "POST": b"POST /api/stats HTTP/1.0\r\n\r\n",
    "HTTP/0.9 said": b"GET /api/status HTTP/0.9\r\n\r\n",
    "HTTP/0.9 two words": b"GET /api/status\r\n\r\n",
    "HTTP/00.9": b"GET /api/status HTTP/00.9\r\n\r\n",
    "HTTP/2.0": b"GET /api/status HTTP/2.0\r\n\r\n",
    "bad version": b"GET /api/status HTTP/x\r\n\r\n",
    "99 headers": b"GET /api/status HTTP/1.0\r\n" + b"A: b\r\n" * 99 + b"\r\n",
    "100 headers": b"GET /api/status HTTP/1.0\r\n" + b"A: b\r\n" * 100 + b"\r\n",
    "long header": b"GET /api/status HTTP/1.0\r\nA: " + b"x" * 70000 + b"\r\n\r\n",
    "long request line": b"GET /" + b"a" * 70000 + b" HTTP/1.0\r\n\r\n",
    "empty line": b"\r\n",
    "//path": b"GET //api/status HTTP/1.0\r\n\r\n",
}


def raw(port, request):
    """The answer's shape, from its bytes: bare or not, status line, header names (not Date, Server), body."""
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(5)
    s.sendall(request)
    data = b""
    try:
        while chunk := s.recv(65536):
            data += chunk
    except (socket.timeout, ConnectionResetError) as e:
        data += f"<{type(e).__name__}>".encode()
    s.close()
    if not data.startswith(b"HTTP/"):
        return {"bare": True, "body": bool(data)}
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").replace("Request-URI Too Long", "URI Too Long").split("\r\n")  # Python < 3.13
    names = sorted(h.split(":")[0] for h in lines[1:] if h.split(":")[0] not in ("Date", "Server"))
    return {"bare": False, "status": lines[0], "headers": names, "body": bool(body)}


def task_status(pid):
    """(threads, VmRSS KiB) of a process."""
    with open(f"/proc/{pid}/status") as f:
        fields = dict(line.split(":", 1) for line in f if ":" in line)
    return int(fields["Threads"]), int(fields["VmRSS"].split()[0])


def burst(port, pid, n=200):
    """n connections opened at once (non-blocking, so the listen backlog fills): the server must keep its
    handler threads bounded, and answer again once they are gone. Not a comparison of which connections
    were refused: that depends on timing."""
    base = task_status(pid)
    socks = []
    for _ in range(n):
        s = socket.socket()
        s.setblocking(False)
        s.connect_ex(("127.0.0.1", port))
        socks.append(s)
    peak = base
    for _ in range(10):
        time.sleep(0.2)
        now = task_status(pid)
        peak = (max(peak[0], now[0]), max(peak[1], now[1]))
    for s in socks:
        s.close()
    t0, code = time.monotonic(), None
    while time.monotonic() - t0 < 15:
        try:
            code = fetch(port, "/api/status", 2)[0]
            break
        except OSError:
            time.sleep(0.2)
    recovered = round(time.monotonic() - t0, 2)
    time.sleep(1)
    return {"connections": n, "threads_before": base[0], "threads_peak": peak[0], "threads_after": task_status(pid)[0],
            "rss_kib_before": base[1], "rss_kib_peak": peak[1], "answered_after_s": recovered, "code": code}


def replay_walk(port):
    """Every core publication from the start, page by page, checked by walk.Walk; at most 50 pages of 2."""
    def get(after, instance):
        q = f"/api/samples?limit=2&after={after}" + (f"&instance_id={instance}" if instance else "")
        code, _, body = fetch(port, q)
        if code != 200:
            raise RuntimeError(f"HTTP {code} for {q}")
        return json.loads(body)
    try:
        return walk.walk(get, "core")
    except RuntimeError as e:
        return {"instance": None, "problems": [str(e)]}


def check(port, interval, pid):
    eps = endpoints(port)
    bodies = {p: json.loads(eps[p]["body"]) for p in ("/api/stats", "/api/status", "/api/observations")}
    bodies["/api/history"] = json.loads(fetch(port, "/api/history?points=5")[2])
    bodies["/api/samples"] = json.loads(fetch(port, "/api/samples?limit=2")[2])
    return {"endpoints": eps, "bodies": bodies, "raw": {k: raw(port, r) for k, r in RAW.items()},
            "replay": replay_walk(port), "concurrent": concurrent(port, interval),
            "connection_limit": connection_limit(port), "request_timeout": request_timeout(port), "burst": burst(port, pid)}


def config(port, interval):
    """platmon.ini with the defaults, but this port on 127.0.0.1: the same for both servers."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({"core": {"interval": str(interval)}, "http": {"bind": "127.0.0.1", "port": str(port)}})
    path = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
    cfg.write(path)
    path.close()
    return path.name


def start(name, binary, port, interval, log):
    if name == "python":
        return subprocess.Popen([sys.executable, f"{REPO}/platmon.py", config(port, interval)], cwd=REPO, stdout=log,
                                stderr=log, env={**os.environ, "PYTHONPATH": REPO})
    return subprocess.Popen([binary, "serve", config(port, interval), "--web", f"{REPO}/frontends/web",
                             "--app-version", version()], stdout=log, stderr=log)


def stop(p):
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def version():
    with open(f"{REPO}/frontends/cli.py") as f:
        return next(line.split('"')[1] for line in f if line.startswith("VERSION ="))


def heads(text):
    """The /text screen's sections: the first word of each line that is not indented."""
    return [line.split()[0] for line in text.split("\n")[2:] if line and not line.startswith(" ")]


def main():
    tested = self_test() & walk.self_test() & worker.self_test()
    if sys.argv[1:] == ["--self-test"] or not tested:
        sys.exit(0 if tested else 1)
    binary, interval = os.path.abspath(sys.argv[1]), 1.0
    ports = {"python": free_port(), "rust": free_port()}
    logs = {n: tempfile.TemporaryFile("w+") for n in ports}
    procs, restarted = {}, {}
    try:
        for n in ports:
            procs[n] = start(n, binary, ports[n], interval, logs[n])
        deadline = time.monotonic() + 15
        for n, port in ports.items():
            if not wait_ready(port, deadline):
                sys.exit(f"{n} is not ready after 15 s")
        time.sleep(6)  # past the first collections' warmup and the first slow observations on both
        threads = {n: worker.Worker(f"{n} check", check, ports[n], interval, procs[n].pid) for n in ports}
        for t in threads.values():  # at the same time, so both read the same moment as far as possible
            t.start()
        res = {n: t.result(300) for n, t in threads.items()}  # what a check raised ends the run here, by name
        for n in ports:  # a restart: the old cursor is refused, never mixed with the new process's ids
            stop(procs[n])
            procs[n] = start(n, binary, ports[n], interval, logs[n])
        deadline = time.monotonic() + 15
        for n, port in ports.items():
            wait_ready(port, deadline)
            old = res[n]["replay"]["instance"]
            code, _, body = fetch(port, f"/api/samples?instance_id={old}&after=1")
            restarted[n] = {"code": code, "body": json.loads(body)}
    finally:
        for p in procs.values():  # only what this run started
            stop(p)
    py, rs = res["python"], res["rust"]
    out = []
    for path in PATHS:
        a, b = py["endpoints"][path], rs["endpoints"][path]
        for k in ("code", "type", "no_store"):
            if a[k] != b[k]:
                out.append(f"{path}: {k} {a[k]!r} (python) != {b[k]!r} (rust)")
        if path in WEB and a["body"] != b["body"]:
            out.append(f"{path}: the bodies differ ({len(a['body'])} / {len(b['body'])} bytes)")
    a, b = (heads(r["endpoints"]["/text"]["body"].decode()) for r in (py, rs))
    if a != b:
        out.append(f"/text: sections {a} (python) != {b} (rust)")
    for name in RAW:
        if py["raw"][name] != rs["raw"][name]:
            out.append(f"raw {name}: {py['raw'][name]} (python) != {rs['raw'][name]} (rust)")
    for path in ("/api/stats", "/api/status", "/api/observations"):
        diff(py["bodies"][path], rs["bodies"][path], "", (d := []))
        out += [f"{path}{x}" for x in d]
    h = {n: r["bodies"]["/api/history"] for n, r in res.items()}
    for k in ("schema_version", "retention_s", "interval_ms", "dropped_series", "series_intervals_ms"):
        if h["python"][k] != h["rust"][k]:
            out.append(f"/api/history: {k} {h['python'][k]!r:.200} (python) != {h['rust'][k]!r:.200} (rust)")
    if list(h["python"]) != list(h["rust"]) or sorted(h["python"]["series"]) != sorted(h["rust"]["series"]):
        out.append(f"/api/history: keys or series ids differ: {sorted(set(h['python']['series']) ^ set(h['rust']['series']))[:5]}")
    for n, body in h.items():
        if not all(type(p[0]) is int and type(p[1]) is float and (p[2] is None or type(p[2]) in (int, float))
                   for points in body["series"].values() for p in points):
            out.append(f"{n}: /api/history: a point that is not [int, float, number or null]")
    sm = {n: r["bodies"]["/api/samples"] for n, r in res.items()}
    if [kind(v) for v in sm["python"].values()] != [kind(v) for v in sm["rust"].values()] or list(sm["python"]) != list(sm["rust"]):
        out.append("/api/samples: the envelope differs")
    for n, body in sm.items():
        stats_keys = list(res[n]["bodies"]["/api/stats"])
        if body["records"] and list(body["records"][0]["data"]) != stats_keys:
            out.append(f"{n}: /api/samples: a record's keys are not /api/stats'")
        if body["records"] and list(body["records"][0]["data"]["sample"])[-1] != "stale":
            out.append(f"{n}: /api/samples: no sample.stale")
    for n, r in res.items():
        out += [f"{n}: replay walk: {p}" for p in r["replay"]["problems"]]
        out += [f"{n}: concurrent: {p}" for p in r["concurrent"]["problems"]]
        lim = r["connection_limit"]
        if lim["closed"] != lim["connections"] - 32 or not lim["refused_while_full"] or lim["after"] != 200:
            out.append(f"{n}: connection limit: {lim}")
        rt = r["request_timeout"]
        if rt["answered"] or not 9.5 <= rt["closed_after_s"] <= 11.5:
            out.append(f"{n}: request timeout: {rt}")
        b = r["burst"]
        if b["threads_peak"] > b["threads_before"] + 34 or b["code"] != 200 or b["threads_after"] > b["threads_before"] + 2:
            out.append(f"{n}: burst: {b}")
        rs_ = restarted[n]
        if rs_["code"] != 409 or rs_["body"].get("code") != "instance_changed" or rs_["body"].get("instance_id") == r["replay"]["instance"]:
            out.append(f"{n}: after a restart: {rs_}")
    stats = py["bodies"]["/api/stats"]
    report = {"host_platform": stats["platform"], "sensor_meta": len(stats.get("sensor_meta", {})),
              "interfaces": len(stats.get("network", {}).get("interfaces", [])),
              "groups": {n: {g: o["state"] for g, o in r["bodies"]["/api/observations"]["groups"].items()} for n, r in res.items()},
              "history_series": {n: len(b["series"]) for n, b in h.items()},
              "codes": {p: [py["endpoints"][p]["code"], rs["endpoints"][p]["code"]] for p in PATHS},
              "raw": py["raw"], "restart": {n: r["code"] for n, r in restarted.items()},
              **{n: {k: r[k] for k in ("replay", "concurrent", "connection_limit", "request_timeout", "burst")} for n, r in res.items()},
              "differences": out}
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as f:
            json.dump({"report": report, "python": py["bodies"], "rust": rs["bodies"],
                       "text": {n: r["endpoints"]["/text"]["body"].decode() for n, r in res.items()}}, f, indent=1)
    print(json.dumps(report, indent=1))
    for n, log in logs.items():
        log.seek(0)
        tail = log.read().splitlines()[-3:]
        print(f"{n} log: " + " | ".join(tail), file=sys.stderr)
    sys.exit(1 if out else 0)


if __name__ == "__main__":
    main()
