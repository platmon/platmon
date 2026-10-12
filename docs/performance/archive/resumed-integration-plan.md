# Resumed integration validation plan

Candidate runtime tree: `e919baf72a5c5100dd5468188fcc97b119b287f6`, combining #40 (`33ada21`),
#41 (`1eae3a4`), #42 (`0589fc4`), #43 (`e0dee29`) and #44 (`ab87519`). Neither main nor production
is updated. Documentation/evidence commits can follow without reusing measurements for changed runtime code.

Before execution: record the existing production checkout, INI hash, process/container identity, governor,
power state, kernel and health on each board. Keep raw, sources and tools in a fresh private board directory
and a local ignored `.validation/20261009-resume/` copy. Never delete older benchmark/rollback material.

## Experiment bounds and acceptance

- Native service, default Network + Disk on, 1 s collector and one 1 s stats client.
- Compare default optional-off to all optional-on (Storage 30 s, Wi-Fi 5 s, PSI, history 600 s,
  TCP connect 10 s to the **isolated service's loopback listener only**).
- Same product/bench tree for both arms, 30 s warmup + 120 s window, two default/all/all/default
  blocks per board: 8 windows, about 20 minutes. No other benchmark on that board concurrently.
- Use `benchmarks/service_cpu.py --page`: process user+system CPU/actual elapsed, RSS/PSS,
  bytes/count/errors/max request latency. This real-service tool does not instrument `_attempt` or
  `do_GET`; the separate Disk reference-instrumented matrix supplies default-path stage accounting.
- Follow with one 660 s all-on retention/worker run per board and a 20-observation storage cost run
  at 30 s plus a 60-observation Wi-Fi cost run at 5 s. Total planned wall time < 47 minutes per board;
  hard cap 50 minutes from the first integration run. Stop at the cap; do not extend automatically.
- Default daemon ≤2.0% of one core in each window. Record optional cost separately, call elapsed
  p95 ≤2 ms without waiving misses, no new errors/overruns/freshness regressions or sustained growth.
- Existing PSI normal-path ARM support and approved non-loopback RTT targets are unavailable. Do not
  claim those paths from unsupported/loopback results. All optional groups remain off by default.

## Other checks

Full pytest, both Node harnesses, Python 3.9/3.13 CI logs, real Chromium desktop/narrow with fixture
PSI/Wi-Fi and live API data, repeated render DOM stability, 503/timeout/tab-return event, single-file CLI,
old INI and explicit off smoke configurations, repeated start/stop PID/port cleanup. Build the existing
deploy/docker/Dockerfile locally and run an isolated read-only non-root container; no registry publication.

The production-style systemd unit requires administrator permission to create a DynamicUser transient
unit. `sudo -n true` requires a password on WSL, Orin and Pi: this exact hardening check is blocked by
existing host authentication, not satisfied by a user service or container. No privilege grants or
production-unit changes will be used to work around it.
