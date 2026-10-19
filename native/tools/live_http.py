"""Live check of the HTTP runtime (step 3A) on a real host: starts the Python service (platmon.py, with the
slow groups, history and replay off, which step 3A does not have) and `platmon-native serve` on two local
ports at the same interval, checks each the same way and compares them. Only the processes it started are
stopped, by their own handles.

  PYTHONPATH=<platmon tree> python3 live_http.py PATH/TO/platmon-native [--json OUT]

Per server:
- every path: status code, Content-Type, Cache-Control (/text is not ported: a 404 from Rust, not compared)
- the web page and its files: the same bytes
- raw requests at the protocol's edges (HEAD, HTTP/0.9, versions, header and line limits): the same answer
  shape from its bytes (status line and headers or none, header names, a body or none)
- /api/stats, /api/status, /api/observations: the same keys, order and JSON types; values exact except
  those that move (compare.py's plausibility rules, sample identity and ages, read offsets)
- 16 clients × 20 requests at once: all 320 answers verified (200, JSON, a sample), no client error, one
  instance, sequences never going back, and no more collections than intervals (requests do not collect).
  A self-test runs first (`--self-test` alone): fake answers that time out, are not JSON, have no sample,
  are a 503 or go back after a client's first answer must each fail the check
- 40 idle connections, opened 20 ms apart: 32 held, the rest closed without an answer, a request refused
  while 32 are held and answered after they close
- a request line cut off: the connection closed after the 10 s request timeout
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

from compare import kind, plausible

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PATHS = ["/api/stats", "/api/status", "/api/observations", "/api/history", "/api/samples",
         "/api/observation-samples?group=storage", "/api/stats?x=1", "/", "/index.html", "/app.js", "/styles.css",
         "/assets/brand/favicon.svg", "/assets/brand/platmon-logo-dark.svg", "/nothing", "/text"]
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
    return (ptr.startswith(("/sample/", "/last_attempt/completed_at", "/last_attempt/duration_ms"))
            or ptr in ("/instance_id", "/collecting_for_ms")
            or "read" in parts[:-1] and parts[-1] in ("started_offset_ms", "completed_offset_ms"))


def diff(a, b, ptr, out):
    if ptr == "/collecting_for_ms" and (a is None or b is None):
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


def check(port, interval):
    eps = endpoints(port)
    bodies = {p: json.loads(eps[p]["body"]) for p in ("/api/stats", "/api/status", "/api/observations")}
    return {"endpoints": eps, "bodies": bodies, "raw": {k: raw(port, r) for k, r in RAW.items()},
            "concurrent": concurrent(port, interval),
            "connection_limit": connection_limit(port), "request_timeout": request_timeout(port)}


def start_python(port, interval, log):
    cfg = configparser.ConfigParser()
    cfg.read_dict({"core": {"interval": str(interval)}, "http": {"bind": "127.0.0.1", "port": str(port)},
                   **{g: {"enabled": "no"} for g in ("storage", "wifi", "battery", "history", "replay")}})
    path = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
    cfg.write(path)
    path.close()
    return subprocess.Popen([sys.executable, f"{REPO}/platmon.py", path.name], cwd=REPO, stdout=log, stderr=log,
                            env={**os.environ, "PYTHONPATH": REPO})


def version():
    with open(f"{REPO}/frontends/cli.py") as f:
        return next(line.split('"')[1] for line in f if line.startswith("VERSION ="))


def main():
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(0 if self_test() else 1)
    if not self_test():
        sys.exit(1)
    binary, interval = os.path.abspath(sys.argv[1]), 1.0
    ports = {"python": free_port(), "rust": free_port()}
    logs = {n: tempfile.TemporaryFile("w+") for n in ports}
    procs = {}
    try:
        procs["python"] = start_python(ports["python"], interval, logs["python"])
        procs["rust"] = subprocess.Popen([binary, "serve", "--bind", "127.0.0.1", "--port", str(ports["rust"]),
                                          "--interval", str(interval), "--web", f"{REPO}/frontends/web",
                                          "--app-version", version()], stdout=logs["rust"], stderr=logs["rust"])
        deadline = time.monotonic() + 15
        for n, port in ports.items():
            if not wait_ready(port, deadline):
                sys.exit(f"{n} is not ready after 15 s")
        time.sleep(3)  # past the first collections' warmup on both
        res = {}
        threads = [threading.Thread(target=lambda n=n: res.__setitem__(n, check(ports[n], interval))) for n in ports]
        for t in threads:  # at the same time, so both read the same moment as far as possible
            t.start()
        for t in threads:
            t.join()
    finally:
        for n, p in procs.items():  # only what this run started
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    py, rs = res["python"], res["rust"]
    out = []
    for path in PATHS:
        a, b = py["endpoints"][path], rs["endpoints"][path]
        if path == "/text":
            continue
        for k in ("code", "type", "no_store"):
            if a[k] != b[k]:
                out.append(f"{path}: {k} {a[k]!r} (python) != {b[k]!r} (rust)")
        if path in WEB and a["body"] != b["body"]:
            out.append(f"{path}: the bodies differ ({len(a['body'])} / {len(b['body'])} bytes)")
    for name in RAW:
        if py["raw"][name] != rs["raw"][name]:
            out.append(f"raw {name}: {py['raw'][name]} (python) != {rs['raw'][name]} (rust)")
    for path in py["bodies"]:
        diff(py["bodies"][path], rs["bodies"][path], "", (d := []))
        out += [f"{path}{x}" for x in d]
    for n, r in res.items():
        out += [f"{n}: concurrent: {p}" for p in r["concurrent"]["problems"]]
        lim = r["connection_limit"]
        if lim["closed"] != lim["connections"] - 32 or not lim["refused_while_full"] or lim["after"] != 200:
            out.append(f"{n}: connection limit: {lim}")
        rt = r["request_timeout"]
        if rt["answered"] or not 9.5 <= rt["closed_after_s"] <= 11.5:
            out.append(f"{n}: request timeout: {rt}")
    stats = py["bodies"]["/api/stats"]
    report = {"host_platform": stats["platform"], "sensor_meta": len(stats.get("sensor_meta", {})),
              "interfaces": len(stats.get("network", {}).get("interfaces", [])),
              "codes": {p: [py["endpoints"][p]["code"], rs["endpoints"][p]["code"]] for p in PATHS},
              "raw": py["raw"],
              **{n: {k: r[k] for k in ("concurrent", "connection_limit", "request_timeout")} for n, r in res.items()},
              "differences": out}
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as f:
            json.dump({"report": report, "python": py["bodies"], "rust": rs["bodies"]}, f, indent=1)
    print(json.dumps(report, indent=1))
    for n, log in logs.items():
        log.seek(0)
        tail = log.read().splitlines()[-3:]
        print(f"{n} log: " + " | ".join(tail), file=sys.stderr)
    sys.exit(1 if out else 0)


if __name__ == "__main__":
    main()
