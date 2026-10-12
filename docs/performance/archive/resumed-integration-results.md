# Resumed integrated results (2026-10-09)

Measured product/benchmark commit: `e919baf72a5c5100dd5468188fcc97b119b287f6`. Source hash `4912055833f36dcd`, benchmark hash `5dcc5ddcf361cef8`. [Predeclared plan](resumed-integration-plan.md). The later reconciliation of #45 changes web display, tests, bench reporting and documentation; collector, service and CLI runtime are identical. These measurements use the older benchmark named here, not its later diagnostics.

## Qualification after concurrent-run audit

The original two-block CPU matrix overlaps an independent Wi-Fi validation near its beginning
(the interrupted run's start records are 15:41 KST). Preserve these raw values, but do not use
the complete matrix as uncontended final-candidate evidence. Low-frequency call measurements
and retention occurred later. A new bounded final run is declared in
[final-candidate-plan.md](final-candidate-plan.md); its results will be recorded separately.

## Service CPU

Native, 1 s collector and stats client; observations/history requests every 10 s. Each window: 30 s warmup + 120 s measurement. Default means Network and Disk on, other groups off. All means Storage 30 s, Wi-Fi 5 s, unsupported PSI, history 600 s and TCP connect 10 s to the isolated service loopback listener. Process user+system CPU includes all worker threads; no helper service process. The client is excluded.

| Board | Default CPU % (run order) | All CPU % | Block deltas pp |
| --- | --- | --- | --- |
| orin | 1.592, 1.600, 1.645, 1.595 | 1.817, 1.876, 1.843, 1.860 | +0.251, +0.231 |
| pi | 1.592, 1.537, 1.583, 1.600 | 1.760, 1.950, 1.909, 1.926 | +0.291, +0.326 |

Every default window meets ≤2.0% of one core. The all-on windows also meet 2%, but include unsupported PSI and loopback TCP only; this is not a normal-PSI or remote-RTT cost result. All optional features remain off. Storage/Wi-Fi fail the independent call p95 criterion below. No budget was increased.

All 16 processes exited 0; stats (120–121 requests/window), observations and enabled history had zero HTTP errors. Disabled history returned the expected 404, counted as an error by this older bench. Maximum stats latency: 5.175 ms Orin / 5.657 ms Pi; history 25.817 / 37.601 ms. Mean response sizes per window: stats 11.23–11.43 KB / 5.05–5.26 KB; all-on observations 4.01 KB / 2.56 KB; history 76.95–79.74 KB / 31.30–32.32 KB.

This service tool records process CPU and endpoint latency, not `_attempt`/`do_GET` thread CPU or every overrun/drop. The [Disk reference matrix](../disk-io-product.md) supplies that accounting for the default collection path. Do not infer zero missed events in these integrated windows from HTTP success.

## Low-frequency calls

| Board/group | Completed calls / cadence | Mean thread CPU ms | Elapsed p95 ms | State |
| --- | --- | --- | --- | --- |
| orin/storage | 20 / 30 s | 2.868 | 3.212 | ok |
| orin/wifi | 60 / 5 s | 1.580 | 3.365 | ok |
| pi/storage | 20 / 30 s | 3.089 | 3.223 | ok |
| pi/wifi | 60 / 5 s | 2.205 | 7.871 | ok |

All four elapsed p95 values exceed 2 ms. These are 20 storage and 60 Wi-Fi samples at actual cadence, not tight loops or an assurance of long-term tail behavior. Storage found two filesystems on each board and 15/2 partitions. Wi-Fi reported one interface with verified carrier and a negative dBm sample on each board. No scan, SSID/BSSID collection, new permission or reconnect. No additional optimization round.

## Retention and memory

Each board ran a separate 660 s all-on service: 67 status/history/memory observations, zero checker errors, normal exit and listener closed. History filled its 600 s window; measured publication IDs were unique and response points stayed ≤600 per series. Final observation IDs: Storage 23, Wi-Fi 133, probe 67, reflecting independent cadence rather than HTTP polls.

| Board | RSS first / at 600 s / final KiB | PSS first / at 600 s / final KiB | Final full-history JSON bytes |
| --- | --- | --- | --- |
| orin | 21704 / 28868 / 28984 | 13805 / 20968 / 21084 | 547203 |
| pi | 24696 / 26992 / 27296 | 15740 / 18036 / 18340 | 221247 |

RSS grows while the ring fills and increases another 116 KiB / 304 KiB during the final minute. This finite run verifies retention turnover, **not a long-term memory plateau**. Longer repeated turnover remains unverified; the 50-minute board budget was not extended. Synthetic maximum allocation (64 series × 3602 points) retained 22,505,990 bytes and peaked at 28,765,190 bytes during read/JSON, under the documented 32/64 MiB targets; this is allocator evidence, not process RSS.

## Environment and operational preservation

Orin: Python 3.10.12, Linux 5.15.199-tegra, schedutil. Pi: Python 3.13.5, Linux 6.18.50+rpt-rpi-v8, ondemand. No PSI files on either board. Both suites exited 0 within the predeclared 50-minute cap (about 45.6 minutes including state collection). No CPU/power/cooling changes.

Structured before/after comparison matched production HEAD `85259427f20ba09c9541c52970243fc07645d21d`, clean checkout, INI hash, kernel, governors, instance ID and ready health. Orin container ID/image/mounts/restart count/PID/start time matched; Pi PID/start ticks/cwd matched. The recorder itself has a different PID. Production was not restarted or modified.

## Evidence and reproduction

Private sources, INIs, `suite.sh`, `run-integration.sh`, `retention_check.py`, state records and raw remain on each board in `~/platmon-resume-e919baf/`; local ignored archives are in `.validation/20261009-resume/`. Public summaries omit addresses and interface identities. Run commands are recorded in the plan and scripts in those archives. Preserve original archives: tar metadata means repacking changes archive hashes.

| Board | Archive SHA-256 | Retention JSON SHA-256 |
| --- | --- | --- |
| orin | `8f37617ba69e703384ea246e8649c72214d389f95bff4ba3dc4cfbd52d64ff2c` | `970405d2b9838195480e5215253920fefe478a8f9aaf482231cce329eb6b9d2a` |
| pi | `b3c465c606c1dfe3dcf6c50e196db5ddd6168d8beae5db1c7a0cde19d1dc4b07` | `f9b97e5d85356029c10c5fade91ae0309a1ad1b79ccb53233d3a54193f453825` |
