#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p data
chmod 700 data
nohup flock --nonblock --no-fork data/web.lock \
  python -u -m spoondev --db data/spoondev.sqlite3 serve --host 127.0.0.1 --port 8080 \
  >> data/web.log 2>&1 < /dev/null &
web_pid=$!
printf '%s\n' "$web_pid" > data/web.last-start.pid
printf 'Search site start requested, PID %s. Check data/web.log and HTTP responses on port 8080.\n' "$web_pid"
