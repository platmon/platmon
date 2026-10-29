#!/usr/bin/env bash
# B C C B: two platmon-native builds (B, C, e.g. the system allocator and the cache-alloc build), 30 s warmup +
# 120 s each, one JSON line per window. Each line has CPU, RSS over the run, collection times, missed deadlines
# and how often each group was in each state, so a cheaper run can be told apart from one that read less.
# stderr (group problems as the service logs them) goes to OUT.jsonl.log.
#   native/tools/bench.sh PATH/TO/platmon-native OUT.jsonl PATH/TO/OTHER
set -euo pipefail
bin=$1 out=$2 other=$3
: > "$out"; : > "$out.log"
for v in B C C B; do
  echo "== $v" >> "$out.log"
  case $v in
    B) "$bin" loop 120 30 ;;
    C) "$other" loop 120 30 ;;
  esac >> "$out" 2>> "$out.log"
done
