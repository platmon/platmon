# Processes

> **TL;DR**
> - The `processes` observation: the busiest processes by CPU and by memory, and the host's process
>   counts, from `/proc/<pid>/stat` every 5 s by default. **Off by default.**
> - Names only (`comm` and the cgroup unit), never command lines, environment or owners; anyone who can
>   reach the API sees them.
> - Linux only. Not kept in `/api/history`; kept in replay like every group.

A low-frequency group ([observations.md](observations.md)) named `processes`. Each observation reads
`/proc/<pid>/stat` of every process, keeps the `top` busiest by CPU and the `top` largest by memory (the
union, at most 2 × `top` rows), and counts the processes by state. It runs on its own thread every
`[processes] interval` seconds (5 by default, 2 to 3600), so its cost never delays the core collection.

**Off by default** (`[processes] enabled = yes` turns it on): it has been measured on a Jetson Orin Nano
and an x86 PC, not on a Raspberry Pi ([design and measurements](../development/processes-design.md)).

```json
"processes": {
  "data": {
    "scope": "host",
    "total": 512, "running": 1, "blocked": 0, "zombie": 0, "threads": 2036,
    "top": 10,
    "processes": [
      {"pid": 6696, "name": "gnome-shell", "kernel": false, "state": "S", "cpu_percent": 5.2,
       "rss_bytes": 317444096, "threads": 26, "unit": "org.gnome.Shell@ubuntu.service"}
    ]
  }
}
```

## Fields

| Field | Meaning |
| --- | --- |
| `scope` | `host` when the service runs in the host's PID namespace; `container` when it has its own (Docker without `pid: host`): then only the container's processes are listed. `null` if unknown |
| `total` | processes listed in `/proc` that could be read in this observation |
| `running`, `blocked`, `zombie` | processes in state `R`, `D` (uninterruptible sleep, usually waiting for storage) and `Z` |
| `threads` | the processes' threads added up (`stat` field 20) |
| `top` | the configured rows per ranking |
| `processes` | the rows, by `cpu_percent` (highest first), then `rss_bytes`, then `pid` |

Each row:

| Field | Meaning |
| --- | --- |
| `pid` | process id |
| `name` | `comm`, the kernel's name of the process (at most 15 bytes for most processes; longer for kernel threads), bytes that are not UTF-8 replaced by U+FFFD |
| `kernel` | true for a kernel thread (`PF_KTHREAD`); they are listed, since a busy one is a real answer |
| `state` | the one-letter state of `stat` (`R`, `S`, `D`, `Z`, `T`, `t`, `I`, ...) |
| `cpu_percent` | user + system CPU since the previous observation, in **% of one core** (a process using two cores is near 200). For a process the previous observation did not list: since it started. `null` in the first observation after a start, or after `/proc` could not be listed. With 100 Hz ticks and 5 s, the resolution is 0.2 % |
| `rss_bytes` | resident memory: `VmRSS` of `/proc/<pid>/status`, as `ps` shows it; `stat`'s count where `status` has none. Shared pages count in every process that maps them; PSS needs privileges platmon does not have |
| `threads` | the process's threads |
| `unit` | the last part of its cgroup v2 path (`ssh.service`, `session-3.scope`); `null` at the root, in a container's own root, or on cgroup v1 alone |

Selection: the rows ranked by CPU use `cpu_percent`; the rows ranked by memory use `stat`'s resident count,
which recent kernels keep approximately (by up to a few MiB per process); a row's `rss_bytes` is the exact
`VmRSS`. A process that starts and exits between two observations is never listed: the host's CPU in
`/api/stats` counts it.

## Diagnostics

The group's `collector` follows the C1 rules ([api.md](api.md#optional-metrics)):

- A process that exits between the listing and its read is skipped silently: that is not a problem.
- `partial` / `permission_denied` on `/proc` when it is mounted with `hidepid` (or systemd's
  `ProtectProc=invisible`): only the processes this user may see are listed. A process whose `stat`
  cannot be read for another reason is a `partial` issue (`<pid>/stat`), and one that is not in the
  `stat` shape is `invalid_data`.
- `error` when `/proc` cannot be listed; the next observation starts the CPU numbers again (`null`).

## Configuration

```ini
[processes]
enabled = no        ; yes to observe
interval = 5        ; seconds between observations, 2 to 3600
top = 10            ; rows per ranking, 1 to 50
```

Cost, one observation on a Jetson Orin Nano with about 300 processes: about 8.4 ms of CPU, so about
0.17 % of one core at 5 s; it grows with the number of processes
([measurements](../development/processes-design.md)).

## Where it is shown

The JSON API only, at `/api/observations` and in replay (`/api/observation-samples?group=processes`). The
web page, `/text` and the `platmon` command do not show it yet. Not in `/api/history`: process ids come and
go, and the history's series slots are already full on some hosts.

platmon-hub 1.1.1 accepts the group and ignores it: it stores only the groups it knows.

## Containers

The Compose files do not share the host's PID namespace, so in the container the group lists the
container's own processes and says `scope: container`. Sharing it (`pid: host`) lists the host's
processes; it also lets the container see every host process's name, which is why it is not in the
shipped Compose files.

## Windows

Not available: the group is not started on Windows, even when enabled.

## Not included

Command lines (arguments can hold passwords and tokens, and the API has no authentication), environment,
owners, per-process I/O and PSS (not readable as the service's unprivileged user), open files, sockets,
process trees, sorting or filtering by request, and acting on a process.
