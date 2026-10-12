# Wi-Fi signal

A low-frequency group ([observations.md](observations.md)) named `wifi`: what the kernel last knew about
each wireless interface's link, read from `/proc/net/wireless` every `[wifi] interval` seconds (5 by
default). **On by default** (`[wifi] enabled = no` turns it off). Passive: platmon never scans,
reconnects or changes a setting, and does not read SSID, BSSID or nearby access points. Rx/tx traffic
stays in `network` ([network.md](network.md)), at its 1 s cadence.

```json
"wifi": {
  "data": {
    "scope": {"kind": "process_network_namespace"},
    "provider": "proc_net_wireless",
    "interfaces": [
      {"name": "wlan0", "connected": true, "signal_dbm": -64, "signal_raw": null, "link_quality": 46,
       "noise_dbm": null}
    ]
  }
}
```

## Fields

The file's format and meaning come from the kernel's `net/wireless/wext-proc.c` and `wext-compat.c`
(cfg80211's wireless-extensions view):

- `connected`: kernel carrier from `/sys/class/net/<name>/carrier`, with ifindex checked before and
  after to reject device replacement/scope mismatch. True/false describes the kernel link, not Internet
  reachability or Wi-Fi authentication. `null` means unknown; `connection_state` explains unavailable,
  unsupported or permission-denied observations. A signal-update dot alone never determines it.
  A disconnected/unknown interface has no current signal/quality/noise in the published result.
- `signal_dbm`: the signal level in dBm when the driver reports dBm (the kernel prints those as negative
  numbers). `signal_raw`: the level of a driver that reports no unit (a non-negative number); platmon does
  not convert it to dBm. One of the two is set while connected.
- `link_quality`: the kernel's "link" value, as given. For cfg80211 drivers with dBm it is derived from the
  signal (−110 dBm and below → 0, −40 dBm and above → 70). It is not a percentage and not a second
  measurement of the signal.
- `noise_dbm`: the noise level in dBm where a driver reports one; `null` otherwise (−256 in the file means
  "not available").
- `scope`: the interfaces of platmon's own network namespace, as for `network`; a container on its own
  bridge sees no wireless interface. The shipped Linux Compose configuration uses host networking
  so this namespace is the host's, without adding a separate Wi-Fi collector.

Reading `/proc/net/wireless` asks each driver for current station data, and clears the "updated" marks
for any other reader of the file. Nothing else is touched.

## Diagnostics

`collector` ([observations.md](observations.md)): no `/proc/net/wireless` (a kernel without wireless
extensions or the cfg80211 compatibility view) is `unavailable` / `not_exposed`; a file without any wireless
interface is `unavailable` / `not_detected`; an unreadable file is `permission_denied` / `io_error`; an
unknown header is `invalid_data`; a row that does not match the format is left out (`line<N>`,
`invalid_data`).

## Configuration

```ini
[wifi]
enabled = yes     ; no: do not observe it
interval = 5      ; seconds between observations, 1 to 3600
```

5 s is a starting point, not a derived optimum. Shown on the web page ("Wi-Fi") and in the `platmon`
command and `/text` (`WIFI` lines) with the observation's age; "not connected" when not associated.

Connection semantics changed in observations schema version 2; see [observations.md](observations.md#version-2-compatibility-boundary).
Sysfs and process-network scope must describe the same interface. Name/index checks catch visible
mismatches, not an undetectable replacement reusing both. AP/other modes can have carrier without a
station signal. WEXT alone cannot distinguish association, missing station data and a failed driver
query; nl80211 would provide richer status but requires a separate, bounded protocol implementation.
This provider keeps the small passive proc/sysfs path, never requests SSID/BSSID and never scans.
Its supported signal range is the kernel's signed dBm or unspecified raw unit, never an inferred conversion.

Official semantics reviewed (no source code copied):
[wireless proc output](https://github.com/torvalds/linux/blob/master/net/wireless/wext-proc.c),
[cfg80211 compatibility interface](https://github.com/torvalds/linux/blob/master/net/wireless/wext-compat.c).

## Not included

SSID, BSSID, frequency, bit rate, scans, roaming, nl80211 queries, other tools (`iw`), history.

For identity-aware history, successful observations also include `scope.id` (the same network namespace
hash convention as Network) and each interface's `ifindex`, obtained once per observation via standard
library network-interface enumeration. Missing identity is null and diagnosed, never inferred from the
name. These additive fields do not change signal units or the provider. The earlier per-call cost
measurements predate the identity lookup and are not a measurement of this updated path.
