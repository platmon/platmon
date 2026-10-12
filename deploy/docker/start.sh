#!/usr/bin/env bash
# Build the platmon image from this checkout and run it with Docker Compose; on Jetson the nvpmodel
# mounts (deploy/docker/compose.jetson.yaml) are added automatically. Also installs or updates the `platmon` command in
# ~/.local/bin (no sudo). Run it again after a git pull to update both.
#   deploy/docker/start.sh
set -euo pipefail

cd "$(dirname "$0")/../.."

# both ways serve port 9797; use one at a time. A systemd service that is only stopped but still
# enabled would take the port back at the next boot, so it has to be removed (or disabled) first.
if systemctl is-active --quiet platmon 2>/dev/null || systemctl is-enabled --quiet platmon 2>/dev/null; then
    echo "platmon is installed as a systemd service (running or enabled at boot); remove it first:" >&2
    echo "  deploy/systemd/uninstall.sh   (or keep it installed but off: sudo systemctl disable --now platmon)" >&2
    exit 1
fi

files=(-f deploy/docker/compose.yaml)
if [ -e /var/lib/nvpmodel/status ] && [ -e /etc/nvpmodel.conf ]; then
    files+=(-f deploy/docker/compose.jetson.yaml)
    echo "Jetson: adding deploy/docker/compose.jetson.yaml"
fi
# Build without the default provenance attestation: it records the build time and git commit, so identical
# content got a new image id on every build, the tag moved off the running container's image and that image
# was deleted. deploy/docker/compose.yaml says provenance: false as well, but not every compose version passes it on.
export BUILDX_NO_DEFAULT_ATTESTATIONS=1

# remember the container before "up" to report what compose did. It recreates the container when the
# rebuilt image or the config differs.
before=$(docker compose "${files[@]}" ps -aq platmon)
was_running=false
if [ -n "$before" ]; then
    was_running=$(docker inspect -f '{{.State.Running}}' "$before")
fi

docker compose "${files[@]}" up -d --build

container=$(docker compose "${files[@]}" ps -q platmon)
if [ -z "$before" ]; then
    change="started: new container"
elif [ "$container" != "$before" ]; then
    change="updated: container recreated with the rebuilt image or changed config"
elif [ "$was_running" = true ]; then
    change="unchanged: running container kept (same image and config)"
else
    change="started: existing container was stopped"
fi
echo "$change"

# the `platmon` command to look at it, same version as the server; a failure here must not stop the start
scripts/install-cli.sh --user || echo "the platmon command was not installed (see above)" >&2

echo "waiting for the health check..."
state=starting
for _ in $(seq 30); do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$container")
    case $state in
        healthy)
            docker compose "${files[@]}" logs --no-log-prefix --tail 2
            # Host mode has no published-port mapping. Read the effective listener inside the container.
            mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container")
            host=localhost
            if [ "$mode" = host ]; then
                listen=$(docker compose "${files[@]}" exec -T platmon python3 -c '
from platmon import load_config
h = load_config("/opt/platmon/platmon.ini")["http"]
print("localhost" if h["bind"] in ("0.0.0.0", "127.0.0.1") else h["bind"], h.getint("port"))
')
                read -r host port <<< "$listen"
            else
                port=$(docker compose "${files[@]}" port platmon 9797 | head -1)
                port=${port##*:}
            fi
            view=platmon
            [ "$host:${port:-9797}" = localhost:9797 ] || view="platmon $host:$port"
            echo "platmon is up on port ${port:-9797} (restarts with Docker at boot) - $change; look at it with: $view"
            exit 0 ;;
        unhealthy) break ;;
    esac
    sleep 2
done
echo "platmon is not healthy ($state); recent log:" >&2
docker compose "${files[@]}" logs --tail 20 >&2
exit 1
