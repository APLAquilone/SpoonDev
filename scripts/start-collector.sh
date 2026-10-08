#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p data
chmod 700 data
python -m spoondev --db data/spoondev.sqlite3 init-db
nohup flock --nonblock --no-fork data/collector.lock \
  python -u -m spoondev --db data/spoondev.sqlite3 collect-spoon \
    --max-rooms 0 --max-pages 100 --interval 300 --concurrency 4 \
    >> data/collector.log 2>&1 < /dev/null &
collector_pid=$!
printf '%s\n' "$collector_pid" > data/collector.last-start.pid
printf 'Collector start requested, PID %s. Check data/collector.log and new DB observations.\n' "$collector_pid"
