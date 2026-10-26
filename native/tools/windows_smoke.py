"""The Windows build's smoke test (CI, native-windows).

  windows_smoke.py BINARY

- build-info: the system allocator's mark;
- collect: one recorded collection (src/collect/windows), with the required data, CPU usage, the elapsed
  clock reported as "boottime", the network and disk counters with rates, each CPU clock, GPU value and
  temperature with its sensor record (a runner can have none: its group is then unavailable, never error;
  with only the Basic Render Driver gpu is null, not_detected), and the other optional groups
  unsupported_platform;
- script and loop exit 2 with the usage: they are Linux's (fake trees, rusage);
- config-check without a file: the defaults, no error;
- runtime-script: answers /api/status as starting, with the given instance id;
- serve on a free local port: /api/status ready with the clock {"source": "boottime"}, /api/stats 200 with a
  snapshot, and /api/observations with the probe connecting to the server itself and the storage
  group listing the system volume (the slow groups there), then stopped by its own handle;
- service: outside the service control manager it exits 2; as a service (sc.exe create, start) it serves
  /api/status ready, and sc.exe stop leaves it stopped with exit code 0 and the port free."""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

TIMEOUT = 30
INSTANCE = "00000000-0000-4000-8000-000000000000"
OPTIONAL = ("npu", "power", "fans", "power_mode", "board_info")


def run(binary, args, stdin=""):
    # the binary writes UTF-8 (a localized interface or drive name); a locale codepage such as cp949 fails on it
    return subprocess.run([binary, *args], input=stdin, capture_output=True, encoding="utf-8", timeout=TIMEOUT)


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
    sensors = out.get("sensors", {})
    cpus = stats.get("cpu", [])
    check("collect: cpu_frequency", collectors.get("cpu_frequency", {}).get("state") in ("ok", "unavailable")
          and all((isinstance(c.get("freq"), int) and c["freq"] > 0 and sensors.get(f"/cpu/{i}/freq", {}).get("span"))
                  or (c.get("freq") is None and f"/cpu/{i}/freq" in sensors) for i, c in enumerate(cpus)),
          json.dumps([c.get("freq") for c in cpus]) + " " + json.dumps(collectors.get("cpu_frequency")))
    gpu = stats.get("gpu")
    check("collect: gpu", (gpu is None and collectors.get("gpu", {}).get("reason") == "not_detected")
          or (gpu.get("provider") == "pdh_gpu_engine" and collectors.get("gpu", {}).get("state") in ("ok", "unavailable")
              and all((isinstance(gpu.get(k), (int, float)) and gpu[k] >= 0 and sensors.get("/gpu/" + k, {}).get("span"))
                      or (gpu.get(k) is None and "/gpu/" + k in sensors) for k in ("usage", "freq", "max_freq"))),
          json.dumps(gpu) + " " + json.dumps(collectors.get("gpu")))
    temps = stats.get("temperature", {})
    check("collect: temperature", collectors.get("temperature", {}).get("state") in ("ok", "unavailable")
          and all(isinstance(t, float) and sensors.get("/temperature/" + k.replace("~", "~0").replace("/", "~1"), {}).get("span")
                  for k, t in temps.items()), json.dumps(temps) + " " + json.dumps(collectors.get("temperature")))
    network = stats.get("network", {})
    interfaces = network.get("interfaces", [])
    check("collect: network", network.get("provider") == "if_table2" and network.get("scope") == {"kind": "host", "id": None}
          and interfaces and all(i.get("reason") in (None, "warmup", "counter_regressed") for i in interfaces)
          and any(i.get("rates") for i in interfaces) and collectors.get("network", {}).get("state") in ("ok", "partial")
          and out.get("passive", {}).get("network"), json.dumps(network))
    disk_io = stats.get("disk_io", {})
    disks = disk_io.get("disks", [])
    check("collect: disk_io", disk_io.get("provider") == "ioctl_disk_performance"
          and disk_io.get("scope") == {"kind": "host_block_devices"} and disks
          and all(d.get("major") is None and d.get("read", {}).get("merges", 0) is None
                  and d.get("reason") in (None, "warmup", "counter_regressed") for d in disks)
          and any(isinstance((d.get("rates") or {}).get("io_time_ratio"), float) for d in disks)
          and collectors.get("disk_io", {}).get("state") in ("ok", "partial")
          and out.get("passive", {}).get("disk_io"), json.dumps(disk_io) + " " + json.dumps(collectors.get("disk_io")))
    print("      " + json.dumps({k: stats.get(k) for k in ("model", "system", "uptime", "memory", "disk", "cpu_sampling")}))
    print("      " + json.dumps({"freq": [c.get("freq") for c in cpus], "gpu": gpu, "temperature": temps,
                                 "collectors": {g: collectors.get(g) for g in ("cpu_frequency", "gpu", "temperature")}},
                                ensure_ascii=False))
    print("      " + json.dumps([(i["name"], i["reason"], i["rates"]) for i in interfaces], ensure_ascii=False))
    print("      " + json.dumps([(d["name"], d["in_flight"], d["reason"], d["rates"]) for d in disks]))
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
    with open("platmon-smoke.ini", "w") as f:  # the probe connects to the server itself
        f.write(f"[http]\nbind = 127.0.0.1\nport = {port}\n[probe]\ninterval = 1\ntimeout = 0.5\n"
                f"targets = 127.0.0.1:{port}\n")
    server = subprocess.Popen([binary, "serve", "platmon-smoke.ini"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8")
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
        deadline, probe = time.monotonic() + TIMEOUT, {}
        while time.monotonic() < deadline:
            code, observations = get(port, "/api/observations")
            groups = (observations or {}).get("groups", {})
            probe, storage = groups.get("probe", {}), groups.get("storage", {})
            if probe.get("state") not in (None, "starting") and storage.get("state") not in (None, "starting"):
                break
            time.sleep(0.5)
        targets = (probe.get("data") or {}).get("targets", [])
        check("serve /api/observations probe", sorted((observations or {}).get("groups", {})) == ["probe", "storage"]
              and probe.get("state") == "ok" and len(targets) == 1 and isinstance(targets[0].get("connect_ms"), float)
              and targets[0].get("reason") is None, json.dumps(probe))
        system = os.environ.get("SystemDrive", "C:") + "\\"
        filesystems = (storage.get("data") or {}).get("filesystems", [])
        check("serve /api/observations storage", storage.get("state") in ("ok", "partial")
              and any(p.get("path") == system and fs.get("device", "").startswith("Volume{") and fs.get("total_bytes")
                      for fs in filesystems for p in fs.get("mount_points", [])), json.dumps(storage))
    finally:
        server.kill()
        output = server.communicate(timeout=TIMEOUT)[0]
    print("      " + output.strip().replace("\n", "\n      "))

    r = run(binary, ["service"])
    check("service outside the service control manager", r.returncode == 2 and "service control manager" in r.stderr,
          repr(r))
    port, name = free_port(), "platmon-smoke"
    ini = os.path.abspath("platmon-service.ini")
    with open(ini, "w") as f:
        f.write(f"[http]\nbind = 127.0.0.1\nport = {port}\n")

    def sc(*args):
        return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=TIMEOUT)

    created = sc("create", name, "binPath=", f'"{os.path.abspath(binary)}" service "{ini}"')
    try:
        started = sc("start", name)
        deadline, status = time.monotonic() + TIMEOUT, None
        while time.monotonic() < deadline:
            code, status = get(port, "/api/status")
            if code == 200 and status.get("ready"):
                break
            time.sleep(0.5)
        check("service start", created.returncode == 0 and started.returncode == 0 and bool(status)
              and status.get("ready") is True, created.stdout + started.stdout + json.dumps(status))
        stopped = sc("stop", name)
        deadline, query = time.monotonic() + TIMEOUT, ""
        while time.monotonic() < deadline:
            query = sc("query", name).stdout
            if "STOPPED" in query:
                break
            time.sleep(0.5)
        check("service stop", stopped.returncode == 0 and "STOPPED" in query
              and re.search(r"WIN32_EXIT_CODE\s*:\s*0\s", query) is not None and get(port, "/api/status") == (None, None),
              stopped.stdout + query)
    finally:
        sc("stop", name)
        sc("delete", name)
    if failures:
        sys.exit(f"{len(failures)} failed: {', '.join(failures)}")
    print("all passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
