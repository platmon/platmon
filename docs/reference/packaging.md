# Packaging and releases

> **TL;DR**
> - One `.deb` per architecture (amd64, arm64, armhf) with the static native server; the `platmon` command
>   needs the host's Python 3.9+.
> - The `VERSION` file is the single version source; releases are built once in CI and
>   published to GitHub Releases and the APT repository.
> - Covers install, upgrade, migration from source or Docker installs, rollback and the release procedure.

The primary distribution is a Debian package for Ubuntu and Debian-derived Linux hosts
(including supported Jetson and Raspberry Pi systems), one per architecture: `amd64`, `arm64` and `armhf`.
The service is a static native server built from `native/` (Rust, musl, the system allocator); the
`platmon` command is one Python file that uses the host's Python 3.9+ standard library only. The package
uses the host's systemd; it bundles neither Python nor OS/NVIDIA binaries. A package for an architecture
does not mean that every OS/kernel on it has been validated. A device whose Python is older than 3.9
needs a supported OS/interpreter before installation. Releases up to 1.3.0 are one `Architecture: all`
package with the Python server, which was removed after 1.6.0; the source installation and the container run
the native server too, built from the checkout ([deployment](../guides/deployment.md)).
`armhf` (32-bit ARM, ARMv6 with VFP and later, e.g. Raspberry Pi OS 32-bit) is built by
`native/build-armhf.sh`; releases 1.4.0 and 1.5.0 had no armhf package, so armhf hosts upgrade from 1.3.0.
No GitHub runner runs 32-bit ARM code: CI checks the armhf package in Debian 13 and Ubuntu 24.04 armhf
userspaces under QEMU (the smoke check and the dpkg lifecycle). The service under systemd, a real board's
kernel and speed are not verified there (native/README.md, 32-bit ARM).

Python 3.8 and Ubuntu 20.04 are CI observation targets under
[ADR 0001](../development/adr/0001-ubuntu-lts-python-support.md), outside official support and
without an ongoing compatibility commitment. The native package minimum remains Python 3.9.
Container checks do not establish native OS, systemd or hardware support. See the
[qualification record](../development/ubuntu-lts-qualification.md) for commit-specific CI evidence
and remaining qualification work for Ubuntu 22.04, 24.04 and 26.04.

## Versioning

The `VERSION` file (one line, `MAJOR.MINOR.PATCH`) is the single source of the application version.
The server reads it at build time (`platmon-native --version`; `native/build.sh` and the Dockerfile put it
beside the crate), and the builder also writes it into the package's unit (`--app-version`, which overrides
it); the server shows it on its web page and logs it at start. `frontends/cli.py` keeps a copy so the CLI stays one independent file; `tests/frontends/test_cli.py`
fails until they say the same, so a release changes both. Check versions with:

```sh
platmon --version
journalctl -u platmon -b | grep -m1 version
dpkg-query -W platmon
```

Use MAJOR.MINOR.PATCH: patch for compatible fixes, minor for compatible features,
major for breaking changes. From 1.0.0 this covers the HTTP API, the `platmon.ini` keys and
the `platmon` command's options. API `schema_version` remains a separate contract; a
release bump does not change it. Update `CHANGELOG.md` in the same change as `VERSION`.

Removing an officially supported OS or raising the minimum Python is also a major-version change.
Announce it at least six months in advance with the final compatible version, migration path and
any maintenance end date. Adding a newly qualified OS is a compatible minor-version change.
CI-only observation targets carry no such retirement guarantee; their removal is recorded in the changelog.

The first release is `1.0.0` (a `v0.1.0` tag exists from packaging validation and was never
published). Debian package versions append a packaging revision: `1.0.0-1`. A packaging-only rebuild can use `-2` without
changing the application version. Never replace an already released version's files.

## Build

Build on Linux with Python 3.9+, the installed `dpkg-deb` tool (dpkg 1.19+) and Docker.
`native/build.sh` builds the server for both architectures in a throwaway `rust:1.88-slim`
container (Cargo.lock as committed, the Rust tests first, then each binary checked for the
system allocator's mark). No Python build dependency, Rust toolchain on the host or root access
is needed:

```sh
native/build.sh
platmon_version=$(python3 scripts/packaging/package.py --version)
platmon_arch=$(dpkg --print-architecture)
platmon_dist="dist/${platmon_version}-1"
python3 scripts/packaging/package.py --output "$platmon_dist"
python3 scripts/packaging/check_package.py "$platmon_dist/platmon_${platmon_version}-1_${platmon_arch}.deb"
lintian --no-cfg --info --pedantic --fail-on error,warning "$platmon_dist"/*.deb
(cd "$platmon_dist" && sha256sum -c SHA256SUMS)
```

The fresh output directory contains `platmon_<version>-1_amd64.deb`, `platmon_<version>-1_arm64.deb`,
`platmon_<version>-1_armhf.deb`,
`platmon-<version>.tar.gz`, `BUILD.json` (input content hashes, the servers' SHA-256 and versions) and
`SHA256SUMS`. The builder takes the servers from `native/target/` by default, or one per
`--binary ARCH=PATH`; it refuses a file that is not an ELF for that architecture or lacks the
system allocator's mark. `check_package.py` runs the extracted server, so it needs a host of the
package's architecture. The source archive includes `native/` (without `native/target/`),
runtime, tests, installation/build scripts and documentation, excluding caches and
private validation artifacts. With the same servers it can rebuild the packages without `.git`.
`SOURCE_DATE_EPOCH` controls timestamps; when unset, the builder uses the dated
maintainer signature in `packaging/debian/changelog`. Update that date and its entries
when changing packaging. The template's `@VERSION@` is replaced by the application
version and requested revision, so version remains defined once. The default timestamp
travels in the source archive and does not require Git or the current clock.
With identical inputs,
epoch and build tools, repeated builds produce identical artifacts. The builder
refuses existing output filenames. Packaging revision: `--revision 2`.

Install Lintian on the build host with `sudo apt install lintian`. CI rejects its
errors and warnings for both packaging revisions. This checks the binary package;
the source archive is an upstream distribution, not a Debian source package with
`debian/rules` or a `.dsc` file.
Revision 1 includes a scoped Lintian override for `initial-upload-closes-no-bugs`:
this upstream distribution is not an upload to the Debian archive and has no Debian
Intent To Package bug. Every revision overrides the tags of the deliberately static server,
per architecture (`statically-linked-binary` on arm64 and armhf; `shared-library-lacks-prerequisites`
on amd64, whose static PIE looks like a shared object). Other policy errors and warnings
remain checked.

## Install and update

### From the APT repository

<https://platmon.github.io/apt> is a signed APT repository (suite `stable`, component
`main`) with the same `.deb` files as GitHub Releases. Its index is signed with the key
`6D42 C14A BFB9 412F 34BA 4BEF F38B 9BB5 26A3 B0C6` (Ed25519, expires 2028-10-17).
[Getting started](../guides/getting-started.md#requirements-and-installation) installs the key in
`/etc/apt/keyrings/platmon.gpg` and the source in `/etc/apt/sources.list.d/platmon.sources`,
with `Signed-By` so that the key is trusted for this repository only, and `Architectures:` set to the
host's architecture, so that a foreign architecture such as i386 is not requested from it.

```sh
sudo apt update && sudo apt install --only-upgrade platmon   # update platmon only
apt list -a platmon                                          # versions in the repository
sudo apt install --allow-downgrades platmon=<version>-<revision>   # roll back
```

Every published release stays in the repository, so a rollback does not need a retained
`.deb`; still back up the INI first (see [Rollback](#rollback)). The repository holds
only published releases: a file that is already there is never replaced, and a release is
added only after its `SHA256SUMS` and, from releases after 1.3.0-1, its provenance attestation
have been checked. It is added by hand after each release, so it can trail GitHub Releases
for a short time. Unattended upgrades do not include this origin unless you add it; when you do,
choose a conffile policy as described below. Before the key expires, a key with a later expiry
date is published at the same address; fetch the keyring file again to keep updating. To stop
using the repository, delete those two files and run `sudo apt update`.

### From a downloaded or built package

Download a fixed release, verify its checksums and keep its files for rollback.
Install the local package using APT, which checks the declared host dependencies. For a
checkout build, use the variables from the Build section:

```sh
sudo apt install "./${platmon_dist}/platmon_${platmon_version}-1_${platmon_arch}.deb"
systemctl status platmon --no-pager
platmon --version
platmon --once
```

For a downloaded release, replace the package path with the exact verified `.deb` file,
including that release's application version, packaging revision and the host's architecture.

`SHA256SUMS` comes from the same release page as the package, so it detects a damaged
download but does not show who built the file. Releases after 1.3.0-1 also carry a GitHub
build provenance attestation. With the GitHub CLI, check that the package was built by this
repository's release workflow from the release tag:

```sh
gh attestation verify platmon_<version>-<revision>_<arch>.deb --repo platmon/platmon \
  --signer-workflow platmon/platmon/.github/workflows/release.yml \
  --source-ref refs/tags/v<version>
```

Use the release's own tag, including a `-<revision>` suffix when it has one.
The published packages 1.0.0-1 to 1.3.0-1 have no attestation.

The server goes to `/usr/lib/platmon/platmon-native`, its web files to `/usr/share/platmon/`,
the client to `/usr/bin/platmon`, the vendor unit to `/usr/lib/systemd/system/platmon.service`,
and config to `/etc/platmon/platmon.ini`.
The CLI manual is available with `man platmon`. Compressed documentation and
changelogs are in `/usr/share/doc/platmon/`; `copyright` references the system's
Apache-2.0 license. Package metadata includes `md5sums` for non-conffile contents
for tools such as `debsums`; release artifact verification uses `SHA256SUMS`.
The service retains DynamicUser, ProtectSystem, ProtectHome and NoNewPrivileges; the server
reads the same INI, with the same keys, defaults and refusal messages, and answers the same
HTTP API, web page and replay as the Python server. Logs:
`journalctl -u platmon`. Configure the listener and restart with
`sudo systemctl restart platmon`; `platmon --once host:port` follows a custom port.

The INI is a dpkg conffile. Locally edited settings are preserved unless you explicitly
choose the maintainer's replacement at a dpkg prompt. For unattended updates, choose
your conffile policy explicitly; do not assume a prompt is answered automatically.
Existing explicit `enabled = no` values remain effective. Omitted options follow the
new version's defaults, so read its changelog before updating.

Upgrades validate the preserved INI with the new server (`platmon-native config-check`)
before restarting the service, so an INI it refuses leaves the running service alone. They
preserve an administrator's disabled/masked service state and respect Debian's service-start
policy. After a start or restart, the package waits up to 20 s for the service to stay running
and for `platmon-native health` to pass: `/api/stats` answers 200 (a current snapshot, as
`/api/status` says ready) on the configured address (`0.0.0.0` is checked on `127.0.0.1`). The
maintainer scripts are plain `sh` and use no Python; a service that was not started (disabled, masked or held by
policy) is reported and not checked. A config, restart or health-check failure fails
configuration: dpkg reports the package half-configured and the unit keeps retrying. It is
not an automatic rollback; install the previous package (see [Rollback](#rollback)) or fix
the cause and run `sudo dpkg --configure platmon`. Check the expected readings on the actual
device. The health check is not evidence that every
sensor or filesystem is exposed. The unauthenticated API belongs on a trusted network.

```sh
sudo apt remove platmon    # removes code and stops service; preserves config
sudo apt purge platmon     # also removes the packaged conffile
```

The package does not remove unrelated files in `/etc/platmon`. Use APT for packaged
installations; source install/uninstall scripts refuse an installed dpkg package.

## Migrate a source or Docker installation

Do this during a maintenance window. First preserve the old checkout/version, INI,
unit, CLI and the previous image (for Docker); record whether the service was running
and enabled. Keep the files outside paths removed by the old uninstaller.

Run the removal command from the retained checkout that matches the existing installation.
The file layout determines the command; check which path exists before running it:

| Retained checkout layout | Source service uninstall | Docker stop/remove |
| --- | --- | --- |
| Before the file reorganization (`scripts/`) | `scripts/systemd/uninstall.sh` | `scripts/docker/stop.sh` |
| After the file reorganization (`deploy/`) | `deploy/systemd/uninstall.sh` | `deploy/docker/stop.sh` |

For the source installation, run the matching source uninstall command from the table,
without `--purge`, after the backup. This stops/disables the service, removes its local
unit and command, and preserves `/etc/platmon/platmon.ini`. Then install the package.
The first package install refuses an existing `/etc/systemd/system/platmon.service`
(including a mask) rather than deleting it. Package upgrades/reinstalls preserve
administrator units and masks. An administrator's custom source unit needs explicit review
and migration to a drop-in. `/etc` units otherwise override the packaged vendor unit.

For Docker, stop/remove the old container with the matching Docker command from the table, keep its image
and compose/config files, and copy the desired INI to `/etc/platmon/platmon.ini` before
installing. Do not run both services on the same listener. The container's mount-specific
environment does not carry into the native service; compare device readings after migration.

Check `command -v platmon` and `type -a platmon`. An old `~/.local/bin/platmon` or
`/usr/local/bin/platmon` can shadow `/usr/bin/platmon`. Remove it with its original
`scripts/install-cli.sh --uninstall` (add `--user` for the user install), after verifying
it belongs to platmon, or explicitly use `/usr/bin/platmon`. The package never deletes
administrator-owned local commands.

## Rollback

From 1.3.0 (the Python server, `_all.deb`) the upgrade to a native package keeps the INI and
swaps the server in the same `platmon.service`: the unit restarts once, so the two servers never
hold the port together. The replay cursor of the previous instance gets 409 (a new
`instance_id`) as after any restart, and in-memory history starts again. Going back is the same
in reverse: `sudo apt install --allow-downgrades platmon=1.3.0-1` (or the retained
`platmon_1.3.0-1_all.deb`), which also recovers from a failed health check.

Before an update, retain the previous `.deb` and back up the operational INI. To roll
back during a maintenance window, stop platmon, restore the compatible backed-up INI,
and install the retained package with `sudo apt install --allow-downgrades ./OLD.deb`.
Check the dpkg conffile prompt, the application/package versions, readiness and expected
readings. Restore the previous enabled/running state explicitly. In-memory history is
lost on restart. Keep both packages and configurations until acceptance completes.

To reverse a source-to-package migration, remove the package without purging config,
restore the old INI, and reinstall the retained old checkout with its source installer.
To reverse a Docker-to-package migration, remove the package, restore the retained
Docker config/CLI and start the retained image using the old deployment procedure.

The source installer now stages files before stopping, restores previous code/unit on
failed replacement/readiness, and keeps successful update backups in the printed
`/opt/.platmon-install.XXXXXX` directory. Retain the matching checkout, CLI and INI too;
the directory is not a complete deployment backup. On failure, inspect the printed
recovery directory and service state, especially if restoring/restarting also failed.
For a later manual rollback, prefer reinstalling the retained old checkout. Backup
directories are never automatically deleted by subsequent updates or uninstall.

## Release procedure

1. Update `VERSION` (and its copy in `frontends/cli.py`) and the changelog. Review the scoped change and run
   `git diff --check`, `pytest -v`, `native/build.sh`, `native/build-armhf.sh`, the build, package checker and Lintian.
2. Test fresh install, upgrade with an edited INI, downgrade, remove/purge and existing
   installation migration. CI uses isolated containers for the dpkg lifecycle and, with systemd
   booted in the container, the service check (below), on amd64 and arm64 runners (armhf: the lifecycle only, under QEMU); device
   readings, a real reboot and a real Hub require real-device acceptance.
3. Merge the reviewed change into `main` using the repository's signing/hooks policy.
   Tag the verified commit included in `main` as `v<version>` matching VERSION; use `v<version>-2` for packaging
   revision 2. Confirm the intended commit and required checks before pushing the tag.
   Creating a local tag alone does not start the workflow. This document does not create a tag.
4. The release workflow fetches `main` and rejects tags whose commit is not an ancestor
   of its current tip, before building. It verifies tag/version/changelog and runs the tests.
   It then builds the release files **once**, from the server binaries the checks built and
   compared in the same run, and checks those same files with the package checker, Lintian,
   the dpkg lifecycle check (amd64, against a next-revision fixture that is not released) and
   `SHA256SUMS`. It records a GitHub build provenance attestation for the
   `.deb` and source archive, and creates a **draft** GitHub Release with the four artifacts
   and the version's `CHANGELOG.md` section as notes. Review the attached bytes, acceptance
   evidence and notes before publishing the draft. Do not distribute a container image;
   the repository's existing image licensing policy still applies.
5. After the release is published, add it to the APT repository: run the `apt` workflow of
   [platmon.github.io](https://github.com/platmon/platmon.github.io) with the tag
   (`gh workflow run apt -R platmon/platmon.github.io -f tags=v<version>`) and approve the
   `apt` environment. It re-checks the published files before signing the new index.

The repository's active `release-tags` tag ruleset targets `refs/tags/v*`: new tags
are allowed, but existing tags cannot be updated or deleted, with no bypass actors.
Use a new application version or packaging revision instead of moving a release tag.
The desired configuration is tracked in `.github/rulesets/release-tags.json`;
changing that file alone does not change GitHub settings. Apply it through the repository
ruleset API and confirm the live settings. The workflow's `main` check runs after a tag
push; it prevents release creation rather than rejecting the original tag push.

The signing key is not part of this repository or its release workflow. Releases currently use
stable MAJOR.MINOR.PATCH versions only.

To run the dpkg lifecycle check locally, use Docker and a disposable Ubuntu container:

```sh
platmon_version=$(python3 scripts/packaging/package.py --version)
platmon_arch=$(dpkg --print-architecture)
platmon_dist="dist/${platmon_version}-lifecycle"
python3 scripts/packaging/package.py --output "$platmon_dist/first"
python3 scripts/packaging/package.py --revision 2 --output "$platmon_dist/second"
python3 scripts/packaging/check_package_install.py \
  "$platmon_dist/first/platmon_${platmon_version}-1_${platmon_arch}.deb" \
  "$platmon_dist/second/platmon_${platmon_version}-2_${platmon_arch}.deb"
```

This installs test OS tools inside the container when its image lacks them, mounts only the artifact
directories read-only and removes the container afterward. CI installs them once per job instead:
`scripts/packaging/prepare_test_image.py ubuntu:<version> platmon-package-test:local` builds a local image
(never pushed) and retries a mirror in the middle of a sync, and every check gets `--image
platmon-package-test:local`, so the checks themselves do not use the Ubuntu mirrors. It checks config preservation,
disabled/masked state, downgrade, remove/purge, source-unit refusal and invalid-config
recovery. It does not boot systemd or change host services. CI runs the same check.

The service check boots systemd in a disposable container and upgrades from the last Python
release (download `platmon_1.3.0-1_all.deb` and check it against that release's `SHA256SUMS`):

```sh
python3 scripts/packaging/check_package_service.py platmon_1.3.0-1_all.deb \
  "$platmon_dist/second/platmon_${platmon_version}-2_${platmon_arch}.deb"
```

It checks that the upgrade keeps an edited INI and runs the native server in the same unit
with one process, that a start that fails (the port held) fails configuration and installing
the previous package recovers, that a disabled service is not started, that a refused INI stops
the upgrade before the restart, and that after the container restarts the enabled service is
back. The container gets `CAP_SYS_ADMIN` without seccomp/AppArmor confinement, which systemd's
sandboxing needs, never `--privileged`; it removes the image's `policy-rc.d` (a host has none).
It restarts the unit faster than systemd's start limit allows, so it clears that count
(`systemctl reset-failed`) before each step. A container's boot is not a device's reboot.
