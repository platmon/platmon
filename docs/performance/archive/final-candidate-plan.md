# Final candidate validation plan (2026-10-09)

The owner authorized necessary merges after the resumed report. Candidate runtime starts at
`fcc29d09c7c475c4170662b869bf2db7dc4f8a78`; the exact archived commit and every input hash are
recorded before measurement. Collector, service, CLI and entrypoint match `e919baf`, while this
candidate includes the reconciled history-age repaint and improved service benchmark.

## Scope and fixed budget

- Both ARM boards, separate directory and loopback port 19869; original production remains `8525942`.
- At most 60 minutes per board, one experiment round. No optimization or threshold increase.
- Eight service windows, default/all/all/default twice, 30 s warmup + 120 s each. Default has
  Network and Disk enabled. All adds Storage 30 s, Wi-Fi 5 s, history 600 s and a 10 s TCP probe
  to this candidate's loopback listener. PSI stays disabled because these kernels do not support it.
- One 1830 s retention run (30 s warmup + 1800 s measurement): at least two full 600 s turnover
  periods after initial filling. Stats every 1 s, observations/history every 10 s; final full history.
- Whole service CPU must remain <=2% of one core in every default window. Report all-on separately.
  Failures, stale drops, observed overruns and freshness fallback must be zero; report sampled
  sequence coverage honestly. No sustained post-fill RSS/PSS trend is allowed: compare successive
  600 s windows, endpoint changes and regression slopes, without claiming indefinite stability.
- Inspect history identities, gaps, bounds, response size, worker exit and listener cleanup.
- Reuse verified independent Disk and low-frequency call measurements: runtime paths unchanged.
  Storage/Wi-Fi still miss the 2 ms elapsed p95 target and remain off.
- After CPU windows, isolated Orin compose build and endpoint/scope checks, no registry push.
  Preserve prior images. Pi native evidence and read-only systemd unit verification; actual
  DynamicUser execution remains subject to available privilege, no production unit changes.
- Final web page: one 660 s Chromium soak on a local isolated service at 1280/390 px, plus
  existing fixture/gap/escaping tests. Browser load never overlaps ARM CPU windows.

Earlier `e919baf` service windows are retained as historical evidence. An independent Wi-Fi
validation briefly overlapped their beginning, so the original two-block matrix is not the final
uncontended performance gate. The earlier 660 s retention remains valid but is too short to
establish a post-fill memory plateau.

All inputs, raw output, timestamps, process identities, before/after production state and SHA-256
manifests remain in private `.validation/final-rc-evidence/` and `~/platmon-final-rc-<sha>/`.
Stop and record a limitation on budget failure or competing validation; do not silently repeat.
PSI support, remote TCP, production deployment, tags and releases remain outside this run.
