# Performance reports

Measurements of what the platmon service costs on the boards (Jetson Orin Nano, Raspberry Pi 4). Each
report records the exact commit it measured; later code changes do not update old numbers. Start with the
budget, then the current evidence. The release decision itself is in [release readiness](../releases/readiness.md).

## Target

- [budget.md](budget.md): the target (the default configuration's whole process at most 2.0 % of one
  core per board) and how features are judged against it.

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

## Superseded

Kept for their raw data and history; their conclusions were replaced by the reports above.

- [final-candidate-plan.md](archive/final-candidate-plan.md), [final-candidate-results.md](archive/final-candidate-results.md):
  a candidate baseline from before the recovery-gap correction.
- [recovery-gap-validation.md](archive/recovery-gap-validation.md): replaced by history-expiry-validation.md after
  a further retention defect.
- [resumed-integration-plan.md](archive/resumed-integration-plan.md),
  [resumed-integration-results.md](archive/resumed-integration-results.md): the integration run before the final
  candidate.

## Background studies

No product change of their own; later work built on them.

- [low-overhead-cadence.md](low-overhead-cadence.md): cost per collection interval and the 1 s cadence.
- [collection-response-breakdown.md](collection-response-breakdown.md): where collection and response
  CPU goes, which led to the two design notes above.
- [passive-cost-rebaseline.md](passive-cost-rebaseline.md): the passive Network / Disk / PSI prototype,
  measured before the product collectors existed.
