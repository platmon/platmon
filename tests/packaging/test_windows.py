"""The experimental Windows zip (scripts/packaging/package_windows.py); no Windows needed."""
import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from scripts.packaging.package import ALLOCATOR_MARK, packaging_epoch, version
from scripts.packaging.package_windows import SCRIPTS, build, check_binary, check_changelog, default_binary, imported_dlls

ROOT = Path(__file__).resolve().parents[2]


def fake_exe(path, machine=0x8664, mark=ALLOCATOR_MARK, dlls=("KERNEL32.dll", "ntdll.dll")):
    """What the builder checks: a DOS stub pointing at a PE32+ header for the machine, one section holding the
    import table of `dlls` (RVA 0x1000 at file offset 0x200), and the allocator mark."""
    u16, u32 = (lambda v: v.to_bytes(2, "little")), (lambda v: v.to_bytes(4, "little"))
    names = b"".join(d.encode() + b"\0" for d in dlls)
    table_size = 20 * (len(dlls) + 1)
    starts = [0x1000 + table_size + sum(len(d) + 1 for d in dlls[:i]) for i in range(len(dlls))]
    imports = b"".join(bytes(12) + u32(s) + bytes(4) for s in starts) + bytes(20) + names
    optional = bytearray(240)
    optional[0:2], optional[120:128] = u16(0x20B), u32(0x1000) + u32(table_size) if dlls else bytes(8)
    section = b".idata\0\0" + u32(len(imports)) + u32(0x1000) + u32(len(imports)) + u32(0x200) + bytes(16)
    head = (b"MZ" + bytes(58) + u32(64) + b"PE\0\0" + u16(machine) + u16(1) + bytes(12) + u16(len(optional)) + u16(0x22)
            + bytes(optional) + section)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + bytes(0x200 - len(head)) + imports + mark)
    return path


@pytest.fixture
def exe(tmp_path):
    real = default_binary()
    return real if real.is_file() else fake_exe(tmp_path / "bin/platmon-server.exe")


def output_with_sums(path):
    """package.py's output as the zip builder finds it: BUILD.json, and SHA256SUMS with its line."""
    path.mkdir(parents=True)
    (path / "BUILD.json").write_text('{"version": "x"}\n', encoding="utf-8")
    build_sum = hashlib.sha256((path / "BUILD.json").read_bytes()).hexdigest()
    (path / "SHA256SUMS").write_text("0" * 64 + f"  platmon-x.tar.gz\n{build_sum}  BUILD.json\n", encoding="utf-8")
    return path


def test_zip_contents_and_checksum(tmp_path, exe):
    out = output_with_sums(tmp_path / "dist")
    target = build(out, exe)
    release = version()
    assert target.name == f"platmon-{release}-windows-x86_64-experimental.zip"
    top = f"platmon-{release}-windows-x86_64/"
    with zipfile.ZipFile(target) as z:
        names = z.namelist()
        assert names[:8] == [top + n for n in ("platmon-server.exe", "platmon.ini", "LICENSE", "LICENSE-MIT", "README.txt")
                             + SCRIPTS]
        assert top + "web/index.html" in names and all(n.startswith(top) for n in names)
        assert z.read(top + "platmon-server.exe") == exe.read_bytes()
        ini = z.read(top + "platmon.ini").decode("utf-8")
        assert "\r\nbind = 127.0.0.1\r\n" in ini and "0.0.0.0" not in ini.split("[http]", 1)[1].split("[", 1)[0]
        readme = z.read(top + "README.txt").decode("utf-8")
        assert f"platmon {release} for Windows" in readme and "EXPERIMENTAL" in readme and "vc_redist" not in readme
        # the service runs as LocalSystem: its files only where administrators alone can write
        assert "\\Program Files\\platmon\\platmon-server.exe" in readme and "Windows 10 was not checked" in readme
        assert all(name in readme for name in SCRIPTS)
        for name in SCRIPTS:  # cmd misreads labels and goto in a batch file with LF line ends
            script = z.read(top + name)
            assert script.count(b"\r\n") == script.count(b"\n") > 0 and script.startswith(b"@echo off\r\n")
            # findstr, never find: Git for Windows' Unix tools on PATH shadow find.exe (CI's bash steps did)
            assert not re.search(rb"(^|[|&(\s])find\s", script), name
        assert all(i.date_time[0] >= 1980 and i.create_system == 0 for i in z.infolist())
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    assert json.loads((out / "BUILD.json").read_text(encoding="utf-8")) == \
        {"version": "x", "windows_server_sha256": {"x86_64": sha(exe)}}
    # BUILD.json changed: its checksum line follows, and the zip's is added
    assert (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines() == \
        ["0" * 64 + "  platmon-x.tar.gz", f"{sha(out / 'BUILD.json')}  BUILD.json", f"{sha(target)}  {target.name}"]


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
    # the C runtime linked dynamically: the zip would need the Visual C++ Redistributable
    with pytest.raises(ValueError, match=r"Visual C\+\+ runtime \(vcruntime140\.dll, api-ms-win-crt-heap-l1-1-0\.dll\)"):
        check_binary(fake_exe(tmp_path / "dynamic.exe", dlls=("KERNEL32.dll", "VCRUNTIME140.dll", "api-ms-win-crt-heap-l1-1-0.dll")))
    with pytest.raises(ValueError, match="no import table"):
        check_binary(fake_exe(tmp_path / "bare.exe", dlls=()))
    with pytest.raises(ValueError, match="not found"):
        build(out, tmp_path / "missing.exe")
    with pytest.raises(ValueError, match="run package.py"):
        build(tmp_path / "empty", fake_exe(tmp_path / "ok.exe"))
    build(out, tmp_path / "ok.exe")
    with pytest.raises(ValueError, match="already exists"):
        build(out, tmp_path / "ok.exe")


def test_the_changelog_must_call_the_zip_experimental():
    """The release workflow refuses a release whose entry does not (the owner's decision)."""
    head = "# Changelog\n\n## 2.0.0 — 2026-11-10\n\n"
    check_changelog(head + "- A Windows x86_64 zip, experimental: one laptop only.\n- rpm.\n", "2.0.0")
    check_changelog(head + "- rpm.\n- The zip for\n  Windows is **experimental**.\n\n## 1.0.0 — x\n", "2.0.0")
    for text, message in ((head + "- A Windows zip.\n- An experimental flag.\n", "must call"),
                          (head + "- rpm.\n\n## 1.9.0 — x\n- Windows, experimental.\n", "must call"),
                          (head, "must call"), ("# Changelog\n", "no changelog entry")):
        with pytest.raises(ValueError, match=message):
            check_changelog(text, "2.0.0")


def test_imported_dlls_reads_the_import_table(tmp_path):
    assert imported_dlls(fake_exe(tmp_path / "a.exe", dlls=("KERNEL32.dll", "WS2_32.dll", "bcrypt.dll")).read_bytes()) == \
        ["kernel32.dll", "ws2_32.dll", "bcrypt.dll"]
    body = bytearray(fake_exe(tmp_path / "b.exe").read_bytes())
    body[88:90] = (0x10B).to_bytes(2, "little")  # PE32, not PE32+
    assert imported_dlls(bytes(body)) is None
