#!/usr/bin/env bash
# A B B A: Python collector (A) and platmon-native (B), 30 s warmup + 120 s each, one JSON line per window.
#   PYTHONPATH=<platmon tree> native/tools/bench.sh PATH/TO/platmon-native OUT.jsonl
set -euo pipefail
bin=$1 out=$2 here=$(cd "$(dirname "$0")" && pwd)
: > "$out"
for v in A B B A; do
  if [ $v = A ]; then python3 "$here/pyloop.py" 30 120; else "$bin" loop 120 30 2>/dev/null; fi >> "$out"
done
