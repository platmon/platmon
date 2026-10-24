# Network counters

`/api/stats` has a `network` object while `[network] enabled = yes` (the default; see
[Configuration](#configuration-and-direct-calls)). It holds the
per-interface counters of **platmon's own network namespace**, read from `/proc/self/net/dev` once per
collection by the collector thread, and rates over the time between two such readings. Nothing is sent,
probed or captured. General API rules (freshness, `collectors`, 503): [api.md](api.md).

```json
"network": {
  "scope": {"kind": "process_network_namespace", "id": "netns1:3f0c…(64 hex digits)"},
  "provider": "proc_net_dev",
  "interfaces": [
    {
      "name": "eth0",
      "ifindex": 2,
      "rx": {"bytes": 1000, "packets": 10, "errors": 0, "dropped": 0},
      "tx": {"bytes": 2000, "packets": 20, "errors": 0, "dropped": 0},
      "rates": {"rx_bytes_per_s": 100.0, "tx_bytes_per_s": 200.0, "rx_packets_per_s": 1.0, "tx_packets_per_s": 2.0},
      "window_ms": 1000.0,
      "reason": null
    }
  ],
  "read": {"started_offset_ms": 12.0, "completed_offset_ms": 12.2}
}
```

(The numbers show the format; they are not a measurement.)

## Fields

- `interfaces`: every interface `/proc/self/net/dev` lists, sorted by name: loopback, bridges, veth and
  tunnels included. They are not summed: one packet can pass several of them (a bridge and its port),
  so a total would not be the host's traffic.
- `rx`, `tx`: the kernel's cumulative counters since the interface was created: `bytes`, `packets`,
  `errors` and `dropped` (the `errs` and `drop` columns). `dropped` counts packets the kernel or driver
  dropped on this interface; it is not packet loss on a path or a probe's loss rate.
- `rates`: counter difference ÷ elapsed time between the two readings' starts, per second: **bytes**
  per second (not bits) and packets per second. A counter that did not move gives `0.0`. Neither link
  speed nor utilisation is reported or estimated.
- `window_ms`: the elapsed time the rates are over, measured on the service's elapsed clock (the
  wall clock does not affect it). It follows the real timing, so it is not `sample.interval_ms`. Also given
  with `gap` and `counter_regressed`; `null` when there was no usable earlier reading.
- `reason`: why `rates` is `null`, else `null`:

| `reason` | Meaning |
| --- | --- |
| `warmup` | no earlier reading of this interface: the service's first, a new or renamed interface, the same name on another index, one back after a reading without it, or the first after a failed reading |
| `identity_unavailable` | the namespace or the interface's index could not be determined: its counters are shown, but cannot be matched to an earlier reading |
| `namespace_changed` | platmon's network namespace is not the one of the previous reading: every interface starts over |
| `invalid_interval` | the elapsed clock did not advance since the previous reading |
| `gap` | the readings are more than `max(3 × interval, 5 s)` apart; exactly that much still counts |
| `counter_regressed` | one of the eight counters went down (the interface was reset or recreated under the same name and index); not treated as a wrap, not clamped to 0 |

Each valid reading of an interface is the baseline for its next one, also when it gives no rate
(`gap`, `counter_regressed`, `invalid_interval`). The baseline is the last *reading*, not the last
published snapshot: a collection dropped as `collection_too_slow` still read the counters. A reading
that is not used for rates (`identity_unavailable`, a broken row, a broken file) leaves no baseline.
Only the interfaces of the latest reading are kept.

## Scope and identity

`scope.kind` is always `process_network_namespace`: the interfaces platmon's process sees. In a
container with its own network (Docker's default bridge) that is the container's `eth0` and `lo`, not
the host's NICs; with `network_mode: host`, or platmon running natively, it is the host's namespace.
platmon does not enter another namespace.

- `scope.id`: `netns1:` and a SHA-256 of the device and inode `/proc/self/ns/net` resolves to. Two
  readings with the same id are from the same namespace while it exists. It is not a hardware or machine
  id: the host's initial namespace has the same device and inode on every Linux machine, so natively
  running platmons on different hosts show the same id, and a namespace created later can get the id of
  one that is gone. Read it together with `sample.instance_id`. `null` when it could not be read.
- An interface is matched to its earlier reading by namespace, index (`ifindex`, `null` if not known) and
  name together.
- Limits: the namespace, the index list and the counters are three reads, not one snapshot. If an
  interface is removed and recreated with the same name *and* index between two readings, and its new
  counters are not lower than the old ones, the rate spans the two interfaces. platmon does not follow
  hotplug events. All threads of the process are assumed to share its namespace.
- No IP or MAC addresses, SSIDs or host names are collected.

## Diagnostics and freshness

`collectors.network` follows the rules of the other optional groups ([api.md](api.md#optional-metrics)):

- A row that does not parse (wrong number of fields, a counter that is not an unsigned 64-bit integer,
  an invalid interface name) is left out and reported as `invalid_data`; the other interfaces are kept.
  A name on several rows is ambiguous: all of them are left out. An invalid name is reported as
  `line<N>`, never by its text.
- A file that cannot be read (`permission_denied`, `io_error`), is not text or has an unknown header
  (`invalid_data`) gives `interfaces: []` and state `error`; one that is not there gives `unavailable`
  (`not_exposed`). An empty list from a readable file is `unavailable` (`not_detected`), not an error.
- A namespace or index lookup that fails is reported (targets `netns`, `ifindex`); the counters are kept
  with `identity_unavailable`.
- An unexpected exception in the network code is `internal_error`; it never fails the collection.
- `partial` or `error` makes `/api/status` say `degraded` with `ready: true`; `last_attempt` stays `ok`.
  Interfaces in `warmup` are normal values.
- The network part runs after the required data. A collection that fails before it
  (`collection_failed`) reads no counters; the last good snapshot keeps its own `network` and its own
  times until it is stale, then 503 without values.

`network.read` is the one counter read every interface comes from, as offsets from the collection's
start like `sensor_meta.read`. It counts for `data_age_ms` (`oldest_current_read_start`); the earlier
reading a rate goes back to does not. It is `null` when there was no successful read, for a direct
`collector.collect()`, and when the record does not check out (missing, reversed, outside the
collection, another clock); in that last case, with interfaces present, the snapshot's data age falls
back to `cycle_start_upper_bound`. A failed reading without interfaces never moves the other values to
the fallback.

## Windows

The Windows build of the native server (not released; native/README.md, Windows) reads the counters
from `GetIfTable2` instead of `/proc/self/net/dev`, with the same fields, rates, reasons and baselines:

- `scope` is `{"kind": "host", "id": null}`: Windows has no network namespace, so `namespace_changed`
  and `identity_unavailable` do not occur, and an interface is matched by `ifindex` and name.
- `provider` is `if_table2`. `name` is the interface's alias (e.g. `Ethernet`, `Wi-Fi`), which can
  contain spaces and non-ASCII characters. The filter drivers' layers over an interface are left out;
  every other interface Windows lists is kept, including the loopback and virtual adapters.
- `packets` is unicast plus non-unicast; `errors` and `dropped` are Windows's errors and discards.
- A table that cannot be read gives `interfaces: []` and state `error`; an empty table is `unavailable`
  (`not_detected`). A name on several rows is left out (`invalid_data`).

## Docker scope

The shipped Compose configuration uses host networking on native Linux Docker Engine, so its
process network namespace is the host's and interface counters/identities describe that host.
A custom bridge configuration still describes only the container's interfaces. Host networking
also makes Probe loopback refer to the host; it does not expose additional filesystem mounts.
See [Docker observation scope](docker-scope.md).

## Configuration and direct calls

Network collection is **on by default**: also without a config file, and with an INI that has no
`[network]` section (written before it existed) or no `enabled` key. To turn it off:

```ini
[network]
enabled = no
```

Then platmon makes no `NetworkCounters` and never reads the counters: there is no `network` and no
`collectors.network`. An INI that already says `no` stays off. The collection interval is
`[core] interval`; there is no separate one.

Cost when on: +0.160 pp (Jetson Orin Nano, 10 interfaces) and +0.175 pp (Raspberry Pi 4, 3 interfaces)
of one core for the whole process at a 1 s interval with one client polling every second; the default
configuration stayed within the release target of [performance/budget.md](../performance/budget.md), then 2.0 %.
It missed the earlier per-feature 0.1 pp target, which stays recorded as not met
([performance/network-product.md](../performance/network-product.md)).

`collector.collect()` and `collect_recorded()` take an optional `network` (a
`collector.network.NetworkCounters` kept between calls); without it they return what they did before.
A one-off `collect(network=NetworkCounters())` has all interfaces in `warmup` and adds no wait.

## Not included

Shown on the web page, in the `platmon` command and `/text` (see [api.md](api.md#web-page)). Not
collected: Wi-Fi signal (RSSI), round-trip time or ping, packet capture, link speed and duplex, access
point scans, pressure stall information (PSI), filesystem usage, history (disk I/O: [disk-io.md](disk-io.md)). Measured cost: [performance/network-product.md](../performance/network-product.md).

The CLI, `/text` and web page show the server's RX/TX bytes/s, packets/s and actual `window_ms`
(converted to seconds). No viewer differences cumulative counters or substitutes the configured interval.
