#!/usr/bin/env python3
"""Build the Windows zip with stdlib, after package.py, into the same output directory.

platmon-VERSION-windows-x86_64.zip: the server built by CI's native-windows job (MSVC, the C
runtime linked statically, so it needs no Visual C++ Redistributable), the web page, platmon.ini listening on
127.0.0.1 only, the licenses, a README, the terminal client (cli.py, run by platmon.cmd with the Python on PATH)
and packaging/windows's scripts: start.cmd (serve as the user, open the browser) and install-service.cmd /
uninstall-service.cmd (the service, in Program Files). The installer is package_msi.py's (the owner's decisions,
docs/development/remaining-work.md, "Windows build"). Its checksum is added to the SHA256SUMS package.py
wrote, and the server's to its BUILD.json (whose own checksum line follows).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import zipfile

if __package__:
    from .package import ALLOCATOR_MARK, ROOT, packaging_epoch, version
else:  # run as a script, from its own directory
    from package import ALLOCATOR_MARK, ROOT, packaging_epoch, version

WEB_SUFFIXES = (".html", ".css", ".js", ".svg", ".json")
# the platmon command: the terminal client beside it, with the Python on PATH (as platmon-cli on Linux)
PLATMON_CMD = b'@python "%~dp0cli.py" %*\r\n'
PE_AMD64 = 0x8664
SCRIPTS = ("start.cmd", "install-service.cmd", "uninstall-service.cmd")
# the Visual C++ runtime's DLLs: a server linked with the C runtime statically (+crt-static) imports none
CRT_DLL = re.compile(r"(vcruntime|msvcp|msvcr|ucrtbase|api-ms-win-crt-)", re.I)
README = """platmon {release} for Windows (x86_64)

Supported on Windows 11 x86_64 (https://github.com/platmon/platmon);
Windows 10 was not checked. The installer, platmon-{release}-windows-x86_64.msi,
is the same server with the service set up for you; this zip is for trying it,
or for running it without installing.

Needs: Windows 11 (x86_64). Nothing to install first. Windows may ask before
it runs a file downloaded from the internet: the files are not signed, so
compare SHA256SUMS first.

Run (as your user): double-click start.cmd. It serves this folder's platmon.ini
and web page and opens http://127.0.0.1:9797/ in the browser; Ctrl+C or closing
its window stops it. The same by hand, in a Command Prompt in this folder:
  platmon-server.exe serve platmon.ini --web web
platmon-server.exe --help lists the other commands; config-check platmon.ini
shows the settings it would use.

platmon.ini listens on 127.0.0.1 only. The API has no authentication: before
setting bind = 0.0.0.0, allow port 9797 only from trusted addresses in Windows
Defender Firewall. A service is not asked about by the firewall.

As a Windows service, starting with Windows: right-click install-service.cmd,
Run as administrator. The service runs as LocalSystem, so the script copies
the server, platmon.ini and the web page to C:\\Program Files\\platmon, which
only administrators can change, registers the service "platmon" and starts
it. Run it again from a newer zip to upgrade: it replaces the server and the
web page and keeps the platmon.ini there. After editing that platmon.ini:
sc.exe stop platmon, then sc.exe start platmon. uninstall-service.cmd, also
as administrator, removes the service and its files but platmon.ini.
Never point a service at a folder your user can write (Downloads, Desktop): a
program running as you could replace platmon-server.exe or platmon.ini there
and run as LocalSystem at the next start. The scripts do these steps; by
hand, after copying this folder to C:\\Program Files\\platmon, in a Command
Prompt (cmd) run as administrator:
  sc.exe create platmon start= auto binPath= "\\"C:\\Program Files\\platmon\\platmon-server.exe\\" service \\"C:\\Program Files\\platmon\\platmon.ini\\" --web \\"C:\\Program Files\\platmon\\web\\""
  sc.exe failure platmon reset= 86400 actions= restart/5000/restart/5000/restart/60000
  sc.exe failureflag platmon 1
  sc.exe start platmon
  sc.exe stop platmon
  sc.exe delete platmon
The failure lines restart it after a crash or an error exit: in 5 s twice,
then every minute. A service has no console: why it stopped (a configuration
it refuses, a port in use) is in Event Viewer, Windows Logs > Application,
source platmon; run config-check after editing platmon.ini.
Event Viewer shows that text after a note that its description cannot be found
until the source is registered, once, in PowerShell run as administrator (the
.NET Framework message file Windows includes; Remove-Item removes the key):
  New-Item -Force HKLM:\\SYSTEM\\CurrentControlSet\\Services\\EventLog\\Application\\platmon |
    New-ItemProperty -Force -Name EventMessageFile -PropertyType ExpandString -Value '%SystemRoot%\\Microsoft.NET\\Framework64\\v4.0.30319\\EventLogMessages.dll'

The platmon command (the terminal client) needs Python 3.9 or later as python
on PATH: platmon.cmd runs cli.py beside it, as platmon --once, platmon
192.168.55.53 or platmon --help. To type platmon anywhere, add this folder to
your user PATH once, in PowerShell (a new terminal then finds it):
  [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', 'User') + ';C:\\Program Files\\platmon', 'User')
On Windows it shows every section on one screen, redrawn in place.

What it reads on Windows, and what it does not (no CPU temperature, pressure,
power rails, fans or power mode; SATA drives give no temperature):
native/README.md, "Windows", in the source.

Checked on two PCs (a laptop: Core Ultra 7 155H, Intel Arc iGPU, Windows 11
26H2; a desktop: Core i5-13600KF, GeForce RTX 4070 Ti, Windows 11 25H2): CPU
and its clocks, GPU usage, clocks and temperature, drive temperature, storage,
battery, Wi-Fi, network and disk history, the service and its recovery, Windows
restarts, sleep and resume (Modern Standby, S3, hibernation), a 24 h soak and
platmon-hub. Not checked: Windows 10, more than 64 CPUs (several processor
groups), an AMD CPU.

Licenses: LICENSE (Apache-2.0) for platmon. platmon-server.exe also includes
the Rust standard library and the libc, windows-sys and windows-link crates,
each MIT or Apache-2.0 (LICENSE-MIT here), and Microsoft's C runtime, linked
statically as Microsoft's Visual C++ license terms allow for a program built
with its tools. It calls Windows's own libraries, which are not included.
"""
MIT = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
MIT_HOLDERS = """The Rust standard library: Copyright (c) The Rust Project Contributors
libc: Copyright (c) The Rust Project Developers
windows-sys, windows-link: Copyright (c) Microsoft Corporation

Each is licensed under the MIT license or the Apache License 2.0 (LICENSE), at
your option. The MIT license:

"""


def default_binary(root=ROOT):
    return root / "native/target/release/platmon-server.exe"


def imported_dlls(body):
    """The DLL names in a PE32+ file's import table (lowercase), or None when it has none to read."""
    pe = int.from_bytes(body[60:64], "little")
    sections, optional_size = int.from_bytes(body[pe + 6:pe + 8], "little"), int.from_bytes(body[pe + 20:pe + 22], "little")
    optional = pe + 24
    if body[optional:optional + 2] != b"\x0b\x02" or optional_size < 112 + 16:  # PE32+, through the import entry
        return None
    rva = int.from_bytes(body[optional + 120:optional + 124], "little")
    table = [(int.from_bytes(body[s + 12:s + 16], "little"), max(int.from_bytes(body[s + 8:s + 12], "little"),
              int.from_bytes(body[s + 16:s + 20], "little")), int.from_bytes(body[s + 20:s + 24], "little"))
             for s in range(optional + optional_size, optional + optional_size + 40 * sections, 40)]

    def offset(address):
        return next((raw + address - start for start, size, raw in table if start <= address < start + size), None)

    names, at = [], offset(rva) if rva else None
    while at is not None and at + 20 <= len(body) and body[at:at + 20] != bytes(20):
        name = offset(int.from_bytes(body[at + 12:at + 16], "little"))
        if name is None:
            return None
        names.append(body[name:body.index(b"\0", name)].decode("ascii").lower())
        at += 20
    return names or None


def check_binary(path):
    """An x86_64 PE built with the system allocator (build-info's mark), as package.py checks the ELF ones, and
    with the C runtime linked statically: no Visual C++ runtime DLL among its imports."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Windows server binary not found: {path} (RUSTFLAGS='-C target-feature=+crt-static' "
                         f"cargo build --release --locked --no-default-features)")
    body = path.read_bytes()
    pe = int.from_bytes(body[60:64], "little") if body[:2] == b"MZ" and len(body) >= 64 else -1
    if pe < 0 or body[pe:pe + 4] != b"PE\0\0" or int.from_bytes(body[pe + 4:pe + 6], "little") != PE_AMD64:
        raise ValueError(f"{path} is not an x86_64 Windows executable")
    if ALLOCATOR_MARK not in body:
        raise ValueError(f"{path} does not carry the system allocator's mark")
    dlls = imported_dlls(body)
    if not dlls:
        raise ValueError(f"{path}: no import table to read")
    runtime = [d for d in dlls if CRT_DLL.match(d)]
    if runtime:
        raise ValueError(f"{path} imports the Visual C++ runtime ({', '.join(runtime)}): build it with "
                         f"RUSTFLAGS='-C target-feature=+crt-static'")
    return body


def local_ini(root):
    """platmon.ini with bind = 127.0.0.1 and Windows line ends (Notepad shows either)."""
    text = (root / "platmon.ini").read_text(encoding="utf-8")
    if text.count("\nbind = 0.0.0.0\n") != 1:
        raise ValueError("platmon.ini: no single 'bind = 0.0.0.0' line to change")
    return text.replace("\nbind = 0.0.0.0\n", "\nbind = 127.0.0.1\n").replace("\n", "\r\n").encode("utf-8")


def members(root, server, release):
    """(path in the zip, body): fixed names and order."""
    web = root / "frontends/web"
    files = [("platmon-server.exe", server), ("platmon.ini", local_ini(root)),
             ("LICENSE", (root / "LICENSE").read_bytes()),
             ("LICENSE-MIT", (MIT_HOLDERS + MIT).replace("\n", "\r\n").encode("utf-8")),
             ("README.txt", README.format(release=release).replace("\n", "\r\n").encode("utf-8")),
             ("cli.py", (root / "frontends/cli.py").read_bytes()),
             ("platmon.cmd", PLATMON_CMD)]
    # cmd reads a batch file with LF line ends wrongly (labels, goto): CRLF in the zip
    files += [(name, (root / "packaging/windows" / name).read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
              for name in SCRIPTS]
    for path in sorted(web.rglob("*")):
        if path.is_file() and path.suffix in WEB_SUFFIXES:
            if path.is_symlink():
                raise ValueError(f"unexpected web file: {path}")
            files.append(("web/" + path.relative_to(web).as_posix(), path.read_bytes()))
    return files


def check_changelog(text, release):
    """The release must have a CHANGELOG.md section, which its notes are made from; the release workflow runs
    it. Until Windows was supported it also had to call the Windows zip experimental (removed with the switch,
    docs/development/remaining-work.md, "Windows: promotion")."""
    if f"## {release} —" not in text:
        raise ValueError("release has no changelog entry")


def build(output, binary=None, root=ROOT, epoch=None, msi=None):
    """Write the zip into `output` (package.py's directory, with its SHA256SUMS and BUILD.json), add its
    checksum and the server's; with msi (package_msi.py's installer of the same release, built on Windows),
    copy it there too and add its checksum."""
    release = version(root)
    epoch = packaging_epoch(root) if epoch is None else epoch
    # zip times are local DOS times from 1980; the UTC fields of the epoch keep repeat builds equal
    stamp = max(time.gmtime(epoch)[:6], (1980, 1, 1, 0, 0, 0))
    server = check_binary(binary or default_binary(root))
    output = Path(output).resolve()
    sums, manifest = output / "SHA256SUMS", output / "BUILD.json"
    for path in (sums, manifest):
        if not path.is_file():
            raise ValueError(f"{path} not found: run package.py into {output} first")
    target = output / f"platmon-{release}-windows-x86_64.zip"
    if target.exists():
        raise ValueError(f"{target} already exists; use a fresh output directory")
    top = f"platmon-{release}-windows-x86_64/"
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in members(root, server, release):
            info = zipfile.ZipInfo(top + name, date_time=stamp)
            info.compress_type, info.create_system = zipfile.ZIP_DEFLATED, 0  # 0: MS-DOS, no Unix modes
            archive.writestr(info, body)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["windows_server_sha256"] = {"x86_64": hashlib.sha256(server).hexdigest()}
    # bytes, not write_text(newline=) (Python 3.10+): LF on every host
    manifest.write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode())
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    lines = [f"{sha(manifest)}  BUILD.json" if line.endswith("  BUILD.json") else line
             for line in sums.read_text(encoding="utf-8").splitlines()]
    added = [target]
    if msi is not None:
        msi = Path(msi)
        if msi.name != f"platmon-{release}-windows-x86_64.msi" or not msi.is_file():
            raise ValueError(f"{msi}: not the installer of {release} (platmon-{release}-windows-x86_64.msi)")
        added.append(output / msi.name)
        shutil.copyfile(msi, added[-1])
    sums.write_bytes("".join(f"{line}\n" for line in lines + [f"{sha(p)}  {p.name}" for p in added]).encode())
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "dist"), help="package.py's output directory (default dist)")
    parser.add_argument("--binary", help="the Windows server (default native/target/release/platmon-server.exe)")
    parser.add_argument("--msi", help="package_msi.py's installer of this release, to add beside the zip")
    parser.add_argument("--version", action="version", version=version())
    args = parser.parse_args()
    try:
        value = os.environ.get("SOURCE_DATE_EPOCH")
        target = build(args.output, args.binary, epoch=int(value) if value is not None else None, msi=args.msi)
    except (ValueError, OSError) as e:
        parser.exit(1, f"Windows zip build failed: {e}\n")
    print(target)


if __name__ == "__main__":
    main()
