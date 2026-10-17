# Documentation

The repository [README](../README.md) gives a short overview and quick start.
Choose a guide for practical usage, a reference for behavior contracts, or an engineering
document for implementation and validation evidence.

## User guides

- [Getting started](guides/getting-started.md) — prerequisites, package installation,
  first connection, configuration and a remote-only terminal client.
- [CLI and web interfaces](guides/interfaces.md) — interactive controls, dashboard views,
  thresholds, data coverage and the HTTP entry points.
- [Deployment and operations](guides/deployment.md) — package versus source/systemd versus
  Docker, upgrades, troubleshooting, networking and image licensing.

## Reference

- [HTTP API](reference/api.md) — endpoints, schemas, response semantics and freshness.
- [Observations](reference/observations.md), [history](reference/history.md) and
  [full-sample replay](reference/replay.md) — independent slow readings,
  numeric history and bounded publication replay.
- Metric contracts: [network](reference/network.md), [disk I/O](reference/disk-io.md),
  [storage](reference/storage.md), [pressure](reference/pressure.md),
  [Wi-Fi](reference/wifi.md), [battery](reference/battery.md) and
  [TCP probes](reference/probe.md).
- [Packaging and releases](reference/packaging.md) — versioning, artifacts,
  migration, rollback and release workflow.
- [Docker observation scope](reference/docker-scope.md) — verified host visibility,
  filesystem and network boundaries.

## Development and validation

- [Architecture](development/architecture.md) — source layout, dependencies
  and module responsibilities.
- [ADR 0001: Ubuntu LTS and Python support policy](development/adr/0001-ubuntu-lts-python-support.md)
  — official support qualification and CI-only observation of Python 3.8 / Ubuntu 20.04.
- [Ubuntu LTS qualification](development/ubuntu-lts-qualification.md) — implementation progress,
  local interpreter and container evidence, and remaining native OS release gates.
- [Remaining work](development/remaining-work.md) and
  [post-integration validation](development/post-integration-validation.md) —
  development checkpoints and validation scope.
- [Release readiness](releases/readiness.md) — acceptance gates and release evidence.
- [Performance reports](performance/README.md) — budgets, current measurements,
  background studies and an index of superseded evidence in `performance/archive/`.
