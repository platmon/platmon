"""The Windows build's smoke test (CI, native-windows): what runs there before any collector exists.

  windows_smoke.py BINARY

- build-info: the system allocator's mark;
- collect and serve: exit 1 with "no collector for this platform", nothing on stdout;
- config-check without a file: the defaults, no error;
- runtime-script: the HTTP server listens (src/platform/windows.rs) and answers /api/status as starting,
  with the given instance id."""
import json
import subprocess
import sys

TIMEOUT = 30
INSTANCE = "00000000-0000-4000-8000-000000000000"


def run(binary, args, stdin=""):
    return subprocess.run([binary, *args], input=stdin, capture_output=True, text=True, timeout=TIMEOUT)


def main(binary):
    failures = []

    def check(name, ok, detail):
        print(("ok    " if ok else "FAIL  ") + name)
        if not ok:
            failures.append(name)
            print("      " + detail[:800].replace("\n", "\n      "))  # a collection's output is long

    r = run(binary, ["build-info"])
    check("build-info", r.returncode == 0 and r.stdout.strip() == "platmon-native-allocator=system;", repr(r))
    for command in ("collect", "serve"):
        r = run(binary, [command])
        check(command + " exits", r.returncode == 1 and not r.stdout
              and "no collector for this platform" in r.stderr, repr(r))
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
    if failures:
        sys.exit(f"{len(failures)} failed: {', '.join(failures)}")
    print("all passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
