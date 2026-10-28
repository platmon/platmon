#!/usr/bin/env bash
# Deterministic equivalence on fake trees (tools/fixture.py), in a throwaway container: it needs to mount
# the trees over /sys and /proc, which an unprivileged process cannot, and to add interfaces to its own network
# namespace (--network none: nothing but lo, and nothing reaches the host). Builds the local image
# platmon-native-fixture:local (Debian 13 and its python3; never pushed) on first use.
#   native/build.sh && native/tools/fixture.sh [SCENARIO ...]
#   native/tools/fixture.sh --record [SCENARIO ...]   # also writes the Python answers to tools/golden/fixture
#   native/tools/fixture.sh --golden [SCENARIO ...]   # the recorded answers in place of the Python side
#   BINARY=native/target/<dir>/x86_64-unknown-linux-musl/release/platmon-native native/tools/fixture.sh   # another build
# The host name and the tmpfs sizes are fixed, so that the answers depend on the trees, not on this host.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
binary=${BINARY:-native/target/x86_64-unknown-linux-musl/release/platmon-native}  # relative to the repository
image=platmon-native-fixture:local
golden=native/tools/golden/fixture
mounts=() mode=()
case "${1:-}" in
  --record)
    mkdir -p "$repo/$golden"
    mounts=(-v "$repo/$golden":"/platmon/$golden")  # the only writable path
    mode=(--record "/platmon/$golden"); shift ;;
  --golden)
    mode=(--golden "/platmon/$golden"); shift ;;
esac
if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf 'FROM debian:13\nRUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*\n' |
    docker build -q -t "$image" - >/dev/null
fi
docker run --rm --cap-add SYS_ADMIN --cap-add NET_ADMIN --security-opt apparmor=unconfined --network none \
  --hostname platmon-fixture -e OWNER="$(id -u):$(id -g)" \
  -v "$repo":/platmon:ro ${mounts[@]+"${mounts[@]}"} "$image" sh -ec '
    mkdir -p /fx /fxhost /realproc
    mount -t tmpfs -o size=64m fx /fx && mount -t tmpfs -o size=64m fxhost /fxhost && mkdir /fx/sys /fx/proc
    mount --rbind /proc /realproc
    mount --bind /fx/sys /sys && mount --bind /fx/proc /proc
    status=0
    python3 /platmon/native/tools/fixture.py "/platmon/$0" "$@" || status=$?
    if [ "${1:-}" = --record ]; then chown -R "$OWNER" "$2"; fi
    exit $status
  ' "$binary" ${mode[@]+"${mode[@]}"} "$@"
