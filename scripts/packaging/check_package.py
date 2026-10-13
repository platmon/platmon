#!/usr/bin/env python3
"""Extract and smoke-test a .deb outside the checkout, without installing it."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def check(package):
    package = Path(package).resolve()
    release = subprocess.check_output(["dpkg-deb", "-f", str(package), "Version"], text=True).strip().split("-")[0]
    with tempfile.TemporaryDirectory(prefix="platmon-smoke-") as directory:
        work = Path(directory)
        root = work / "root"
        subprocess.run(["dpkg-deb", "-R", str(package), str(root)], check=True)
        app, cli = root / "usr/share/platmon", root / "usr/bin/platmon"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        # PYTHONPATH must not accidentally make the source checkout available.
        env.pop("PYTHONPATH", None)
        for command, expected in (([sys.executable, str(app / "platmon.py"), "--version"], f"platmon server {release}"),
                                  ([sys.executable, str(cli), "--version"], f"platmon {release}")):
            result = subprocess.check_output(command, cwd=work, env=env, text=True).strip()
            if result != expected:
                raise ValueError(f"version mismatch: {result!r} != {expected!r}")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = work / "smoke.ini"
        config.write_text(f"[http]\nbind = 127.0.0.1\nport = {port}\n", encoding="utf-8")
        with (work / "server.log").open("w+") as log:
            process = subprocess.Popen([sys.executable, str(app / "platmon.py"), str(config)],
                                       cwd=work, env=env, stdout=log, stderr=log)
            try:
                base = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        log.seek(0)
                        raise ValueError("packaged server exited: " + log.read())
                    try:
                        with urllib.request.urlopen(base + "/api/stats", timeout=1) as response:
                            stats = json.load(response)
                        break
                    except (OSError, ValueError):
                        if time.monotonic() >= deadline:
                            raise ValueError("packaged server did not become ready")
                        time.sleep(0.1)
                if stats.get("schema_version") != 1 or "cpu" not in stats:
                    raise ValueError("invalid packaged stats response")
                for route in ("/api/status", "/api/observations", "/api/history", "/api/samples",
                              "/api/observation-samples?group=battery", "/text", "/", "/app.js", "/styles.css",
                              "/assets/brand/platmon-logo-light.svg", "/assets/brand/platmon-logo-dark.svg",
                              "/assets/brand/favicon.svg"):
                    with urllib.request.urlopen(base + route, timeout=5) as response:
                        if not response.read():
                            raise ValueError("empty packaged response: " + route)
                output = subprocess.check_output([sys.executable, str(cli), f"127.0.0.1:{port}", "--once"],
                                                 cwd=work, env=env, text=True, timeout=15)
                if "CPU" not in output:
                    raise ValueError("packaged CLI did not display stats")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print(f"package smoke passed: {package.name} (server, CLI, API, web and SVG)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package")
    args = parser.parse_args()
    try:
        check(args.package)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        parser.exit(1, f"package check failed: {e}\n")


if __name__ == "__main__":
    main()
