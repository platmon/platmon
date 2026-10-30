#!/usr/bin/env python3
"""Check the .rpm packages under a booted systemd, inside a disposable container.

The first install refuses a local unit and then enables and starts the service without platmon-cli (only
recommended), which then brings the platmon command; an upgrade keeps an edited INI and restarts the
service; an invalid INI is reported and the running server left alone; a disabled service is not started;
a failed start is reported; a downgrade recovers; erasing stops and disables the service and keeps the
edited INI as .rpmsave. The packages beside each one: platmon-cli of the same version. The container gets
CAP_SYS_ADMIN without seccomp/AppArmor confinement for systemd's sandboxing, never --privileged; no host
service, rpm database or source checkout is mounted in it.

  python3 scripts/packaging/check_rpm.py dist/first/platmon-V-1.x86_64.rpm dist/second/platmon-V-2.x86_64.rpm \
    --image platmon-rpm-test:local
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

SERVER = "/usr/lib/platmon/platmon-server"
INI = Path("/etc/platmon/platmon.ini")
LOCAL_UNIT = Path("/etc/systemd/system/platmon.service")
CUSTOM = "[http]\nport = 19797\n[history]\nretention = 300\n"
PORT = 19797


def invoke(*args, success=True):
    result = subprocess.run(args, text=True, capture_output=True)
    if success is not None and (result.returncode == 0) != success:
        raise RuntimeError("command failed expectation: " + " ".join(args) + "\n" + result.stdout + result.stderr)
    return result.stdout.strip() + result.stderr.strip()


def rpm(*args, success=True):
    # The steps restart the unit within seconds of each other, more often than systemd's default start
    # limit (5 in 10 s) allows; clear the count as an administrator would between attempts.
    invoke("systemctl", "reset-failed", "platmon.service", success=None)
    return invoke("rpm", *args, success=success)


def field(package, tag):
    return invoke("rpm", "-qp", "--qf", f"%{{{tag}}}", str(package))


def cli_beside(package):
    path = package.parent / f"platmon-cli-{field(package, 'VERSION')}-{field(package, 'RELEASE')}.noarch.rpm"
    if not path.is_file():
        raise RuntimeError(f"no {path.name} beside {package.name}")
    return path


def unit():
    out = invoke("systemctl", "show", "platmon.service", "-p", "ActiveState", "-p", "MainPID", "-p", "ControlGroup")
    return dict(line.split("=", 1) for line in out.splitlines())


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
    return status, Path(f"/proc/{state['MainPID']}/cmdline").read_bytes().split(b"\0")[0].decode()


def lifecycle(first, second):
    first_cli, second_cli = cli_beside(first), cli_beside(second)
    first_version = field(first, "VERSION") + "-" + field(first, "RELEASE")
    second_version = field(second, "VERSION") + "-" + field(second, "RELEASE")
    installed = lambda name: invoke("rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", name, success=None)
    # A local unit (a source install) is refused before anything is installed.
    LOCAL_UNIT.write_text("[Service]\nExecStart=/old/platmon-server serve\n")
    out = rpm("-i", str(first), success=False)
    assert "local platmon.service exists" in out, out
    assert not Path(SERVER).exists() and LOCAL_UNIT.read_text().endswith("serve\n")
    LOCAL_UNIT.unlink()  # Only the fixture created above is removed.
    # The first install enables and starts the service; the command comes with platmon-cli.
    rpm("-i", str(first))
    assert installed("platmon") == first_version
    assert invoke("systemctl", "is-enabled", "platmon.service") == "enabled"
    status, exe = ready(9797)
    assert exe == SERVER, exe
    assert not Path("/usr/bin/platmon").exists()
    rpm("-i", str(first_cli))
    assert invoke("/usr/bin/platmon", "--version") == "platmon " + field(first, "VERSION")
    INI.write_text(CUSTOM)
    invoke("systemctl", "restart", "platmon.service")
    before, _ = ready()
    # The upgrade keeps the INI and restarts the service.
    out = rpm("-U", str(second), str(second_cli))
    assert "health check skipped" not in out and "failed" not in out, out
    assert installed("platmon") == second_version and installed("platmon-cli") == second_version
    after, exe = ready()
    assert exe == SERVER and after["instance_id"] != before["instance_id"]
    assert INI.read_text() == CUSTOM and not Path(str(INI) + ".rpmnew").exists()
    # An invalid INI is reported before the restart: the running server is left alone.
    pid = unit()["MainPID"]
    INI.write_text("[http]\nprot = 19797\n")
    out = rpm("-U", "--replacepkgs", str(second), success=None)
    assert "invalid platmon config: [http] unknown key(s): prot" in out, out
    assert unit()["MainPID"] == pid
    INI.write_text(CUSTOM)
    # A disabled, stopped service stays stopped; the check says it was skipped.
    invoke("systemctl", "disable", "--now", "platmon.service")
    out = rpm("-U", "--replacepkgs", str(second))
    assert "health check skipped" in out, out
    assert unit()["ActiveState"] == "inactive"
    assert invoke("systemctl", "is-enabled", "platmon.service", success=False) == "disabled"
    invoke("systemctl", "enable", "--now", "platmon.service")
    ready()
    # A start that fails (the port held by another process) is reported.
    invoke("systemctl", "stop", "platmon.service")
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # past the stopped server's TIME_WAIT
    holder.bind(("0.0.0.0", PORT))
    holder.listen()
    try:
        out = rpm("-U", "--replacepkgs", str(second), success=None)
        assert "platmon.service failed its health check" in out, out
    finally:
        holder.close()
    # The downgrade recovers, with the INI.
    rpm("-U", "--oldpackage", str(first), str(first_cli))
    assert installed("platmon") == first_version and installed("platmon-cli") == first_version
    status, exe = ready()
    assert exe == SERVER and INI.read_text() == CUSTOM
    # Erasing stops and disables the service, keeps the edited INI and leaves the command to platmon-cli.
    rpm("-e", "platmon")
    assert unit()["ActiveState"] == "inactive" and not Path(SERVER).exists()
    assert not Path("/etc/systemd/system/multi-user.target.wants/platmon.service").exists()
    assert Path(str(INI) + ".rpmsave").read_text() == CUSTOM
    assert Path("/usr/bin/platmon").exists()
    rpm("-e", "platmon-cli")
    assert not Path("/usr/bin/platmon").exists()
    print("rpm lifecycle passed: local unit refused, first install started without platmon-cli, then with it, "
          "upgrade with the INI kept, invalid INI, disabled service, failed start, downgrade, erase")


def inside(first, second):
    # This entry point is used only in the disposable, systemd-booted test container.
    if not Path("/.dockerenv").is_file() or os.environ.get("PLATMON_PACKAGE_TEST_CONTAINER") != "1":
        raise RuntimeError("inside mode requires the disposable package test container")
    if Path("/proc/1/comm").read_text().strip() != "systemd":
        raise RuntimeError("inside mode requires systemd as the container's init")
    try:
        lifecycle(first, second)
    except (RuntimeError, AssertionError):
        subprocess.run(["journalctl", "-u", "platmon.service", "-n", "40", "--no-pager"])
        raise


def booted(name, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        # A container whose boot command failed (no systemd to exec) is gone: say so now, not at the deadline.
        running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], text=True,
                                 capture_output=True).stdout.strip()
        if running != "true":
            raise RuntimeError("the container stopped before systemd booted (its log is above)")
        state = subprocess.run(["docker", "exec", name, "systemctl", "is-system-running"], text=True,
                               capture_output=True).stdout.strip()
        if state in ("running", "degraded"):
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"systemd did not finish booting in the container ({state or 'no answer'})")
        time.sleep(1)


def check(first, second, image):
    first, second = Path(first).resolve(), Path(second).resolve()
    if not first.is_file() or not second.is_file():
        raise ValueError("both package files must exist")
    name = "platmon-rpm-check-" + uuid.uuid4().hex[:12]
    # The OS tools come with the image (prepare_test_image.py); installed here only when it lacks them.
    boot = ("if ! { command -v python3 && [ -x /usr/lib/systemd/systemd ]; } >/dev/null; then "
            "dnf install -y -q --setopt=install_weak_deps=False python3 systemd || exit; fi; "
            "mount -o remount,rw /sys/fs/cgroup && exec /usr/lib/systemd/systemd")
    script = Path(__file__).read_text(encoding="utf-8")
    try:
        subprocess.run(["docker", "run", "-d", "--name", name, "--env", "container=docker",
                        "--cap-add", "SYS_ADMIN", "--security-opt", "seccomp=unconfined",
                        "--security-opt", "apparmor=unconfined", "--cgroupns", "private",
                        "--tmpfs", "/run", "--tmpfs", "/run/lock",
                        "--mount", f"type=bind,src={first.parent},dst=/first,readonly",
                        "--mount", f"type=bind,src={second.parent},dst=/second,readonly",
                        image, "sh", "-c", boot], check=True, stdout=subprocess.DEVNULL, timeout=60)
        booted(name)
        subprocess.run(["docker", "exec", "-i", "--env", "PYTHONDONTWRITEBYTECODE=1",
                        "--env", "PLATMON_PACKAGE_TEST_CONTAINER=1", name, "python3", "-", "--inside",
                        f"/first/{first.name}", f"/second/{second.name}"], input=script, text=True, check=True,
                       timeout=600)
    except (RuntimeError, subprocess.SubprocessError):
        subprocess.run(["docker", "logs", "--tail", "60", name], timeout=60)
        raise
    finally:
        # The unique name identifies only this invocation's disposable container.
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("package", help="a platmon .rpm, with platmon-cli's beside it")
    parser.add_argument("upgrade", help="a platmon .rpm with a greater version-release, likewise")
    parser.add_argument("--image", default="fedora:44")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.inside:
            inside(Path(args.package), Path(args.upgrade))
        else:
            check(args.package, args.upgrade, args.image)
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as e:
        parser.exit(1, f"rpm check failed: {e}\n")


if __name__ == "__main__":
    main()
