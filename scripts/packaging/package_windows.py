#!/usr/bin/env python3
"""Build the experimental Windows zip with stdlib, after package.py, into the same output directory.

platmon-VERSION-windows-x86_64-experimental.zip: the server built by CI's native-windows job (MSVC, the
Visual C++ runtime linked dynamically), the web page, platmon.ini listening on 127.0.0.1 only, the license
and a README. No installer, no service registration, no Microsoft binaries (the owner's decisions,
docs/development/remaining-work.md, "Windows build"). Its checksum is added to the SHA256SUMS package.py
wrote.
"""
import argparse
import hashlib
import os
from pathlib import Path
import time
import zipfile

if __package__:
    from .package import ALLOCATOR_MARK, ROOT, packaging_epoch, version
else:  # run as a script, from its own directory
    from package import ALLOCATOR_MARK, ROOT, packaging_epoch, version

WEB_SUFFIXES = (".html", ".css", ".js", ".svg", ".json")
PE_AMD64 = 0x8664
README = """platmon {release} for Windows (x86_64), EXPERIMENTAL

Not supported: built and checked in CI and on one laptop only. Linux is the supported
platform (https://github.com/platmon/platmon).

Needs: Windows 10 or 11 (x86_64) and the Microsoft Visual C++ 2015-2022
Redistributable (x64), which this zip does not include:
https://aka.ms/vs/17/release/vc_redist.x64.exe
Without it the server does not start (vcruntime140.dll was not found).

Run (as your user):
  platmon-server.exe serve platmon.ini --web web
then open http://127.0.0.1:9797/ . Ctrl+C stops it. platmon-server.exe --help
lists the other commands; config-check platmon.ini shows the settings it would use.

platmon.ini listens on 127.0.0.1 only. The API has no authentication: before
setting bind = 0.0.0.0, allow port 9797 only from trusted addresses in Windows
Defender Firewall. A service is not asked about by the firewall.

As a Windows service (administrator shell; use full paths):
  sc.exe create platmon binPath= "C:\\path\\platmon-server.exe service C:\\path\\platmon.ini --web C:\\path\\web" start= auto
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
each MIT or Apache-2.0 (LICENSE-MIT here); it calls Windows's own libraries
and the Visual C++ runtime, which are not included.
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


def check_binary(path):
    """An x86_64 PE built with the system allocator (build-info's mark), as package.py checks the ELF ones."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Windows server binary not found: {path} (cargo build --release --no-default-features)")
    body = path.read_bytes()
    pe = int.from_bytes(body[60:64], "little") if body[:2] == b"MZ" and len(body) >= 64 else -1
    if pe < 0 or body[pe:pe + 4] != b"PE\0\0" or int.from_bytes(body[pe + 4:pe + 6], "little") != PE_AMD64:
        raise ValueError(f"{path} is not an x86_64 Windows executable")
    if ALLOCATOR_MARK not in body:
        raise ValueError(f"{path} does not carry the system allocator's mark")
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
    for path in sorted(web.rglob("*")):
        if path.is_file() and path.suffix in WEB_SUFFIXES:
            if path.is_symlink():
                raise ValueError(f"unexpected web file: {path}")
            files.append(("web/" + path.relative_to(web).as_posix(), path.read_bytes()))
    return files


def build(output, binary=None, root=ROOT, epoch=None):
    """Write the zip into `output` (package.py's directory, with its SHA256SUMS) and add its checksum."""
    release = version(root)
    epoch = packaging_epoch(root) if epoch is None else epoch
    # zip times are local DOS times from 1980; the UTC fields of the epoch keep repeat builds equal
    stamp = max(time.gmtime(epoch)[:6], (1980, 1, 1, 0, 0, 0))
    server = check_binary(binary or default_binary(root))
    output = Path(output).resolve()
    sums = output / "SHA256SUMS"
    if not sums.is_file():
        raise ValueError(f"{sums} not found: run package.py into {output} first")
    target = output / f"platmon-{release}-windows-x86_64-experimental.zip"
    if target.exists():
        raise ValueError(f"{target} already exists; use a fresh output directory")
    top = f"platmon-{release}-windows-x86_64/"
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in members(root, server, release):
            info = zipfile.ZipInfo(top + name, date_time=stamp)
            info.compress_type, info.create_system = zipfile.ZIP_DEFLATED, 0  # 0: MS-DOS, no Unix modes
            archive.writestr(info, body)
    with sums.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {target.name}\n")
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
