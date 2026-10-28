# ADR 0001: Ubuntu LTS and Python support policy

> **TL;DR**
> - Accepted 2026-10-17: Python 3.8 / Ubuntu 20.04 are CI observation targets only, not official support.
> - The package keeps its Python 3.9 minimum; Ubuntu 22.04, 24.04 and 26.04 are native support targets
>   subject to qualification.

- Date: 2026-10-17
- Status: Accepted; CI evidence and official support are separate
- Scope: platmon service, web viewer, terminal client and distribution artifacts

## Context

platmon uses the Python standard library and reads Linux and vendor-specific device interfaces.
Its native package requires Python 3.9 or later. Before this work, interpreter CI covered Python
3.9 and 3.13. Ubuntu 20.04, 22.04, 24.04 and 26.04 use Python 3.8, 3.10, 3.12 and 3.14 respectively.

Small compatibility changes allow Python 3.8 tests to run, but that result does not justify the
cost of qualifying and maintaining Ubuntu 20.04 devices. Containers test distribution userspace
against the host kernel; they do not qualify the target kernel, live systemd or vendor drivers.

## Decision

Keep Python 3.8 and Ubuntu 20.04 as CI observation targets, outside official support. Retain the
small compatibility fixes and automated checks to provide evidence about current behavior.
Do not promise future compatibility fixes, a maintenance period or native deployment support for
these observation targets. Do not lower the package's Python 3.9 minimum.

| Environment | Policy |
| --- | --- |
| Python 3.8 | Automated-test observation only; outside official support |
| Ubuntu 20.04 / Python 3.8 | Container userspace observation only; native deployment outside official support |
| Ubuntu 22.04 / Python 3.10 | Native support target, subject to OS and hardware qualification |
| Ubuntu 24.04 / Python 3.12 | Native support target and development baseline, subject to qualification |
| Ubuntu 26.04 / Python 3.14 | Native support target, subject to qualification |
| Existing Python 3.9 / 3.13 and Debian environments | Preserve existing support scope and validation |

An observation target is not a pending promise of official support. Adding Ubuntu 20.04 to official
support would require a separate decision supported by actual deployment needs and qualification
resources. This policy does not remove existing Python 3.9 or Debian support, and does not authorize
adopting Wirestead or changing the HTTP protocol.

### Evidence and public wording

Report CI results against a specific commit and link the successful run. Use the following wording
only when the corresponding jobs actually pass:

> Python 3.8 automated tests passed, and basic execution was verified in an Ubuntu 20.04 container.
> Native OS, systemd and hardware behavior remain unverified. Python 3.8 and Ubuntu 20.04 are not
> official support targets and carry no ongoing compatibility commitment.

A configured job, skipped job, failed dependency installation or unavailable runner is not a passing
check. Retain truthful pass/fail results; this decision does not mask failures or change check
requirements. Investigate observed failures before deciding whether the cost of retaining the
observation check is justified. Removing an observation target does not require the support-retirement
process below, but must be recorded in the changelog.

### Runtime and distribution

Maintain one codebase and a standard-library-only runtime. Development and package builds may use
an explicitly tested newer interpreter. Keep the native dependency at `python3 (>= 3.9)` and the
standalone CLI independent of collector modules and third-party Python packages.

Retain the architecture-independent Debian package while its contents remain architecture independent.
`Architecture: all` is not a claim of qualification on every CPU, kernel or board. Keep the container
interpreter independently pinned and tested; the current Python 3.13 container remains appropriate.

### Qualification for official support

Interpreter CI covers 3.8, 3.9, 3.10, 3.12, 3.13 and 3.14 with compatible test-tool versions.
Container smoke checks cover all four Ubuntu userspaces. Native package lifecycle checks cover
22.04, 24.04 and 26.04; 20.04's default Python does not satisfy the package dependency.

Before adding a new officially supported OS combination, verify native installation, upgrade,
removal, API/CLI behavior and service start/restart under systemd on a booted VM or device. Record
OS, kernel, Python and architecture. Qualify representative x86_64 and arm64 devices, and Jetson,
GPU/NPU or other vendor integrations separately. Missing hardware must retain documented unavailable
or error semantics without preventing unrelated metrics from working.

Application compatibility does not provide OS or interpreter security maintenance. Those remain
with the distribution and deployment operator. Interim Ubuntu releases and future Python versions
are not automatically qualified.

### Versioning and retirement

Extend the existing [release versioning contract](../../reference/packaging.md#versioning) to
advertised official OS and Python support:

- Patch releases preserve supported environments and fix compatible defects.
- Minor releases may add compatible features and newly qualified environments.
- Removing official support or raising the minimum Python requires a major release.
- Announce removal at least six months in advance with the final compatible version, migration
  path and any maintenance end date.

These commitments do not apply to CI-only observation targets. Review those checks when their
maintenance cost changes; no legacy branch or six-month review schedule is required for Ubuntu 20.04.

## Alternatives considered

| Alternative | Reason not selected |
| --- | --- |
| Official Python 3.8 / Ubuntu 20.04 support | Small code changes do not justify native OS, device and long-term maintenance obligations |
| Remove all 3.8 checks and compatibility fixes | Discards useful existing evidence and inexpensive fixes without a demonstrated conflict |
| Raise the minimum Python immediately | Would remove existing 3.9 support unnecessarily; handle any later increase as a separate major-version decision |
| Bundle Python or create a legacy branch | Adds distribution and maintenance work without a committed legacy deployment requirement |

## Consequences and implementation

The project can report verified CI compatibility without promising official legacy support.
The cost is additional CI time and occasional triage; Python 3.8 does not constrain future design
through an official maintenance commitment. Keep package prerequisites and public support claims
unchanged until a separate qualified change warrants updating them.

Track commit-specific CI results and native qualification gaps in the
[qualification record](../ubuntu-lts-qualification.md). No Ubuntu 20.04 native qualification or
Python 3.8 dependency reduction is scheduled by this decision.

## References

- [Ubuntu default Python versions](https://ubuntu.com/developers/docs/reference/availability/python/)
- [Ubuntu 26.04 LTS Python 3.14](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/)
- [Current packaging contract](../../reference/packaging.md)
- [CI configuration](../../../.github/workflows/test.yml)
