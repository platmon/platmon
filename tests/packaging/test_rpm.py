"""The .rpm builder (scripts/packaging/package_rpm.py); no host installation or service changes.

The builds need the host's rpmbuild and skip without it; CI's rpm job installs it. The lifecycle under
systemd is scripts/packaging/check_rpm.py's, in a container.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.packaging.package import (command_beside, default_binary, packaging_epoch, stage_frontend, stage_server,
                                     version)
from scripts.packaging.package_rpm import ARCHES, build
from tests.packaging.test_packaging import fake_build, fake_server

ROOT = Path(__file__).resolve().parents[2]
SPECS = ("platmon", "platmon-frontend")


def run(*args):
    return subprocess.run(args, text=True, capture_output=True, check=True).stdout


def query(rpm, fmt):
    return run("rpm", "-qp", "--qf", fmt, str(rpm))


@pytest.fixture
def binaries(tmp_path):
    """The binaries native/build.sh left, else fakes (enough to package, not to run)."""
    real = {arch: default_binary(arch) for arch in ARCHES}
    if all(path.is_file() and command_beside(path).is_file() for path in real.values()):
        return real
    return {arch: fake_build(tmp_path / "bin" / arch, arch) for arch in ARCHES}


@pytest.fixture
def rpms(tmp_path, binaries):
    if shutil.which("rpmbuild") is None:
        pytest.skip("rpmbuild is required for the .rpm builds")
    return build(tmp_path / "dist", binaries=binaries)


def scriptlets(name):
    """{section: body} of the spec's shell scriptlets."""
    spec = (ROOT / "packaging/rpm" / f"{name}.spec").read_text(encoding="utf-8")
    parts = re.split(r"^(%(?:pre|post|preun|postun|posttrans))\n", spec, flags=re.M)
    return {parts[i]: re.split(r"^%(?:files|changelog|pre|post|preun|postun|posttrans)\b", parts[i + 1], flags=re.M)[0]
            for i in range(1, len(parts), 2)}


@pytest.mark.parametrize("name", SPECS)
def test_scriptlets_are_valid_sh(name):
    bodies = scriptlets(name)
    assert bodies, name
    for section, body in bodies.items():
        assert subprocess.run(["sh", "-n", "-c", body]).returncode == 0, section


def test_server_scriptlets_check_before_starting_and_keep_the_choice():
    bodies = scriptlets("platmon")
    assert set(bodies) == {"%pre", "%post", "%preun", "%postun", "%posttrans"}
    assert "local platmon.service exists" in bodies["%pre"]
    assert 'if [ "$1" -eq 1 ]' in bodies["%post"] and "enable platmon.service" in bodies["%post"]
    assert 'if [ "$1" -eq 0 ]' in bodies["%preun"] and "disable --now" in bodies["%preun"]
    posttrans = bodies["%posttrans"]
    assert (posttrans.index("platmon-server config-check") < posttrans.index("systemctl restart platmon.service")
            < posttrans.index("platmon-server health"))
    # The messages are the .deb's, which the documentation and check_rpm.py name.
    deb = (ROOT / "packaging/debian/postinst").read_text(encoding="utf-8")
    for message in ("invalid platmon config: ", "health check skipped", "platmon.service failed its health check: "):
        assert message in posttrans and message in deb, message


@pytest.mark.parametrize("name", SPECS)
def test_spec_files_are_the_staged_ones(tmp_path, binaries, name):
    """%files lists what stage_server() or stage_frontend() and docs() put in the package, nothing else."""
    from scripts.packaging.package_rpm import docs
    stage = tmp_path / "stage"
    if name == "platmon":
        stage_server(ROOT, stage, binaries["amd64"].read_bytes(), version())
        docs(ROOT, stage, "platmon", (ROOT / "docs/reference/packaging.md",))
    else:
        stage_frontend(ROOT, stage, command_beside(binaries["amd64"]).read_bytes(), packaging_epoch())
        docs(ROOT, stage, name)
    spec = (ROOT / f"packaging/rpm/{name}.spec").read_text(encoding="utf-8")
    listed = [line.split()[-1] for line in spec.split("%files\n", 1)[1].split("\n%changelog", 1)[0].splitlines()
              if line.strip()]
    staged = {"/" + p.relative_to(stage).as_posix() for p in stage.rglob("*") if not p.is_dir() or p.is_symlink()}
    for path in staged:
        assert any(path == entry or path.startswith(entry + "/") for entry in listed), path
    assert all(any(path == entry or path.startswith(entry + "/") for path in staged) for entry in listed), listed


def test_architectures_and_release_refused(tmp_path, binaries):
    with pytest.raises(ValueError, match="architectures must be among: amd64, arm64"):
        build(tmp_path / "armhf", binaries={"armhf": binaries["amd64"]})
    for release in ("0", "1.1", "x", ""):
        with pytest.raises(ValueError, match="release must be a positive integer"):
            build(tmp_path / "release", release, binaries=binaries)
    bad = fake_server(tmp_path / "bad", "amd64", mark=b"other")
    with pytest.raises(ValueError, match="system allocator"):
        build(tmp_path / "mark", binaries={"amd64": bad})


def test_package_metadata_and_contents(rpms, binaries):
    assert [p.name for p in rpms] == [f"platmon-{version()}-1.x86_64.rpm", f"platmon-{version()}-1.aarch64.rpm",
                                      f"platmon-frontend-{version()}-1.x86_64.rpm",
                                      f"platmon-frontend-{version()}-1.aarch64.rpm"]
    for rpm, arch in zip(rpms[:2], ("x86_64", "aarch64")):
        assert query(rpm, "%{NAME} %{VERSION} %{RELEASE} %{ARCH} %{BUILDTIME}") == \
            f"platmon {version()} 1 {arch} {packaging_epoch()}"
        assert query(rpm, "%{LICENSE}") == "Apache-2.0 AND (MIT OR Apache-2.0) AND MIT"
        assert "systemd" in run("rpm", "-qpR", str(rpm)).split("\n")
        assert run("rpm", "-qp", "--recommends", str(rpm)).split() == ["platmon-frontend"]
        files = dict(line.split(" ", 1) for line in query(rpm, "[%{FILENAMES} %{FILEFLAGS:fflags}\n]").splitlines())
        assert files["/etc/platmon/platmon.ini"] == "cn"  # %config(noreplace)
        assert "/usr/bin/platmon" not in files and not any(path.endswith(".py") for path in files)
        assert not any(path.startswith("/usr/share/platmon") for path in files)  # the page is platmon-frontend's
        links = dict(line.split(" ", 1) for line in query(rpm, "[%{FILENAMES} %{FILELINKTOS}\n]").splitlines())
        assert links["/usr/lib/platmon/platmon-native"] == "platmon-server"  # the name up to 1.6.0
        server = [line for line in query(rpm, "[%{FILENAMES} %{FILEMODES:perms}\n]").splitlines()
                  if line.startswith("/usr/lib/platmon/platmon-server ")]
        assert server == ["/usr/lib/platmon/platmon-server -rwxr-xr-x"]
    for cli, arch in zip(rpms[2:], ("x86_64", "aarch64")):
        assert query(cli, "%{NAME} %{ARCH}") == f"platmon-frontend {arch}"
        # platmon-cli up to this release: replaced on upgrade, and still what platmon-cli asks for
        assert run("rpm", "-qp", "--obsoletes", str(cli)).split("\n")[0] == f"platmon-cli < {version()}"
        assert f"platmon-cli = {version()}-1" in run("rpm", "-qp", "--provides", str(cli)).split("\n")
        assert query(cli, "%{LICENSE}") == "Apache-2.0 AND (MIT OR Apache-2.0) AND MIT"
        assert "python" not in run("rpm", "-qpR", str(cli))  # one static binary
        listed = sorted(run("rpm", "-qpl", str(cli)).split())
        assert [p for p in listed if not p.startswith("/usr/share/platmon")] == [
            "/usr/bin/platmon", "/usr/share/doc/platmon-frontend", "/usr/share/doc/platmon-frontend/CHANGELOG.md",
            "/usr/share/doc/platmon-frontend/README.md", "/usr/share/licenses/platmon-frontend",
            "/usr/share/licenses/platmon-frontend/LICENSE", "/usr/share/licenses/platmon-frontend/copyright",
            "/usr/share/man/man1/platmon.1.gz"]
        assert {"/usr/share/platmon/frontends/web/index.html", "/usr/share/platmon/frontends/web/app.js",
                "/usr/share/platmon/frontends/web/assets/brand/favicon.svg"} <= set(listed)
        command = [line for line in query(cli, "[%{FILENAMES} %{FILEMODES:perms}\n]").splitlines()
                   if line.startswith("/usr/bin/platmon ")]
        assert command == ["/usr/bin/platmon -rwxr-xr-x"]


def test_checksums_and_repeated_builds_match(rpms, binaries, tmp_path):
    output = rpms[0].parent
    second = build(tmp_path / "repeat", binaries=binaries)
    for name in (*(p.name for p in rpms), "SHA256SUMS"):
        assert (output / name).read_bytes() == (second[0].parent / name).read_bytes(), name
    with pytest.raises(ValueError, match="already exist"):
        build(output, binaries=binaries)
