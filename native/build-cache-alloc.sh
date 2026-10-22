#!/usr/bin/env bash
# The cache-alloc build, kept as the record of the 2026-10-18/19 measurements (README); not a candidate:
#   native/build-cache-alloc.sh   -> native/target/cache-alloc/{x86_64,aarch64}-unknown-linux-musl/release/platmon-native
set -euo pipefail
cd "$(dirname "$0")"
docker run --rm -u "$(id -u):$(id -g)" -e CARGO_HOME=/src/target/cargo-home -v "$PWD":/src -w /src rust:1.88-slim bash -euo pipefail -c '
  rustup target add x86_64-unknown-linux-musl aarch64-unknown-linux-musl >/dev/null
  for t in x86_64-unknown-linux-musl aarch64-unknown-linux-musl; do
    RUSTFLAGS="-C linker=rust-lld -C target-feature=+crt-static" cargo build -q --release --locked --target $t \
      --features cache-alloc --target-dir target/cache-alloc
  done'
sha256sum target/cache-alloc/*/release/platmon-native
