"""The Windows build's smoke test (CI, native-windows).

  windows_smoke.py BINARY

- build-info: the system allocator's mark;
- collect: one recorded collection (src/collect/windows), with the required data, CPU usage, the elapsed
  clock reported as "boottime", the network counters with rates, and the other optional groups
  unsupported_platform;
- script and loop exit 2 with the usage: they are Linux's (fake trees, rusage);
- config-check without a file: the defaults, no error;
- runtime-script: answers /api/status as starting, with the given instance id;
- serve on a free local port: /api/status ready with the clock {"source": "boottime"} and /api/stats 200 with a
  snapshot, then stopped by its own handle."""
import json
import socket
import subprocess
import sys
import time
import urllib.request

TIMEOUT = 30
INSTANCE = "00000000-0000-4000-8000-000000000000"
OPTIONAL = ("cpu_frequency", "gpu", "npu", "temperature", "power", "fans", "power_mode", "board_info")


def run(binary, args, stdin=""):
    return subprocess.run([binary, *args], input=stdin, capture_output=True, text=True, timeout=TIMEOUT)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except OSError:
        return None, None


def main(binary):
    failures = []

    def check(name, ok, detail):
        print(("ok    " if ok else "FAIL  ") + name)
        if not ok:
            failures.append(name)
            print("      " + detail[:800].replace("\n", "\n      "))  # a collection's output is long

    r = run(binary, ["build-info"])
    check("build-info", r.returncode == 0 and r.stdout.strip() == "platmon-native-allocator=system;", repr(r))

    r = run(binary, ["collect"])
    out = json.loads(r.stdout) if r.returncode == 0 else {}
    stats = out.get("stats", {})
    memory, disk = stats.get("memory", {}), stats.get("disk", {})
    check("collect", out.get("clock") == "boottime" and stats.get("platform") == "Windows"
          and memory.get("total", 0) > 0 and 0 <= memory.get("used", -1) <= memory["total"]
          and disk.get("total", 0) > 0 and stats.get("uptime", 0) > 0
          and all(isinstance(c.get("usage"), float) for c in stats.get("cpu", []))
          and len(stats.get("cpu", [])) + len(stats.get("cpu_sampling", {}).get("unavailable", [])) > 0
          and list(out.get("required", {})) == ["cpu", "memory", "disk", "uptime"]
          and all(out["required"].values()), repr(r))
    collectors = stats.get("collectors", {})
    check("collect: optional groups", all(collectors.get(g, {}).get("reason") == "unsupported_platform" for g in OPTIONAL),
          json.dumps(collectors))
    network = stats.get("network", {})
    interfaces = network.get("interfaces", [])
    check("collect: network", network.get("provider") == "if_table2" and network.get("scope") == {"kind": "host", "id": None}
          and interfaces and all(i.get("reason") in (None, "warmup", "counter_regressed") for i in interfaces)
          and any(i.get("rates") for i in interfaces) and collectors.get("network", {}).get("state") in ("ok", "partial")
          and out.get("passive", {}).get("network"), json.dumps(network))
    print("      " + json.dumps({k: stats.get(k) for k in ("model", "system", "uptime", "memory", "disk", "cpu_sampling")}))
    print("      " + json.dumps([(i["name"], i["reason"], i["rates"]) for i in interfaces], ensure_ascii=False))
    for command in ("script", "loop"):
        r = run(binary, [command])
        check(command + " exits", r.returncode == 2 and not r.stdout and "usage:" in r.stderr, repr(r))

    r = run(binary, ["config-check"])
    config = json.loads(r.stdout) if r.returncode == 0 else {}
    check("config-check", "error" not in config and config.get("http", {}).get("port") == 9797, repr(r))
    ops = [{"op": "setup", "interval": 1.0, "instance_id": INSTANCE, "clock": "monotonic", "slow": [],
            "levels": {}, "history": None, "replay": None},
           {"op": "http", "now": 10**12, "request": "GET /api/status HTTP/1.0\r\n\r\n"}]
    r = run(binary, ["runtime-script"], "".join(json.dumps(op) + "\n" for op in ops))
    lines = r.stdout.splitlines()
    response = json.loads(lines[1]).get("response", "") if r.returncode == 0 and len(lines) == 2 else ""
    head, _, body = response.partition("\r\n\r\n")
    status = json.loads(body) if head.startswith("HTTP/1.0 ") else {}
    check("runtime-script /api/status", status.get("instance_id") == INSTANCE and status.get("state") == "starting",
          repr(r))

    port = free_port()
    with open("platmon-smoke.ini", "w") as f:
        f.write(f"[http]\nbind = 127.0.0.1\nport = {port}\n")
    server = subprocess.Popen([binary, "serve", "platmon-smoke.ini"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
    try:
        deadline, status, stats = time.monotonic() + TIMEOUT, None, None
        while time.monotonic() < deadline and server.poll() is None:
            code, status = get(port, "/api/status")
            if code == 200 and status.get("ready"):
                break
            time.sleep(0.5)
        check("serve /api/status", bool(status) and status.get("ready") is True
              and status.get("clock") == {"source": "boottime", "suspend_aware": True},
              json.dumps(status))
        code, stats = get(port, "/api/stats")
        check("serve /api/stats", code == 200 and stats.get("platform") == "Windows"
              and stats.get("sample", {}).get("data_age_basis") == "oldest_current_read_start", f"{code} {json.dumps(stats)}")
    finally:
        server.kill()
        output = server.communicate(timeout=TIMEOUT)[0]
    print("      " + output.strip().replace("\n", "\n      "))
    if failures:
        sys.exit(f"{len(failures)} failed: {', '.join(failures)}")
    print("all passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
