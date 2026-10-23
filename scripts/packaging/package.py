#!/usr/bin/env python3
"""Build .deb (amd64, arm64), source archive and checksums with stdlib + the host's dpkg-deb.

The server is the static Rust binary from native/build.sh; the `platmon` client stays one Python file.
No interpreter, OS software, build tools or hardware binaries are bundled.
"""
import argparse
import ast
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = ("platmon.py", "platmon.ini", "deploy/systemd/platmon.service", "README.md", "LICENSE",
                "CHANGELOG.md", "pytest.ini", "deploy/docker/Dockerfile",
                "deploy/docker/compose.yaml", "deploy/docker/compose.jetson.yaml", ".dockerignore", ".gitignore")
SOURCE_DIRS = ("collector", "runtime", "frontends", "native", "scripts", "packaging", "deploy", "benchmarks", "docs",
               "tests", ".github")
# Debian architecture: the Rust target native/build.sh builds for it, its ELF e_machine and class (bits).
ARCHES = {"amd64": ("x86_64-unknown-linux-musl", 62, 64), "arm64": ("aarch64-unknown-linux-musl", 183, 64),
          "armhf": ("arm-unknown-linux-musleabihf", 40, 32)}
# What a release packages. armhf is kept buildable (native/build-armhf.sh) and packaged only when its binary
# is passed (--binary armhf=PATH); it is not released (native/README.md, support scope).
RELEASED = ("amd64", "arm64")
ALLOCATOR_MARK = b"platmon-native-allocator=system;"


def file_md5(path):
    """Debian's file integrity checksum, not a security digest."""
    data = path.read_bytes()
    try:
        digest = hashlib.md5(data, usedforsecurity=False)
    except TypeError:  # Python 3.8 has no usedforsecurity keyword.
        digest = hashlib.md5(data)
    return digest.hexdigest()


def version(root=ROOT):
    """Read the single source without importing Linux collectors during a build."""
    tree = ast.parse((root / "runtime/__init__.py").read_text(encoding="utf-8"))
    values = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets)]
    if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", values[0]):
        raise ValueError("runtime/__init__.py must define one VERSION = 'MAJOR.MINOR.PATCH'")
    return values[0]


def source_files(root):
    paths = [root / name for name in SOURCE_FILES]
    for directory in SOURCE_DIRS:
        paths.extend(p for p in (root / directory).rglob("*") if p.is_file()
                     and not any(part in ("__pycache__", ".pytest_cache") for part in p.parts)
                     and p.relative_to(root).parts[:2] != ("native", "target")
                     and p.suffix not in (".pyc", ".pyo"))
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"distribution input must be a regular file: {path}")
        yield path


def default_binary(arch, root=ROOT):
    return root / "native/target" / ARCHES[arch][0] / "release/platmon-native"


def check_binary(arch, path):
    """The binary must be the system-allocator build for this architecture (native/build.sh checks the same mark)."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{arch} server binary not found: {path} (build it with native/build.sh)")
    body = path.read_bytes()
    _, machine, bits = ARCHES[arch]
    if body[:6] != b"\x7fELF" + bytes((bits // 32, 1)) or int.from_bytes(body[18:20], "little") != machine:
        raise ValueError(f"{path} is not a {bits}-bit little-endian ELF for {arch}")
    if ALLOCATOR_MARK not in body:
        raise ValueError(f"{path} does not carry the system allocator's mark")
    return body


def copy_file(source, target, executable=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755 if executable else 0o644)


def packaging_epoch(root=ROOT):
    """Keep the default timestamp in the source archive, independent of Git."""
    changelog = (root / "packaging/debian/changelog").read_text(encoding="utf-8")
    for line in changelog.splitlines():
        if line.startswith(" -- "):
            date = parsedate_to_datetime(line.rsplit("  ", 1)[-1])
            if date.tzinfo is None:
                raise ValueError("packaging changelog date must include a timezone")
            return int(date.timestamp())
    raise ValueError("packaging changelog must include a dated maintainer signature")


def write_gzip(body, target, epoch):
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9) as gz:
            gz.write(body)
    target.chmod(0o644)


def source_archive(root, output, release, epoch):
    # Fixed ownership, permissions, order and times make repeat builds comparable.
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for path in source_files(root):
                    relative = path.relative_to(root)
                    body = path.read_bytes()
                    info = tarfile.TarInfo(f"platmon-{release}/{relative.as_posix()}")
                    info.size, info.mtime = len(body), epoch
                    info.mode = 0o755 if path.suffix == ".sh" else 0o644
                    archive.addfile(info, io.BytesIO(body))


def build(output, revision="1", root=ROOT, epoch=None, binaries=None):
    """Build one package per architecture in `binaries` ({arch: path}; default: RELEASED, from native/target)."""
    release = version(root)
    if not re.fullmatch(r"[1-9][0-9]*", revision):
        raise ValueError("revision must be a positive integer")
    if epoch is None:
        epoch = packaging_epoch(root)
    if epoch < 0 or epoch > 4294967295:
        raise ValueError("SOURCE_DATE_EPOCH must fit an unsigned 32-bit timestamp")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if binaries is None:
        binaries = {arch: default_binary(arch, root) for arch in RELEASED}
    if not binaries or set(binaries) - set(ARCHES):
        raise ValueError("architectures must be among: " + ", ".join(ARCHES))
    bodies = {arch: check_binary(arch, binaries[arch]) for arch in sorted(binaries)}
    deb_version = f"{release}-{revision}"
    debs = [output / f"platmon_{deb_version}_{arch}.deb" for arch in bodies]
    source = output / f"platmon-{release}.tar.gz"
    outputs = (*debs, source, output / "BUILD.json", output / "SHA256SUMS")
    if any(path.exists() for path in outputs):
        raise ValueError("output files already exist; use a fresh output directory")
    for (arch, body), deb in zip(bodies.items(), debs):
        build_deb(root, deb, arch, body, release, revision, epoch)
    source_archive(root, source, release, epoch)
    inputs = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source_files(root)}
    manifest = {"version": release, "package_version": deb_version, "source_date_epoch": epoch,
                "server_sha256": {arch: hashlib.sha256(body).hexdigest() for arch, body in bodies.items()},
                "source_files_sha256": inputs}
    (output / "BUILD.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in outputs[:-1])
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    return debs


def build_deb(root, deb, arch, server, release, revision, epoch):
    deb_version = f"{release}-{revision}"
    with tempfile.TemporaryDirectory(prefix="platmon-package-") as directory:
        stage = Path(directory)
        web = root / "frontends/web"
        for path in sorted(web.rglob("*")):
            if path.is_file() and path.suffix in (".html", ".css", ".js", ".svg", ".json"):
                if path.is_symlink():
                    raise ValueError(f"unexpected web file: {path}")
                copy_file(path, stage / "usr/share/platmon/frontends/web" / path.relative_to(web))
        binary = stage / "usr/lib/platmon/platmon-native"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(server)
        binary.chmod(0o755)
        cli = stage / "usr/bin/platmon"
        copy_file(root / "frontends/cli.py", cli, executable=True)
        cli.write_text(cli.read_text(encoding="utf-8").replace(
            "#!/usr/bin/env python3\n", "#!/usr/bin/python3\n", 1), encoding="utf-8")
        unit = stage / "usr/lib/systemd/system/platmon.service"
        copy_file(root / "packaging/debian/platmon.service", unit)
        unit.write_text(unit.read_text(encoding="utf-8").replace("@VERSION@", release), encoding="utf-8")
        copy_file(root / "platmon.ini", stage / "etc/platmon/platmon.ini")
        doc = stage / "usr/share/doc/platmon"
        for source_path, name in ((root / "README.md", "README.md.gz"),
                                  (root / "CHANGELOG.md", "changelog.gz"),
                                  (root / "docs/reference/packaging.md", "packaging.md.gz")):
            write_gzip(source_path.read_bytes(), doc / name, epoch)
        changelog = (root / "packaging/debian/changelog").read_text(encoding="utf-8")
        write_gzip(changelog.replace("@VERSION@", deb_version).encode("utf-8"),
                   doc / "changelog.Debian.gz", epoch)
        copy_file(root / "packaging/debian/copyright", doc / "copyright")
        write_gzip((root / "packaging/debian/platmon.1").read_bytes(),
                   stage / "usr/share/man/man1/platmon.1.gz", epoch)
        overrides = (root / "packaging/debian/lintian-overrides").read_text(encoding="utf-8")
        if revision == "1":
            overrides += (root / "packaging/debian/lintian-overrides.initial").read_text(encoding="utf-8")
        target = stage / "usr/share/lintian/overrides/platmon"
        target.parent.mkdir(parents=True)
        target.write_text(overrides, encoding="utf-8")
        target.chmod(0o644)
        control = stage / "DEBIAN"
        control.mkdir()
        for name in ("preinst", "postinst", "prerm", "postrm", "conffiles"):
            copy_file(root / "packaging/debian" / name, control / name, executable=name != "conffiles")
        conffiles = set((control / "conffiles").read_text().splitlines())
        md5sums = "".join(
            f"{file_md5(path)}  {path.relative_to(stage).as_posix()}\n"
            for path in sorted(stage.rglob("*")) if path.is_file()
            and control not in path.parents and "/" + path.relative_to(stage).as_posix() not in conffiles)
        (control / "md5sums").write_text(md5sums, encoding="utf-8")
        size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
        template = (root / "packaging/debian/control").read_text(encoding="utf-8")
        (control / "control").write_text(template.replace("@VERSION@", deb_version).replace("@ARCH@", arch).replace(
            "@SIZE@", str((size + 1023) // 1024)), encoding="utf-8")
        for path in stage.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            os.utime(path, (epoch, epoch))
        stage.chmod(0o755)
        os.utime(stage, (epoch, epoch))
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch), DPKG_DEB_THREADS_MAX="2")
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(stage), str(deb)],
                       check=True, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "dist"), help="fresh output directory (default dist)")
    parser.add_argument("--revision", default="1", help="Debian packaging revision (default 1)")
    parser.add_argument("--binary", action="append", metavar="ARCH=PATH",
                        help="a server binary for one architecture (repeat; default: amd64 and arm64 from "
                             "native/target, built by native/build.sh; armhf only when given)")
    parser.add_argument("--version", action="version", version=version())
    args = parser.parse_args()
    try:
        value = os.environ.get("SOURCE_DATE_EPOCH")
        binaries = None
        if args.binary:
            pairs = [item.split("=", 1) for item in args.binary]
            if any(len(pair) != 2 for pair in pairs) or len({arch for arch, _ in pairs}) != len(pairs):
                raise ValueError("--binary takes ARCH=PATH, once per architecture")
            binaries = {arch: Path(path) for arch, path in pairs}
        debs = build(args.output, args.revision, epoch=int(value) if value is not None else None, binaries=binaries)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        parser.exit(1, f"package build failed: {e}\n")
    for deb in debs:
        print(deb)


if __name__ == "__main__":
    main()
