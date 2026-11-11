# Security policy

> **TL;DR**
> - Report a vulnerability privately with GitHub's **Report a vulnerability** button
>   ([new advisory](https://github.com/platmon/platmon/security/advisories/new)), not in a public issue.
> - Fixes are made for the latest release only.
> - The HTTP API is read-only and has no authentication by design: anyone who can reach the port can read
>   the device's readings. That is documented behavior; what goes beyond it is a vulnerability.

## Supported versions

| Version | Security fixes |
| --- | --- |
| The latest release | yes |
| Older releases | no: upgrade to the latest release |

The APT repository's `stable` suite always carries the latest release.

## Reporting a vulnerability

Use [private vulnerability reporting](https://github.com/platmon/platmon/security/advisories/new). Please
include:

- the version (`/usr/lib/platmon/platmon-server --version`) and how it was installed (APT, release
  file, source, Docker);
- the device, OS release, kernel and architecture;
- what you changed in `platmon.ini`, if anything;
- the steps or the request that show the problem, and what an attacker gains.

platmon is a personal project maintained in spare time. Expect a first answer within about a week. A
confirmed vulnerability is fixed in a new release first and described in a published advisory afterwards;
reporters are credited unless they ask not to be.

Do not test against devices you do not own or operate.

## What the service exposes

The service answers HTTP `GET` only. No request changes the host, starts a collection or runs a command;
settings come from `platmon.ini` alone.

There is **no authentication and no TLS**. By default the service listens on `0.0.0.0:9797` (IPv4).
Whoever can reach that port can read:

- the host name, board model, OS release, kernel version, architecture and uptime;
- CPU, GPU/NPU and memory use, pressure, temperatures, power rails, fans and the power mode;
- interface names with their counters and rates, disk names with their I/O, file systems with their mount
  points and capacity;
- battery state, the connected Wi-Fi interface's signal, and the configured probe targets with their
  connect times;
- with `[processes] enabled = yes` (off by default): process names, PIDs and systemd unit names.

Not collected or served: command lines, environment variables, file contents, IP or MAC addresses, SSIDs,
serial numbers.

The packaged service runs as a systemd dynamic user with `ProtectSystem=strict`, `ProtectHome=yes` and
`NoNewPrivileges=yes`. The server accepts at most 32 connections, gives a request 10 s in all, and limits
a line to 64 KiB and a request to 100 headers.

## What counts as a vulnerability

- A crash, hang or memory-safety problem caused by a network request or by the contents of `/proc` or
  `/sys` (the server aborts on a panic, which also ends collection).
- Reading a file outside the web page's allowlist, or any path that makes the service write, execute or
  change host state.
- Serving information beyond the list above.
- Script injection in the web page or the terminal client through a name the device reports (host name,
  interface, sensor, file system, process).
- A weakness in how packages are built, signed or published (the APT key, `SHA256SUMS`, build
  provenance).
- An escape from the systemd unit's sandbox.

Not a vulnerability: that an unauthenticated reader on a reachable network can read the readings above.
Restrict who can reach the port instead ([below](#hardening)).

## Known limitations

<!-- Keep this list current: remove an entry when the issue that fixes it is released. -->

- A client that holds 32 idle connections blocks other clients until the 10 s request limit ends them;
  collection is not affected.

## Hardening

- Listen on one address: `[http] bind = 127.0.0.1` (this host only, reach the page through an SSH tunnel)
  or the address of a trusted interface. See
  [Choosing what is served](docs/guides/deployment.md#choosing-what-is-served).
- Serve the API without the page: `[http] web = no`.
- Limit the port with the host's firewall, or put a reverse proxy that adds TLS and authentication in
  front of a loopback listener.
- Leave `[processes]` off unless the readers of the API may see process names.
- Verify what you install: the APT key's fingerprint
  ([getting started](docs/guides/getting-started.md#requirements-and-installation)), `SHA256SUMS`, and
  the build provenance (`gh attestation verify`,
  [packaging](docs/reference/packaging.md)).
