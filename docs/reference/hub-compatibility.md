# platmon-hub compatibility

> **TL;DR**
> - Which [platmon-hub](https://github.com/platmon/platmon-hub) release reads which platmon release, and
>   what each pairing was checked by. Current releases: platmon 1.8.1, platmon-hub 1.1.1.
> - platmon-hub 1.1.1 reads every platmon release from 1.0.0 to 1.8.1. Its opt-in raw capture needs platmon
>   1.3.0 or later; older releases have no replay, and Hub 1.1.0 and later capture their polled documents instead.
> - CI checks every build of `main` against platmon-hub 1.1.1, pinned by its PyPI hash.

Hub validates each endpoint's schema, not platmon's application version (platmon-hub's
[Agent contract](https://github.com/platmon/platmon-hub/blob/main/docs/design/agent-contract.md)); additions
it does not know are ignored, not errors. A pairing is listed as checked only where one of the checks below
ran on it.

| platmon | platmon-hub 1.1.1 | platmon-hub 1.0.0, 1.1.0 |
| --- | --- | --- |
| next release (`main`) | **CI**: every build | not checked |
| 1.8.1 | **CI**: every build up to its tag; **devices** (1.8.1 release gate) | not checked |
| 1.8.0 | **CI**: every build up to its tag | not checked |
| 1.7.0 | **contract**, 2026-11-11 | not checked |
| 1.6.0 | **contract**, 2026-11-11 | not checked |
| 1.5.0 | **contract**, 2026-11-11 | not checked |
| 1.4.0 | **devices** (1.4.0 release gate); **contract**, 2026-11-11 | not checked |
| 1.3.0 | **declared** | **declared**; **devices**, 1.1.0 (1.3.0 release gate) |
| 1.0.0 to 1.2.1 | **declared**; no replay: sampled capture | **declared**; no replay: sampled capture in 1.1.0, no raw capture in 1.0.0 |

- **CI**: the `native` job of `.github/workflows/test.yml` runs `native/tools/equivalence/hub_contract.py`
  against the server it built, with the Hub release pinned in `native/tools/equivalence/hub-requirements.txt`:
  Hub's own validators accept `/api/status`, `/api/stats` and `/api/observations`, then a raw-capture Hub
  polls the server through a restart, and its records and database are checked.
- **contract**: the same `hub_contract.py` (from `main` on that date), run by hand against the released amd64
  server extracted from that release's `.deb`, checked against its `SHA256SUMS`.
- **devices**: a released Hub polling real devices. At 1.8.1, Hub 1.1.1 on the x86-64 PC polling the Jetson Orin
  Nano's candidate unit through the same events (`hub_check.py`), and the Orin's production service for a day
  ([readiness](../releases/readiness.md#181-release-gate-2026-11-13)). At 1.4.0, Hub 1.1.1 on the x86-64 PC, the Jetson Orin Nano
  and the Raspberry Pi 4 through an agent restart, a SIGKILL, the Hub down 60 s and the agents down 30 s
  ([readiness](../releases/readiness.md#140-release-gate-2026-10-22)). At 1.3.0, the Orin Nano and the
  Raspberry Pi 4 registered in Hub 1.1.0 on the PC with raw capture on
  ([readiness](../releases/readiness.md#130-release-gate-2026-10-17)).
- **declared**: the Hub release states the support itself (platmon-hub's `CHANGELOG.md`: "Supports platmon
  Agents 1.0.0–1.3.0" from 1.0.0, with a synthetic 1.3.0 compatibility test there; 1.1.1 is a packaging
  release of 1.1.0). No check of the pairing on a platmon server is recorded in this repository.
- **sampled capture**: Hub 1.1.0's raw capture level `auto` stores the `/api/stats` and `/api/observations`
  documents it polls when the agent has no replay (platmon before 1.3.0).

Groups a Hub does not store are accepted and dropped: platmon-hub 1.1.1 ignores the `processes`
observation ([processes.md](processes.md)).

An older Hub with a newer platmon is not checked: upgrade the Hub first. When a Hub release reads changed
answers, raise the pin in `hub-requirements.txt` in the same change, and add a column here.
