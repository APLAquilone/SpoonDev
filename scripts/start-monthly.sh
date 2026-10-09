#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p data
chmod 700 data
nohup flock --nonblock --no-fork data/monthly.lock \
  python -u -m spoondev --db data/spoondev.sqlite3 collect-monthly \
    --max-djs 0 --max-pages 0 --interval 3600 --concurrency 4 \
    >> data/monthly.log 2>&1 < /dev/null &
monthly_pid=$!
printf '%s\n' "$monthly_pid" > data/monthly.last-start.pid
printf 'Monthly index start requested, PID %s. Check data/monthly.log and monthly observations.\n' "$monthly_pid"
