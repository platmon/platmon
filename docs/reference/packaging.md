# Packaging and releases

The primary distribution is a Debian package for Ubuntu and Debian-derived Linux hosts
(including supported Jetson and Raspberry Pi systems). It uses the host's Python 3.9+
and systemd; it bundles neither Python nor OS/NVIDIA binaries. Runtime Python remains
standard-library only. `Architecture: all` means the files are architecture-independent,
not that every OS/kernel has been validated. A device whose Python is older than 3.9
needs a supported OS/interpreter before installation.

## Versioning

`VERSION` in `frontends/cli.py` is the single source of the application version. The
server imports it; the builder reads that assignment without importing the collector.
The copied CLI stays one independent file. Check versions with:

```sh
platmon --version
python3 /usr/share/platmon/platmon.py --version
dpkg-query -W platmon
```

Use MAJOR.MINOR.PATCH: patch for compatible fixes, minor for compatible features,
major for breaking changes. From 1.0.0 this covers the HTTP API, the `platmon.ini` keys and
the `platmon` command's options. API `schema_version` remains a separate contract; a
release bump does not change it. Update `CHANGELOG.md` in the same change as `VERSION`.

The first release is `1.0.0` (a `v0.1.0` tag exists from packaging validation and was never
published). Debian package versions append a packaging revision: `1.0.0-1`. A packaging-only rebuild can use `-2` without
changing the application version. Never replace an already released version's files.

## Build

Build on Linux with Python 3.9+ and the installed `dpkg-deb` tool (dpkg 1.19+).
No Python build dependency or root access is needed:

```sh
platmon_version=$(python3 scripts/packaging/package.py --version)
platmon_dist="dist/${platmon_version}-1"
python3 scripts/packaging/package.py --output "$platmon_dist"
python3 scripts/packaging/check_package.py "$platmon_dist/platmon_${platmon_version}-1_all.deb"
lintian --no-cfg --info --pedantic --fail-on error,warning "$platmon_dist/platmon_${platmon_version}-1_all.deb"
(cd "$platmon_dist" && sha256sum -c SHA256SUMS)
```

The fresh output directory contains `platmon_<version>-1_all.deb`, `platmon-<version>.tar.gz`, `BUILD.json`
(input content hashes and versions) and `SHA256SUMS`. The source archive includes
runtime, tests, installation/build scripts and documentation, excluding caches and
private validation artifacts. It can rebuild the package without `.git`.
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

## Install and update

Download a fixed release, verify its checksums and keep its files for rollback.
Install the local package using APT, which checks the declared host dependencies. For a
checkout build, use the variables from the Build section:

```sh
sudo apt install "./${platmon_dist}/platmon_${platmon_version}-1_all.deb"
systemctl status platmon --no-pager
platmon --version
platmon --once
```

For a downloaded release, replace the package path with the exact verified `.deb` file,
including that release's application version and packaging revision.

Code goes to `/usr/share/platmon/`, the client to `/usr/bin/platmon`, the vendor unit
to `/usr/lib/systemd/system/platmon.service`, and config to `/etc/platmon/platmon.ini`.
The CLI manual is available with `man platmon`. Compressed documentation and
changelogs are in `/usr/share/doc/platmon/`; `copyright` references the system's
Apache-2.0 license. Package metadata includes `md5sums` for non-conffile contents
for tools such as `debsums`; release artifact verification uses `SHA256SUMS`.
The code keeps its existing directory structure and relative web paths. The service
retains DynamicUser, ProtectSystem, ProtectHome and NoNewPrivileges. Logs:
`journalctl -u platmon`. Configure the listener and restart with
`sudo systemctl restart platmon`; `platmon --once host:port` follows a custom port.

The INI is a dpkg conffile. Locally edited settings are preserved unless you explicitly
choose the maintainer's replacement at a dpkg prompt. For unattended updates, choose
your conffile policy explicitly; do not assume a prompt is answered automatically.
Existing explicit `enabled = no` values remain effective. Omitted options follow the
new version's defaults, so read its changelog before updating.

Upgrades validate the preserved INI before restarting the service. They preserve an
administrator's disabled/masked service state and respect Debian's service-start policy.
A config or restart failure leaves dpkg reporting the failure; it is not an automatic
rollback. Check `/api/status` ready and `/api/stats` 200 after starting, and check the
expected readings on the actual device. The health check is not evidence that every
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

1. Update the single VERSION and changelog. Review the scoped change and run
   `git diff --check`, `pytest -v`, the build, package checker and Lintian.
2. Test fresh install, upgrade with an edited INI, downgrade, remove/purge and existing
   installation migration. CI uses an isolated container for dpkg lifecycle checks;
   actual systemd sandbox/startup and ARM device readings require real-device acceptance.
3. Merge the reviewed change into `main` using the repository's signing/hooks policy.
   Tag the verified commit included in `main` as `v<version>` matching VERSION; use `v<version>-2` for packaging
   revision 2. Confirm the intended commit and required checks before pushing the tag.
   Creating a local tag alone does not start the workflow. This document does not create a tag.
4. The release workflow fetches `main` and rejects tags whose commit is not an ancestor
   of its current tip, before building. It verifies tag/version/changelog, runs tests and package checks,
   then creates a **draft** GitHub Release with the four artifacts. Review the attached
   bytes, acceptance evidence and notes before publishing the draft. Do not distribute
   a container image; the repository's existing image licensing policy still applies.

The repository's active `release-tags` tag ruleset targets `refs/tags/v*`: new tags
are allowed, but existing tags cannot be updated or deleted, with no bypass actors.
Use a new application version or packaging revision instead of moving a release tag.
The desired configuration is tracked in `.github/rulesets/release-tags.json`;
changing that file alone does not change GitHub settings. Apply it through the repository
ruleset API and confirm the live settings. The workflow's `main` check runs after a tag
push; it prevents release creation rather than rejecting the original tag push.

APT hosting is a later operational choice; no APT repository or signing key is created
by these scripts. Releases currently use stable MAJOR.MINOR.PATCH versions only.

To run the dpkg lifecycle check locally, use Docker and a disposable Ubuntu container:

```sh
platmon_version=$(python3 scripts/packaging/package.py --version)
platmon_dist="dist/${platmon_version}-lifecycle"
python3 scripts/packaging/package.py --output "$platmon_dist/first"
python3 scripts/packaging/package.py --revision 2 --output "$platmon_dist/second"
python3 scripts/packaging/check_package_install.py \
  "$platmon_dist/first/platmon_${platmon_version}-1_all.deb" \
  "$platmon_dist/second/platmon_${platmon_version}-2_all.deb"
```

This installs test OS tools inside the container, mounts only the artifact directories
read-only and removes the container afterward. It checks config preservation,
disabled/masked state, downgrade, remove/purge, source-unit refusal and invalid-config
recovery. It does not boot systemd or change host services. CI runs the same check.
