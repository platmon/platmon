#!/usr/bin/env bash
# Deterministic equivalence on fake trees (tools/fixture.py), in a throwaway container: it needs to mount
# the trees over /sys and /proc, which an unprivileged process cannot. Builds the local image
# platmon-native-fixture:local (Debian 13 and its python3; never pushed) on first use.
#   native/build.sh && native/tools/fixture.sh [SCENARIO ...]
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
image=platmon-native-fixture:local
if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf 'FROM debian:13\nRUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*\n' |
    docker build -q -t "$image" - >/dev/null
fi
docker run --rm --cap-add SYS_ADMIN --security-opt apparmor=unconfined --network none \
  -v "$repo":/platmon:ro "$image" sh -ec '
    mkdir -p /fx /fxhost /realproc
    mount -t tmpfs fx /fx && mount -t tmpfs fxhost /fxhost && mkdir /fx/sys /fx/proc
    mount --rbind /proc /realproc
    mount --bind /fx/sys /sys && mount --bind /fx/proc /proc
    exec python3 /platmon/native/tools/fixture.py /platmon/native/target/x86_64-unknown-linux-musl/release/platmon-native "$@"
  ' fixture "$@"
