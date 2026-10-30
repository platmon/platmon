# platmon in a container. It monitors the host, so it needs the host mounts in deploy/docker/compose.yaml
# (plus deploy/docker/compose.jetson.yaml on Jetson); see README "Docker".
# Build it yourself; images are not published (the base images contain GPL/LGPL software,
# see docs/guides/deployment.md "Container image licensing").

# The server, as native/build.sh builds it: static (musl), the system allocator, Cargo.lock as committed.
FROM rust:1.88-slim AS build
WORKDIR /src
COPY native ./native
COPY VERSION ./
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) target=x86_64-unknown-linux-musl ;; \
      aarch64) target=aarch64-unknown-linux-musl ;; \
      armv6l | armv7l | armv8l) target=arm-unknown-linux-musleabihf ;; \
      *) echo "no native build for $(uname -m)" >&2; exit 1 ;; \
    esac; \
    rustup target add "$target"; \
    cd native; \
    RUSTFLAGS="-C linker=rust-lld -C target-feature=+crt-static" \
      cargo build --release --locked --no-default-features --target "$target"; \
    mkdir /out; \
    cp "target/$target/release/platmon-server" /out/

# A plain Debian base: the server is one static binary, so no NVIDIA runtime, L4T base or Python is needed.
FROM debian:13-slim
WORKDIR /opt/platmon
COPY --from=build /out/platmon-server ./
RUN ln -s platmon-server platmon-native  # its name up to 1.6.0
COPY VERSION ./
COPY platmon.ini ./
COPY frontends/web ./frontends/web

# everything platmon reads in /sys and /proc is world-readable; no root needed
USER 65534:65534
EXPOSE 9797
# /api/stats answers 503 while there is no current data, which marks the container unhealthy. health follows
# the configured bind and port, including isolated checks on a non-default host port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["/opt/platmon/platmon-server", "health", "/opt/platmon/platmon.ini"]
# exec: the server is the container's process (compose's init forwards docker stop's SIGTERM to it)
ENTRYPOINT ["/bin/sh", "-c", "exec /opt/platmon/platmon-server serve \"$0\" --web /opt/platmon/frontends/web --app-version \"$(cat /opt/platmon/VERSION)\"", "/opt/platmon/platmon.ini"]
