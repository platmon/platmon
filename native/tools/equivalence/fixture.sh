#!/usr/bin/env bash
# Deterministic equivalence on fake trees (fixture.py, against the answers in golden/fixture), in a
# throwaway container: it needs to mount the trees over /sys and /proc, which an unprivileged process cannot,
# and to add interfaces to its own network namespace (--network none: nothing but lo, and nothing reaches the
# host). Builds the local image
# platmon-server-fixture:local (Debian 13 and its python3; never pushed) on first use.
#   native/build.sh && native/tools/equivalence/fixture.sh [SCENARIO ...]
#   native/tools/equivalence/fixture.sh --bless [SCENARIO ...]   # rewrite golden/fixture from the server's answers
#   BINARY=native/target/<dir>/x86_64-unknown-linux-musl/release/platmon-server native/tools/equivalence/fixture.sh   # another build
# The host name and the tmpfs sizes are fixed, as they were when the answers were recorded.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../../.." && pwd)
binary=${BINARY:-native/target/x86_64-unknown-linux-musl/release/platmon-server}  # relative to the repository
image=platmon-server-fixture:local
golden=native/tools/equivalence/golden/fixture
bless=()  # --bless: golden/fixture writable, its files given back to this user afterwards
for arg in "$@"; do
  [ "$arg" != --bless ] || bless=(-v "$repo/$golden:/platmon/$golden" -e "OWNER=$(id -u):$(id -g)")
done
if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf 'FROM debian:13\nRUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*\n' |
    docker build -q -t "$image" - >/dev/null
fi
docker run --rm --cap-add SYS_ADMIN --cap-add NET_ADMIN --security-opt apparmor=unconfined --network none \
  --hostname platmon-fixture \
  -v "$repo":/platmon:ro "${bless[@]}" "$image" sh -ec '
    mkdir -p /fx /fxhost /realproc
    mount -t tmpfs -o size=64m fx /fx && mount -t tmpfs -o size=64m fxhost /fxhost && mkdir /fx/sys /fx/proc
    mount --rbind /proc /realproc
    mount --bind /fx/sys /sys && mount --bind /fx/proc /proc
    status=0
    python3 /platmon/native/tools/equivalence/fixture.py "/platmon/$0" "$@" || status=$?
    [ -z "${OWNER:-}" ] || chown -R "$OWNER" /platmon/native/tools/equivalence/golden/fixture
    exit $status
  ' "$binary" "$@"
