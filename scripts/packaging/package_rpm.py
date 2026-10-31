#!/usr/bin/env python3
"""Build .rpm packages with stdlib + the host's rpmbuild, from the same files as the .deb packages.

platmon, one package per architecture (x86_64, aarch64): the static Rust server from native/build.sh, its
unit and web page, without Python. platmon-cli, noarch: the `platmon` command, one Python file, which
platmon recommends. Nothing is compiled: packaging/rpm/*.spec copy the staged files (package.py's). No
interpreter, OS software, build tools or hardware binaries are bundled; the packages are not signed.
rpmbuild comes with the host's rpm (Fedora: rpm-build; Debian and Ubuntu: rpm).
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

if __package__:
    from .package import ROOT, check_binary, copy_file, default_binary, packaging_epoch, stage_cli, stage_server, version
else:  # run as a script, from its own directory
    from package import ROOT, check_binary, copy_file, default_binary, packaging_epoch, stage_cli, stage_server, version

# Debian architecture (package.py's, for the binaries): the RPM one. armhf is not built: its binary is
# ARMv6 and the RPM distributions that still have 32-bit ARM have armv7hl.
ARCHES = {"amd64": "x86_64", "arm64": "aarch64"}


def build(output, release="1", root=ROOT, epoch=None, binaries=None):
    """Build one platmon package per architecture in `binaries` ({Debian arch: path}; default: amd64 and
    arm64, from native/target), then platmon-cli, and SHA256SUMS; returns their paths, platmon-cli's last."""
    upstream = version(root)
    if not re.fullmatch(r"[1-9][0-9]*", release):
        raise ValueError("release must be a positive integer")
    if epoch is None:
        epoch = packaging_epoch(root)
    if epoch < 0 or epoch > 4294967295:
        raise ValueError("SOURCE_DATE_EPOCH must fit an unsigned 32-bit timestamp")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if binaries is None:
        binaries = {arch: default_binary(arch, root) for arch in ARCHES}
    if not binaries or set(binaries) - set(ARCHES):
        raise ValueError("architectures must be among: " + ", ".join(ARCHES))
    bodies = {arch: check_binary(arch, binaries[arch]) for arch in sorted(binaries)}
    rpms = [output / f"platmon-{upstream}-{release}.{ARCHES[arch]}.rpm" for arch in bodies]
    rpms.append(output / f"platmon-cli-{upstream}-{release}.noarch.rpm")
    if any(path.exists() for path in (*rpms, output / "SHA256SUMS")):
        raise ValueError("output files already exist; use a fresh output directory")
    for (arch, body), rpm in zip(bodies.items(), rpms):
        with tempfile.TemporaryDirectory(prefix="platmon-package-") as directory:
            stage = Path(directory)
            stage_server(root, stage, body, upstream)
            docs(root, stage, "platmon", (root / "docs/reference/packaging.md",))
            rpmbuild(root, "platmon", stage, rpm, ARCHES[arch], upstream, release, epoch)
    with tempfile.TemporaryDirectory(prefix="platmon-package-") as directory:
        stage = Path(directory)
        stage_cli(root, stage, epoch)
        docs(root, stage, "platmon-cli")
        rpmbuild(root, "platmon-cli", stage, rpms[-1], "noarch", upstream, release, epoch)
    checksums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in rpms)
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    return rpms


def docs(root, stage, package, extra=()):
    """/usr/share/doc/PACKAGE (the changelog, README and any extra files) and /usr/share/licenses/PACKAGE
    (the license and the .deb's copyright, which names the server's statically linked parts)."""
    for path in (root / "CHANGELOG.md", root / "README.md", *extra):
        copy_file(path, stage / "usr/share/doc" / package / path.name)
    copy_file(root / "LICENSE", stage / "usr/share/licenses" / package / "LICENSE")
    copy_file(root / "packaging/debian/copyright", stage / "usr/share/licenses" / package / "copyright")


def rpmbuild(root, package, stage, rpm, arch, upstream, release, epoch):
    """The staged tree as rpm: the spec template with its fields filled in, fixed timestamps and build host."""
    for path in stage.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o755)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    date = time.strftime("%a %b %d %Y", time.gmtime(epoch))
    spec = (root / "packaging/rpm" / f"{package}.spec").read_text(encoding="utf-8")
    for key, value in {"@VERSION@": upstream, "@RELEASE@": release, "@DATE@": date, "@STAGE@": str(stage)}.items():
        spec = spec.replace(key, value)
    with tempfile.TemporaryDirectory(prefix="platmon-rpmbuild-") as directory:
        top = Path(directory)
        (top / "platmon.spec").write_text(spec, encoding="utf-8")
        defines = {"_topdir": top, "_rpmdir": rpm.parent, "_build_name_fmt": rpm.name,
                   "_buildhost": "platmon", "source_date_epoch_from_changelog": 0,
                   "use_source_date_epoch_as_buildtime": 1, "clamp_mtime_to_source_epoch": 1,
                   "_binary_payload": "w9.xzdio", "_binary_filedigest_algorithm": 8,
                   "_build_id_links": "none"}
        args = ["rpmbuild", "-bb", "--quiet", "--target", arch]
        for key, value in defines.items():
            args += ["--define", f"{key} {value}"]
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch), TZ="UTC")
        subprocess.run([*args, str(top / "platmon.spec")], check=True, env=env)
    if not rpm.is_file():
        raise ValueError(f"rpmbuild did not write {rpm.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "dist/rpm"), help="fresh output directory (default dist/rpm)")
    parser.add_argument("--release", default="1", help="RPM release, the packaging revision (default 1)")
    parser.add_argument("--binary", action="append", metavar="ARCH=PATH",
                        help="a server binary for one architecture, amd64 or arm64 (repeat; default: both "
                             "from native/target, built by native/build.sh)")
    args = parser.parse_args()
    try:
        value = os.environ.get("SOURCE_DATE_EPOCH")
        binaries = None
        if args.binary:
            pairs = [item.split("=", 1) for item in args.binary]
            if any(len(pair) != 2 for pair in pairs) or len({arch for arch, _ in pairs}) != len(pairs):
                raise ValueError("--binary takes ARCH=PATH, once per architecture")
            binaries = {arch: Path(path) for arch, path in pairs}
        rpms = build(args.output, args.release, epoch=int(value) if value is not None else None, binaries=binaries)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        parser.exit(1, f"package build failed: {e}\n")
    for rpm in rpms:
        print(rpm)


if __name__ == "__main__":
    main()
