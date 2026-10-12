#!/usr/bin/env bash
# Start the installed platmon service, or restart it if it is running (picks up new code and config),
# then wait until /api/stats answers.
#   deploy/systemd/start.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"

prefix=/opt/platmon
config=/etc/platmon/platmon.ini
unit=/etc/systemd/system/platmon.service

if [ ! -f "$unit" ]; then
    echo "platmon is not installed; run deploy/systemd/install.sh" >&2
    exit 1
fi
# both ways serve port 9797; use one at a time
if command -v docker >/dev/null && [ -n "$(docker ps -q --filter label=com.docker.compose.project=platmon 2>/dev/null)" ]; then
    echo "platmon is running in Docker; stop it first: deploy/docker/stop.sh" >&2
    exit 1
fi

# where to check: the installed code reads the config ("host port")
listen=$(python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from platmon import load_config
try:
    http = load_config(sys.argv[2])["http"]
except ValueError as e:
    sys.exit(f"invalid config: {e}")
print("127.0.0.1" if http["bind"] == "0.0.0.0" else http["bind"], http.getint("port"))
' "$prefix" "$config")
read -r host port <<< "$listen"
# how to look at it: plain `platmon` covers localhost:9797, anything else needs the address
addr=$host
[ "$addr" = 127.0.0.1 ] && addr=localhost
view=platmon
[ "$addr:$port" = localhost:9797 ] || view="platmon $addr:$port"

systemctl restart platmon

check='import sys, urllib.request; urllib.request.urlopen(f"http://{sys.argv[1]}:{sys.argv[2]}/api/stats", timeout=2)'
for _ in $(seq 20); do
    if python3 -c "$check" "$host" "$port" 2>/dev/null; then
        journalctl -u platmon -n 2 --no-pager -o cat
        echo "platmon is up on port $port; look at it with: $view"
        exit 0
    fi
    sleep 0.5
done
echo "platmon did not answer on $host:$port; recent log:" >&2
journalctl -u platmon -n 20 --no-pager >&2
exit 1
