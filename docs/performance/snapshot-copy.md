# Snapshot and /api/stats copy cost

> **TL;DR**
> - The two `copy.deepcopy` calls were the largest step of an `/api/stats` request.
> - Adopted change: per-request CPU fell by half on the Orin and by 39 % on the Pi.

Follow-up to [collection-response-breakdown.md](collection-response-breakdown.md), which found the two
`copy.deepcopy` calls in `Sampler.read` (0.60 ms per request on a Jetson Orin Nano, 0.98 ms on a
Raspberry Pi 4) to be the largest step of an `/api/stats` request, larger than `json.dumps`. This note
records who owns the snapshot, what had to stay true, the change, and its measured effect.

## Result (2026-10-08)

**Adopted**, with the effect stated where it was measured: the per-request CPU of `/api/stats` fell by
half on the Orin and by 39 % on the Raspberry Pi, in every run; the whole process's CPU went the same
way on both boards, by about the size this predicts, but that difference is not larger than the spread
between repeats, so it is not shown at the process level.

Three A B B A blocks per board (12 windows of 120 s after 30 s warmup; WSL one block, supplementary),
collection 1 s, one client polling `/api/stats` every 1 s, reference instrumentation (D2's): A is main
`e1bc82a`, B this change, same bench code (`benchmarks/cadence.py run --phase ab` from two trees).

| | Orin Nano A | Orin Nano B | Raspberry Pi 4 A | Raspberry Pi 4 B | WSL A | WSL B |
| --- | --- | --- | --- | --- | --- | --- |
| request CPU (`do_GET` thread CPU), mean of runs, ms | 1.191 (range 0.054) | **0.598** (0.045) | 2.052 (0.221) | **1.259** (0.105) | 0.938 | 0.531 |
| change | | **-0.59 ms (-50 %)**, every B run below every A run | | **-0.79 ms (-39 %)**, every B run below every A run | | -0.41 ms |
| snapshot step (A: `read()`; B: `_stats_json()` incl. JSON), ms | 0.69 | 0.36 | 1.08 | 0.68 | 0.50 | 0.27 |
| process CPU % of one core, runs | 1.47, 1.69, 1.34, 1.47, 1.50, 1.49 | 1.44, 1.41, 1.41, 1.41, 1.45, 1.42 | 1.28, 1.28, 1.31, 1.27, 1.22, 1.20 | 1.17, 1.22, 1.15, 1.21, 1.20, 1.17 | 0.48, 0.49 | 0.49, 0.45 |
| process CPU, B - A (mean) | | -0.07 pp; spread of A 0.36 pp: **not distinguishable** | | -0.07 pp; spread of A 0.11 pp: **not distinguishable**; lower in all 3 blocks (-0.08, -0.11, -0.03) | | -0.02 pp |
| client latency p50 / p95, ms (mean of runs) | 3.54 / 3.98 | 2.94 / 3.34 | 5.93 / 6.21 | 5.19 / 5.38 | 3.11 / 4.01 | 2.62 / 3.55 |
| collection thread CPU, ms | 11.84 | 11.83 | 6.50 | 6.53 | 2.08 | 2.28 |
| RSS end / PSS end, MiB (mean) | 24.3 / 16.9 | 24.7 / 17.1 | 28.2 / 18.7 | 28.3 / 18.8 | 28.4 | 28.5 |
| response bytes, requests per window | 7008, 120 | 7008, 120 | 3078, 120 | 3078, 120 | 6218, 120 | 6218, 120 |

- Computed check: one request per second saving 0.59 / 0.79 ms is 0.06 / 0.08 pp of one core, the size
  of the measured process-level differences (-0.07 / -0.07 pp). On the Orin, two A runs (1.69 % and
  1.34 %) differ from the rest through the collection's own CPU (13.5 and 10.3 ms against about 11.8),
  not through requests; they were kept.
- Per client and per request: the saving scales with requests (the web page, the `platmon` command and the
  health check each poll `/api/stats`), not with the collection interval.
- No change in collection CPU, memory (within run-to-run variation), response size, actual interval
  (max 1000.5 ms), failed collections (0) or overruns (0).
- Real API and CLI on both boards (tree B on a loopback test port next to the running service):
  `/api/stats`, `/api/status`, `/text` 200 with the same Content-Type and `Cache-Control: no-store`, 404
  for an unknown path; the same top-level and `sample` key order, source ids, identity bases, collector
  states and schema version as the running service; `frontends/cli.py --once` renders it.
- Environment: the boards' own platmon services (both `e1bc82a`) kept running and were not touched
  (state compared before and after); 30.3 min per board (budget 50 min / 16 windows), no invalid run, no
  dropped record; temperatures 50.0-52.9 °C (Orin), 50.6-53.1 °C (Raspberry Pi), `get_throttled` 0x0
  throughout. Raw records kept outside the repository: SHA-256 (first 16 hex digits) orin
  `9e228692713a8cb4`, rpi4 `db3e2f30f952df2e`, wsl `210860ba83e5c41b`.

## Who owns what

```text
collector thread                                   HTTP worker thread (one per request)
----------------                                   ------------------------------------
collect() -> stats (the collector's own objects)
Sampler._attempt
  deepcopy(stats)  ............ publish copy: the collector may reuse or change its objects later
  build record = {stats, sequence, times, sensor_meta (new dicts from _check)}
  with lock: self._record = record   (replaced, never changed in place)
                                                   Sampler.read / _stats_json
                                                     with lock: take record, attempt, now
                                                     status: new dicts, request-time ages
                                                     view: new top-level dict, values = record's own
                                                   read():        deepcopy(view) -> caller (may change it)
                                                   _stats_json(): json.dumps(view) -> bytes -> HTTP reply
```

- **The record** is owned by the Sampler. After `self._record = record` nothing changes it: the next
  collection builds a new record and replaces the reference under the lock. That is what makes it safe
  to read the record's values from another thread without the lock.
- **The publish copy** in `_attempt` stays: it separates the snapshot from the collector's own objects
  (contract 3 below). It runs once per collection, not per request.
- **`read()`** still returns objects nobody else holds: it deep-copies the view (one `deepcopy` call
  instead of two; the result is the same structure).
- **`_stats_json()`** is the new private path for `frontends/server.py`: it serializes the view and returns
  only the JSON bytes and the (new) status dict, so no reference into the record leaves the Sampler.
- `/api/status` and `/text` keep using `read()` (the web page, the `platmon` command and the container
  health check all poll `/api/stats`).

## What had to stay true

| | contract | how it is kept | test (`tests/runtime/test_snapshot_copy.py`) |
| --- | --- | --- | --- |
| 1 | a caller changing nested values of `read()`'s stats does not reach others | `read()` deep-copies | `test_callers_changing_nested_values_do_not_reach_others` |
| 2 | the same for `sensor_meta` and its `read` spans | the same copy covers it | `test_sensor_meta_ids_and_reads_match_and_stay_owned` |
| 3 | the collector changing its own dict later does not reach the snapshot | the publish copy, unchanged | `test_the_collectors_own_dict_changed_later_does_not_reach_the_snapshot` |
| 4 | concurrent requests for one sequence | the record is read-only after publish | `test_concurrent_requests_for_one_sequence` (16 at once, all equal) |
| 5 | a publish while an older snapshot is being serialized | the view holds the old record; a new one replaces the reference only | `test_a_publish_during_serialization_does_not_mix_snapshots` |
| 6-8 | ready, degraded (failed attempt or optional group), last good, stale (503), starting | the same state code; no body exactly where `read()` gives none | `test_status_and_absence_match_read` (7 cases) |
| 9 | the same snapshot read twice: ages from the request time | ages are computed per call, as before | `test_same_snapshot_twice_ages_move` |
| 10 | JSON Pointers, source ids, identity bases, data age basis | the same values serialized | `test_sensor_meta_ids_and_reads_match_and_stay_owned` |
| 11 | `/api/status`, `/text`, codes, headers | unchanged code paths; existing `tests/frontends/test_server.py` | all server tests pass unchanged (one test helper also skips the first-snapshot wait for `_stats_json`) |
| 12 | generic collectors: tuples, dict subclasses, shared objects | `read()` still deep-copies them as before; the HTTP JSON equals `json.dumps(read())` | `test_generic_collector_values`, `test_unserializable_values_fail_as_before` |
| - | same bytes | with the same clock reading, `_stats_json()` equals `json.dumps(read()[0])` byte for byte, key order included | `test_stats_json_is_read_serialized` |
| - | the snapshot is not changed by serving | the record before and after repeated reads is equal and the same object | `test_serializing_does_not_change_the_snapshot` |

The state lock is held only to take the record, as before; JSON is built outside it, and a slow
response never blocks the next collection.

A value whose `__deepcopy__` returns something that serializes differently from the value itself would
now be sent as published rather than as copied; collectors return plain JSON data, and nothing in the
product defines such a type.

## Options considered

| option | verdict |
| --- | --- |
| one `deepcopy` of stats + sensor_meta in `read()` instead of two | kept for `read()` (it falls out of the view); alone it does not remove the per-request copy |
| a faster JSON-tree copy (type-specific) | not needed once the HTTP path does not copy; would add type special cases |
| **HTTP serializes the published record without copying (private `_stats_json`)** | **chosen**: removes the per-request copy for `/api/stats`, returns only bytes |
| `read()` returning the record's own objects, or a shallow copy | rejected: callers could change the snapshot |
| caching the whole JSON | rejected: ages and status change per request |
