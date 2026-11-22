#!/usr/bin/env bash
# Install the `platmon` command: the terminal client, the static binary native/build.sh (amd64, arm64) or
# native/build-armhf.sh (32-bit ARM) built for this machine. No Python needed; the device itself or a Linux PC
# that watches it remotely.
#   scripts/install-cli.sh                    /usr/local/bin/platmon (asks for sudo)
#   scripts/install-cli.sh --user             ~/.local/bin/platmon (no sudo)
#   scripts/install-cli.sh --uninstall [--user]
# The service scripts run this too (systemd/install.sh: /usr/local/bin, docker/start.sh: --user), so on a
# device the command comes with the service; run it yourself on a PC that only watches devices.
set -euo pipefail

usage() { echo "usage: $0 [--user] [--uninstall]" >&2; exit 2; }
dir=/usr/local/bin
uninstall=false
for arg in "$@"; do
    case $arg in
        --user) dir=$HOME/.local/bin ;;
        --uninstall) uninstall=true ;;
        *) usage ;;
    esac
done
target=$dir/platmon
repo=$(cd "$(dirname "$0")/.." && pwd)
# the build for this machine (as deploy/systemd/install.sh finds the server's)
case "$(uname -m)" in
    x86_64) src=x86_64-unknown-linux-musl build=native/build.sh ;;
    aarch64 | arm64) src=aarch64-unknown-linux-musl build=native/build.sh ;;
    armv6l | armv7l | armv8l) src=arm-unknown-linux-musleabihf build=native/build-armhf.sh ;;
    *) src= build= ;;
esac
src=$repo/native/target/$src/release/platmon

if [ "$dir" = /usr/local/bin ] && [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

# only ever replace or remove our own command: the binary (its usage line) or the Python one before it (its marker)
if [ -e "$target" ] && ! grep -aq -e '^# platmon-cli:' -e 'usage: platmon \[-h\] \[--once\]' "$target"; then
    echo "$target exists and is not the platmon client; leaving it alone" >&2
    exit 1
fi

if $uninstall; then
    rm -f "$target"
    echo "removed $target"
    exit 0
fi

if [ -z "$build" ]; then
    echo "no platmon command for $(uname -m)" >&2
    exit 1
fi
if [ ! -f "$src" ]; then
    echo "$src not found: run $build first" >&2
    exit 1
fi
mkdir -p "$dir"
install -m 755 "$src" "$target"
echo "installed $target"
case ":$PATH:" in
    *":$dir:"*) ;;
    *)  # e.g. a non-login ssh command; Ubuntu's ~/.profile and many zsh setups add ~/.local/bin in login shells
        echo "note: $dir is not in this shell's PATH. A new login shell often has it; if 'platmon' is still"
        echo "      not found there, add $dir to PATH or run $target" ;;
esac
