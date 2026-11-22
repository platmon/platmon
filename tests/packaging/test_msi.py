"""scripts/packaging/package_msi.py: the installer's tables (on any platform) and, on Windows, a built package
read back through msi.dll."""
import ctypes
import os
import re
from pathlib import Path

import pytest

from scripts.packaging.package import version
from scripts.packaging.package_msi import COMPONENT_64, LEFT_OUT, NEVER_OVERWRITE, PERMANENT, SCHEMA, UPGRADE_CODE, \
    build_msi, idt, msi_version, tables
from scripts.packaging.package_windows import members

ROOT = Path(__file__).resolve().parents[2]
SERVER, COMMAND, TRAY = b"MZ fake server", b"MZ fake command", b"MZ fake tray"


def files():
    return members(ROOT, SERVER, COMMAND, TRAY, "1.2.3")


def test_every_table_fits_its_schema_and_its_references_resolve():
    t = tables(files(), "1.2.3")
    assert set(t) == set(SCHEMA)
    for name, rows in t.items():
        columns, keys = SCHEMA[name]
        assert all(len(r) == len(columns) for r in rows), name
        index = [i for i, (c, _) in enumerate(columns) if c in keys]
        assert len({tuple(r[i] for i in index) for r in rows}) == len(rows), f"{name}: a repeated key"
        for row in rows:  # a required column (lower-case type) is never empty
            assert all(v is not None for v, (_, kind) in zip(row, columns) if kind[0].islower()), (name, row)
    dirs = {r[0] for r in t["Directory"]}
    components = {r[0]: r for r in t["Component"]}
    assert all(r[1] in dirs for r in t["Directory"] if r[1]) and all(c[2] in dirs for c in components.values())
    assert {r[1] for r in t["File"]} == set(components) == {r[1] for r in t["FeatureComponents"]}
    assert all(components[r[1]][5] == r[0] for r in t["File"])  # each component's key path is its file
    for table in ("ServiceInstall", "ServiceControl", "MsiServiceConfig", "Registry", "Environment"):
        assert all(r[-1] in components or r[-2] in components for r in t[table]), table


def test_the_files_and_their_names():
    t = tables(files(), "1.2.3")
    installed = [p for p, _ in files() if p not in LEFT_OUT]
    assert [r[2].split("|")[1] for r in t["File"]] == [p.rpartition("/")[2] for p in installed]
    assert {"platmon-server.exe", "platmon.ini", "platmon.exe", "platmon-tray.exe", "LICENSE", "LICENSE-MIT",
            "web/index.html"} <= set(installed)
    assert not any(p.endswith((".py", ".cmd")) for p in installed)  # no Python command any more
    assert not set(installed) & LEFT_OUT  # the zip's own scripts and README are not installed
    shorts = [r[2].split("|")[0] for r in t["File"]]
    assert len(set(shorts)) == len(shorts) and all(re.fullmatch(r"[A-Z0-9]{1,8}(\.[A-Za-z0-9]{1,3})?", s) for s in shorts)
    assert [r[7] for r in t["File"]] == list(range(1, len(installed) + 1))
    assert t["Media"] == [(1, len(installed), None, "#platmon.cab", None, None)]
    assert all(r[3] & COMPONENT_64 for r in t["Component"])
    ini = next(r for r in t["Component"] if r[5] == "F_platmon_ini")
    assert ini[3] == COMPONENT_64 | PERMANENT | NEVER_OVERWRITE  # an edited platmon.ini is kept and left behind


def test_the_service_its_recovery_the_event_log_the_tray_and_path():
    t = tables(files(), "1.2.3")
    (svc,) = t["ServiceInstall"]
    assert svc[1:6] == ("platmon", "platmon", 16, 2, 1) and svc[8] is None  # own process, automatic, LocalSystem
    assert svc[10] == 'service "[INSTALLDIR]platmon.ini" --web "[INSTALLDIR]web"' and svc[11] == "C_platmon_server_exe"
    assert t["ServiceControl"] == [("platmon", "platmon", 1 | 2 | 32 | 128, None, 1, "C_platmon_server_exe")]
    # the restart actions: the guide's sc.exe line from System32, deferred and as LocalSystem, not on removal
    assert t["CustomAction"] == [("ConfigureRecovery", 34 | 1024 | 2048, "System64Folder",
                                  '"[System64Folder]sc.exe" failure platmon reset= 86400 actions= restart/5000/restart/5000/restart/60000')]
    assert ("System64Folder", "TARGETDIR", ".") in t["Directory"]
    seq = {r[0]: r for r in t["InstallExecuteSequence"]}
    assert seq["ConfigureRecovery"][1] == 'NOT REMOVE~="ALL"'
    assert seq["InstallServices"][2] < seq["ConfigureRecovery"][2] < seq["StartServices"][2]
    assert t["MsiServiceConfig"][0][2:5] == (1, 4, "1")  # the failure-actions flag
    reg, tray = t["Registry"]
    assert reg[1] == 2 and reg[2].endswith(r"EventLog\Application\platmon") and reg[4].startswith("#%%SystemRoot%\\")
    # the tray at every user's login: HKLM's Run value, with the tray's component (removed with it); no custom action
    assert tray == ("platmon_tray_run", 2, r"Software\Microsoft\Windows\CurrentVersion\Run", "platmon-tray",
                    '"[INSTALLDIR]platmon-tray.exe"', "C_platmon_tray_exe")
    assert any(r[0] == "C_platmon_tray_exe" and r[5] == "F_platmon_tray_exe" for r in t["Component"])
    assert [r[0] for r in t["CustomAction"]] == ["ConfigureRecovery"]
    assert t["Environment"] == [("platmon_path", "=-*PATH", "[~];[INSTALLDIR]", "C_platmon_server_exe")]


def test_versions_and_upgrades():
    a, b = tables(files(), "1.2.3"), tables(files(), "1.2.4")
    props = lambda t: dict(t["Property"])
    assert props(a)["UpgradeCode"] == props(b)["UpgradeCode"] == UPGRADE_CODE
    assert props(a)["ProductCode"] != props(b)["ProductCode"] and props(a)["ProductVersion"] == "1.2.3"
    assert [r[1] for r in a["Component"]] == [r[1] for r in b["Component"]]  # components keep their GUIDs
    assert a["Upgrade"] == [(UPGRADE_CODE, "0.0.0", "1.2.3", None, 256 | 512, None, "OLDERFOUND"),
                            (UPGRADE_CODE, "1.2.3", None, None, 2, None, "NEWERFOUND")]
    assert ("NOT NEWERFOUND", "A newer platmon is already installed.") in a["LaunchCondition"]
    seq = dict((r[0], r[2]) for r in a["InstallExecuteSequence"])
    assert seq["InstallInitialize"] < seq["RemoveExistingProducts"] < seq["InstallFiles"] < seq["InstallServices"] \
        < seq["MsiConfigureServices"] < seq["StartServices"] < seq["InstallFinalize"]
    assert msi_version(version()) == version()
    for bad in ("1.2", "256.0.0", "1.2.65536", "1.2.3-rc1"):
        with pytest.raises(ValueError):
            msi_version(bad)


def test_the_text_archive_format():
    text = idt("Property", [("ProductName", "platmon"), ("Empty", None)])
    assert text == "Property\tValue\r\ns72\tl0\r\nProperty\tProperty\r\nProductName\tplatmon\r\nEmpty\t\r\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows Installer is Windows's")
def test_a_built_package_reads_back(tmp_path):
    target = tmp_path / "platmon.msi"
    build_msi(target, files(), "1.2.3", "0" * 64)
    dll = ctypes.WinDLL("msi")

    def rows(sql):
        db, view, rec, out = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong(), []
        assert dll.MsiOpenDatabaseW(str(target), ctypes.c_wchar_p(0), ctypes.byref(db)) == 0
        assert dll.MsiDatabaseOpenViewW(db, sql, ctypes.byref(view)) == 0 and dll.MsiViewExecute(view, 0) == 0
        while dll.MsiViewFetch(view, ctypes.byref(rec)) == 0:
            fields = []
            for i in range(1, dll.MsiRecordGetFieldCount(rec) + 1):
                buf, size = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
                dll.MsiRecordGetStringW(rec, i, buf, ctypes.byref(size))
                fields.append(buf.value)
            out.append(fields)
            dll.MsiCloseHandle(rec)
        dll.MsiCloseHandle(view)
        dll.MsiCloseHandle(db)
        return out

    assert ["ProductVersion", "1.2.3"] in rows("SELECT `Property`, `Value` FROM `Property`")
    assert len(rows("SELECT `File` FROM `File`")) == len([p for p, _ in files() if p not in LEFT_OUT])
    assert ["platmon.cab"] in rows("SELECT `Name` FROM `_Streams`")
    assert rows("SELECT `Name`, `StartType` FROM `ServiceInstall`") == [["platmon", "2"]]
