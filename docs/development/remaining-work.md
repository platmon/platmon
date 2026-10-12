# Remaining work

The latest status updates (container visibility and Pi PSI, default enablement policy, 2026-10-09) and
the release evidence are kept in one place: [release readiness](../releases/readiness.md). This file tracks the
staged plan, checkpoints, rollouts and device records.

Status of the staged plan (Disk I/O → UI → storage capacity → PSI → Wi-Fi → RTT → history → release
readiness). Checked against the repository and devices whenever work resumes. Statuses: done, in
progress, waiting for validation, blocked (access), waiting for approval. Merging, production rollout
and releases always wait for the owner's approval.

Last update: 2026-10-11 (deferred work after the battery group; the staged plan below is unchanged).
Earlier update: 2026-10-09. Resume baseline: `e1e7c9f38bfac83e9844ddb8110a9eae08a7fb5a`
(`origin/main`, #32–#39 already merged before this resumed task). The table below records historical
implementation; merging alone does **not** complete the expanded validation requirements.

## Final candidate checkpoint — aggregate merge

The owner authorized necessary merges on 2026-10-09. #40, #43, #41, #42 and #44 are merged;
component main is `e3ff8bbd565fd8d080e104fe7cfac02f6a97a2e8`. No production rollout was performed.
Six of the seven requested stages are complete; stage 7 has its reports/PRs prepared and closes
with [#46's merge and merged-main CI](https://github.com/jwsung91/platmon/pull/46). This is the
pre-merge checkpoint; the PR records the final aggregate merge SHA and CI outcome.

| Requested stage | Completed evidence / disposition |
| --- | --- |
| 1 Current state | latest remote/board work audited; existing raw reused only where applicable |
| 2 Frontend #40 | request lifecycle/age/escaping regressions, Node, actual 1280/390 Chromium, 661 s final-page soak |
| 3 Disk #41 | independent current-classifier ARM ABBA twice; CPU/p95 pass; main merge verified |
| 4 History #42 | independent IDs, gaps, strict expiry, native Wi-Fi identity, bounded 1830 s ARM retention and UI integration |
| 5 Local integration | 676 passed / 3 skipped; Python 3.9 CI 678/1, Python 3.13 CI 677/2; compatibility/concurrency/API/resource bounds |
| 6 Final ARM/deployment forms | both default CPU matrices pass; native cleanup and actual isolated Orin compose complete; systemd static-only and unsupported paths disclosed |
| 7 Documentation/PR delivery | final report and component PR bodies updated; final aggregate disposition is linked above |

Final measured runtime: `aac7ed796faf2be0e387312c04a78925198fe11d`. The
[complete results and hashes](../performance/history-expiry-validation.md) distinguish it from
preserved pre-correction `30d377a` and `65bcc39` measurements. Two reproduced History defects were
fixed: a single failed core attempt hiding a graph gap, and expired slow observations remaining
internally until the point cap filled. Focused 53/53 and failing-before regressions cover both.

Default CPU: Orin 1.600–1.678%, Pi 1.529–1.583%, every window <=2%. Optional Orin short window
2.174% and long all-on 2.079%/2.019% exceed 2%; Storage/Wi-Fi call p95 also exceeds 2 ms. These are
not passes, no window was discarded and no threshold was raised. Network/Disk stay on; all other
groups stay off. Final-period RSS settles with the documented small page variations; this finite
observation is not an indefinite plateau claim. Every native candidate exits and releases its port.

Orin compose is healthy under UID 65534, read-only rootfs/host binds, cap-drop and no-new-privileges.
Network is container-only; host-visible sensors and Disk are available, Storage file-bind capacity
is null/partial, Wi-Fi unavailable, and one startup loopback Probe failure is retained. Its test
container/network/port are reclaimed, its local image is kept, and all 12 prior images/35 rollback
files remain. Pi has no matching rollback directory in the recorded search scope.

Production remains `85259427f20ba09c9541c52970243fc07645d21d` on both boards, with before/after
identity/configuration/governor/power records unchanged. Actual DynamicUser execution is unavailable
without administrator authentication (unit static verification passed); normal ARM PSI and remote
TCP paths remain unvalidated. No production, kernel, power, tag, Release or registry operation.
These limits remain explicit; they do not authorize privilege workarounds or a default-on change.

PR #45's complete ancestry and validation work are preserved in #46; it must not be merged again
as a separate aggregate. Raw/state/CI/scripts/screenshots remain in `.validation/expiry-rc-evidence/`
and earlier evidence directories, with durable `~/platmon-expiry-rc-aac7ed7/` copies on both boards.
No further performance round is scheduled. Deployment requires its own fixed-SHA backup/approval.

## Historical checkpoint — resumed integration

The eight requested stages have **4/8 completed within the documented support scope** (1, 2, 3, 5).
Stages 4 and 6 have implementation/automated checks but environment or target validation gaps;
stage 7 has working retention and bounded-allocation evidence but insufficient real post-fill duration
for long-term stability; stage 8 remains Draft. No stage count includes merge or deployment.

Main is unchanged at `e1e7c9f38bfac83e9844ddb8110a9eae08a7fb5a`. Production on both boards is
`85259427f20ba09c9541c52970243fc07645d21d`, verified before/after the experiments. The original
#32–#39 were already merged when this task resumed. They were reused, not rebuilt.

| Stage | Current state | Follow-up PR / branch / verified code HEAD | Evidence / remaining limit |
| --- | --- | --- | --- |
| 1 Disk | complete, review ready | #41 `fix/disk-classification-identity` / `2aa7d793df552688ae39aced58020fe4c2ae551e` | measured `1eae3a4`, 8 windows per ARM, CPU and p95 pass; default on |
| 2 UI | complete, review ready | #40 `fix/frontend-observation-contract` / `ee3142247093b95780a6e881232075c2b8264531` | bounded polling, age, units; actual Chromium 1280/390, XSS, 503, timeout, synthetic visibility event |
| 3 Storage | complete for directory mounts, review ready | #43 `fix/storage-mount-race` / `e0dee298f6c50af9ec4c5e9adcdf5c6e3cf12ed5` | descriptor identity fix; ARM normal path; container file-bind capacities unavailable; off, call p95 fails |
| 4 PSI | normal ARM validation blocked by unsupported kernels | existing #35 in main | fixtures and hosted Linux normal path pass; both ARM/WSL lack pressure files; off |
| 5 Wi-Fi | complete for documented provider, review ready | #44 `fix/wifi-connection-state` / `ab875197e3708347f4031a25866126879f357a13` | carrier/ifindex validation; real negative dBm on both ARM; off, call p95 fails |
| 6 TCP probe | remote validation waiting for approved target | existing #37 in main | fake/deadline/error and own loopback checks; no arbitrary target; off |
| 7 History | implementation/tests complete; extended stability pending | #42 `feat/history-observation-contract` / `9555c235d999c45fd5899f4a48d1e77e099e44e1` | independent low-frequency append, gaps, strict limits; 660 s ARM retention; only 60 s after fill, no long-term plateau claim; off |
| 8 Integration | Draft; systemd access blocked | #46 `integration/resumed-release-readiness` | measured `e919baf72a5c5100dd5468188fcc97b119b287f6`; combined #45 follow-up at `2a0e209`; final documentation HEAD is the commit containing this checkpoint |

PR #45 (`integration/post-integration-rc`, `8df75e8`) was reconciled into #46, retaining its UI/tests/bench
improvements and this task's Storage/Wi-Fi fixes. Its earlier open-gate report is historical context;
[resumed results](../performance/archive/resumed-integration-results.md) supersede the pending ARM entries.
#42 depends on #41; #44 depends on #42; #46 includes #40–#45. Review component diffs against their PR
bases, not the full aggregate as a new feature. No force-push, main merge or commit rewrite.

Validation: final combined `pytest -v` **672 passed / 3 skipped** (Python 3.12); all Node harnesses run
through pytest; actual Chromium fixture passed after reconciliation. Before the display-only
reconciliation, #46 CI run 37898089620 passed 672/1 (3.9), 671/2 (3.13), including hosted real PSI and
Node. Final exact-HEAD CI is linked in PR #46; its original logs are retained privately. See the release
readiness page for the complete evidence boundaries and commands.

Private raw/state/CI/screenshots: `.validation/20261009-resume/`, and durable board directories
`~/platmon-diskbench-1eae3a4/` and `~/platmon-resume-e919baf/`. Both suites ended normally. Older raw and
rollback material remain intact. Operational records are private; public reports contain anonymized
summaries and hashes. No extra ARM performance rounds after the predeclared budget.

Next executable gate: isolated production-style systemd validation with existing administrator
authentication. `sudo -n true` requires a password on the available hosts; no credential request,
new capability or production-unit change is used to bypass it. Extended history stability needs a
separately recorded experiment budget. Remote TCP needs an owner-selected allowlisted target; PSI
normal ARM validation needs an already-supported environment, without reboot/kernel changes.

## Merge checkpoint (2026-10-09)

The owner authorized necessary merges. #40 is merged at `4b1bc080`; its lifecycle tests and
viewer fixes are preserved in this branch. #41 retains the independently measured classifier
`1eae3a4` and the completed two-block ARM results in the Disk report. This merge resolves
documentation only; classifier and measured collector code are unchanged. Integration #46
contains the combined History, Storage and Wi-Fi work and is undergoing final bounded validation.
Production rollout and release publication remain unapproved.

## Historical resumed work checkpoint

- Branch: `feat/history-observation-contract`, stacked on Disk PR #41 at
  `1eae3a4bd5ddc6395d6c9c871e6af60b61041f27`; history follow-up PR pending.
  UI follow-up: #40, `33ada21`, signed; Python 3.9 CI 645/1, Python 3.13 CI 644/2 passed/skipped
  on push and PR event (runs 37891815033, 37891851001); branch `fix/frontend-observation-contract`.
  Clean worktree at entry. No user changes were present.
- Stage 1: **in progress**. Three classification-cache regressions reproduced and fixed; focused
  Disk/Network/provenance tests: **179 passed**. New ARM measurement required (plan in disk report); raw SHA-256 prefixes for the integrated
  `c2752c1` runs checked on both boards and matched. No collection changes in the UI follow-up.
- Stage 2: in progress. Reproduced missing observation windows, hidden zero in-flight counters, duplicate
  hanging optional HTTP requests and cached observations not becoming stale. CLI/web fixes and regression
  tests added; `pytest -v`: **643 passed / 3 skipped**, focused CLI/web: **68 passed**; final CI pending. Real Chromium fixture checks cover 1280/390 px, PSI/Wi-Fi panels, escaped
  labels, 503, a real 5 s timeout and 120 repeated renders (not a long-duration stability claim).
- Stages 3–6: implementations retained; validation still required against the complete work order.
  PSI normal-path ARM support and approved external probe targets remain unavailable; defaults stay off.
- Stage 7: **in progress, not complete**. Existing history explicitly excludes low-frequency groups;
  observation-ID-based append, explicit missing/elapsed-gap markers, gap-preserving downsampling,
  CPU/slow graphs and elapsed spacing implemented here. Wi-Fi gets namespace/index identity; no signal
  unit change. Local full tests: 654 passed / 3 skipped including the additional graph test;
  CI, real browser and real-time board retention verification pending. Maximum configuration synthetic
  tracemalloc: 22,505,990 bytes retained, 28,765,190 bytes peak with one read/JSON; targets 32/64 MiB.
- Stage 8: **waiting for validation**. Container/systemd isolation, board history retention, the second
  integrated ABBA block, and final exact-HEAD CI/measurement reconciliation remain outstanding.
- Devices rechecked over SSH: both production checkouts are `8525942`; Orin's original container and
  Pi's original process are running. No production modifications. Private state and local validation
  logs are in `.validation/20261009-resume/` (ignored), with original ARM raw still on the boards.
- Main CI run `37890446587`: Python 3.9 **642 passed / 1 skipped**, Python 3.13 **641 passed / 2 skipped**;
  Node-backed web tests and real hosted-runner PSI tests executed. These are baseline, not follow-up CI.
- Local baseline: `pytest -v`, Python 3.12, **640 passed / 3 skipped** with loopback allowed. The initial
  sandbox run failed on denied sockets; no assertions were weakened to accommodate the sandbox.
- Disk measurement is running in each board's `~/platmon-diskbench-1eae3a4/`, 8 windows (do not restart).
  Next: finalize the history PR and validate the integrated tree with #40.
  Nothing in this resumed task authorizes merging, production deployment or release publication.

## Historical implementation

| # | Stage | Status | PR | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Disk I/O product | merged; default on | #32 | Orin/Pi off/on rounds `60962e3`, `e184f8c` ([disk-io-product.md](../performance/disk-io-product.md)) |
| 2 | Network / Disk I/O in CLI, web, `/text` | merged | #33 | Chromium desktop/narrow screenshots |
| 3 | Storage capacity, `/api/observations` | merged; off (call p95 above 2 ms) | #34 | per-observation cost, 2 rounds ([low-frequency.md](../performance/low-frequency.md)) |
| 4 | PSI | merged; off (normal path not verifiable on the boards) | #35 | CI real `/proc/pressure` read on the hosted runner |
| 5 | Wi-Fi signal (passive) | merged; off (elapsed p95 above 2 ms) | #36 | Orin/Pi functional and cost |
| 6 | TCP connect probe | merged; off; no approved remote targets | #37 | loopback and fakes only |
| 7 | Recent history | merged; off | #38 | memory bound, integrated runs |
| 8 | Integration, release readiness | merged with this page | #39 | integrated service on Orin/Pi ([release-readiness.md](../releases/readiness.md)) |

Not rolled out: #32–#38 (the devices run `8525942`, #31). Rolling them out is a separate decision.

## Deferred after the battery group (2026-10-11)

Left out of `feat(collector): report batteries and external power supplies` on purpose, recorded here so
the reasons do not have to be rediscovered. None of them is a defect in what shipped.

| Item | What it would take | Why it waited |
| --- | --- | --- |
| A battery level in `[thresholds]` | `DEFAULTS["thresholds"]`, `levels()` and its range check, the CLI's `LIMITS`, the web page's `LIMITS` — and an inverted comparison | For a battery the **low** reading is the bad one, the opposite of every level `level()` compares, so it is not a new row in an existing table but a second kind of threshold. The viewers show the charge without a warning colour until that is designed |
| `temp`, `time_to_empty_now`, `time_to_full_now` | Three more standard `power_supply` attributes in `collector/battery.py`; absent ones already cost nothing | `temp` is a temperature and `/api/stats` already has a `temperature` table, so where it belongs is a design question, not a field to add. The two time estimates are a driver's own extrapolation, not a reading |
| What reading a battery costs the battery | A power measurement on a battery-powered host, with the group off and on | It is the reason the default interval is 60 s rather than the 10 s floor, and it is the one number behind that choice that is not measured |
| Battery in a fleet server's own history | `platmon-hub`: its history projection lists the groups it stores (`storage`, `wifi`, `probe`), so the group is validated, shown on its dashboard and not persisted | Work in that repository, not this one. Nothing here blocks it: the group is accepted by its validator as it is |

### A battery behind an external device

A host can have its charge reported by a separate device — a power board, a controller — rather than by
the kernel's class, with another program on the host already reading that device. Such a source would sit
ahead of `/sys/class/power_supply` in the same group, chosen the way the GPU's sources already are
([api.md](../reference/api.md), "Where one value has several sources"), with `provider` saying which one answered
and the field model this group defines staying as it is.

It would stay a source platmon **reads**, not one that pushes to it. Three reasons:

- A pushed value has an age platmon never observed. `Sampler._check` requires every number in a snapshot
  to carry a read span from that collection and falls the whole snapshot back to
  `cycle_start_upper_bound` otherwise; a group's `data_age_ms` makes the same promise for its own data.
- The HTTP frontend is read-only GET. A write path asks who may write, and platmon has no answer: an
  injected charge level is a safety-adjacent falsehood on a vehicle, and authentication is not in scope.
- `collector/jetson.py` already reads another program's file (`/var/lib/nvpmodel/status`), so the pattern
  exists and needs no new mechanism.

Deferred until a second real source exists, so that the format is settled by the field model rather than
by one device's payload. The external device keeps whatever else it reports — motion, actuators, anything
else on its bus — out of platmon: only the fields this group defines would be read.

## Rollouts

- Orin: switched to `8525942` (#31) on 2026-10-09 14:09 KST at the owner's request. Healthy, `/api/status`
  ready, network collector ok in the container's namespace (`eth0`, `lo`), the same 20 source records; the
  service used 1.32 % of one core over 60 s without clients. Rollback to `e1bc82a`:
  `~/platmon-rollback-e1bc82a-20261009-114530/ROLLBACK.md` (image `platmon:rollback-e1bc82a`; SHA256SUMS verify).
- Raspberry Pi: fast-forwarded to `8525942` and restarted the same way it was started (detached
  `python3 platmon.py platmon.ini`) on 2026-10-09 14:20 KST at the owner's request. `/api/status` ready,
  network ok (`eth0`, `lo`, `wlan0`), the same 5 source records, 0.97 % of one core over 60 s without
  clients, `get_throttled` 0x0. Previous log: `~/platmon.log.e1bc82a`; to go back, `git switch --detach
  e1bc82a` in its checkout and restart the same way.

## Device records

Bench and state records stay on each device, not only in a session's temporary directory:
`~/platmon-netbench-<sha>/` (Network), `~/platmon-diskbench-<sha>/` (Disk I/O),
`~/platmon-storagebench-<sha>/` and `~/platmon-obsbench-<sha>/` (low-frequency groups),
`~/platmon-rc-<sha>/` (integrated service); the later ones with `ops-before.txt` / `ops-after.txt`.
None of them is deleted by this work; removing old ones is the owner's decision.

## Test commands

```bash
pytest                                        # all tests; CI runs the same on Python 3.9 and 3.13
node tests/frontends/web_harness.js frontends/web/index.html
git diff --check
```
