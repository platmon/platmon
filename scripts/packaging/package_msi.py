#!/usr/bin/env python3
"""Build the Windows installer, platmon-VERSION-windows-x86_64.msi, with Windows's own Windows Installer
(msi.dll) and makecab.exe through ctypes: no WiX or other third-party tool (WiX is MS-RL, outside the
dependency rules; docs/development/remaining-work.md, "Windows: promotion"). Windows only, after the server
is built; tables() is plain data and is tested on any platform.

  package_msi.py --binary platmon-server.exe --output DIR [--version X.Y.Z]   (platmon.exe, platmon-tray.exe beside)

What it installs, with MSI's own tables and no custom action DLL: the zip's server, platmon.ini (listening on
127.0.0.1), the web page, the licenses, the platmon command (platmon.exe) and the tray (platmon-tray.exe) in
C:\\Program Files\\platmon; the service platmon (LocalSystem, automatic) with the guide's recovery actions,
started at the end; the event log source and the tray at every user's login (HKLM Run, Registry); the folder on
the system PATH (Environment). The
failure-actions flag is MsiServiceConfig's; the restart actions are the guide's sc.exe failure line, run by
one deferred action from System32 (MsiServiceConfigFailureActions fails with access denied for restart
actions: Windows Installer opens the service without SERVICE_START, which ChangeServiceConfig2 needs for them). An edited platmon.ini is kept on upgrade and
left on removal. Each version is a major upgrade of the earlier ones (the same UpgradeCode); a newer one
installed refuses an older package."""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

if __package__:
    from .package import ROOT, version as release_version
    from .package_windows import check_binary, command_beside, members, tray_beside
else:  # run as a script, from its own directory
    from package import ROOT, version as release_version
    from package_windows import check_binary, command_beside, members, tray_beside

UPGRADE_CODE = "{7C4C5D2B-6F1E-4F5A-9E0D-3B1A2C8D9E4F}"  # never changes: it ties every version together
NAMESPACE = uuid.UUID(UPGRADE_CODE.strip("{}"))
LEFT_OUT = {"README.txt", "start.cmd", "install-service.cmd", "uninstall-service.cmd"}  # the zip's own
EVENT_LOG_KEY = r"SYSTEM\CurrentControlSet\Services\EventLog\Application\platmon"
MESSAGE_FILE = r"%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\EventLogMessages.dll"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"  # every user's login; a user's own is under HKCU
# component attributes: 64-bit; platmon.ini also permanent (left on removal) and never overwritten
COMPONENT_64, PERMANENT, NEVER_OVERWRITE = 256, 16, 128
SCHEMA = {  # table: (columns as IDT types, primary key columns); the Windows Installer SDK's standard tables
    "Property": ([("Property", "s72"), ("Value", "l0")], ["Property"]),
    "Directory": ([("Directory", "s72"), ("Directory_Parent", "S72"), ("DefaultDir", "l255")], ["Directory"]),
    "Component": ([("Component", "s72"), ("ComponentId", "S38"), ("Directory_", "s72"), ("Attributes", "i2"),
                   ("Condition", "S255"), ("KeyPath", "S72")], ["Component"]),
    "File": ([("File", "s72"), ("Component_", "s72"), ("FileName", "l255"), ("FileSize", "i4"), ("Version", "S72"),
              ("Language", "S20"), ("Attributes", "I2"), ("Sequence", "i2")], ["File"]),
    "Media": ([("DiskId", "i2"), ("LastSequence", "i2"), ("DiskPrompt", "L64"), ("Cabinet", "S255"),
               ("VolumeLabel", "S32"), ("Source", "S72")], ["DiskId"]),
    "Feature": ([("Feature", "s38"), ("Feature_Parent", "S38"), ("Title", "L64"), ("Description", "L255"),
                 ("Display", "I2"), ("Level", "i2"), ("Directory_", "S72"), ("Attributes", "i2")], ["Feature"]),
    "FeatureComponents": ([("Feature_", "s38"), ("Component_", "s72")], ["Feature_", "Component_"]),
    "ServiceInstall": ([("ServiceInstall", "s72"), ("Name", "s255"), ("DisplayName", "L255"), ("ServiceType", "i4"),
                        ("StartType", "i4"), ("ErrorControl", "i4"), ("LoadOrderGroup", "S255"), ("Dependencies", "S255"),
                        ("StartName", "S255"), ("Password", "S255"), ("Arguments", "S255"), ("Component_", "s72"),
                        ("Description", "L255")], ["ServiceInstall"]),
    "ServiceControl": ([("ServiceControl", "s72"), ("Name", "l255"), ("Event", "i2"), ("Arguments", "L255"),
                        ("Wait", "I2"), ("Component_", "s72")], ["ServiceControl"]),
    "MsiServiceConfig": ([("MsiServiceConfig", "s72"), ("Name", "l255"), ("Event", "i2"), ("ConfigType", "i4"),
                          ("Argument", "L0"), ("Component_", "s72")], ["MsiServiceConfig"]),
    "CustomAction": ([("Action", "s72"), ("Type", "i2"), ("Source", "S72"), ("Target", "S255")], ["Action"]),
    "Registry": ([("Registry", "s72"), ("Root", "i2"), ("Key", "l255"), ("Name", "L255"), ("Value", "L0"),
                  ("Component_", "s72")], ["Registry"]),
    "Environment": ([("Environment", "s72"), ("Name", "l255"), ("Value", "L255"), ("Component_", "s72")], ["Environment"]),
    "Upgrade": ([("UpgradeCode", "s38"), ("VersionMin", "S20"), ("VersionMax", "S20"), ("Language", "S255"),
                 ("Attributes", "i4"), ("Remove", "S255"), ("ActionProperty", "s72")],
                ["UpgradeCode", "VersionMin", "VersionMax", "Language", "Attributes"]),
    "LaunchCondition": ([("Condition", "s255"), ("Description", "l255")], ["Condition"]),
    "InstallExecuteSequence": ([("Action", "s72"), ("Condition", "S255"), ("Sequence", "I2")], ["Action"]),
    "InstallUISequence": ([("Action", "s72"), ("Condition", "S255"), ("Sequence", "I2")], ["Action"]),
}
# the standard actions and their usual numbers; the old product is removed before anything is installed
EXECUTE = [("FindRelatedProducts", 25), ("LaunchConditions", 100), ("CostInitialize", 800), ("FileCost", 900),
           ("CostFinalize", 1000), ("InstallValidate", 1400), ("InstallInitialize", 1500), ("RemoveExistingProducts", 1501),
           ("ProcessComponents", 1600), ("StopServices", 1900), ("DeleteServices", 2000), ("RemoveRegistryValues", 2600),
           ("RemoveEnvironmentStrings", 3300), ("RemoveFiles", 3500), ("InstallFiles", 4000), ("WriteRegistryValues", 5000),
           ("WriteEnvironmentStrings", 5200), ("InstallServices", 5800), ("MsiConfigureServices", 5850),
           ("ConfigureRecovery", 5860), ("StartServices", 5900), ("RegisterProduct", 6100), ("PublishFeatures", 6300), ("PublishProduct", 6400),
           ("InstallFinalize", 6600)]
UI = [("FindRelatedProducts", 25), ("LaunchConditions", 100), ("CostInitialize", 800), ("FileCost", 900),
      ("CostFinalize", 1000), ("ExecuteAction", 1300)]


def msi_version(release):
    """MSI's ProductVersion: three numbers, the first two up to 255, the third up to 65535."""
    parts = release.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts) or int(parts[0]) > 255 or int(parts[1]) > 255 \
            or int(parts[2]) > 65535:
        raise ValueError(f"{release!r} is not an MSI ProductVersion (A.B.C, A and B up to 255, C up to 65535)")
    return release


def guid(*names):
    """A GUID that is the same for the same names: components keep theirs across versions."""
    return "{" + str(uuid.uuid5(NAMESPACE, "/".join(names))).upper() + "}"


def ident(prefix, path):
    """An MSI identifier for a path: letters, digits, underscores and dots, starting with a letter."""
    return prefix + "_" + "".join(c if c.isalnum() else "_" for c in path)


def tables(files, release):
    """{table: [rows]} for files [(path in the folder, body)], as Windows Installer reads them; the cabinet
    holds each file under its File key, in Sequence order."""
    release = msi_version(release)
    product = guid("product", release)
    dirs = {"": "INSTALLDIR"}
    t = {name: [] for name in SCHEMA}
    t["Directory"] += [("TARGETDIR", None, "SourceDir"), ("ProgramFiles64Folder", "TARGETDIR", "."),
                       ("INSTALLDIR", "ProgramFiles64Folder", "platmon"), ("System64Folder", "TARGETDIR", ".")]
    for n, (path, body) in enumerate(f for f in files if f[0] not in LEFT_OUT):
        folder, _, name = path.rpartition("/")
        parent = ""
        for part in folder.split("/") if folder else []:  # web, web/assets, web/assets/brand
            here = f"{parent}/{part}".lstrip("/")
            if here not in dirs:
                dirs[here] = ident("D", here)
                t["Directory"].append((dirs[here], dirs[parent], part))
            parent = here
        key, component = ident("F", path), ident("C", path)
        stem, dot, ext = name.rpartition(".")
        short = f"F{n:07d}" + (f".{ext[:3]}" if dot else "")  # a unique 8.3 name, then the real one
        attributes = COMPONENT_64 | (PERMANENT | NEVER_OVERWRITE if path == "platmon.ini" else 0)
        t["Component"].append((component, guid("component", path), dirs[folder], attributes, None, key))
        t["File"].append((key, component, f"{short}|{name}", len(body), None, None, None, n + 1))
        t["FeatureComponents"].append(("Complete", component))
    server = ident("C", "platmon-server.exe")
    t["Media"].append((1, len(t["File"]), None, "#platmon.cab", None, None))
    t["Feature"].append(("Complete", None, "platmon", None, 0, 1, "INSTALLDIR", 0))
    t["Property"] += [("ProductCode", product), ("UpgradeCode", UPGRADE_CODE), ("ProductName", "platmon"),
                      ("ProductVersion", release), ("Manufacturer", "platmon"), ("ProductLanguage", "1033"),
                      ("ALLUSERS", "1"), ("ARPNOMODIFY", "1"), ("ARPNOREPAIR", "1"),
                      ("ARPURLINFOABOUT", "https://github.com/platmon/platmon"),
                      ("SecureCustomProperties", "OLDERFOUND;NEWERFOUND")]
    # LocalSystem (no StartName), own process (16), automatic (2), errors logged (1); the guide's command line
    t["ServiceInstall"].append(("platmon", "platmon", "platmon", 16, 2, 1, None, None, None, None,
                                'service "[INSTALLDIR]platmon.ini" --web "[INSTALLDIR]web"', server,
                                "platmon, a lightweight platform monitor"))
    # start on install (1), stop on install (2) and removal (32), delete on removal (128); wait for each
    t["ServiceControl"].append(("platmon", "platmon", 1 | 2 | 32 | 128, None, 1, server))
    # the guide's sc.exe failure line, from System32 after the service is installed: an executable in a directory
    # (34), deferred (1024), as LocalSystem (2048); a failure fails the installation
    t["CustomAction"].append(("ConfigureRecovery", 34 | 1024 | 2048, "System64Folder",
                              '"[System64Folder]sc.exe" failure platmon reset= 86400 actions= restart/5000/restart/5000/restart/60000'))
    # sc.exe failureflag platmon 1: also after an error exit, not only a crash (SERVICE_CONFIG_FAILURE_ACTIONS_FLAG)
    t["MsiServiceConfig"].append(("platmon_failureflag", "platmon", 1, 4, "1", server))
    # HKLM (2); "#%": REG_EXPAND_SZ, so %SystemRoot% stays for Windows to expand
    t["Registry"].append(("platmon_eventlog", 2, EVENT_LOG_KEY, "EventMessageFile", "#%" + MESSAGE_FILE, server))
    # the tray at every login (HKLM, 64-bit view), with its file's component: written on install, removed with it
    t["Registry"].append(("platmon_tray_run", 2, RUN_KEY, "platmon-tray", '"[INSTALLDIR]platmon-tray.exe"',
                          ident("C", "platmon-tray.exe")))
    # the system PATH (*), set on install (=), the part added removed on removal (-), appended ([~];...)
    t["Environment"].append(("platmon_path", "=-*PATH", "[~];[INSTALLDIR]", server))
    # older and the same versions are removed first (min and max inclusive); a newer one is only detected
    t["Upgrade"] += [(UPGRADE_CODE, "0.0.0", release, None, 256 | 512, None, "OLDERFOUND"),
                     (UPGRADE_CODE, release, None, None, 2, None, "NEWERFOUND")]
    t["LaunchCondition"] += [("VersionNT64", "platmon needs 64-bit Windows."),
                             ("NOT NEWERFOUND", "A newer platmon is already installed.")]
    when = {"ConfigureRecovery": 'NOT REMOVE~="ALL"'}  # not on removal: the service is being deleted
    t["InstallExecuteSequence"] += [(a, when.get(a), s) for a, s in EXECUTE]
    t["InstallUISequence"] += [(a, None, s) for a, s in UI]
    return t


def idt(name, rows):
    """A table as Windows Installer's text archive format (MsiDatabaseImport): names, types, keys, rows."""
    columns, keys = SCHEMA[name]
    lines = ["\t".join(c for c, _ in columns), "\t".join(k for _, k in columns), "\t".join([name] + keys)]
    for row in rows:
        lines.append("\t".join("" if v is None else str(v) for v in row))
    return "\r\n".join(lines) + "\r\n"


class Msi:
    """The few msi.dll calls a build needs; each raises on a non-zero result."""

    def __init__(self):
        self.dll = ctypes.WinDLL("msi")

    def check(self, result, what):
        if result:
            raise OSError(f"{what}: Windows Installer error {result}")

    def handle(self):
        return ctypes.c_ulong(0)

    def close(self, h):
        self.dll.MsiCloseHandle(h)


def build_msi(target, files, release, binary_sha):
    """Write target from files: the tables, the cabinet as a stream, the summary information."""
    msi = Msi()
    work = Path(tempfile.mkdtemp(prefix="platmon-msi-"))
    try:
        t = tables(files, release)
        kept = [(p, b) for p, b in files if p not in LEFT_OUT]
        # the cabinet: each file under its File key, in Sequence order (makecab keeps the directive's order)
        ddf = [".OPTION EXPLICIT", ".Set CabinetNameTemplate=platmon.cab", f'.Set DiskDirectoryTemplate="{work}"',
               ".Set Cabinet=on", ".Set Compress=on", ".Set CompressionType=MSZIP", ".Set MaxDiskSize=0",
               ".Set UniqueFiles=on", f'.Set InfFileName="{work / "cab.inf"}"', f'.Set RptFileName="{work / "cab.rpt"}"']
        for (path, body), row in zip(kept, t["File"]):
            src = work / "files" / row[0]
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_bytes(body)
            ddf.append(f'"{src}" "{row[0]}"')
        (work / "platmon.ddf").write_text("\r\n".join(ddf) + "\r\n", encoding="ascii")
        subprocess.run(["makecab.exe", "/F", str(work / "platmon.ddf")], check=True, capture_output=True, cwd=work)
        db = msi.handle()
        if target.exists():
            target.unlink()
        msi.check(msi.dll.MsiOpenDatabaseW(ctypes.c_wchar_p(str(target)), ctypes.c_wchar_p(3), ctypes.byref(db)),
                  "MsiOpenDatabase")  # MSIDBOPEN_CREATE
        try:
            idts = work / "idt"
            idts.mkdir()
            for name, rows in t.items():
                (idts / f"{name}.idt").write_text(idt(name, rows), encoding="cp1252")
                msi.check(msi.dll.MsiDatabaseImportW(db, ctypes.c_wchar_p(str(idts)), ctypes.c_wchar_p(f"{name}.idt")),
                          f"MsiDatabaseImport {name}")
            view, rec = msi.handle(), msi.dll.MsiCreateRecord(2)
            msi.check(msi.dll.MsiDatabaseOpenViewW(db, ctypes.c_wchar_p("SELECT `Name`, `Data` FROM `_Streams`"),
                                                   ctypes.byref(view)), "MsiDatabaseOpenView")
            msi.check(msi.dll.MsiRecordSetStringW(rec, 1, ctypes.c_wchar_p("platmon.cab")), "MsiRecordSetString")
            msi.check(msi.dll.MsiRecordSetStreamW(rec, 2, ctypes.c_wchar_p(str(work / "platmon.cab"))), "MsiRecordSetStream")
            msi.check(msi.dll.MsiViewExecute(view, 0), "MsiViewExecute")
            msi.check(msi.dll.MsiViewModify(view, 1, rec), "MsiViewModify")  # MSIMODIFY_INSERT
            msi.dll.MsiViewClose(view)
            msi.close(rec)
            msi.close(view)
            info = msi.handle()
            msi.check(msi.dll.MsiGetSummaryInformationW(db, None, 20, ctypes.byref(info)), "MsiGetSummaryInformation")
            package = guid("package", release, binary_sha)
            for pid, vt, number, text in [(1, 2, 1252, None), (2, 30, 0, "Installation Database"), (3, 30, 0, "platmon"),
                                          (4, 30, 0, "platmon"), (5, 30, 0, "Installer"),
                                          (6, 30, 0, f"platmon {release} for Windows (x86_64)"), (7, 30, 0, "x64;1033"),
                                          (9, 30, 0, package), (14, 3, 500, None), (15, 3, 2, None),
                                          (18, 30, 0, "platmon package_msi.py"), (19, 3, 2, None)]:
                msi.check(msi.dll.MsiSummaryInfoSetPropertyW(info, pid, vt, number, None, ctypes.c_wchar_p(text)),
                          f"MsiSummaryInfoSetProperty {pid}")
            msi.check(msi.dll.MsiSummaryInfoPersist(info), "MsiSummaryInfoPersist")
            msi.close(info)
            msi.check(msi.dll.MsiDatabaseCommit(db), "MsiDatabaseCommit")
        finally:
            msi.close(db)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build(output, binary, root=ROOT, release=None):
    """The installer in output, named for the release; its path."""
    if os.name != "nt":
        raise SystemExit("package_msi.py builds with Windows Installer: run it on Windows")
    release = release or release_version(root)
    server, command = check_binary(binary), check_binary(command_beside(binary), server=False)
    tray = check_binary(tray_beside(binary), server=False, what="tray")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"platmon-{release}-windows-x86_64.msi"
    build_msi(target, members(root, server, command, tray, release), release,
              hashlib.sha256(server + command + tray).hexdigest())
    return target


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--binary", required=True,
                   help="platmon-server.exe, built with the C runtime static, platmon.exe and platmon-tray.exe beside it")
    p.add_argument("--output", required=True)
    p.add_argument("--version", help="the ProductVersion, instead of VERSION's (a test of an upgrade)")
    a = p.parse_args()
    print(build(a.output, a.binary, release=a.version))


if __name__ == "__main__":
    main()
