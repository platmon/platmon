# Ubuntu LTS qualification

> **TL;DR**
> - Progress of ADR 0001: CI now covers Python 3.8 to 3.14, with local interpreter and container evidence.
> - Still open for new official OS support: systemd runs on booted Ubuntu 22.04, 24.04 and 26.04 and
>   representative hardware; nothing new is advertised yet.

This record tracks implementation of [ADR 0001](adr/0001-ubuntu-lts-python-support.md).
Python 3.8 and Ubuntu 20.04 are CI observation targets, outside official support and without an
ongoing compatibility commitment. The native package continues to require Python 3.9 or later.
No minimum-version reduction or native Ubuntu 20.04 qualification is planned by this policy.

## Implementation

- Runtime prefix/suffix removal now works on Python 3.8 with the same matching behavior.
- Packaging supports Python 3.8's hashlib and symlink APIs. Package checksum tests use the independent
  `md5sum --check` command rather than the builder's hashing implementation.
- Interpreter CI covers 3.8, 3.9, 3.10, 3.12, 3.13 and 3.14. Test-only pytest versions are pinned
  in `tests/requirements.txt`; dependency resolver constraints select compatible transitive packages.
- Ubuntu userspace CI checks extracted packages on 20.04, 22.04, 24.04 and 26.04, and dpkg lifecycle
  on 22.04, 24.04 and 26.04. The 20.04 lifecycle check is excluded because its default interpreter
  does not satisfy the package dependency; dependency checks must not be bypassed.

## Local evidence on 2026-10-17

Host: Linux 7.0.0-38-generic, x86_64. Changes were tested in the working tree following ADR commit
`9b3d7ff`. Tests used temporary virtual environments with the pinned pytest versions.

| Check | Result |
| --- | --- |
| Python 3.8.20 full suite | 979 passed, 1 skipped, 1 failed in the checksum test's unsupported `usedforsecurity` keyword |
| Python 3.8.20 packaging suite after correcting that test | 38 passed; includes the previously failing test |
| Python 3.14.4 full suite | 979 passed, 2 skipped |
| Python 3.14.4 packaging suite after builder/checksum changes | 38 passed |
| Workflow YAML | Parsed successfully; hosted Actions execution pending |

The initial sandboxed 3.8 run could not create sockets. The full-suite results above are from the
subsequent run with loopback sockets allowed. The packaging reruns validate the final packaging
changes; the full suites were not repeated after the final test-only checksum correction.

Containers used each release's default `python3` package. They tested the same runtime payload,
built as `platmon_1.2.1-1_all.deb`, with revision `-2` for lifecycle checks. These are temporary
validation artifacts, not published releases. Later build-helper and documentation changes were
covered by the packaging suite; they do not alter the runtime payload tested in these containers.

| Container userspace | Python reported | Extracted server/API/web/CLI smoke | dpkg lifecycle |
| --- | --- | --- | --- |
| Ubuntu 20.04 | 3.8.10 | Passed | Outside scope: package requires Python 3.9 |
| Ubuntu 22.04 | 3.10.12 | Passed | Passed |
| Ubuntu 24.04 | 3.12.3 | Passed | Passed |
| Ubuntu 26.04 | 3.14.4 | Passed | Passed |

Lifecycle checks cover install, config-preserving upgrade and downgrade, disabled/masked state,
remove/purge, source-install migration refusal and invalid configuration recovery. They deliberately
do not run against a live systemd host. Extracted smoke checks start the Python server directly.

Reproduction entrypoints:

```sh
python -m pip install -r tests/requirements.txt
python -m pytest -q --tb=short
python -m pytest -q tests/packaging --tb=short
python3 scripts/packaging/package.py --output /tmp/platmon-first
python3 scripts/packaging/package.py --revision 2 --output /tmp/platmon-second
python3 scripts/packaging/check_package.py /tmp/platmon-first/platmon_1.2.1-1_all.deb
python3 scripts/packaging/check_package_install.py \
  /tmp/platmon-first/platmon_1.2.1-1_all.deb \
  /tmp/platmon-second/platmon_1.2.1-2_all.deb --image ubuntu:22.04
```

Use each intended interpreter for the Python commands and fresh output paths when rebuilding.
The workflow contains the read-only container mounts and default-interpreter smoke invocation.

## Hosted CI evidence

Commit `48a7a63723738dd67524fd9ccde6d924004942a7` passed all six interpreter jobs, packaging,
and all four Ubuntu userspace jobs in [PR run 39026693707](https://github.com/platmon/platmon/actions/runs/39026693707).
The [Python 3.8 job](https://github.com/platmon/platmon/actions/runs/39026693707/job/117378965348)
and [Ubuntu 20.04 job](https://github.com/platmon/platmon/actions/runs/39026693707/job/117379321326)
passed on 2026-10-17. This supersedes the earlier local CI-pending note, not its historical test results.
These results establish CI compatibility at that commit, not official OS or hardware support.
Later commits must be checked separately before claiming their CI passed.

## Remaining gates for new official OS support

1. Verify service start/restart and isolation under systemd on booted Ubuntu 22.04, 24.04 and 26.04 targets. Container
   userspace tests share the host kernel and do not qualify those targets' kernels.
2. Exercise representative x86_64 and arm64 hardware and record collector availability; qualify
   Jetson/vendor images separately. Local containers establish no new board support.
3. Publish only the combinations actually qualified. Any change to native Python requirements
   needs a separate decision; successful observation checks alone do not justify it.

No new native OS support is advertised by this intermediate implementation.
