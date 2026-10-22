#!/usr/bin/env python3
"""Check the packaged service under a booted systemd, inside a disposable container.

From a previous package (the released Python one) to this one: the upgrade keeps an edited INI and swaps
the server in the same unit; a failed start fails configuration and installing the previous package
recovers; a disabled service is not started; an invalid INI stops the upgrade before the restart; after
the container restarts (systemd boots again) the enabled service is back. The container gets
CAP_SYS_ADMIN without seccomp/AppArmor confinement for systemd's sandboxing, never --privileged;
no host service, dpkg database or source checkout is mounted in it.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import uuid

SERVER = "/usr/lib/platmon/platmon-native"
INI = Path("/etc/platmon/platmon.ini")
CUSTOM = "[http]\nport = 19797\n[history]\nretention = 300\n"
PORT = 19797


def invoke(*args, success=True):
    result = subprocess.run(args, text=True, capture_output=True)
    if success is not None and (result.returncode == 0) != success:
        raise RuntimeError("command failed expectation: " + " ".join(args) + "\n" + result.stdout + result.stderr)
    return result.stdout.strip() + result.stderr.strip()


def dpkg(*args, success=True):
    # The steps restart the unit within seconds of each other, more often than systemd's default start
    # limit (5 in 10 s) allows; clear the count as an administrator would between attempts.
    invoke("systemctl", "reset-failed", "platmon.service", success=None)
    return invoke("dpkg", *args, success=success)


def unit():
    out = invoke("systemctl", "show", "platmon.service", "-p", "ActiveState", "-p", "MainPID", "-p", "NRestarts",
                 "-p", "ControlGroup")
    return dict(line.split("=", 1) for line in out.splitlines())


def package():
    return invoke("dpkg-query", "-W", "-f=${Version} ${Architecture} ${Status}", "platmon")


def ready(port=PORT, timeout=20):
    """The service's /api/status once it is ready, and the program its one process runs (argv[0])."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=2) as response:
                status = json.load(response)
            if status.get("ready") is True:
                break
        except (OSError, ValueError):
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"no ready /api/status on port {port}: {unit()}")
        time.sleep(0.2)
    state = unit()
    assert state["ActiveState"] == "active", state
    procs = Path("/sys/fs/cgroup", state["ControlGroup"].lstrip("/"), "cgroup.procs").read_text().split()
    assert procs == [state["MainPID"]], f"one process in the unit, not {procs}"
    # /proc/PID/exe of the DynamicUser process needs CAP_SYS_PTRACE; the command line does not.
    return status, Path(f"/proc/{state['MainPID']}/cmdline").read_bytes().split(b"\0")[0].decode()


def python_server(exe):
    return Path(exe).name.startswith("python3")


def upgrade(old, new):
    old_version = invoke("dpkg-deb", "-f", str(old), "Version")
    new_version = invoke("dpkg-deb", "-f", str(new), "Version")
    invoke("dpkg", "--compare-versions", old_version, "lt", new_version)
    # The previous package, then an administrator's INI.
    dpkg("-i", str(old))
    status, exe = ready(9797)
    assert python_server(exe), exe
    INI.write_text(CUSTOM)
    invoke("systemctl", "restart", "platmon.service")
    before, exe = ready()
    assert python_server(exe), exe
    # The upgrade: the INI kept, the same unit now runs the native server, a new instance.
    out = dpkg("--force-confold", "-i", str(new))
    assert "health check skipped" not in out, out
    assert package() == f"{new_version} {invoke('dpkg', '--print-architecture')} install ok installed", package()
    after, exe = ready()
    assert exe == SERVER, exe
    assert after["instance_id"] != before["instance_id"]
    assert INI.read_text() == CUSTOM
    # A start that fails (the port held by another process) fails configuration.
    invoke("systemctl", "stop", "platmon.service")
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # past the stopped server's TIME_WAIT
    holder.bind(("0.0.0.0", PORT))
    holder.listen()
    try:
        out = dpkg("--force-confold", "-i", str(new), success=False)
        assert "platmon.service failed its health check" in out, out
        assert package().endswith("install ok half-configured"), package()
    finally:
        holder.close()
    # Installing the previous package recovers from there.
    dpkg("--force-confold", "-i", str(old))
    assert package() == f"{old_version} all install ok installed", package()
    status, exe = ready()
    assert python_server(exe), exe
    assert INI.read_text() == CUSTOM
    dpkg("--force-confold", "-i", str(new))
    status, exe = ready()
    assert exe == SERVER, exe
    # A disabled, stopped service stays stopped; the check says it was skipped.
    invoke("systemctl", "disable", "--now", "platmon.service")
    out = dpkg("--force-confold", "-i", str(new))
    assert "health check skipped" in out, out
    assert unit()["ActiveState"] == "inactive"
    invoke("systemctl", "enable", "--now", "platmon.service")
    status, exe = ready()
    # An invalid INI stops configuration before the restart: the running server is left alone.
    pid = unit()["MainPID"]
    INI.write_text("[http]\nprot = 19797\n")
    out = dpkg("--force-confold", "-i", str(new), success=False)
    assert "invalid platmon config: [http] unknown key(s): prot" in out, out
    assert unit()["MainPID"] == pid
    INI.write_text(CUSTOM)
    dpkg("--configure", "platmon")
    status, exe = ready()
    assert exe == SERVER and package().endswith("install ok installed")
    print("service upgrade passed: INI kept, server swapped in the unit, failed start, recovery by the previous "
          "package, disabled service, invalid INI")


def after_restart():
    state = unit()
    status, exe = ready()
    assert exe == SERVER and state["NRestarts"] == "0", state
    assert invoke("systemctl", "is-enabled", "platmon.service") == "enabled"
    assert INI.read_text() == CUSTOM
    print("service after a systemd boot passed: enabled, started once, ready")


def inside(phase, old, new):
    # This entry point is used only in the disposable, systemd-booted test container.
    if not Path("/.dockerenv").is_file() or os.environ.get("PLATMON_PACKAGE_TEST_CONTAINER") != "1":
        raise RuntimeError("inside mode requires the disposable package test container")
    if Path("/proc/1/comm").read_text().strip() != "systemd":
        raise RuntimeError("inside mode requires systemd as the container's init")
    try:
        if phase == "upgrade":
            upgrade(old, new)
        else:
            after_restart()
    except (RuntimeError, AssertionError):
        subprocess.run(["journalctl", "-u", "platmon.service", "-n", "40", "--no-pager"])
        raise


def booted(name, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        state = subprocess.run(["docker", "exec", name, "systemctl", "is-system-running"], text=True,
                               capture_output=True).stdout.strip()
        if state in ("running", "degraded"):
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"systemd did not finish booting in the container ({state or 'no answer'})")
        time.sleep(1)


def check(old, new, image):
    old, new = Path(old).resolve(), Path(new).resolve()
    if not old.is_file() or not new.is_file():
        raise ValueError("both package files must exist")
    name = "platmon-service-check-" + uuid.uuid4().hex[:12]
    boot = ("if [ ! -x /lib/systemd/systemd ]; then apt-get update -qq && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "
            "systemd python3 init-system-helpers >/dev/null; fi; "
            # The image's policy-rc.d forbids service starts; a host has none.
            "rm -f /usr/sbin/policy-rc.d; mount -o remount,rw /sys/fs/cgroup && exec /lib/systemd/systemd")
    script = Path(__file__).read_text(encoding="utf-8")
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "--env", "container=docker",
                        "--cap-add", "SYS_ADMIN", "--security-opt", "seccomp=unconfined",
                        "--security-opt", "apparmor=unconfined", "--cgroupns", "private",
                        "--tmpfs", "/run", "--tmpfs", "/run/lock",
                        "--mount", f"type=bind,src={old.parent},dst=/old,readonly",
                        "--mount", f"type=bind,src={new.parent},dst=/new,readonly",
                        image, "sh", "-c", boot], check=True, stdout=subprocess.DEVNULL, timeout=60)
        for phase in ("upgrade", "restart"):
            if phase == "restart":
                subprocess.run(["docker", "restart", name], check=True, stdout=subprocess.DEVNULL, timeout=120)
            booted(name)
            subprocess.run(["docker", "exec", "-i", "--env", "PYTHONDONTWRITEBYTECODE=1",
                            "--env", "PLATMON_PACKAGE_TEST_CONTAINER=1", name, "python3", "-", "--inside", phase,
                            f"/old/{old.name}", f"/new/{new.name}"], input=script, text=True, check=True,
                           timeout=600)
    finally:
        # The unique name identifies only this invocation's disposable container.
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous", help="the package upgraded from (the released one)")
    parser.add_argument("package", help="this package, with a greater dpkg version")
    parser.add_argument("--image", default="ubuntu:24.04")
    parser.add_argument("--inside", choices=("upgrade", "restart"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.inside:
            inside(args.inside, Path(args.previous), Path(args.package))
        else:
            check(args.previous, args.package, args.image)
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as e:
        parser.exit(1, f"package service check failed: {e}\n")


if __name__ == "__main__":
    main()
