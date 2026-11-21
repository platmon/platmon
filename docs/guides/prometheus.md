# Prometheus

> **TL;DR**
> - platmon serves no `/metrics`. To scrape it, run `scripts/prometheus_exporter.py` (standard library,
>   Python 3.8+) next to Prometheus or on the device: it reads `/api/stats` and `/api/observations` at
>   each scrape and answers in Prometheus's text format.
> - Every metric is `platmon_*`; counters are the kernel's totals for `rate()`; a value platmon reports as
>   `null` is left out, never written as 0.

## Why outside the server

Decided on [#198](https://github.com/platmon/platmon/issues/198) (owner, 2026-11-21): a documented exporter
instead of an endpoint in the server. The server stays one JSON API with one contract
([HTTP API](../reference/api.md)) and gains no second format to keep stable and measured against
[the budget](../performance/budget.md); the exporter adds no dependency and can run on another machine.
Requests never make platmon collect, so a scrape reads the snapshot it already has, as any client does.

## Run it

The script is in the source tree and in the release source archive (`platmon-<version>.tar.gz`):

```sh
python3 scripts/prometheus_exporter.py --platmon http://192.168.55.53:9797 --listen 127.0.0.1:9798
curl -s 127.0.0.1:9798/metrics | head
```

`--platmon` is the service to read (default `http://127.0.0.1:9797`); `--listen` is the IPv4 address and
port to answer on (default `127.0.0.1:9798`). Like platmon's API, `/metrics` has no authentication. One
exporter reads one service: run one per device, or one per device on the Prometheus host.

A systemd unit for it, as an example (adjust the path and the device):

```ini
[Unit]
Description=platmon Prometheus exporter
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/platmon/prometheus_exporter.py --platmon http://192.168.55.53:9797 --listen 127.0.0.1:9798
DynamicUser=yes
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

And in `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: platmon
    scrape_interval: 15s
    static_configs:
      - targets: ["127.0.0.1:9798"]
        labels: {device: orin-1}
```

A scrape interval at or above platmon's `[core] interval` (1 s by default) reads each snapshot at most once;
`platmon_data_age_seconds` shows how old it was.

## Metrics

From `/api/stats`:

| Metric | Labels | From |
| --- | --- | --- |
| `platmon_up` | | 1 when `/api/stats` answered 200 with a snapshot, else 0 and nothing else |
| `platmon_data_age_seconds` | | `sample.data_age_ms` |
| `platmon_uptime_seconds` | | `uptime` |
| `platmon_cpu_usage_percent`, `platmon_cpu_frequency_hertz` | `cpu` | `cpu[]` |
| `platmon_gpu_usage_percent`, `_frequency_hertz`, `_max_frequency_hertz` | `device` | `gpu` |
| `platmon_npu_usage_percent`, `_frequency_hertz`, `_max_frequency_hertz` | `device` | `npu` |
| `platmon_memory_bytes`, `platmon_swap_bytes` | `kind` (`total`, `used`) | `memory` |
| `platmon_root_filesystem_bytes` | `kind` | `disk` |
| `platmon_temperature_celsius` | `sensor` | `temperature` |
| `platmon_power_watts` | `rail` | `power` |
| `platmon_fan_rpm`, `platmon_fan_percent` | `fan` | `fans` |
| `platmon_network_{bytes,packets,errors,dropped}_total` | `interface`, `direction` (`rx`, `tx`) | `network` counters |
| `platmon_disk_io_bytes_total`, `platmon_disk_io_operations_total` | `disk`, `direction` (`read`, `write`) | `disk_io` |
| `platmon_disk_io_time_seconds_total` | `disk` | `disk_io` `io_time_ms` (Linux) |
| `platmon_pressure_stall_percent` | `resource`, `kind` (`some`, `full`), `window` (`10s`, `60s`, `300s`) | `pressure` |
| `platmon_pressure_stall_seconds_total` | `resource`, `kind` | `pressure` `total_us` |
| `platmon_collector_state` | `collector`, `state` | `collectors`: 1 for the state each is in |

From `/api/observations`:

| Metric | Labels | From |
| --- | --- | --- |
| `platmon_observation_age_seconds` | `group` | each group's `data_age_ms` |
| `platmon_filesystem_bytes` | `mount`, `fstype`, `kind` (`total`, `used`, `available`) | `storage` filesystems |
| `platmon_wifi_connected`, `platmon_wifi_signal_dbm` | `interface` | `wifi` |
| `platmon_battery_percent`, `platmon_battery_temperature_celsius` | `battery` | `battery` |
| `platmon_power_supply_online` | `supply` | `battery` external supplies |
| `platmon_probe_connect_seconds`, `platmon_probe_failure_ratio` | `target` | `probe` |

Rates are left to Prometheus (`rate(platmon_network_bytes_total[1m])`); platmon's own rates in
`/api/stats` are not exported. The processes group, the replay and history endpoints and `sensor_meta` are
not exported either: they are lists that change with the host, or what Prometheus keeps itself.

## Limits

- A metric whose value is `null` (not readable now, no such device) is absent from that scrape, as
  Prometheus expects; `platmon_collector_state` says why a group has none.
- Sensor, interface and mount names are labels as platmon reports them; a host with many of them makes
  many series.
- The exporter is a client of the published API, not part of platmon's contract: its metric names may
  change with it, and they are described here.
