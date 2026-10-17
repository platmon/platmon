# ADR 0001: Ubuntu LTS and Python support policy

- Date: 2026-10-17
- Status: Accepted; platform qualification in progress
- Scope: platmon service, bundled web viewer, terminal client and distribution artifacts

## Context

platmon runs on existing Linux devices, including systems whose OS upgrades are constrained
by board drivers and vendor images. Requiring a separately installed Python increases deployment
and maintenance work on those devices. The intended Ubuntu coverage spans 20.04 through 26.04 LTS.

The runtime uses the Python standard library. Before this decision, the Debian package required Python 3.9 or
later, CI tested Python 3.9 and 3.13, and the Dockerfile used Python 3.13. Python 3.8 compatibility
requires changes, including replacing runtime uses of `str.removeprefix()` and `str.removesuffix()`.
Test and packaging helpers also contain Python 3.9 APIs and dictionary merge operations.
The current checks do not establish support for every target OS below.

## Decision

Support the default system Python on each named Ubuntu LTS release. Keep the runtime compatible
with Python 3.8, subject to the implementation and release gates below. Do not require users to
replace system Python or install an additional interpreter for native package deployment.

| Target OS | Default Python | Intended support tier | Ubuntu standard maintenance ends |
| --- | --- | --- | --- |
| Ubuntu 20.04 LTS | 3.8 | Existing-device compatibility | May 2025; now in ESM |
| Ubuntu 22.04 LTS | 3.10 | Full support | May 2027 |
| Ubuntu 24.04 LTS | 3.12 | Full support; development baseline | May 2029 |
| Ubuntu 26.04 LTS | 3.14 | Full support after qualification | May 2031 |

All rows are policy targets, not new validation claims. Publish an OS as supported only after its
release gates pass. The scope is these four LTS releases; interim releases and future Python
versions are not automatically qualified. This ADR does not remove existing Debian or Python 3.9
support. Maintain their existing validation while that support remains advertised.

### Runtime and distribution

Maintain one codebase and a standard-library-only runtime. Development and package builds may use
Python 3.12 or another explicitly tested newer interpreter; shipped runtime code must still pass
the minimum-version tests. Record build and test tool versions independently from runtime requirements.

Retain the architecture-independent Debian package while its contents remain architecture independent.
Lower its dependency to `python3 (>= 3.8)` only after qualification. `Architecture: all` does not
guarantee every CPU, kernel or board is supported. Keep the standalone CLI installable without
collector modules or third-party Python packages.

Keep the container interpreter independently pinned and tested. The current Python 3.13 container
does not need to move to 3.8. A container running a newer Python does not establish native Python
3.8 compatibility or remove host kernel and device constraints.

New dependencies must justify their maintenance and platform costs. An extension requiring a newer
runtime must either be isolated from the supported core or accompany an explicit support-policy
change. This ADR does not authorize adopting Wirestead or changing the HTTP protocol.

### Meaning of support

Both support tiers cover installation, service operation, HTTP API, web viewer, CLI and regressions
in available metric collectors. Existing-device compatibility prioritizes keeping these functions
working; it does not promise to backport new kernel interfaces or vendor capabilities.

Discover hardware capabilities at runtime. Missing sensors or interfaces must retain their documented
unavailable/error semantics rather than preventing unrelated metrics from working. Qualify Jetson,
Raspberry Pi, GPU and NPU behavior separately on representative hardware. An Ubuntu version alone
does not certify a vendor image or its drivers.

platmon maintains application compatibility. OS and interpreter security updates remain the responsibility
of the distribution and deployment operator. Python upstream end of life alone does not immediately
remove an Ubuntu target, but support does not imply that an unmaintained interpreter is secure.
Ubuntu 20.04 compatibility does not commit platmon to the full Ubuntu Pro/ESM lifecycle.

### Validation gates

| Gate | Required coverage |
| --- | --- |
| Pull requests | Full Python test suite on 3.8, 3.10, 3.12 and 3.14; retain 3.9 and 3.13 coverage for existing supported environments |
| Release packaging | Install, upgrade and remove the artifact on each named Ubuntu LTS using its default Python; verify service start and API/CLI smoke checks |
| Hardware qualification | Representative x86_64 and arm64 systems; record OS, kernel, Python, board/vendor image and collectors exercised |
| Container changes | Build and smoke-test the configured container interpreter and documented host mounts |

Pin compatible test-tool versions per interpreter, especially for Python 3.8. CI must distinguish
an unavailable runner or dependency installation failure from a passing compatibility check.

Use containers for package and userspace checks, and a booted VM or device for systemd and kernel
behavior. Containers share the host kernel and cannot establish coverage of another Ubuntu kernel.
Publish the tested combinations and remaining hardware limitations with release evidence.

### Versioning and retirement

Extend the existing [release versioning contract](../../reference/packaging.md#versioning) to cover
advertised OS and Python support:

- Patch releases preserve supported environments and fix compatible defects.
- Minor releases may add compatible features and newly qualified environments.
- Removing an advertised OS or raising the minimum Python requires a major release.
- Announce removal at least six months before its effective release, naming the final compatible
  platmon version, migration path and any maintenance end date for that version.

Review Ubuntu 20.04 usage, validation availability and compatibility costs every six months. Review
other targets when standard OS maintenance ends or a required dependency creates a concrete conflict.
Record the review in a dated follow-up ADR or release decision. A review does not automatically end
support. Do not create a separate legacy branch now; any later maintenance branch needs an explicit
scope and end date.

## Alternatives considered

| Alternative | Reason not selected |
| --- | --- |
| Keep Python 3.9 as the minimum | Does not cover Ubuntu 20.04's default Python and still requires an additional interpreter there |
| Raise the minimum to 3.10 or 3.12 | Simplifies access to newer APIs but excludes native default-interpreter deployments within the requested OS scope |
| Bundle Python or require containers everywhere | Adds runtime distribution and device-access responsibilities to a currently lightweight native package |
| Maintain a separate 20.04 branch immediately | Duplicates fixes and release work before a demonstrated architectural incompatibility |

## Consequences

Existing devices can use their OS-provided interpreter, and supported releases share one codebase
and package format. Newer OS releases receive explicit test coverage rather than assumed compatibility.

The costs are a wider CI matrix, older compatible test tooling, OS lifecycle checks and restraint in
using newer Python APIs. Future mandatory dependencies may require a deliberate major-version policy
change. No performance improvement is implied by lowering the runtime minimum.

## Implementation and acceptance

The policy is accepted. Acceptance does not itself change package metadata or public support claims;
platform qualification remains a separate release gate.

1. Replace incompatible runtime operations with equivalent Python 3.8 behavior. Adapt tests and
   packaging checks, and preserve regression coverage for the affected collectors and CLI.
2. Add the interpreter matrix and compatible test-tool pins; investigate failures on both 3.8 and 3.14.
3. Qualify native packages on the four Ubuntu releases, including booted service checks and
   representative device collection. Record exact environments and results.
4. Update the package dependency, installation guides, release policy and support matrix together
   once the applicable gates pass. Keep unqualified environments explicitly pending.
5. Track implementation evidence in the [qualification record](../ubuntu-lts-qualification.md)
   separately from policy acceptance, so acceptance is not confused with completed qualification.

## References

- [Ubuntu default Python versions](https://ubuntu.com/developers/docs/reference/availability/python/)
- [Ubuntu 26.04 LTS changes from 24.04, including Python 3.14](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/)
- [Ubuntu maintenance lifecycle](https://ubuntu.com/about/release-cycle)
- [Python 3.8 lifecycle](https://peps.python.org/pep-0569/)
- [Python 3.9 string prefix/suffix methods](https://peps.python.org/pep-0616/)
- [Current packaging and release contract](../../reference/packaging.md)
- [Current CI configuration](../../../.github/workflows/test.yml)
- [Current container runtime](../../../deploy/docker/Dockerfile)
