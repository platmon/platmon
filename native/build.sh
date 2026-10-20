#!/usr/bin/env bash
# The candidate's build, in a throwaway container (no Rust toolchain on the host needed):
#   native/build.sh   -> native/target/{x86_64,aarch64}-unknown-linux-musl/release/platmon-native
# The system allocator only: no features (cache-alloc and mimalloc are records, built by their own scripts,
# never here and never with --all-features), Cargo.lock as committed (--locked), both static musl targets.
# Each binary must carry the system allocator's mark (src/main.rs, ALLOCATOR_MARK); then the SHA-256.
# The shell in the container stops at the first failure too (-e there, not only here): a failed test fails the build.
set -euo pipefail
cd "$(dirname "$0")"
docker run --rm -u "$(id -u):$(id -g)" -e CARGO_HOME=/src/target/cargo-home -v "$PWD":/src -w /src rust:1.88-slim bash -euo pipefail -c '
  rustup target add x86_64-unknown-linux-musl aarch64-unknown-linux-musl >/dev/null
  cargo test -q --release --locked
  for t in x86_64-unknown-linux-musl aarch64-unknown-linux-musl; do
    RUSTFLAGS="-C linker=rust-lld -C target-feature=+crt-static" cargo build -q --release --locked --target $t
  done'
for t in x86_64-unknown-linux-musl aarch64-unknown-linux-musl; do
  marks=$(grep -a -o 'platmon-native-allocator=[a-z-]*;' "target/$t/release/platmon-native" | sort -u | tr '\n' ' ' || true)
  if [ "$marks" != "platmon-native-allocator=system; " ]; then
    echo "target/$t/release/platmon-native: allocator mark '$marks', not the system allocator's" >&2
    exit 1
  fi
done
sha256sum target/*-unknown-linux-musl/release/platmon-native
