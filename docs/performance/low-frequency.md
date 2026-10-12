# Low-frequency groups and history: cost

What the opt-in groups of [../observations.md](../reference/observations.md) (storage, Wi-Fi, TCP connect probe) and
the recent history ([../history.md](../reference/history.md)) cost, judged by [budget.md](budget.md). All of them are
**off by default**, so none of this enters the default configuration's 2.0 % target.

Post-integration audit: the new #42 observation identity/gap implementation still needs real ARM
retention and changed Wi-Fi cost evidence. Its cached latest-point age fix is in `9555c23`. A synthetic
64 × 3602-point test with frequent gaps retained 21,667,030 bytes and peaked at 27,937,318 bytes including
one 600-point read/JSON (900,411 bytes for the series object). This is Python 3.12 allocation accounting,
**not ARM RSS**. The original 22.5/28.8 MB synthetic result remains in [history.md](../reference/history.md).
The new physical-board attempt was interrupted because another suite overlapped; no result from that
attempt is counted. See [current validation gates](../development/post-integration-validation.md).
All measured costs below remain attached to their original SHAs; Storage/Wi-Fi p95 misses are unchanged.

A group observed every 30 s gives 4 observations per 120 s window, too few for an off/on process
comparison. Each observation is therefore timed itself, on the product path
(`Slow.observe_once` → the group's observe function), at the real cadence and at a shorter one for more
samples, with `benchmarks/slow_cost.py` on each board, natively, in its own process.

## Result (2026-10-09)

| | Orin Nano | Raspberry Pi 4 |
| --- | --- | --- |
| storage, per observation (`9ea8af1`, 30 s × 20): thread CPU mean / elapsed p95 | 2.80 / 3.08 ms | 3.10 / 3.25 ms |
| storage at 5 s × 60 | 2.92 / 3.11 ms | 2.68 / 3.43 ms |
| storage share of one core at 30 s (mean CPU ÷ 30 s, calculated) | 0.009 pp | 0.010 pp |
| Wi-Fi, per observation (`9ea8af1`, 5 s × 60): thread CPU mean / elapsed p95 | 0.57 / 2.23 ms | 0.81 / 6.42 ms |
| Wi-Fi share of one core at 5 s (calculated) | 0.011 pp | 0.016 pp |
| call elapsed p95 ≤ 2 ms (budget's responsiveness criterion) | **not met** (storage 3.1, Wi-Fi 2.2 ms) | **not met** (storage 3.3–3.4, Wi-Fi 6.4 ms) |

- The average cost is small: about 0.01 pp of one core each at their default cadences.
- The per-call criterion is not met by either group on either board. Storage is CPU work in Python (one
  pass over mountinfo, statvfs); Wi-Fi is mostly waiting: its elapsed time is 3–8 times its CPU time, the
  kernel asking the wireless driver for station data. Both run on their own group thread, so neither
  delays the 1 s core collection or HTTP answers (tested with a blocked group,
  `tests/collector/test_storage.py::test_http_answers_while_a_group_is_blocked_and_stale`); the criterion is recorded
  as not met, not waived. Both stay off by default.
- Storage had one optimization round with a concrete cause (round 1, `eda772f`: every mountinfo line was
  unescaped and converted; round 2 converts only local disk filesystem lines): thread CPU mean 3.07 →
  2.80 ms (Orin), 3.56 → 3.10 ms (Raspberry Pi) at 30 s. No further round.
- The TCP connect probe was only run against loopback (0.3–0.7 ms connect on the boards); with no approved
  remote target its cost and behaviour on a real path are unmeasured.
- History: at its bound (61 series of the integrated data × 602 points, retention 600 s at 1 s) it holds
  3.5 MiB (`tracemalloc`, WSL); 64 series is the limit. A `/api/history?points=120` answer for that data is
  143 KiB, `points=600` 424 KiB; the web page asks for 120 points every 10 s.

The integrated service (all groups and history on, polled like the web page) is in
[../release-readiness.md](../releases/readiness.md).

## Runs

| board | round, SHA | group, period × count | thread CPU mean / p50 / p95 ms | elapsed p95 / max ms | found |
| --- | --- | --- | --- | --- | --- |
| orin | 1 `eda772f` | storage 30 s × 20 | 3.07 / 3.43 / 3.47 | 3.47 / 3.48 | 2 filesystems, 15 partitions |
| orin | 1 | storage 5 s × 60 | 3.33 / 3.47 / 3.55 | 3.59 / 3.81 | |
| orin | 2 `9ea8af1` | storage 30 s × 20 | 2.80 / 3.02 / 3.08 | 3.08 / 3.40 | |
| orin | 2 | storage 5 s × 60 | 2.92 / 3.02 / 3.10 | 3.11 / 3.33 | |
| orin | 2 | wifi 5 s × 60 | 0.57 / 0.55 / 0.67 | 2.23 / 2.71 | 1 interface, connected |
| rpi4 | 1 `eda772f` | storage 30 s × 20 | 3.56 / 3.80 / 3.94 | 3.94 / 3.99 | 2 filesystems, 2 partitions |
| rpi4 | 1 | storage 5 s × 60 | 3.83 / 3.90 / 3.97 | 3.97 / 4.01 | |
| rpi4 | 2 `9ea8af1` | storage 30 s × 20 | 3.10 / 3.19 / 3.25 | 3.25 / 3.28 | |
| rpi4 | 2 | storage 5 s × 60 | 2.68 / 3.04 / 3.44 | 3.43 / 3.55 | |
| rpi4 | 2 | wifi 5 s × 60 | 0.81 / 0.86 / 1.05 | 6.42 / 6.50 | 1 interface, connected |

Every observation's state was `ok`. Python 3.10.12 (Orin), 3.13.5 (Raspberry Pi). The boards' own services
(`e1bc82a`) kept running untouched; for round 2 a full state record written on each device before and after
the runs is the same (round 1: a record before only). Raw files stay on the devices in
`~/platmon-storagebench-eda772f/` and `~/platmon-obsbench-9ea8af1/`:

| file | SHA-256 |
| --- | --- |
| orin round 1 storage-30s.json / storage-5s.json | `8e1f9a58…` / `7594d550…` |
| rpi4 round 1 storage-30s.json / storage-5s.json | `ac0d60bc…` / `255ef1e3…` |
| orin round 2 storage-30s / storage-5s / wifi-5s | `bafda063…` / `32b7921d…` / `cd27f805…` |
| rpi4 round 2 storage-30s / storage-5s / wifi-5s | `242718b8…` / `52a77b3d…` / `cf7b08d7…` |

## Resumed combined path

The [2026-10-09 resumed results](archive/resumed-integration-results.md) supersede earlier cost claims for
Storage descriptor verification, Wi-Fi carrier checks and low-frequency history. Runtime `e919baf`:
20 Storage calls at 30 s and 60 Wi-Fi calls at 5 s per board; 660 s real history retention plus two
integrated ABBA blocks. All remain optional. The finite retention run does not prove a long-term
RSS plateau. Old results above remain historical and are not relabeled as the corrected path.
