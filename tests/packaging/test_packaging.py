"""Distribution and installation checks; no host installation or service changes."""
import os
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from scripts.packaging.package import build, packaging_epoch, version

ROOT = Path(__file__).resolve().parents[2]


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


@pytest.fixture
def source_install(tmp_path):
    """Run the real installer with relocated paths and fake service commands."""
    repo = tmp_path / "repo"
    scripts = repo / "deploy/systemd"
    scripts.mkdir(parents=True)
    prefix = tmp_path / "installed"
    prefix.mkdir()
    (prefix / "old-code").write_text("previous release")
    config = tmp_path / "etc/platmon.ini"
    config.parent.mkdir()
    config.write_text("custom config")
    unit = tmp_path / "etc/platmon.service"
    unit.write_text("old unit\n")
    (repo / "platmon.py").write_text("def load_config(path): return {}\n")
    for name in ("collector", "runtime", "frontends"):
        (repo / name).mkdir()
        (repo / name / "new-code").write_text("candidate")
    (repo / "deploy/systemd/platmon.service").write_text("new unit\n")
    installer = scripts / "install.sh"
    text = (ROOT / "deploy/systemd/install.sh").read_text()
    for old, new in (("prefix=/opt/platmon", f"prefix={prefix}"),
                     ("config=/etc/platmon/platmon.ini", f"config={config}"),
                     ("unit=/etc/systemd/system/platmon.service", f"unit={unit}")):
        assert old in text
        text = text.replace(old, new)
    installer.write_text(text)
    start = scripts / "start.sh"
    start.write_text('#!/bin/sh\n[ "${START_FAIL:-0}" = 0 ]\n')
    start.chmod(0o755)
    cli = repo / "scripts/install-cli.sh"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = {
        "id": "echo 0\n",
        "docker": "exit 1\n",
        "dpkg-query": "exit 1\n",
        "systemctl": 'echo "$*" >> "$SERVICE_CALLS"\n[ "$1" != is-active ]\n',
        "cp": '[ "${COPY_FAIL:-0}" = 0 ] || exit 1\nexec /bin/cp "$@"\n',
    }
    for name, body in commands.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
    env = dict(PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               PYTHONDONTWRITEBYTECODE="1", SERVICE_CALLS=str(tmp_path / "service-calls"))
    return installer, prefix, config, unit, env


@pytest.mark.parametrize("failure", ["COPY_FAIL", "START_FAIL"])
def test_source_update_failure_preserves_previous_install(source_install, failure):
    installer, prefix, config, unit, env = source_install
    result = run("bash", str(installer), env=dict(env, **{failure: "1"}))
    assert result.returncode != 0
    assert (prefix / "old-code").read_text() == "previous release"
    assert unit.read_text() == "old unit\n"
    assert config.read_text() == "custom config"


def test_source_update_success_retains_backup(source_install):
    installer, prefix, config, unit, env = source_install
    result = run("bash", str(installer), env=env)
    assert result.returncode == 0, result.stderr
    assert (prefix / "frontends/new-code").read_text() == "candidate"
    assert (prefix / "runtime/new-code").read_text() == "candidate"
    backups = list(prefix.parent.glob(".platmon-install.*/previous/old-code"))
    assert len(backups) == 1 and backups[0].read_text() == "previous release"
    assert unit.read_text() == "new unit\n"
    assert config.read_text() == "custom config"


@pytest.fixture
def package(tmp_path):
    if shutil.which("dpkg-deb") is None:
        pytest.skip("dpkg-deb is required for distribution tests")
    return build(tmp_path / "dist")


def test_release_versions_match_and_cli_stays_standalone(tmp_path):
    release = version()
    cli = tmp_path / "platmon"
    shutil.copyfile(ROOT / "frontends/cli.py", cli)
    for path, expected in ((cli, f"platmon {release}"),
                           (ROOT / "platmon.py", f"platmon server {release}")):
        result = run(sys.executable, str(path), "--version", cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


@pytest.mark.parametrize("value", ["1.0", "01.0.0", "1.0.0-rc.1", "not-a-version"])
def test_bad_version_refused(tmp_path, value):
    (tmp_path / "frontends").mkdir()
    (tmp_path / "frontends/cli.py").write_text(f"VERSION = {value!r}\n")
    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        version(tmp_path)


def test_package_contents_and_config_ownership(package, tmp_path):
    metadata = run("dpkg-deb", "-f", str(package))
    assert metadata.returncode == 0
    assert "Architecture: all" in metadata.stdout and "python3 (>= 3.9)" in metadata.stdout
    assert f"Version: {version()}-1" in metadata.stdout
    root = tmp_path / "extracted"
    assert run("dpkg-deb", "-R", str(package), str(root)).returncode == 0
    assert (root / "DEBIAN/conffiles").read_text() == "/etc/platmon/platmon.ini\n"
    unit = (root / "usr/lib/systemd/system/platmon.service").read_text()
    assert "/usr/share/platmon/platmon.py" in unit and "DynamicUser=yes" in unit
    assert (root / "usr/share/platmon/frontends/web/assets/brand/favicon.svg").is_file()
    assert not (root / "usr/local").exists()
    assert not list(root.rglob("*.pyc"))
    assert os.access(root / "usr/bin/platmon", os.X_OK)
    for name in ("preinst", "postinst", "prerm", "postrm"):
        assert run("sh", "-n", str(root / "DEBIAN" / name)).returncode == 0
    doc = root / "usr/share/doc/platmon"
    changelog = gzip.decompress((doc / "changelog.Debian.gz").read_bytes()).decode()
    assert changelog.startswith(f"platmon ({version()}-1) unstable;")
    assert gzip.decompress((doc / "changelog.gz").read_bytes()) == (ROOT / "CHANGELOG.md").read_bytes()
    assert "/usr/share/common-licenses/Apache-2.0" in (doc / "copyright").read_text()
    assert gzip.decompress((root / "usr/share/man/man1/platmon.1.gz").read_bytes()).startswith(b".TH PLATMON 1")
    checksums = {}
    for line in (root / "DEBIAN/md5sums").read_text().splitlines():
        digest, relative = line.split("  ")
        assert hashlib.md5((root / relative).read_bytes(), usedforsecurity=False).hexdigest() == digest
        checksums[relative] = digest
    expected = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
                and root / "DEBIAN" not in p.parents and p != root / "etc/platmon/platmon.ini"}
    assert set(checksums) == expected


def test_package_smoke_outside_checkout(package, tmp_path):
    result = run(sys.executable, str(ROOT / "scripts/packaging/check_package.py"), str(package), cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "package smoke passed" in result.stdout


def test_checksums_and_repeated_builds_match(package, tmp_path):
    output = package.parent
    manifest = json.loads((output / "BUILD.json").read_text())
    assert manifest["version"] == version() and manifest["package_version"] == f"{version()}-1"
    assert manifest["source_date_epoch"] == packaging_epoch() > 0
    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    second = build(tmp_path / "repeat")
    for name in (package.name, f"platmon-{version()}.tar.gz", "BUILD.json", "SHA256SUMS"):
        assert (output / name).read_bytes() == (second.parent / name).read_bytes(), name
    with pytest.raises(ValueError, match="already exist"):
        build(output)


def test_source_archive_rebuilds_without_git(package, tmp_path):
    source = package.parent / f"platmon-{version()}.tar.gz"
    with tarfile.open(source) as archive:
        assert all(m.isfile() and ".." not in Path(m.name).parts for m in archive.getmembers())
        assert not any(".validation" in m.name or "__pycache__" in m.name for m in archive.getmembers())
        # Extract only these verified regular files; works on every supported Python.
        for member in archive.getmembers():
            assert not Path(member.name).is_absolute()
            target = tmp_path / "source" / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(member.mode)
    root = tmp_path / "source" / f"platmon-{version()}"
    assert not (root / ".git").exists()
    result = run(sys.executable, str(root / "scripts/packaging/package.py"), "--output", str(tmp_path / "rebuilt"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "rebuilt" / package.name).read_bytes() == package.read_bytes()
    # The archive carries benchmark tests, so their implementations must also be available
    # without accidentally importing them from this checkout.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = run(sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/benchmarks",
                 cwd=root, env=env)
    assert result.returncode == 0, result.stdout + result.stderr


def test_revision_and_epoch_override_reach_package_metadata(tmp_path):
    epoch = 1780000000
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
    result = run(sys.executable, str(ROOT / "scripts/packaging/package.py"),
                 "--revision", "2", "--output", str(tmp_path / "dist"), env=env)
    assert result.returncode == 0, result.stderr
    package = tmp_path / "dist" / f"platmon_{version()}-2_all.deb"
    root = tmp_path / "extracted"
    assert run("dpkg-deb", "-R", str(package), str(root)).returncode == 0
    manifest = json.loads((package.parent / "BUILD.json").read_text())
    assert manifest["source_date_epoch"] == epoch
    changelog = root / "usr/share/doc/platmon/changelog.Debian.gz"
    assert gzip.decompress(changelog.read_bytes()).decode().startswith(f"platmon ({version()}-2)")
    assert int(changelog.stat().st_mtime) == epoch


@pytest.mark.parametrize("revision", ["0", "01", "-1", "1/2", "x"])
def test_invalid_packaging_revision_refused(tmp_path, revision):
    with pytest.raises(ValueError, match="positive integer"):
        build(tmp_path / "dist", revision)


def test_lifecycle_inside_mode_refuses_host():
    result = run(sys.executable, str(ROOT / "scripts/packaging/check_package_install.py"),
                 "missing.deb", "missing-upgrade.deb", "--inside",
                 env=dict(PATH=os.environ["PATH"], PYTHONDONTWRITEBYTECODE="1"))
    assert result.returncode != 0
    assert "requires the disposable package test container" in result.stderr


def test_source_installer_refuses_dpkg_owned_install(source_install):
    installer, prefix, config, unit, env = source_install
    command = Path(env["PATH"].split(os.pathsep)[0]) / "dpkg-query"
    command.write_text("#!/bin/sh\necho installed\n")
    result = run("bash", str(installer), env=env)
    assert result.returncode != 0 and "managed by dpkg" in result.stderr
    assert (prefix / "old-code").read_text() == "previous release"
    assert unit.read_text() == "old unit\n"


def test_source_update_validates_before_changing_files(source_install):
    installer, prefix, config, unit, env = source_install
    repo = installer.parent.parent.parent
    (repo / "platmon.py").write_text("def load_config(path): raise ValueError('invalid config')\n")
    result = run("bash", str(installer), env=env)
    assert result.returncode != 0 and "invalid config" in result.stderr
    assert (prefix / "old-code").read_text() == "previous release"
    assert unit.read_text() == "old unit\n"
    assert config.read_text() == "custom config"


@pytest.mark.parametrize("target", ["ancestor", "tip", "unmerged"])
def test_release_main_guard_with_real_git_history(tmp_path, target):
    """Run the workflow's actual guard against isolated Git history, without publishing tags."""
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"

    def git(*args, cwd=remote, **kwargs):
        result = run("git", *args, cwd=cwd, **kwargs)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    git("init", "--bare", str(remote), cwd=tmp_path)
    tree = git("hash-object", "-w", "-t", "tree", "--stdin", input="")

    def commit(message, parent=None):
        # Write fixture objects directly; this never invokes signing or repository hooks.
        body = f"tree {tree}\n" + (f"parent {parent}\n" if parent else "")
        body += "author Fixture <fixture@example.com> 1 +0000\n"
        body += "committer Fixture <fixture@example.com> 1 +0000\n\n" + message + "\n"
        return git("hash-object", "-w", "-t", "commit", "--stdin", input=body)

    ancestor = commit("accepted base")
    tip = commit("main tip", ancestor)
    unmerged = commit("unmerged candidate", ancestor)
    git("update-ref", "refs/heads/main", tip)
    git("update-ref", "refs/heads/candidate", unmerged)
    git("clone", "--no-checkout", str(remote), str(checkout), cwd=tmp_path)
    git("checkout", "--detach", {"ancestor": ancestor, "tip": tip, "unmerged": unmerged}[target], cwd=checkout)
    # An outdated local main ref must be refreshed from the remote by the guard.
    git("update-ref", "refs/remotes/origin/main", ancestor, cwd=checkout)
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    block = workflow.split("      - name: Verify tag commit is on main\n        run: |\n", 1)[1]
    guard = textwrap.dedent(block.split("\n\n", 1)[0])
    result = run("bash", "-e", "-c", guard, cwd=checkout)
    assert (result.returncode == 0) == (target != "unmerged"), result.stdout + result.stderr
    assert git("rev-parse", "refs/remotes/origin/main", cwd=checkout) == tip
    if target == "unmerged":
        assert "release tag must point to a commit included in main" in result.stdout
