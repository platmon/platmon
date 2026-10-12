# Pressure stall information (PSI)

`/api/stats` has a `pressure` object while `[pressure] enabled = yes`. It is **on by default**. It holds
the kernel's pressure stall information from `/proc/pressure/{cpu,memory,io}`, read once per collection
by the collector thread. Format and meaning: the kernel's Documentation/accounting/psi.rst.

```json
"pressure": {
  "scope": {"kind": "host"},
  "provider": "proc_pressure",
  "resources": {
    "cpu":    {"some": {"avg10": 1.25, "avg60": 0.5, "avg300": 0.1, "total_us": 123456}, "full": null},
    "memory": {"some": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total_us": 10},
               "full": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total_us": 4}},
    "io":     {"some": {"avg10": 3.0, "avg60": 1.0, "avg300": 0.2, "total_us": 99999},
               "full": {"avg10": 0.5, "avg60": 0.1, "avg300": 0.0, "total_us": 4567}}
  },
  "read": {"started_offset_ms": 12.0, "completed_offset_ms": 12.3}
}
```

(The numbers show the format; they are not a measurement.)

## Fields

- `some`: the share of time in which at least some tasks were stalled on the resource. `full`: the share
  in which all non-idle tasks were stalled at once (psi.rst). Neither is CPU usage or disk busy time: a busy
  CPU with nothing waiting has no pressure.
- `avg10`, `avg60`, `avg300`: those shares in percent over the last 10, 60 and 300 seconds, as the kernel
  computes them. `total_us`: the "total absolute stall time (in us)" the kernel exports, as read. platmon
  does not compute rates from it.
- `cpu.full` is `null`: the kernel reports it since 5.13 but calls it undefined at the system level and
  sets it to zero; it is not a measured 0. Kernels without a `full` line give `null` for it too.
- `scope.kind` is `host`: `/proc/pressure` is system-wide (per-cgroup pressure files are not read).
- `resources` lists the readable files only; one that cannot be read is left out and reported.

## Diagnostics and freshness

- No `/proc/pressure` directory (PSI not built in, or disabled at boot, e.g. `CONFIG_PSI_DEFAULT_DISABLED`
  without `psi=1`): one directory check per collection, no file opened, `collectors.pressure`
  `unavailable` / `not_exposed`, `resources: {}`. Enabling PSI needs a kernel or boot change, which
  platmon never makes.
- A file that is missing is `not_exposed`; unreadable is `permission_denied` / `io_error`; a line that
  is not `some|full avg10=… avg60=… avg300=… total=…` is `invalid_data`. The other files are kept.
- `pressure.read` is one span from before the first read to after the last, and counts for the data age
  like `network.read` ([network.md](network.md#diagnostics-and-freshness)); with no file read it is `null`
  and does not move other values to the fallback.

Shown on the web page ("Pressure") and in the `platmon` command and `/text` (`PSI` line): `avg10` of
`some` and, where defined, `full`, per resource.

## Support (checked 2026-10-09)

| environment | kernel | PSI | result |
| --- | --- | --- | --- |
| Jetson Orin Nano, L4T R36 | 5.15-tegra | not built in (`CONFIG_PSI` not set) | `unavailable` / `not_exposed` |
| Raspberry Pi 4, Debian 13 | 6.18.50+rpt-rpi-v8 | built in; owner-approved `psi=1` applied and rebooted | CPU/memory/I/O collected, `ok` |
| WSL 2 | 6.6 | not built in | `unavailable` / `not_exposed` |
| CI host (GitHub Actions, Ubuntu) | as provided | whatever the runner has | `tests/collector/test_pressure.py::test_real_host_pressure` reads it when present (see the CI log) |

The normal path is covered by fixtures, supported CI hosts, and a real Raspberry Pi read after the
owner-approved boot-option change on 2026-10-09. All three `/proc/pressure` files and the API were
verified after the boot ID changed. The original boot command line was backed up before adding
`psi=1`; the Orin kernel is unchanged and has no PSI support. This is functional validation, not
a normal-path ARM performance benchmark.

## Cost

Measured only in a tight loop on WSL, not as an ARM off/on comparison: the unsupported path (one directory
check) takes about 1.8 µs per collection; parsing and building the output for three files, without the
file reads, about 6 µs. Neither is the cost of the normal path on a board, which is unmeasured.
The default is now on by user policy; unavailable kernels retain their explicit unsupported result,
and this does not establish a normal-path ARM performance result.

## Not included

Per-cgroup pressure, `/proc/pressure/irq`, triggers (poll-based notifications), rates from `total_us`,
history.
