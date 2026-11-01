"""The experimental Windows zip (scripts/packaging/package_windows.py); no Windows needed."""
import hashlib
import zipfile
from pathlib import Path

import pytest

from scripts.packaging.package import ALLOCATOR_MARK, packaging_epoch, version
from scripts.packaging.package_windows import build, check_binary, default_binary

ROOT = Path(__file__).resolve().parents[2]


def fake_exe(path, machine=0x8664, mark=ALLOCATOR_MARK):
    """A DOS stub pointing at a PE header for the machine, and the allocator mark: what the builder checks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + bytes(58) + (64).to_bytes(4, "little") + b"PE\0\0" + machine.to_bytes(2, "little")
                     + bytes(18) + mark)
    return path


@pytest.fixture
def exe(tmp_path):
    real = default_binary()
    return real if real.is_file() else fake_exe(tmp_path / "bin/platmon-server.exe")


def output_with_sums(path):
    path.mkdir(parents=True)
    (path / "SHA256SUMS").write_text("0" * 64 + "  platmon-x.tar.gz\n", encoding="utf-8")
    return path


def test_zip_contents_and_checksum(tmp_path, exe):
    out = output_with_sums(tmp_path / "dist")
    target = build(out, exe)
    release = version()
    assert target.name == f"platmon-{release}-windows-x86_64-experimental.zip"
    top = f"platmon-{release}-windows-x86_64/"
    with zipfile.ZipFile(target) as z:
        names = z.namelist()
        assert names[:5] == [top + n for n in ("platmon-server.exe", "platmon.ini", "LICENSE", "LICENSE-MIT", "README.txt")]
        assert top + "web/index.html" in names and all(n.startswith(top) for n in names)
        assert z.read(top + "platmon-server.exe") == exe.read_bytes()
        ini = z.read(top + "platmon.ini").decode("utf-8")
        assert "\r\nbind = 127.0.0.1\r\n" in ini and "0.0.0.0" not in ini.split("[http]", 1)[1].split("[", 1)[0]
        readme = z.read(top + "README.txt").decode("utf-8")
        assert f"platmon {release} for Windows" in readme and "EXPERIMENTAL" in readme and "vc_redist" in readme
        # the service runs as LocalSystem: its files only where administrators alone can write
        assert "\\Program Files\\platmon\\platmon-server.exe" in readme and "Windows 10 was not checked" in readme
        assert all(i.date_time[0] >= 1980 and i.create_system == 0 for i in z.infolist())
    lines = (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[1] == f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {target.name}"


def test_repeated_builds_match(tmp_path, exe):
    first = build(output_with_sums(tmp_path / "a"), exe)
    second = build(output_with_sums(tmp_path / "b"), exe, epoch=packaging_epoch())
    assert first.read_bytes() == second.read_bytes()


def test_refusals(tmp_path):
    out = output_with_sums(tmp_path / "dist")
    with pytest.raises(ValueError, match="not an x86_64"):
        build(out, fake_exe(tmp_path / "arm64.exe", machine=0xAA64))
    with pytest.raises(ValueError, match="allocator"):
        check_binary(fake_exe(tmp_path / "mimalloc.exe", mark=b"other"))
    with pytest.raises(ValueError, match="not found"):
        build(out, tmp_path / "missing.exe")
    with pytest.raises(ValueError, match="run package.py"):
        build(tmp_path / "empty", fake_exe(tmp_path / "ok.exe"))
    build(out, tmp_path / "ok.exe")
    with pytest.raises(ValueError, match="already exists"):
        build(out, tmp_path / "ok.exe")
