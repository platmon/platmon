# Low-frequency observations (`/api/observations`)

> **TL;DR**
> - Slow groups (storage, Wi-Fi, battery, probe) run on their own threads and cadences and are served at
>   `/api/observations`, never in `/api/stats`.
> - Each group has its own state, interval and age, so a slow or stuck one cannot make the core snapshot
>   stale.
> - Envelope `schema_version` 2: Wi-Fi `connected` can be `null` (unknown); test `connected === true`.

Some data changes slowly or is slow to read: [storage](storage.md), [Wi-Fi](wifi.md),
[battery](battery.md) and the [TCP connect probe](probe.md). Each such group is observed on its own cadence by its own thread, apart from the core Sampler, and served at
`/api/observations` with its own id, times and age. `/api/stats` (the 1 s snapshot) is unchanged and never
contains these groups, so an old or blocked observation can never make the core snapshot stale or 503.

```json
{
  "schema_version": 2,
  "instance_id": "8dfad9fa-9bf0-4d61-91e0-ef22aebd2b91",
  "clock": {"source": "boottime", "suspend_aware": true},
  "groups": {
    "storage": {
      "state": "ok",
      "interval_ms": 30000.0,
      "stale_after_ms": 90000.0,
      "collecting_for_ms": null,
      "observation": {"id": 12, "started_at": 1791345600.0, "completed_at": 1791345600.004,
                      "duration_ms": 4.2, "age_ms": 7000.0, "data_age_ms": 7004.2, "stale": false},
      "collector": {"state": "ok", "reason": null, "issues": [], "issues_truncated": 0},
      "data": {"partitions": [], "filesystems": []}
    }
  }
}
```

- Always HTTP 200 with `Cache-Control: no-store`, also with no group enabled (`groups: {}`). Requests
  only read the latest published observation; they never trigger one.
- `instance_id` is the service's (as in `/api/stats`); `observation.id` counts this group's observations
  from 1 and is not the core `sample.sequence`. The same id read again is the same observation.
- Times: `started_at`, `completed_at` are wall-clock seconds, for people. `duration_ms`, `age_ms` (since it
  completed) and `data_age_ms` (since it started: the oldest read in it) are on the elapsed clock of
  `clock`, as of this answer, so they keep growing while the same observation is served.
- `interval_ms`: the configured time between starts. `stale_after_ms`: 3 intervals. `stale` is true once
  `data_age_ms` is above it.
- `state`: `starting` (no observation yet), `stale` (the last observation is older than `stale_after_ms`,
  or the first one is still running after that long), otherwise the C1 state of the latest observation
  (`ok`, `partial`, `error`, `unavailable`; see [api.md](api.md#optional-metrics)). A stale observation is
  still served with its `data`, marked `stale`, never as current; clients show it as not current.
- `collecting_for_ms`: how long the running observation has taken so far, `null` between them. A read
  that blocks (a hung filesystem) stalls only its own group: the group's age grows and this value says for
  how long. platmon cannot cancel a blocked read, and does not start another thread for the group while it
  is blocked, so the next observation of that group starts only after it returns.
- `collector`, `data`: the group's C1 diagnostics and data from that observation, published as one unit
  and never changed afterwards.

Clients ask for `/api/observations` on the groups' cadence, not every second: the web page and the
`platmon` command ask at most every 10 s (the page only while visible with Network, Storage or Power & battery selected) and add the time since that
answer to the ages shown. A server without the endpoint (404) is not asked again.

Groups: `storage` ([storage.md](storage.md)), `wifi` ([wifi.md](wifi.md)), `battery` ([battery.md](battery.md)), `probe` ([probe.md](probe.md)).

The viewers also compare the growing age to `stale_after_ms` between answers, so a cached observation
becomes not current without waiting for a new server response. The web page limits each optional
endpoint to one outstanding request with a 5 s deadline. Tab return invalidates older requests and
clears their panels until fresh answers arrive; a hidden page suspends all polling and rendering.
Switching between in-page views shares the cache and polling deadline. Only age text updates between
responses; observation rows are rebuilt on a response or view selection.

## Version 2 compatibility boundary

The connection-state correction changes only the low-frequency envelope to `schema_version: 2`.
Wi-Fi `connected` becomes true/false/**null**: null means the connection could not be determined.
The additive `connection_state` is `connected`, `disconnected`, `unknown`, `unsupported`,
`permission_denied` or `unavailable`. A wireless signal-update mark alone never establishes a connection.
Clients must test `connected is true` / `=== true`, not treat every other value as disconnected.
CLI/web in this change accept both envelope versions; on version 1 they retain its legacy boolean display.
Core `/api/stats` and history remain version 1. Storage/probe fields and observation timing are unchanged.
