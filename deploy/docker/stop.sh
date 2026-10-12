#!/usr/bin/env bash
# Stop and remove the platmon container. The built image stays; remove it with: docker image rm platmon:local
#   deploy/docker/stop.sh
set -euo pipefail

cd "$(dirname "$0")/../.."
docker compose -f deploy/docker/compose.yaml down
