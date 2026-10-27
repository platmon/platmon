# Post-integration stabilization evidence (2026-10-09)

> **TL;DR**
> - Historical checkpoint for PR #45 (later folded into #46); kept for provenance, not current status.
> - For later evidence see [remaining work](remaining-work.md) and the [resumed
>   results](../performance/archive/resumed-integration-results.md).

Historical checkpoint for PR #45. Its changes are now incorporated into PR #46; consult
[remaining-work.md](remaining-work.md) and [resumed results](../performance/archive/resumed-integration-results.md)
for the later ARM, Storage/Wi-Fi and container evidence. Earlier pending entries below are retained
for provenance and are not the latest status.


Three of seven requested stages are complete. The local History and integration checks are complete;
ARM retention, changed Wi-Fi cost, final integrated performance and deployment-shape checks remain
open. This is a review candidate, **not release approval**. No main merge, tag, release, registry push
or production rollout was performed.

## Scope and exact revisions

| target | original revision | current reviewed revision | disposition |
| --- | --- | --- | --- |
| main, already merged #32–#39 | `e1e7c9f38bfac83e9844ddb8110a9eae08a7fb5a` | unchanged at audit start | baseline only |
| #40, frontend lifecycle | `33ada21301d53eb9f3a2babfb19b5567896adfcc` | `ee3142247093b95780a6e881232075c2b8264531` | Ready; added race tests, runtime unchanged |
| #41, Disk classifier | `1eae3a4bd5ddc6395d6c9c871e6af60b61041f27` | `2aa7d793df552688ae39aced58020fe4c2ae551e` | Ready; current ARM results documented |
| #42, History | `0589fc4f4fe2f93493956fc625c8da5651090e3f` | `9555c235d999c45fd5899f4a48d1e77e099e44e1` | Draft; ARM gates remain |
| isolated integration | `c5393411be7ea6c06f757fbee8f8b64705df2b5b` | branch `integration/post-integration-rc` | the three PRs plus validation tooling |

#42 remains based on `fix/disk-classification-identity`. The #41 documentation update was merged
normally into #42 (`56c5421`); its diff against #41 contains only History/Wi-Fi identity changes and
History display validation. No Disk commits were copied or counted as History work. #40's request
ownership and #42's advancing History age were combined in signed merge `e8eb788`. No force-push or
signature/hook bypass was used.

The shared checkout was being changed concurrently by another session. All this work uses separate
Git worktrees under `.validation/`. Concurrent PRs #43 (storage) and #44 (Wi-Fi connection state), and
their `integration/resumed-release-readiness` branch, are **not part of this three-PR candidate**.
Their measurements cannot establish the performance of this candidate. They also need reconciliation
before selecting a broader release candidate.

## Completed checks

| check | evidence |
| --- | --- |
| #40 full local suite | Python 3.12.3: 643 passed / 3 skipped; focused CLI/web 68 passed |
| #40 current CI | Python 3.9: 645 / 1; Python 3.13: 644 / 2 passed/skipped; runs 37894343027, 37894347514 |
| #41 full local suite | 643 passed / 3 skipped |
| #41 current CI | Python 3.9: 645 / 1; Python 3.13: 644 / 2; runs 37894971969, 37894975594 |
| #42 after display fix | full local 655 passed / 3 skipped; focused History/Wi-Fi/web 53 passed |
| #42 current CI | Python 3.9: 657 / 1; Python 3.13: 656 / 2; runs 37895515176, 37895520260 |
| integrated code `ebf6509` | local 659 passed / 3 skipped; Python 3.9 CI 661 / 1, Python 3.13 660 / 2 (37895800355) |
| web scripts | `node tests/frontends/web_harness.js frontends/web/index.html`, `web_optional_harness.js`, `web_history_age_harness.js`: pass |
| whitespace | `git diff --check`: pass for each change |
| browser | real Chromium 153, 1280 px and 390 px; both original #40 and combined History UI pass |
| Pi systemd | `systemd-analyze verify` on the archived unit: pass; no installed unit changed or started |

WSL native subprocess smoke also passed default, explicit Network/Disk off, all optional groups and
restart configurations (one-second collection/polling, one-second warmup plus 12 seconds per smoke).
Stats/observations return 200, disabled History returns expected 404, enabled History returns 200;
instance IDs agree across endpoints and change on restart. Measured publication IDs are unique,
series/response point caps hold, loopback probes succeed, and each PID and listener is gone afterwards.
These short functional smokes are not performance windows or ARM support evidence. Their first attempt
exposed the benchmark TIME_WAIT check below; the corrected rerun passed all four configurations.

Local skips are two unavailable profiling capabilities and WSL's missing normal PSI path. Hosted CI
executes the real PSI read test; this is not ARM PSI support. The test suite covers fixed endpoint
paths and 200/400/404/503 contracts, independent publication IDs, duplicate/out-of-order rejection,
missing/reappearing devices, elapsed gaps, concurrent History reads/appends, stopped/failed collection,
identity bounds and old API/INI compatibility. These deterministic fixtures are distinct from real
device support claims.

Browser fixtures include PSI, Storage, Wi-Fi, escaped sensor/fan/interface labels, actual rate windows,
packets/s and Disk `in_flight=0`. History has 64 series with 120 points and frequent null gaps: at most
12 graph rows render, an omission notice is shown, gaps break paths and 120 repeated renders do not
grow the DOM. Both layouts have no horizontal overflow or injected HTML. Real five-second timeout,
503 recovery and a synthetic tab-return event in Chromium pass. Node separately controls body
completion after abort, epoch invalidation, ownership until `finally`, and permanent 404 polling stop.
This is not a long-duration browser or real Wi-Fi/PSI sensor test.

One reproducible defect was fixed: #42's new latest-point age stayed at 2 seconds after four elapsed
seconds without another response. `9555c23` advances it using browser elapsed time and skips hidden-tab
rendering. The regression fails before the fix and passes after it. Collection/API code is unchanged.

Synthetic maximum History validation (64 series × 3602 points with gaps, one 600-point read/JSON):
21,667,030 retained bytes, 27,937,318 peak bytes, 900,411-byte series JSON. These meet the 32/64 MiB
allocation targets and preserve newest points/nulls. The earlier 22,505,990 / 28,765,190-byte result is
retained in [history.md](../reference/history.md). Neither result is daemon RSS or ARM retention evidence.

## ARM evidence and open gates

The completed Disk experiment was reused after auditing its raw data, exact SHA, eight valid windows
per board, two ABBA blocks, scope equality and production state. Network stays on in both arms;
Disk is the only off/on difference. Collection/client cadence is 1 s, warmup 30 s, measurement 120 s.

| classifier at `1eae3a4` | Orin Nano | Raspberry Pi 4 |
| --- | --- | --- |
| Disk on mean CPU, one core | 1.709% | 1.614% |
| largest Disk on window | 1.717% | 1.634% |
| Disk elapsed p95 | 1.263 ms | 1.555 ms |
| added collector CPU per collection | 1.188 ms | 1.982 ms |
| added HTTP CPU per request | 0.026 ms | 0.115 ms |

The 2.0%/2 ms criteria are met separately on both boards. Errors, overruns, freshness fallbacks and
HTTP/client failures are zero. One Orin on-window had a 1560 KiB RSS step, not repeated in the next
on-window; all other Orin windows and every Pi window had at most 8 KiB second-half growth. Full data,
limitations and SHA-256 values: [disk-io-product.md](../performance/disk-io-product.md).

A new attempt at `f96a98f` was started in `~/platmon-stabilize-f96a98f/` on both boards. During its
Wi-Fi phase, another session started an overlapping suite in `~/platmon-resume-e919baf/` (port 19797).
Only this task's runner and Wi-Fi PIDs were terminated. The partial attempt is **invalid and unused**;
retention and its queued integrated ABBA windows were not reached. Source, plan, before/interrupted
state, logs and `INTERRUPTED-SHA256SUMS` remain in each directory. The other session's jobs were not
stopped. Their first overlapping windows also need that overlap disclosed when they are assessed.

Current missing evidence:

- Real ARM namespace/ifindex verification and changed Wi-Fi observation cost for #42.
- A 600-second retention filled on each board, followed by RSS stability and worker cleanup checks.
- Two ABBA blocks of the final combined code: default Network/Disk on; separate supported opt-in
  configuration; real HTTP policies, whole-process CPU/RSS, errors, freshness and overruns.
- Orin's isolated Docker build/compose run and actual namespace/sensor/Disk/Storage scope comparison.
- Pi candidate native run and isolated systemd execution (unit syntax alone does not prove execution).

The benchmark helper now preserves status, per-status request counts, optional endpoint payloads,
RSS samples, History bounds, independent IDs and process/port cleanup. It refuses occupied listeners;
`2d85a57` fixes a reproduced false refusal of a closed port in TIME_WAIT, with a same-port restart test.
`a2a4b76` records observed collection durations/overruns, missing publication sequences and logged
collection failures separately. A missing-sequence result limits overrun coverage; this API observer
must not be presented as full per-attempt instrumentation. No ARM result is attributed to this revised
helper yet.

## Defaults, production and rollback

Network and Disk remain default on. Storage, Wi-Fi, PSI, Probe and History remain off. The historical
Storage/Wi-Fi p95 misses are preserved; no budget was raised. ARM PSI normal path remains unsupported,
and no kernel change or reboot was attempted. Probe testing uses loopback only; remote latency/error/
cost remains unverified without an approved target.

Production remains `85259427f20ba09c9541c52970243fc07645d21d`. Orin's container
`95d0fe7048f2962c9073913a110d1a804206ac8ccd83e64e7f2bc19cd8c6b3b7` and Pi PID 45459/start time are
unchanged in the captured states, as are their INI, instance IDs and power/governor settings. Orin's
container is healthy. All original images and rollback directories remain. Existing rollback details
are retained in [remaining-work.md](remaining-work.md#rollouts) and
[release-readiness.md](../releases/readiness.md#upgrade-and-rollout-for-after-approval-not-done).

Local raw evidence is under `.validation/post-integration-evidence/` and `.validation/pr40-browser/`.
No private state dump is committed. Release selection must reconcile the pending board work and other
session before approving merge/deployment. Passing #40/#41 review gates does not waive those gates.
