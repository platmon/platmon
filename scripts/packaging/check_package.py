#!/usr/bin/env python3
"""Extract and smoke-test platmon's .deb and platmon-cli's outside the checkout, without installing them, on a
host of the server's architecture."""
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


def check(package, cli_package=None):
    package = Path(package).resolve()
    full = subprocess.check_output(["dpkg-deb", "-f", str(package), "Version"], text=True).strip()
    release = full.split("-")[0]
    # platmon-cli of the same version, beside it, as package.py writes them
    cli_package = Path(cli_package).resolve() if cli_package else package.parent / f"platmon-cli_{full}_all.deb"
    if not cli_package.is_file():
        raise ValueError(f"no {cli_package.name} beside {package.name}")
    arch = subprocess.check_output(["dpkg-deb", "-f", str(package), "Architecture"], text=True).strip()
    host = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    if arch != host:
        raise ValueError(f"a {arch} package needs a {arch} host to run its server (this is {host})")
    with tempfile.TemporaryDirectory(prefix="platmon-smoke-") as directory:
        work = Path(directory)
        root = work / "root"
        subprocess.run(["dpkg-deb", "-R", str(package), str(root)], check=True)
        subprocess.run(["dpkg-deb", "-x", str(cli_package), str(root)], check=True)  # the platmon command
        server, cli = root / "usr/lib/platmon/platmon-native", root / "usr/bin/platmon"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        # PYTHONPATH must not accidentally make the source checkout available.
        env.pop("PYTHONPATH", None)
        result = subprocess.check_output([sys.executable, str(cli), "--version"], cwd=work, env=env, text=True).strip()
        if result != f"platmon {release}":
            raise ValueError(f"version mismatch: {result!r} != 'platmon {release}'")
        # The server's arguments are the packaged unit's, with the extracted paths and a test config.
        unit = (root / "usr/lib/systemd/system/platmon.service").read_text(encoding="utf-8")
        exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart=")).split()
        expected = ["ExecStart=/usr/lib/platmon/platmon-native", "serve", "/etc/platmon/platmon.ini", "--web",
                    "/usr/share/platmon/frontends/web", "--app-version", release]
        if exec_start != expected:
            raise ValueError(f"unexpected ExecStart: {exec_start}")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = work / "smoke.ini"
        config.write_text(f"[http]\nbind = 127.0.0.1\nport = {port}\n", encoding="utf-8")
        with (work / "server.log").open("w+") as log:
            process = subprocess.Popen([str(server), "serve", str(config), "--web",
                                        str(root / "usr/share/platmon/frontends/web"), "--app-version", release],
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
                        body = response.read()
                        if not body:
                            raise ValueError("empty packaged response: " + route)
                        if route == "/" and release.encode() not in body:
                            raise ValueError("the web page does not show version " + release)
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
    print(f"package smoke passed: {package.name} and {cli_package.name} (server, CLI, API, web and SVG)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help="platmon's .deb for this host's architecture")
    parser.add_argument("cli_package", nargs="?", help="platmon-cli's .deb (default: the one of the same version beside it)")
    args = parser.parse_args()
    try:
        check(args.package, args.cli_package)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        parser.exit(1, f"package check failed: {e}\n")


if __name__ == "__main__":
    main()
