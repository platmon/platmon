# Battery and external power

A low-frequency group ([observations.md](observations.md)) named `battery`: the batteries and the external
supplies the kernel knows about, read from `/sys/class/power_supply` every `[battery] interval` seconds
(60 by default, 10 to 3600). **On by default** (`[battery] enabled = no` turns it off). Passive: platmon
never switches a supply, starts or stops charging, calibrates or resets a gauge, and never reads a serial
number.

The whole class is standard Linux, not one board's interface: the same attributes come from an ACPI
laptop, a phone or robot gauge on I²C, and a USB UPS. Which of them a driver exposes differs, so only the
attributes this observation found by listing are read; one a driver does not have is left out and is not
reported as a problem.

```json
"battery": {
  "data": {
    "provider": "sysfs_power_supply",
    "batteries": [
      {"name": "BAT1", "present": true, "percent": 89, "status": "Not charging",
       "capacity_level": "Normal", "technology": "Li-ion", "health": null,
       "charge_now_uah": 3026000, "charge_full_uah": 3400000, "charge_full_design_uah": 4023000,
       "energy_now_uwh": null, "energy_full_uwh": null, "energy_full_design_uwh": null,
       "current_ua": 0, "power_uw": null, "voltage_uv": 16436000, "cycle_count": 237}
    ],
    "external": [{"name": "ADP1", "type": "Mains", "online": true}]
  }
}
```

## Why it is a low-frequency group

On an ACPI host every attribute of a battery comes from one firmware call behind the driver's cache
(`/sys/module/battery/parameters/cache_time`, 1000 ms by default). Measured on an x86_64 laptop: one
reading of a whole battery takes **101 ms** elapsed, of which **0.6 ms** is CPU — the rest is waiting on
firmware. A read within the cache window costs 0.01 ms, one after it the full 101 ms.

The core interval is 1 s, which sits exactly on that cache, so a battery read in the core cycle would pay
the full 101 ms on every collection — six times the whole of the rest of a collection. Here it runs on its
own thread and cadence instead, where at 60 s it costs about 0.001 pp of one core and the core cycle is
untouched. See [performance/budget.md](performance/budget.md).

The default is 60 s rather than the shortest allowed because battery state changes slowly and because
reading a battery costs the battery: each reading wakes the firmware of the device whose charge is being
reported.

## Fields

`batteries` holds every supply whose `type` stores charge (`Battery`, `UPS`), sorted by name:

- `name`: the directory name in the class (`BAT0`, `BAT1`, a driver's own name).
- `present`: whether a cell is in the bay. `false` means an empty bay, and then every field below is
  `null`: what the attributes still hold is not a reading of something that is not there.
- `percent`: `capacity`, 0 to 100. `null` where the driver does not report it; `capacity_level`
  (`Full`, `Normal`, `Low`, `Critical`, …) may still be there.
- `status`, `capacity_level`, `technology`, `health`: the driver's own words, as the kernel spells them.
  Translating them would invent states the driver never reported. At most 32 characters; anything longer
  is not one of these short enums and is reported as `invalid_data`.
- `charge_*_uah` (µAh) and `energy_*_uwh` (µWh): a gauge counts charge or energy, so a driver exposes one
  family and the other is `null`. They are never converted into each other: that needs a voltage, which
  is its own momentary reading, and the result would look like one measurement instead of two.
  `charge_full` / `energy_full` is what the gauge now calls full, `*_design` what the cell was built for;
  their ratio is the wear the viewers show as "% of design".
- `voltage_uv` (µV), `cycle_count`: non-negative; a negative value is reported as `invalid_data`.
- `current_ua` (µA), `power_uw` (µW): **signed, and passed through**. Drivers differ: some report a
  negative current while discharging, others a positive one with a `Discharging` status. platmon does not
  reinterpret the sign, so a reader must use `status` to tell charging from discharging.

`external` holds every other supply (`Mains`, `USB` and its subtypes, `Wireless`) with `name`, `type` and
`online`. Those say whether power is coming in, not how much charge is stored, so nothing else is read
from them.

## Where it is shown

The web page has a **Battery** card under **Power & battery**, beside the power rails and the fans; the
`platmon` live view carries the same readings in its **Power & battery** view (`6`), with a one-line
`BATT` summary on a wide Overview, and the plain screen (`--once`, a pipe, `/text`) prints a `BATT`
block. Rails and charge share a view because that is how consumption is read: the draw over a window
against the charge that window cost.

A narrow terminal gives up the charge bar and then wraps the readings onto their own lines; it does not
drop one. Neither viewer colours the charge, for the reason in Not included.

The view's Recent history panel draws both the power rails and this group: the charge, the gauge's own
count in µAh or µWh, and the draw where the driver reports one ([history.md](history.md)). That is what a
consumption reading is made of — the draw over a window against the charge that window cost — so the two
are graphed on the same view. The history is in memory only and keeps the last few minutes; a fleet
server is the store for longer than that.

## Diagnostics

The group's `collector` entry follows the usual rules ([api.md](api.md), Optional metrics):

- `ok`: at least one supply was read. An attribute the driver does not expose is not a problem, so a
  gauge that reports less than another's does not make the group `partial`.
- `unavailable` / `not_detected`: no `/sys/class/power_supply`, or it is empty — the usual case on a
  server or an SBC with no battery and no charger.
- `partial` or `error`: a supply is there but something could not be read (`permission_denied`) or is not
  a value (`invalid_data`, e.g. a `capacity` outside 0 to 100). A supply whose `type` cannot be read is
  left out, noted as `not_exposed`, because without the type there is no telling a battery from a charger.

A fleet server may treat any current group in `error` or `partial` as an optional-endpoint error for that
node, so the distinction above matters beyond platmon: absence is `unavailable`, not an error.

## Configuration

```ini
[battery]
enabled = yes
interval = 60
```

`interval` is 10 to 3600 seconds. The floor is 10 rather than 1 so no configuration can put the reading
near the driver's 1 s cache, where every read pays the full firmware call.

## Not included

No charge or discharge control, no calibration, no battery threshold in `[thresholds]`: for a battery a
low reading is the bad one, the opposite of every other level platmon colours, so the viewers show the
charge without a warning colour. No battery history series. No `temp`, `time_to_empty_now` or
`time_to_full_now`, and no serial number or identifying string.

Only the kernel's class is read. A host whose charge is reported by an external device rather than by
`/sys/class/power_supply` is not covered; what adding such a source would take, and why it would stay a
path platmon reads rather than one it is pushed to, is in
[development/remaining-work.md](development/remaining-work.md).
