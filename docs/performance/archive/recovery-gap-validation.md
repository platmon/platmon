# Recovery-gap correction validation (2026-10-09)

## Reproduction and scoped correction

Success, one failed core attempt, then success produced `[1001, 1003]` in history rather than
`[1001, null, 1003]`. The elapsed gap was shorter than the long-gap heuristic. Sampler's consecutive
failure count was reset before the next publication, so History could not identify this recovery.

#42 `00781fda6667234e088aa14107786be9e0a88532` carries one internal `gap_before` hint on the next
successful record. History adds a null break when a prior series exists, and the next uninterrupted
success adds no extra break. Failed attempts still publish no measured sample. Public stats/status
schemas, defaults, workers, collection calls and history bounds are unchanged. Focused tests: 50
passed; full component tests: 662 passed / 3 skipped. The regression failed before the correction.

## Predeclared corrected-runtime round

The previous single 60-minute round is kept as a baseline, including its 1830 s retention.
This additional round is justified by the reproduced correctness defect, not an optimization retry.
Do not overlap board runs. After the baseline runner finishes, archive the corrected integration
commit and hashes into a new directory `~/platmon-recovery-rc-<sha>/` on each board.

- One round, at most 60 minutes per board. No further automatic repeat or budget increase.
- Same ABBAABBA, 30 s warmup + 120 s/window, actual 1 s stats / 10 s optional API policy;
  default Network/Disk versus supported optional groups with own-loopback TCP only.
- Same 30 s warmup + 1800 s retention measurement, retention 600 s. Compare post-fill RSS/PSS
  across two additional 600 s periods, report slopes and point/identity bounds, no indefinite claim.
- Same default whole-process CPU <=2%, zero recorded failures/drops/fallbacks and no observed
  overruns. Explicitly retain sampled-timing coverage limits; do not relabel missing observations
  as collection failures or claim complete per-attempt timing.
- The unchanged Storage/Wi-Fi readers reuse their actual-cadence cost results and default-off
  p95 failure disclosure. The unchanged frontend reuses the final 661 s browser soak and fixtures.
- Only after native measurement, run the prepared Orin compose check with a unique project/image,
  loopback port, non-root/read-only/cap-drop/no-new-privileges configuration; preserve the image.
- Preserve raw, all before/after operational identities, original production `8525942` and rollback
  material. Never modify production units, power/governors, kernel, remote probe targets or registries.

Corrected integration full tests and Python 3.9/3.13 CI must pass before component merge. The measured
SHA and result hashes will be recorded below after completion. PSI and actual systemd privilege limits
remain disclosed. The earlier CPU/memory results are not mislabeled as measurements of this correction.

## Results — completed pre-expiry baseline

The run at `65bcc39621707b51fa701e90027bea3179bae6ad` completed normally on both boards at
19:02 KST, 2026-10-09 (3042 s each). Source hash `59bc9dc85387da88`, benchmark hash
`49a338fd5f082aa2`; archive SHA-256
`b657ce4e371352355a374891e6b61208c87163c134c944d9601d079a911caa57`.
The expiry defect reproduced during this run prevents a post-fill memory stability pass. These
results validate the recovery-gap candidate only; the [expiry correction](../history-expiry-validation.md)
is a separately measured runtime. The prepared old-code container build was not run.

| metric | Orin Nano | Raspberry Pi 4 |
| --- | --- | --- |
| default CPU, four windows (%) | 1.669, 1.592, 1.595, 1.633 | 1.625, 1.612, 1.525, 1.600 |
| all CPU, four windows (%) | 1.851, 1.826, 1.785, 1.818 | 1.934, 1.942, 1.909, 1.892 |
| all-on retention CPU (%) | 2.010553 | 2.017778 |
| final RSS / PSS (KiB) | 28620 / 20326 | 26580 / 16932 |
| history series / max returned points | 41 / 600 | 20 / 600 |
| retention observed sequence misses | 4 | 2 |
| maximum observed core duration (ms) | 57.728 | 70.463 |

Every default CPU window passes <=2%; long all-on CPU exceeds 2% on **both** boards and is not
relabeled as passing. Optional groups remain off. All runs have zero HTTP errors, logged core
failures/drops, diagnostics, stderr and observed overruns. All candidate processes exited normally
without forced kill and released their ports. Sampling misses are not collection failures and
prevent full per-attempt timing claims. The first Orin CPU window missed three intermediate
sequences; other short windows missed none.

Post-fill memory, measured from `smaps_rollup`, not process-list RSS:

| board / interval | RSS first → last (KiB) | min–max (KiB) | fitted KiB/min |
| --- | --- | --- | --- |
| orin / 600–1200 s | 28388 → 28564 | 28024–28564 | 20.002 |
| orin / 1200–1800 s | 28564 → 28616 | 28156–28616 | 21.463 |
| pi / 600–1200 s | 26440 → 26516 | 26440–26516 | 4.435 |
| pi / 1200–1800 s | 26520 → 26580 | 26520–26580 | 3.669 |

PSS follows the same pattern (approximately 8294 KiB lower on Orin and 9648 KiB lower on Pi).
Six threads remain constant. RSS still grows after filling; the independent fake-clock reproduction
identified retained expired slow points as a real cause of further allocation, without claiming it
explains every allocator/RSS fluctuation. The correction is covered separately.

Retention requests: Orin 1801 stats / 181 observations / 181 history; Pi 1800 / 180 / 180, all 200.
All saved observation records and history responses pass state, ID order, identity and request-bound
checks (744/738 group records and 248/246 history responses across the complete board suites).
Final Storage/Wi-Fi/Probe IDs: Orin 62/367/184; Pi 61/365/183, each state ok. Interface namespace and
ifindex match core Network; no duplicate measured IDs or series drops. Full-history JSON:
558789 / 229927 bytes. Public response bounds alone do not prove expired internal points were freed.

Before/after production SHA, INI, instance, native PID/start time, container/image/mounts/restarts,
kernel, governors and power mode match at `8525942`. No production setting was changed.

Raw remains in `~/platmon-recovery-rc-65bcc39/` on each board and locally in
`.validation/recovery-rc-evidence/`. All 6 input entries and 22/21 result entries were hash-verified.

| board | retention JSON SHA-256 | result manifest SHA-256 |
| --- | --- | --- |
| orin | `ad9071d3e3c7794d838dfbbf733ede97f36ca0f173f191210d9aaea540a571c5` | `26b043df8b9133e0c8acc1a9d92ddfb9db88ff79715f43968ced70ee5ae71b9d` |
| pi | `78f234606736f7683bc029e0a230da7f826eb88b04c514db5c8504f40190f840` | `5ade037575fdbdfedbc3f962aafcef0c8149fca1052bbdcd199c60e0b8efcf27` |

Shared input manifest SHA-256:
`552892bee87229cc0b1d0f3d310e5d6cc62e33c4c9cb6b99bbcc4980caff3988`.
