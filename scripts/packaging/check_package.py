#!/usr/bin/env python3
"""Extract and smoke-test platmon's .deb and platmon-frontend's outside the checkout, without installing them, on
a host of the server's architecture: the server alone first (the API, and / naming platmon-frontend), then with
platmon-frontend's files beside it (the web page, the platmon command, the tray's --version)."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def check(package, frontend_package=None):
    package = Path(package).resolve()
    full = subprocess.check_output(["dpkg-deb", "-f", str(package), "Version"], text=True).strip()
    release = full.split("-")[0]
    arch = subprocess.check_output(["dpkg-deb", "-f", str(package), "Architecture"], text=True).strip()
    # platmon-frontend of the same version and architecture, beside it, as package.py writes them
    frontend_package = (Path(frontend_package).resolve() if frontend_package
                        else package.parent / f"platmon-frontend_{full}_{arch}.deb")
    if not frontend_package.is_file():
        raise ValueError(f"no {frontend_package.name} beside {package.name}")
    host = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    if arch != host:
        raise ValueError(f"a {arch} package needs a {arch} host to run its server (this is {host})")
    with tempfile.TemporaryDirectory(prefix="platmon-smoke-") as directory:
        work = Path(directory)
        root = work / "root"
        subprocess.run(["dpkg-deb", "-R", str(package), str(root)], check=True)
        server, cli = root / "usr/lib/platmon/platmon-server", root / "usr/bin/platmon"
        env = dict(os.environ)  # the server and the command, both static: nothing from the checkout
        if cli.exists() or (root / "usr/share/platmon").exists():
            raise ValueError("platmon has platmon-frontend's files (the command or the web page)")
        # The server's arguments are the packaged unit's, with the extracted paths and a test config.
        unit = (root / "usr/lib/systemd/system/platmon.service").read_text(encoding="utf-8")
        exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart=")).split()
        expected = ["ExecStart=/usr/lib/platmon/platmon-server", "serve", "/etc/platmon/platmon.ini", "--web",
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
                # the core alone: the API answers, the page is missing and / says which package has it
                try:
                    urllib.request.urlopen(base + "/", timeout=5)
                    raise ValueError("/ answered without platmon-frontend's files")
                except urllib.error.HTTPError as e:
                    if e.code != 404 or b"platmon-frontend" not in e.read():
                        raise ValueError(f"/ without the page: {e.code}, not a 404 naming platmon-frontend")
                subprocess.run(["dpkg-deb", "-x", str(frontend_package), str(root)], check=True)  # the page, command, tray
                result = subprocess.check_output([str(cli), "--version"], cwd=work, env=env, text=True).strip()
                if result != f"platmon {release}":
                    raise ValueError(f"version mismatch: {result!r} != 'platmon {release}'")
                tray = root / "usr/bin/platmon-tray"
                result = subprocess.check_output([str(tray), "--version"], cwd=work, env=env, text=True).strip()
                if result != f"platmon-tray {release}":
                    raise ValueError(f"version mismatch: {result!r} != 'platmon-tray {release}'")
                if (root / "etc/xdg/autostart").exists():  # the tray starts only for users who ask (--autostart)
                    raise ValueError("platmon-frontend starts the tray for every user (/etc/xdg/autostart)")
                for route in ("/api/status", "/api/observations", "/api/history", "/api/samples",
                              "/api/observation-samples?group=battery", "/text", "/", "/app.js", "/styles.css",
                              "/assets/brand/platmon-logo-light.svg", "/assets/brand/platmon-logo-dark.svg",
                              "/assets/brand/favicon.svg"):
                    with urllib.request.urlopen(base + route, timeout=5) as response:
                        body = response.read()
                        if not body:
                            raise ValueError("empty packaged response: " + route)
                        if route == "/" and b'src="/app.js"' not in body:
                            raise ValueError("/ is not the web page")
                        if route == "/api/status" and json.loads(body).get("version") != release:
                            raise ValueError("/api/status does not say version " + release)
                output = subprocess.check_output([str(cli), f"127.0.0.1:{port}", "--once"],
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
    print(f"package smoke passed: {package.name} alone (API, / naming platmon-frontend), then with "
          f"{frontend_package.name} (CLI, tray, web and SVG)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help="platmon's .deb for this host's architecture")
    parser.add_argument("frontend_package", nargs="?",
                        help="platmon-frontend's .deb (default: the one of the same version beside it)")
    args = parser.parse_args()
    try:
        check(args.package, args.frontend_package)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        parser.exit(1, f"package check failed: {e}\n")


if __name__ == "__main__":
    main()
