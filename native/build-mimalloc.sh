#!/usr/bin/env bash
# Build the mimalloc variant for this machine's own architecture, in the same rust:1.88-slim image as
# build.sh, natively (its C needs a C compiler for the target; this image has none for the other one):
#   native/build-mimalloc.sh   -> native/target/mimalloc/<arch>-unknown-linux-musl/release/platmon-native
# mimalloc's C is compiled by musl-gcc (Debian's musl-tools) at the compiler's default CPU baseline, never
# -march=native; the compile commands go to target/mimalloc/<arch>-cc.txt.
set -euo pipefail
cd "$(dirname "$0")"
arch=$(uname -m)
docker run --rm -v "$PWD":/src -w /src -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" rust:1.88-slim bash -euo pipefail -c '
  apt-get update -qq >/dev/null && apt-get install -y -qq musl-tools >/dev/null
  mkdir -p target/mimalloc
  export CARGO_HOME=/src/target/cargo-home t='"$arch"'-unknown-linux-musl
  rustup target add $t >/dev/null
  echo "musl-gcc: $(musl-gcc --version | head -1); default $(gcc -Q --help=target 2>/dev/null | grep -E "^ +-march=" | tr -s " \t" " ")"
  CC_ENABLE_DEBUG_OUTPUT=1 RUSTFLAGS="-C linker=rust-lld -C target-feature=+crt-static" \
    cargo build -q --release --locked --target $t --features mimalloc --target-dir target/mimalloc
  grep -h "static\.c" target/mimalloc/$t/release/build/libmimalloc-sys-*/output | sort -u > target/mimalloc/'"$arch"'-cc.txt
  cargo test -q --release --locked --features mimalloc --target $t --target-dir target/mimalloc 2>&1 | grep -E "test result|FAILED|panicked"
  chown -R $HOST_UID:$HOST_GID target'
cat "target/mimalloc/$arch-cc.txt"
sha256sum "target/mimalloc/$arch-unknown-linux-musl/release/platmon-native"
