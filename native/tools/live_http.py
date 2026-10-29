"""Live check of the HTTP runtime on a real host: starts `platmon-native serve` with platmon.ini's defaults on
a local port and checks it. What does not depend on the host is checked against the answers the Python
service gave (recorded 2026-10-29, before its removal in 1.6.0, when this compared the two servers). Only
the process it started is stopped (and restarted once), by its own handle.

  python3 live_http.py PATH/TO/platmon-native [--json OUT]

- every path: status code, Content-Type, Cache-Control as the Python service answered (PATHS); the web page
  and its files: the bytes of frontends/web (the page with the version filled in)
- the /text screen: the sections frontends/cli.py's renderer gives for the same /api/stats and
  /api/observations
- raw requests at the protocol's edges (HEAD, HTTP/0.9, versions, header and line limits): the answer's
  shape from its bytes (status line and headers or none, header names, a body or none), as the Python
  service gave it (RAW); for HTTP/2.0 and an invalid version (a known difference from some CPython releases)
  the body alone, no status line (KNOWN)
- /api/history: the envelope's keys, schema, retention and cadence of the defaults, every point [int, float,
  number or null]; /api/samples: the envelope's keys and JSON types, records with /api/stats' keys and
  sample.stale
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
  once they close, and its threads go back
- a restart: SIGTERM ends the server at once with exit code 0; the previous instance's cursor gets 409
  instance_changed with the new instance id
The fields of /api/stats, /api/status and /api/observations are runtime_fixture.py's and fixture.py's to
compare (fixed inputs); a live host's values move. Exit 1 on any difference or failed check."""
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "frontends"))
from cli import render  # noqa: E402  the terminal client, one standalone file

PATHS = ["/api/stats", "/api/status", "/api/observations", "/api/history", "/api/samples",
         "/api/observation-samples?group=storage", "/api/stats?x=1", "/", "/index.html", "/app.js", "/styles.css",
         "/assets/brand/favicon.svg", "/assets/brand/platmon-logo-dark.svg", "/nothing", "/text", "/text?x"]
WEB = ["/", "/index.html", "/app.js", "/styles.css", "/assets/brand/favicon.svg", "/assets/brand/platmon-logo-dark.svg"]
JSON, HTML, ERROR = "application/json", "text/html; charset=utf-8", "text/html;charset=utf-8"
EXPECTED = {  # path: (code, Content-Type, Cache-Control no-store), as the Python service answered
    "/api/stats": (200, JSON, True), "/api/status": (200, JSON, True), "/api/observations": (200, JSON, True),
    "/api/history": (200, JSON, True), "/api/samples": (200, JSON, True),
    "/api/observation-samples?group=storage": (200, JSON, True), "/api/stats?x=1": (404, ERROR, False),
    "/": (200, HTML, False), "/index.html": (200, HTML, False), "/app.js": (200, "text/javascript; charset=utf-8", False),
    "/styles.css": (200, "text/css; charset=utf-8", False), "/assets/brand/favicon.svg": (200, "image/svg+xml", False),
    "/assets/brand/platmon-logo-dark.svg": (200, "image/svg+xml", False), "/nothing": (404, ERROR, False),
    "/text": (200, "text/plain; charset=utf-8", True), "/text?x": (404, ERROR, False),
}
HISTORY = {"schema_version": 1, "retention_s": 600.0, "interval_ms": 1000.0}  # the defaults' history
HISTORY_KEYS = ["schema_version", "instance_id", "retention_s", "interval_ms", "dropped_series", "series",
                "series_intervals_ms"]
SAMPLES = {"schema_version": "int", "instance_id": "str", "clock": "dict", "stream": "str", "data_schema_version": "int",
           "historical": "bool", "retention_s": "float", "max_bytes": "int", "max_records": "int",
           "max_record_bytes": "int", "max_response_bytes": "int", "oldest_id": "int", "latest_id": "int",
           "next_after": "int", "has_more": "bool", "missing_ranges": "list", "records": "list"}


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


def kind(v):
    return "null" if v is None else type(v).__name__


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
    "99 headers": b"GET /api/status HTTP/1.0\r\n" + b"A: b\r\n" * 99 + b"\r\n",
    "100 headers": b"GET /api/status HTTP/1.0\r\n" + b"A: b\r\n" * 100 + b"\r\n",
    "long header": b"GET /api/status HTTP/1.0\r\nA: " + b"x" * 70000 + b"\r\n\r\n",
    "long request line": b"GET /" + b"a" * 70000 + b" HTTP/1.0\r\n\r\n",
    "empty line": b"\r\n",
    "//path": b"GET //api/status HTTP/1.0\r\n\r\n",
}


ERROR_HEADERS, OK_HEADERS = ["Connection", "Content-Length", "Content-Type"], ["Cache-Control", "Content-Length", "Content-Type"]
RAW_EXPECTED = {  # the Python service's answers
    "HEAD": {"bare": False, "status": "HTTP/1.0 501 Unsupported method ('HEAD')", "headers": ERROR_HEADERS, "body": False},
    "POST": {"bare": False, "status": "HTTP/1.0 501 Unsupported method ('POST')", "headers": ERROR_HEADERS, "body": True},
    "HTTP/0.9 said": {"bare": True, "body": True},
    "HTTP/0.9 two words": {"bare": True, "body": True},
    "HTTP/00.9": {"bare": False, "status": "HTTP/1.0 200 OK", "headers": OK_HEADERS, "body": True},
    "99 headers": {"bare": False, "status": "HTTP/1.0 200 OK", "headers": OK_HEADERS, "body": True},
    "100 headers": {"bare": False, "status": "HTTP/1.0 431 Too many headers", "headers": ERROR_HEADERS, "body": True},
    "long header": {"bare": False, "status": "HTTP/1.0 431 Line too long", "headers": ERROR_HEADERS, "body": True},
    "long request line": {"bare": False, "status": "HTTP/1.0 414 URI Too Long", "headers": ERROR_HEADERS, "body": True},
    "empty line": {"bare": True, "body": False},
    "//path": {"bare": False, "status": "HTTP/1.0 200 OK", "headers": OK_HEADERS, "body": True},
}


# A known difference between CPython releases: 3.13.16 answers these with a status line and headers;
# 3.10.12, 3.13.5 and 3.14.4, which the server was verified against, and the server answer with the body
# alone.
KNOWN = {
    "HTTP/2.0": b"GET /api/status HTTP/2.0\r\n\r\n",
    "bad version": b"GET /api/status HTTP/x\r\n\r\n",
}
KNOWN_RUST = {"bare": True, "body": True}


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
            "known": {k: raw(port, r) for k, r in KNOWN.items()},
            "replay": replay_walk(port), "concurrent": concurrent(port, interval),
            "connection_limit": connection_limit(port), "request_timeout": request_timeout(port), "burst": burst(port, pid)}


def config(port, interval):
    """platmon.ini with the defaults, but this port on 127.0.0.1."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({"core": {"interval": str(interval)}, "http": {"bind": "127.0.0.1", "port": str(port)}})
    path = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
    cfg.write(path)
    path.close()
    return path.name


def start(binary, port, interval, log):
    return subprocess.Popen([binary, "serve", config(port, interval), "--web", f"{REPO}/frontends/web",
                             "--app-version", version()], stdout=log, stderr=log)


def stop(p):
    """SIGTERM, as systemd and docker stop send it: (exit code, seconds to exit); killed after 5 s."""
    t0 = time.monotonic()
    p.terminate()
    try:
        code = p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
        code = f"{p.wait()} (killed: still running 5 s after SIGTERM)"
    return code, round(time.monotonic() - t0, 2)


def version():
    with open(f"{REPO}/VERSION") as f:
        return f.read().strip()


def heads(text):
    """The /text screen's sections: the first word of each line that is not indented."""
    return [line.split()[0] for line in text.split("\n")[2:] if line and not line.startswith(" ")]


def problems(r, stopped, restarted):
    out = []
    eps = r["endpoints"]
    for path in PATHS:
        got = (eps[path]["code"], eps[path]["type"], eps[path]["no_store"])
        if got != EXPECTED[path]:
            out.append(f"{path}: {got} != {EXPECTED[path]}")
    page = eps["/"]["body"]
    if eps["/index.html"]["body"] != page or f"platmon v{version()}".encode() not in page or b"@VERSION@" in page \
            or b"@LEVELS@" in page:
        out.append("/: not the page with its version and levels filled in")
    for path in WEB[2:]:
        with open(f"{REPO}/frontends/web{path}", "rb") as f:
            if eps[path]["body"] != f.read():
                out.append(f"{path}: not the bytes of frontends/web{path}")
    a, b = heads(eps["/text"]["body"].decode()), heads(render(r["bodies"]["/api/stats"], r["bodies"]["/api/observations"]))
    if a != b:
        out.append(f"/text: sections {a} != {b} (frontends/cli.py)")
    for name in RAW:
        if r["raw"][name] != RAW_EXPECTED[name]:
            out.append(f"raw {name}: {r['raw'][name]} != {RAW_EXPECTED[name]}")
    for name in KNOWN:
        if r["known"][name] != KNOWN_RUST:
            out.append(f"known difference {name}: {r['known'][name]} != {KNOWN_RUST}")
    h = r["bodies"]["/api/history"]
    if list(h) != HISTORY_KEYS or any(h[k] != v for k, v in HISTORY.items()) or set(h["series"]) != set(h["series_intervals_ms"]):
        out.append(f"/api/history: {json.dumps({k: h[k] for k in h if k != 'series'})[:300]}")
    if not all(type(p[0]) is int and type(p[1]) is float and (p[2] is None or type(p[2]) in (int, float))
               for points in h["series"].values() for p in points):
        out.append("/api/history: a point that is not [int, float, number or null]")
    sm = r["bodies"]["/api/samples"]
    if {k: kind(v) for k, v in sm.items()} != SAMPLES or list(sm) != list(SAMPLES):
        out.append(f"/api/samples: the envelope {[(k, kind(v)) for k, v in sm.items()]}")
    if sm["records"] and list(sm["records"][0]["data"]) != list(r["bodies"]["/api/stats"]):
        out.append("/api/samples: a record's keys are not /api/stats'")
    if sm["records"] and list(sm["records"][0]["data"]["sample"])[-1] != "stale":
        out.append("/api/samples: no sample.stale")
    out += [f"replay walk: {p}" for p in r["replay"]["problems"]]
    out += [f"concurrent: {p}" for p in r["concurrent"]["problems"]]
    lim = r["connection_limit"]
    if lim["closed"] != lim["connections"] - 32 or not lim["refused_while_full"] or lim["after"] != 200:
        out.append(f"connection limit: {lim}")
    rt = r["request_timeout"]
    if rt["answered"] or not 9.5 <= rt["closed_after_s"] <= 11.5:
        out.append(f"request timeout: {rt}")
    b = r["burst"]
    if b["threads_peak"] > b["threads_before"] + 34 or b["code"] != 200 or b["threads_after"] > b["threads_before"] + 2:
        out.append(f"burst: {b}")
    code, seconds = stopped
    if code != 0 or seconds >= 2:  # a stop exits at once, with status 0
        out.append(f"SIGTERM: exit {code} after {seconds} s")
    if restarted["code"] != 409 or restarted["body"].get("code") != "instance_changed" \
            or restarted["body"].get("instance_id") == r["replay"]["instance"]:
        out.append(f"after a restart: {restarted}")
    return out


def main():
    tested = self_test() & walk.self_test() & worker.self_test()
    if sys.argv[1:] == ["--self-test"] or not tested:
        sys.exit(0 if tested else 1)
    binary, interval, port = os.path.abspath(sys.argv[1]), 1.0, free_port()
    log = tempfile.TemporaryFile("w+")
    proc = start(binary, port, interval, log)
    try:
        if not wait_ready(port, time.monotonic() + 15):
            sys.exit("the server is not ready after 15 s")
        time.sleep(6)  # past the first collections' warmup and the first slow observations
        t = worker.Worker("check", check, port, interval, proc.pid)
        t.start()
        r = t.result(300)  # what the check raised ends the run here
        stopped = stop(proc)  # a restart: the old cursor is refused, never mixed with the new process's ids
        proc = start(binary, port, interval, log)
        wait_ready(port, time.monotonic() + 15)
        code, _, body = fetch(port, f"/api/samples?instance_id={r['replay']['instance']}&after=1")
        restarted = {"code": code, "body": json.loads(body)}
    finally:
        stop(proc)  # only what this run started
    out = problems(r, stopped, restarted)
    stats = r["bodies"]["/api/stats"]
    report = {"host_platform": stats["platform"], "sensor_meta": len(stats.get("sensor_meta", {})),
              "interfaces": len(stats.get("network", {}).get("interfaces", [])),
              "groups": {g: o["state"] for g, o in r["bodies"]["/api/observations"]["groups"].items()},
              "history_series": len(r["bodies"]["/api/history"]["series"]),
              "codes": {p: r["endpoints"][p]["code"] for p in PATHS}, "raw": r["raw"], "known": r["known"],
              "restart": restarted["code"], "stop": stopped,
              **{k: r[k] for k in ("replay", "concurrent", "connection_limit", "request_timeout", "burst")},
              "differences": out}
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as f:
            json.dump({"report": report, "bodies": r["bodies"], "text": r["endpoints"]["/text"]["body"].decode()}, f, indent=1)
    print(json.dumps(report, indent=1))
    log.seek(0)
    print("server log: " + " | ".join(log.read().splitlines()[-3:]), file=sys.stderr)
    sys.exit(1 if out else 0)


if __name__ == "__main__":
    main()
