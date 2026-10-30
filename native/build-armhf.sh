#!/usr/bin/env bash
# The 32-bit ARM build, released as armhf (native/README.md, 32-bit ARM). Same image, flags and system
# allocator as build.sh, for arm-unknown-linux-musleabihf (ARMv6 with VFP, so Raspberry Pi OS's armhf and
# Debian's ARMv7 armhf both run it):
#   native/build-armhf.sh   -> native/target/arm-unknown-linux-musleabihf/release/platmon-server
#                              and platmon-server-tests next to it (the unit tests, not run here)
# An x86 machine cannot run either; a 32-bit ARM host or a 64-bit ARM kernel with 32-bit support can (the
# Raspberry Pi 4's Debian 13 runs them as they are; GitHub's arm64 runners, Neoverse N2, are AArch64 only):
# copy both there, run platmon-server-tests, then the server through the native/tools checks.
set -euo pipefail
cd "$(dirname "$0")"
t=arm-unknown-linux-musleabihf
docker run --rm -u "$(id -u):$(id -g)" -e CARGO_HOME=/src/target/cargo-home -v "$PWD":/src -v "$PWD/../VERSION":/VERSION:ro -w /src rust:1.88-slim bash -euo pipefail -c '
  rustup target add '"$t"' >/dev/null
  export RUSTFLAGS="-C linker=rust-lld -C target-feature=+crt-static"
  cargo build -q --release --locked --no-default-features --target '"$t"'
  tests=$(cargo test -q --release --locked --no-default-features --target '"$t"' --no-run --message-format=json |
    grep -o "\"executable\":\"[^\"]*\"" | cut -d\" -f4)
  cp "$tests" target/'"$t"'/release/platmon-server-tests'
marks=$(grep -a -o 'platmon-server-allocator=[a-z-]*;' "target/$t/release/platmon-server" | sort -u | tr '\n' ' ' || true)
if [ "$marks" != "platmon-server-allocator=system; " ]; then
  echo "target/$t/release/platmon-server: allocator mark '$marks', not the system allocator's" >&2
  exit 1
fi
sha256sum "target/$t/release/platmon-server"
