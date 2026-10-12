#!/usr/bin/env python3
"""Check real dpkg lifecycle inside a disposable container, with read-only artifacts.

Uses a local Ubuntu image by default. Only the test container installs OS tools;
no host service, dpkg database, source checkout or Docker socket is mounted in it.
"""
import argparse
import os
from pathlib import Path
import subprocess
import uuid


def invoke(*args, success=True):
    result = subprocess.run(args, text=True, capture_output=True)
    if (result.returncode == 0) != success:
        raise RuntimeError("command failed expectation: " + " ".join(args) + "\n" + result.stdout + result.stderr)
    return result.stdout.strip()


def inside(first, second):
    # This entry point is used only in the disposable, root-owned test container.
    if not Path("/.dockerenv").is_file() or os.environ.get("PLATMON_PACKAGE_TEST_CONTAINER") != "1":
        raise RuntimeError("inside mode requires the disposable package test container")
    if Path("/proc/1/comm").read_text().strip() == "systemd":
        raise RuntimeError("this lifecycle test must not run against a live systemd host")
    ini = Path("/etc/platmon/platmon.ini")
    unit = Path("/etc/systemd/system/platmon.service")
    first_version = invoke("dpkg-deb", "-f", str(first), "Version")
    second_version = invoke("dpkg-deb", "-f", str(second), "Version")
    invoke("dpkg", "--compare-versions", first_version, "lt", second_version)
    invoke("dpkg", "-i", str(first))
    assert invoke("dpkg-query", "-W", "-f=${Version}", "platmon") == first_version
    assert invoke("/usr/bin/platmon", "--version") == "platmon " + first_version.split("-")[0]
    invoke("systemctl", "is-enabled", "platmon.service")
    custom = "[http]\nbind = 127.0.0.1\nport = 19797\n[history]\nenabled = no\n"
    ini.write_text(custom)
    invoke("systemctl", "disable", "platmon.service")
    invoke("dpkg", "--force-confold", "-i", str(second))
    assert ini.read_text() == custom
    assert invoke("dpkg-query", "-W", "-f=${Version}", "platmon") == second_version
    assert invoke("systemctl", "is-enabled", "platmon.service", success=False) == "disabled"
    invoke("systemctl", "mask", "platmon.service")
    invoke("dpkg", "--force-confold", "-i", str(first))
    assert unit.is_symlink() and unit.readlink() == Path("/dev/null")
    assert ini.read_text() == custom
    invoke("systemctl", "unmask", "platmon.service")
    invoke("dpkg", "--remove", "platmon")
    assert ini.read_text() == custom
    assert not Path("/usr/bin/platmon").exists()
    assert not Path("/usr/share/platmon/platmon.py").exists()
    invoke("dpkg", "--purge", "platmon")
    assert not ini.exists()
    # A source unit must be refused before it or any source-installed code is lost.
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=/old/platmon.py\n")
    invoke("dpkg", "-i", str(first), success=False)
    assert unit.read_text().endswith("ExecStart=/old/platmon.py\n")
    assert not Path("/usr/share/platmon/platmon.py").exists()
    unit.unlink()  # Only the fixture created above is removed.
    # Migration preserves a pre-existing INI and administrator-owned local command.
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_text(custom)
    local = Path("/usr/local/bin/platmon")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("retained local command\n")
    invoke("dpkg", "--force-confold", "-i", str(first))
    assert ini.read_text() == custom and local.read_text() == "retained local command\n"
    # Invalid configuration fails configure instead of silently starting with defaults.
    ini.write_text("[http]\nprot = 9797\n")
    invoke("dpkg", "--force-confold", "-i", str(second), success=False)
    assert ini.read_text() == "[http]\nprot = 9797\n"
    ini.write_text(custom)
    invoke("dpkg", "--configure", "platmon")
    extra = ini.parent / "retained-backup.ini"
    extra.write_text("local backup\n")
    invoke("dpkg", "--purge", "platmon")
    assert not ini.exists() and extra.read_text() == "local backup\n"
    assert local.read_text() == "retained local command\n"
    print("dpkg lifecycle passed: install, config-preserving upgrade/downgrade, disabled/masked state, "
          "remove/purge, migration refusal and invalid config recovery")


def check(first, second, image):
    first, second = Path(first).resolve(), Path(second).resolve()
    if not first.is_file() or not second.is_file():
        raise ValueError("both package files must exist")
    name = "platmon-package-check-" + uuid.uuid4().hex[:12]
    setup = ('apt-get update -qq >&2 && '
             'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends '
             'python3 systemd init-system-helpers >&2 && exec python3 - "$@"')
    command = ["docker", "run", "--rm", "-i", "--name", name,
               "--security-opt=no-new-privileges", "--env", "PYTHONDONTWRITEBYTECODE=1",
               "--env", "PLATMON_PACKAGE_TEST_CONTAINER=1",
               "--mount", f"type=bind,src={first.parent},dst=/first,readonly",
               "--mount", f"type=bind,src={second.parent},dst=/second,readonly",
               image, "sh", "-c", setup, "sh", "--inside",
               f"/first/{first.name}", f"/second/{second.name}"]
    try:
        subprocess.run(command, input=Path(__file__).read_text(encoding="utf-8"), text=True,
                       check=True, timeout=300)
    finally:
        # The unique name identifies only this invocation's disposable container.
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package")
    parser.add_argument("upgrade", help="a package with a greater dpkg version")
    parser.add_argument("--image", default="ubuntu:24.04")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.inside:
            inside(Path(args.package), Path(args.upgrade))
        else:
            check(args.package, args.upgrade, args.image)
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as e:
        parser.exit(1, f"package lifecycle failed: {e}\n")


if __name__ == "__main__":
    main()
