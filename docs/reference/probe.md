# TCP connect probe

A low-frequency group ([observations.md](observations.md)) named `probe`: the time to open a TCP
connection to each configured target, every `[probe] interval` seconds (10 by default). This is the only
**active** feature: it sends packets. It is **enabled by default**, probes nothing without configured targets,
and never picks a target by itself (no gateway, DNS server or public address).

```json
"probe": {
  "data": {
    "provider": "tcp_connect",
    "timeout_ms": 2000.0,
    "targets": [
      {"address": "192.0.2.10", "port": 443, "connect_ms": 12.3, "reason": null,
       "attempts": 10, "failures": 1, "failure_ratio": 0.1}
    ]
  }
}
```

## What is measured

`connect_ms` is the time from starting a non-blocking `connect()` until the kernel reports the connection
established: the TCP handshake (SYN → SYN-ACK) plus however long the target takes to accept. It is **not**
an ICMP echo round trip ("ping") and is never shown as one; there is no fallback to another protocol. The
connection is closed at once; nothing is sent over it. No name is resolved: targets are IP literals, so DNS
time never mixes in.

- `reason` (with `connect_ms: null`): `timeout` (no answer within `timeout`), `refused` (the target sent a
  reset: reachable, nothing listening; it is still a failure), `unreachable`, `error`. A failure is never
  shown as a time, and a first failure is not 0 ms.
- `attempts`, `failures`, `failure_ratio`: over the target's last 10 attempts (fewer right after the start).
  This is the share of failed connection attempts in that window, not packet loss on the path.
- `timeout_ms`: the configured timeout.

## Control and limits

- Targets come only from the config read at start; the HTTP API cannot add, change or trigger a probe
  (no endpoint takes a target, port or command, and no GET starts one).
- At most 4 targets. They are probed one after another with one socket at a time, so at most one attempt
  is outstanding; each socket is closed before the next attempt, so a late answer cannot be matched to a
  later attempt.
- `interval` must be longer than `timeout × number of targets`, so one observation always ends before the
  next is due. The probe runs on its group's own thread; a slow or unreachable target never delays the core
  collection or HTTP answers.
- No raw sockets, no capabilities, no external tools: standard TCP sockets of an unprivileged process.

## Configuration

```ini
[probe]
enabled = yes
interval = 10       ; seconds, 1 to 3600
timeout = 2         ; seconds per attempt, 0.1 to 10
targets =           ; e.g. 192.0.2.10:443, [2001:db8::1]:22 (IP literals, at most 4)
```

With no targets, Probe stays idle: no worker, observation group or connection is created.
Set targets and restart to begin probing; `enabled = no` disables it even with targets configured.
A host name, an IPv6 address without brackets, a duplicate, more than 4 targets, or an interval
not longer than timeout × targets are refused at start.

Shown on the web page ("TCP connect") and in the `platmon` command and `/text` (`TCP` lines) with the
observation's age.

## Verification

Tested against listeners on this machine's loopback (connect time, refused) and with fakes (timeout,
window, order). It has not been run against real remote targets: none was approved, so its behaviour and
cost on a real network path are unmeasured.

## Not included

ICMP echo, UDP probes, name resolution, per-target intervals, configuration over HTTP, history.
