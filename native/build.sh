#!/usr/bin/env bash
# Build the static musl binaries in a throwaway container (no Rust toolchain on the host needed):
#   native/build.sh   -> native/target/{x86_64,aarch64}-unknown-linux-musl/release/platmon-native
set -euo pipefail
cd "$(dirname "$0")"
docker run --rm -u "$(id -u):$(id -g)" -e CARGO_HOME=/src/target/cargo-home -v "$PWD":/src -w /src rust:1.88-slim bash -c '
  rustup target add x86_64-unknown-linux-musl aarch64-unknown-linux-musl >/dev/null 2>&1 || true
  cargo test -q --release
  for t in x86_64-unknown-linux-musl aarch64-unknown-linux-musl; do
    RUSTFLAGS="-C linker=rust-lld -C target-feature=+crt-static" cargo build -q --release --locked --target $t
  done'
sha256sum target/*-unknown-linux-musl/release/platmon-native
