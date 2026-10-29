# Docker observation scope

> **TL;DR**
> - What the container sees, checked on a Jetson Orin Nano (2026-10-09, and with the native image on
>   2026-10-30): CPU, memory, GPU, sensors and disks match the host with the supplied mounts.
> - Compose uses `network_mode: host` to see host network and Wi-Fi; still non-root, read-only mounts, no
>   privileged mode.
> - Filesystem capacity through file bind mounts is partial or `null`.

Checked on Orin Nano on 2026-10-09 with native Linux Docker Engine, when the image ran the Python
server, and again with the native image on 2026-10-30 ([below](#native-image-check)): the host-network
column holds for it. The same collector was read on the host, in the production bridge container, and in an isolated host-network container.
Dynamic readings differ in time; this comparison checks availability, identity and scope.

| Item | Bridge container | Host-network configuration |
| --- | --- | --- |
| CPU usage/frequency | all six cores and frequency sources visible | unchanged |
| Memory/swap, uptime | host values visible | unchanged |
| Board, host OS/kernel/L4T, power mode | same as host with supplied mounts | unchanged |
| GPU usage/frequency | visible | unchanged |
| Temperature / power / fans | six temperatures, three rails, two fan entries; all 20 sensor source records match native | unchanged |
| Network | only lo/eth0 in a separate namespace | host interfaces and namespace identities |
| Wi-Fi | host wireless interface absent | wlP1p1s0 connected, negative dBm, same namespace/ifindex as native |
| Disk I/O / partitions | host NVMe and all 15 partitions visible | unchanged |
| Root disk capacity | works through /host/root | unchanged |
| Storage filesystem capacity | old first-file-bind selection returns null | fixed reader skips ENOTDIR and measures a verified directory |
| Other mounted filesystems | host /boot/efi is not exposed by the non-recursive root bind | still requires an explicit read-only directory bind |
| PSI | unavailable on the Orin host too; kernel lacks CONFIG_PSI | networking cannot enable kernel PSI |
| Probe | configured targets use the container network; loopback is container-local | loopback means the host; empty targets remain idle |
| History / CLI / web | only records/displays the data the collectors can see | restored Network/Wi-Fi data becomes available to the same consumers |

## Native image check

On 2026-10-30 the image built from `main` at `0ba5e2c` (the native server on `debian:13-slim`, 142 MB,
built on the Orin) ran on the Orin Nano (L4T R36.5.2, Docker 29.9.2) with the shipped Compose and
Jetson settings (host network, host UTS, the device tree, root and nvpmodel mounts), on its own INI at
127.0.0.1:19799, beside the production `platmon.service` (the 1.6.0 package) on 9797. Both were read
after every slow group had observed once:

- the same: platform, model, power mode, OS, kernel, L4T and host name; six CPUs with their clocks; GPU
  usage and clocks; six temperatures, three INA3221 rails and two fans with the same 20 sensor source
  ids; every group's state and reason (NPU `not_detected`, PSI `not_exposed`, battery unavailable); the
  network namespace id and all nine interfaces with their ifindex; the NVMe disk; Wi-Fi `wlP1p1s0`
  connected with a dBm signal and the same namespace and ifindex; 15 partitions; the clock
  (`boottime`); the `/text` sections
- storage: the root filesystem with the same capacity (981,814,759,424 bytes), under the container's
  own mount paths; `/boot/efi` only with the extra bind of [Filesystems](#filesystems), then the same
  66,059,264 bytes as the host
- history: 40 series against the host's 44: the EFI filesystem's two and two of a `veth` interface that
  had gone before the container started (the host's 600 s window still held it)
- the image's health check `healthy` on the non-default port; user 65534:65534, not privileged;
  `docker stop` ended it in 0.17 s with exit code 0

The container, the image and the staged source were removed afterwards; the production service did not
restart (`NRestarts=0`), and the earlier Compose container and images on the board were not touched.

## Networking

The shipped Compose configuration uses `network_mode: host`, authorized by the owner to expose
host Network and Wi-Fi. The HTTP bind and port come from `platmon.ini`; host mode has no published
port mapping. The image health check and start script use that configured listener, including a
non-default port. A custom bridge override must remove host network mode and publish its own port.

Host mode removes network namespace isolation and changes the meaning of Probe loopback. It does
not require privileged mode, host PID namespace, root execution, writable host mounts or Docker socket
access. The existing non-root user and mount boundaries remain. This scope was tested on native Linux
Docker Engine; Docker Desktop's VM networking is not evidence of access to the physical host's Wi-Fi.
See the [Docker host driver documentation](https://docs.docker.com/engine/network/drivers/host/).

## Filesystems

The root bind stays read-only and non-recursive. Making it recursive would expose unrelated mounts
such as /proc, /sys and /run; that is not needed for this fix. Additional local filesystems must be
listed explicitly. An example extra Compose file, used only where /boot/efi actually exists:

```yaml
services:
  platmon:
    volumes:
      - type: bind
        source: /boot/efi
        target: /host/boot/efi
        read_only: true
        bind:
          recursive: disabled
          create_host_path: false
```

Include that file explicitly after the usual compose files when running Docker Compose. The generic
start script does not auto-discover host mounts. In the isolated Orin check, this read-only bind made
EFI capacity visible: 66,059,264 bytes, matching native; root capacity was 981,814,759,424 bytes.
No operating host mount is added or changed. Unmounted partitions still have size but no filesystem
capacity; permission errors, device changes and file-only mount groups remain explicit.

## Evidence and limits

Baseline runtime: `6984ff6`; Storage fix: `d1b76de`. Three new regression cases failed before the
Storage fix; all 36 Storage tests pass after it. Source fields, sensor sources, partition identities
and capacities were compared, not merely endpoint status codes. Raw snapshots and comparison output
are retained locally in `.validation/container-host-audit/` and on Orin in
`~/platmon-container-audit-6984ff6/`. Candidate containers/networks and test ports are reclaimed.
No new CPU-budget or normal PSI performance claim is made by this availability audit.

## Final candidate checks

Candidate `ce16d07` passed an isolated host-network run on 127.0.0.1:19799. The image's own health
check became healthy on that non-default port. Every host interface matched `socket.if_nameindex`,
Network/Wi-Fi namespace IDs agreed, Wi-Fi was connected at -50 dBm, Storage was `ok`, and 38 History
series included Wi-Fi. `/text` displayed Wi-Fi and Storage. The non-root user was 65534:65534 and
privileged mode was false. The candidate container and port were removed, and the operating bridge
container's identity/start time stayed unchanged during this check.

Pi PSI was separately enabled with the owner's approval and password-authenticated administrator
step. After reboot and native service restart, CPU/memory/I/O pressure collectors report `ok`.
Orin kernel replacement was explicitly deferred. [PR #48](https://github.com/jwsung91/platmon/pull/48)
records final main CI and deployment status. Historical bridge limitations in earlier reports refer
to their measured revisions and are not the current host-network configuration.
