import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT_DIR = ROOT / "scripts"
SCRIPTS = sorted([*SCRIPT_DIR.glob("**/*.sh"), *(ROOT / "deploy").glob("**/*.sh")])


def test_expected_scripts_exist():
    assert [str(p.relative_to(ROOT)) for p in SCRIPTS] == \
           ["deploy/docker/start.sh", "deploy/docker/stop.sh",
            "deploy/systemd/install.sh", "deploy/systemd/start.sh", "deploy/systemd/stop.sh",
            "deploy/systemd/uninstall.sh", "scripts/install-cli.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_script_is_executable_bash(script):
    assert os.access(script, os.X_OK)
    subprocess.run(["bash", "-n", str(script)], check=True)  # syntax only; they need sudo/docker to run


def test_install_cli_user(tmp_path):
    """--user installs ~/.local/bin/platmon (no sudo), never overwrites a foreign file, and uninstalls."""
    script = str(SCRIPT_DIR / "install-cli.sh")
    env = dict(os.environ, HOME=str(tmp_path))
    target = tmp_path / ".local/bin/platmon"

    out = subprocess.run([script, "--user"], env=env, capture_output=True, text=True, check=True).stdout
    assert f"installed {target}" in out
    assert "is not in this shell's PATH. A new login shell often has it" in out  # temp HOME is not on PATH
    assert os.access(target, os.X_OK) and "# platmon-cli:" in target.read_text()
    help_text = subprocess.run([str(target), "--help"], capture_output=True, text=True, check=True).stdout
    assert help_text.startswith("usage: platmon")

    subprocess.run([script, "--user", "--uninstall"], env=env, check=True, capture_output=True)
    assert not target.exists()

    target.write_text("#!/bin/sh\necho someone else's platmon\n")
    r = subprocess.run([script, "--user"], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "not the platmon client" in r.stderr
    assert "someone else's" in target.read_text()


def test_source_uninstall_removes_cli_from_relocated_checkout(tmp_path):
    """Only temporary paths and fake privilege checks are used; no host service is touched."""
    repo = tmp_path / "repo"
    uninstall = repo / "deploy/systemd/uninstall.sh"
    uninstall.parent.mkdir(parents=True)
    prefix = tmp_path / "installed"
    prefix.mkdir()
    (prefix / "old-code").write_text("previous installation")
    text = (ROOT / "deploy/systemd/uninstall.sh").read_text()
    text = text.replace("unit=/etc/systemd/system/platmon.service", f"unit={tmp_path / 'unit'}")
    text = text.replace("/opt/platmon", str(prefix))
    uninstall.write_text(text)
    cli = repo / "scripts/install-cli.sh"
    cli.parent.mkdir()
    cli.write_text('#!/bin/sh\n[ "$1" = --uninstall ] || exit 1\nprintf "%s\\n" "$1" > "$CLI_CALLS"\n')
    cli.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, command in (("id", "echo 0"), ("dpkg-query", "exit 1")):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n" + command + "\n")
        stub.chmod(0o755)
    calls = tmp_path / "cli-calls"
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], CLI_CALLS=str(calls))
    result = subprocess.run(["bash", str(uninstall)], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not prefix.exists()
    assert calls.exists(), result.stderr
    assert calls.read_text() == "--uninstall\n"


@pytest.mark.parametrize("mode, listener, expected", [
    ("host", "localhost 9797", "look at it with: platmon\n"),
    ("host", "localhost 19799", "look at it with: platmon localhost:19799"),
    ("host", "192.0.2.1 9797", "look at it with: platmon 192.0.2.1:9797"),
    ("bridge", "unused", "look at it with: platmon localhost:19800"),
])
def test_docker_start_reports_host_or_published_listener(tmp_path, mode, listener, expected):
    """Exercise the real start script without a Docker daemon or privileged commands."""
    import json
    import sys
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[0] == "inspect":
    field = args[2]
    print("healthy" if "Health" in field else os.environ["TEST_MODE"] if "NetworkMode" in field else "true")
elif "ps" in args:
    print("candidate")
elif "exec" in args:
    assert "load_config" in args[-1] and "/opt/platmon/platmon.ini" in args[-1]
    print(os.environ["TEST_LISTENER"])
elif "port" in args:
    assert os.environ["TEST_MODE"] != "host", "host mode must not query port mappings"
    print("0.0.0.0:19800")
elif not ("up" in args or "logs" in args):
    raise SystemExit("unexpected Docker command: " + repr(args))
''')
    docker.chmod(0o755)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 1\n")
    systemctl.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    env = dict(os.environ, HOME=str(tmp_path), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               TEST_MODE=mode, TEST_LISTENER=listener, DOCKER_CALLS=str(calls))
    result = subprocess.run([str(ROOT / "deploy/docker/start.sh")], env=env, capture_output=True, text=True, check=True)
    assert expected in result.stdout
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert any("exec" in call for call in recorded) == (mode == "host")
