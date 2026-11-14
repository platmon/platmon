#!/usr/bin/env python3
"""Build the experimental Windows zip with stdlib, after package.py, into the same output directory.

platmon-VERSION-windows-x86_64-experimental.zip: the server built by CI's native-windows job (MSVC, the C
runtime linked statically, so it needs no Visual C++ Redistributable), the web page, platmon.ini listening on
127.0.0.1 only, the licenses, a README and packaging/windows's scripts: start.cmd (serve as the user, open the
browser) and install-service.cmd / uninstall-service.cmd (the service, in Program Files). No installer (the
owner's decisions, docs/development/remaining-work.md, "Windows build"). Its checksum is added to the
SHA256SUMS package.py wrote, and the server's to its BUILD.json (whose own checksum line follows).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import zipfile

if __package__:
    from .package import ALLOCATOR_MARK, ROOT, packaging_epoch, version
else:  # run as a script, from its own directory
    from package import ALLOCATOR_MARK, ROOT, packaging_epoch, version

WEB_SUFFIXES = (".html", ".css", ".js", ".svg", ".json")
PE_AMD64 = 0x8664
SCRIPTS = ("start.cmd", "install-service.cmd", "uninstall-service.cmd")
# the Visual C++ runtime's DLLs: a server linked with the C runtime statically (+crt-static) imports none
CRT_DLL = re.compile(r"(vcruntime|msvcp|msvcr|ucrtbase|api-ms-win-crt-)", re.I)
README = """platmon {release} for Windows (x86_64), EXPERIMENTAL

Not supported: built and checked in CI and on one laptop only. Linux is the supported
platform (https://github.com/platmon/platmon).

Needs: Windows 11 (x86_64; Windows 10 was not checked). Nothing to install
first. Windows may ask before it runs a file downloaded from the internet.

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
  sc.exe start platmon
  sc.exe stop platmon
  sc.exe delete platmon
A service has no console: a configuration error shows only as the service
stopping (run config-check to see it). There is no log and no recovery action.

What it reads on Windows, and what it does not (no pressure, no power or fans):
native/README.md, "Windows", in the source.

Checked on a real PC (Core Ultra 7 155H, Intel Arc iGPU, NVMe, Windows 11 26H2):
CPU and its clocks, GPU usage and clocks, drive temperature, storage, battery,
Wi-Fi, network and disk history, the service, sleep and resume (Modern
Standby). Not checked: more than 64 CPUs (several processor groups), a
discrete GPU's temperature, S3 sleep and hibernation.

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
             ("README.txt", README.format(release=release).replace("\n", "\r\n").encode("utf-8"))]
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
    """The release's CHANGELOG.md section must call the Windows zip experimental (the owner's decision,
    docs/development/remaining-work.md, "Windows build"): one entry names both. The release workflow runs it."""
    start = text.find(f"## {release} —")
    if start == -1:
        raise ValueError("release has no changelog entry")
    end = text.find("\n## ", start + 1)
    entries = text[start:end if end != -1 else None].split("\n- ")
    if not any("windows" in e.lower() and "experimental" in e.lower() for e in entries):
        raise ValueError(f"the {release} changelog entry must call the Windows zip experimental")


def build(output, binary=None, root=ROOT, epoch=None):
    """Write the zip into `output` (package.py's directory, with its SHA256SUMS and BUILD.json), add its
    checksum and the server's."""
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
    target = output / f"platmon-{release}-windows-x86_64-experimental.zip"
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
    sums.write_bytes("".join(f"{line}\n" for line in lines + [f"{sha(target)}  {target.name}"]).encode())
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "dist"), help="package.py's output directory (default dist)")
    parser.add_argument("--binary", help="the Windows server (default native/target/release/platmon-server.exe)")
    parser.add_argument("--version", action="version", version=version())
    args = parser.parse_args()
    try:
        value = os.environ.get("SOURCE_DATE_EPOCH")
        target = build(args.output, args.binary, epoch=int(value) if value is not None else None)
    except (ValueError, OSError) as e:
        parser.exit(1, f"Windows zip build failed: {e}\n")
    print(target)


if __name__ == "__main__":
    main()
