# Full-sample replay

> **TL;DR**
> - `/api/samples` and `/api/observation-samples` return complete published values after a client's
>   cursor, so a slower consumer (a Hub) can catch up.
> - Memory only and bounded: retention up to 3600 s, 1–64 MiB shared, 4096 records, 1 MiB per record;
>   evicted IDs are reported as missing ranges.
> - Responses are paged (at most 100 records or 2 MiB).

The live endpoints expose the latest reading; a slower Hub can miss intermediate publications.
Replay retains **complete published values**, including CPU rows, clocks, individual sensors,
network/disk counters, PSI and sensor provenance, rather than the selected numbers in `/api/history`.
A consumer can retrieve publications after its own cursor and persist them for analysis.
It is independent of `[history]`, uses only memory, and never initiates hardware reads.
Existing live endpoint schemas and freshness behavior are unchanged.

## Configuration and bounds

```ini
[replay]
enabled = yes
retention = 600
max_bytes = 16777216
```

These are the defaults. Retention is 1–3600 seconds, measured from each publication's collection
start on the service's elapsed clock. Payload capacity is shared by core and enabled slow groups,
configurable from 1 to 64 MiB. Retention is an upper bound: capacity pressure can evict data sooner.
Additional bounds are 4096 records across all streams and 1 MiB per encoded record. Oldest inserted
records are evicted first for capacity; time expiration checks all streams, including idle ones.
Oversized or unserializable publications are unavailable and their IDs appear in missing ranges.
Only finite JSON numbers can be replayed. Expiration happens on append and replay requests.

The byte budget counts retained compact JSON payloads, not total process RSS. Indexes, serialization
and decoded responses consume additional memory. A response admits at most 2 MiB of payload and
reserved envelope overhead, with at most 100 records. A large backlog therefore requires pagination.
At a 0.1 s core interval, the record cap can shorten the default ten-minute window.
Replay serializes each publication once in its publisher thread, outside the replay lock; requests
capture bounded byte references under the lock and decode outside it. Serialization adds work to each
publication. Live and numeric-history publication continue if replay encoding fails.

## Requests

```text
GET /api/samples?instance_id=<process-id>&after=100&limit=100
GET /api/observation-samples?group=battery&instance_id=<process-id>&after=7&limit=100
```

- `after`: exclusive, nonnegative publication ID, default 0, maximum `2**63 - 1`.
- `limit`: 1–100, default 100. The byte budget can return fewer records.
- `instance_id`: the process whose cursor is being resumed. Required when `after > 0`.
  On the initial request, omit it and use `after=0`; save the returned instance ID.
  It may also be supplied with `after=0` to check process continuity.
- `group`: required only for observation replay; `storage`, `wifi`, `battery` or `probe`.
  Disabled groups and an unconfigured Probe have no replay stream.
- Unknown fields, duplicates, empty values and malformed bounds return JSON HTTP 400.

Each stream has its own cursor. Core IDs are `sample.sequence`; slow IDs are that group's
`observation.id`. They must never be substituted for one another.

## Response

HTTP 200 uses `Cache-Control: no-store`, even when there are no publications or all have expired.
The replay envelope has schema 1, independently of the data's live schema:

| Field | Meaning |
| --- | --- |
| `instance_id`, `clock` | Current process identity and elapsed-clock description |
| `stream` | `core` or the requested observation group |
| `data_schema_version` | 1 for core snapshots; 2 for observation entries |
| `historical` | Always true: records are archived publications, not a live health response |
| `retention_s`, `max_bytes`, `max_records`, `max_record_bytes`, `max_response_bytes` | Configured retention and capacity bounds |
| `oldest_id` | Oldest retained ID in this stream; null if none are retained |
| `latest_id` | Latest publication ID known to the buffer, including discarded publications |
| `records` | Ascending `{id, gap_before, data}` entries |
| `next_after` | Cursor to save after committing this page and its missing-range evidence |
| `has_more` | Another retained record was excluded by count/byte limits; fetch again |
| `missing_ranges` | Inclusive `{from, to, reason: "buffer_unavailable"}` ranges in this page's cursor advance |

Core `data` has all `/api/stats` fields, plus `sample.stale`. Slow `data` is the complete entry
normally found at `/api/observations.groups[group]`, including `collector`, `observation` and `data`.
The outer replay envelope supplies its instance, stream, clock and data schema version.
Values, source metadata, observation times and collector diagnostics remain as published.
Age fields advance on the elapsed clock as of the replay response; `stale` is recomputed.
A stale observation's entry state is `stale`, while its collector retains the original diagnostic state.
`collecting_for_ms` is null in historical observation entries: a later running read is not part of
that archived publication. Clients may persist stale historical values but must not label them live.

A recovered core publication sets `gap_before=true` if one or more preceding collection attempts
failed or were discarded as too slow. Failed attempts never receive a sample ID, so contiguous IDs
alone do not prove uninterrupted measurement. Slow publications preserve their own error/unavailable
states and null data. Replay does not create zero values or reconstruct measurements that never existed.

Missing ranges identify publications that cannot be replayed because of expiration, capacity,
oversized payloads or encoding failure; these causes share `buffer_unavailable`. They cover only
`after + 1` through `next_after`, not future pages. When no retained records remain after the cursor,
`next_after` still advances to `latest_id` and reports the unavailable range. Consumers must save that
advance as well, rather than repeatedly requesting vanished records. `after=0` includes evidence of
expired publications from the start of this process; consumers wanting only future records can first
read the envelope and deliberately choose its `latest_id` as their starting cursor.

## Restart, errors and consumer flow

A different instance ID returns HTTP **409**:

```json
{"code":"instance_changed","instance_id":"the-current-process-id"}
```

A cursor beyond the current stream's latest ID returns 409 with `code: "cursor_ahead"`.
Neither case silently mixes generations or resets a cursor. HTTP 404 with `code: "replay_unavailable"`
means replay is disabled or that group has no stream. All replay responses, including errors, are
JSON with `Cache-Control: no-store`. Endpoints also work with `web = no`.

A Hub should:

1. Keep a stable node identity of its own and a separate `(instance_id, cursor)` per stream.
2. Commit records and missing-range evidence, then advance to `next_after`; deduplicate by
   `(node_id, instance_id, stream, id)` when retrying a page.
3. Continue while `has_more`, then poll on its chosen schedule.
4. On `instance_changed`, record a restart boundary and start at 0 in the new process.
   The previous process's memory was lost; its final missing extent cannot be determined.
5. Treat `cursor_ahead` as a cursor/configuration problem to investigate, not a successful catch-up.
6. Maintain transport/disconnection evidence separately: replay cannot prove what happened while
   the Hub was disconnected, or recover expired data or anything across a platmon restart.

Durable retention, AI analysis and Hub adoption of these endpoints are consumer responsibilities.
This feature adds no disk spool and makes no guarantee of lossless collection across restarts.
