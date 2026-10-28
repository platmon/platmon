# Performance reports

> **TL;DR**
> - Index of the CPU and memory measurements on a Jetson Orin Nano and a Raspberry Pi 4; each report is
>   pinned to the commit it measured.
> - Start with [budget.md](budget.md) (the target), then the current evidence; superseded reports are in
>   the git history.

Measurements of what the platmon service costs on the boards (Jetson Orin Nano, Raspberry Pi 4). Each
report records the exact commit it measured; later code changes do not update old numbers. Start with the
budget, then the current evidence. The release decision itself is in [release readiness](../releases/readiness.md).

## Target

- [budget.md](budget.md): the target (the default configuration's whole process at most 2.5 % of one
  core per board, 2.0 % until 2026-10-18) and how features are judged against it.

## Current evidence

- [history-expiry-validation.md](history-expiry-validation.md): the final validated runtime (`aac7ed7`),
  default and all-on CPU per board, history retention and memory. The default configuration has since
  turned every feature on; that configuration's 1.0.0 gate (every window <= 2.0 % on both boards) is in
  [release readiness](../releases/readiness.md#100-release-gate-2026-10-10).
- [network-product.md](network-product.md), [disk-io-product.md](disk-io-product.md): what Network and
  Disk I/O add to the default configuration.
- [low-frequency.md](low-frequency.md): what Storage, Wi-Fi, the TCP connect probe and history cost.
  Written when they were off by default; the cost numbers still apply.
- [identity-resolution.md](identity-resolution.md), [snapshot-copy.md](snapshot-copy.md): measured design
  decisions behind code that is still in place (sysfs identity reuse, the `/api/stats` copy).

## Exploratory

- [rust-feasibility.md](rust-feasibility.md): what a native (Rust) collector could save, and one binary per
  architecture. One exploratory round, not a release judgment; no rewrite decided.

## Superseded

The reports replaced by the ones above (the final-candidate plan and results, the recovery-gap validation,
the resumed integration plan and results) were in `archive/` and are in the repository as of `b96aba8`.

## Background studies

No product change of their own; later work built on them.

- [low-overhead-cadence.md](low-overhead-cadence.md): cost per collection interval and the 1 s cadence.
- [collection-response-breakdown.md](collection-response-breakdown.md): where collection and response
  CPU goes, which led to the two design notes above.
- [passive-cost-rebaseline.md](passive-cost-rebaseline.md): the passive Network / Disk / PSI prototype,
  measured before the product collectors existed.
