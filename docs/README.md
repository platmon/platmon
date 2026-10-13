# Documentation

Start with the repository [README](../README.md) for installation and usage.

- [Reference](reference/api.md): HTTP API, freshness and response contracts. Related contracts:
  [observations](reference/observations.md), [history](reference/history.md),
  [full-sample replay](reference/replay.md),
  [network](reference/network.md), [disk I/O](reference/disk-io.md),
  [storage](reference/storage.md), [pressure](reference/pressure.md),
  [Wi-Fi](reference/wifi.md), [battery](reference/battery.md) and [TCP probes](reference/probe.md).
- [Packaging](reference/packaging.md) and [Docker scope](reference/docker-scope.md): distribution,
  migration, rollback and container visibility.
- [Architecture](development/architecture.md): source layout and module responsibilities.
  [Remaining work](development/remaining-work.md) records development checkpoints and deferred work;
  [post-integration validation](development/post-integration-validation.md) records validation scope.
- [Release readiness](releases/readiness.md): release gates and acceptance evidence.
- [Performance](performance/README.md): budgets, current measurements and background studies.
  Superseded reports are retained in `performance/archive/` and indexed there by the performance guide.
