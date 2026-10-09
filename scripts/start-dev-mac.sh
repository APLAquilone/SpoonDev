#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(basename "$PWD")" != *-dev ]]; then echo 'Run this script inside the separate SpoonDev-dev checkout'; exit 1; fi
case "${SPOONDEV_COLLECT:-0}" in 0|1) ;; *) echo 'SPOONDEV_COLLECT must be 0 or 1'; exit 1 ;; esac
python - <<'PY'
import socket,sqlite3,sys
from pathlib import Path
if sys.version_info < (3,12): raise SystemExit('Activate the spoondev Python 3.12 environment')
p=Path('data/accounts.sqlite3').resolve()
if not p.is_file(): raise SystemExit('First create a Dev login: python -m spoondev create-user kitomoya')
with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as c:
    if not c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]: raise SystemExit('Create a Dev account first')
with socket.socket() as s:
    try: s.bind(('127.0.0.1',8081))
    except OSError: raise SystemExit('Stop the existing Dev web process with Ctrl+C first')
PY
umask 077
web_pid=''; worker_pid=''
cleanup(){
    if [ -n "$web_pid" ]; then kill "$web_pid" 2>/dev/null || true; fi
    # Only stop the child this launcher owns. Another launcher may already
    # hold the DB lease, in which case our short-lived child has exited.
    if [ -n "$worker_pid" ]; then
        parent=$(ps -p "$worker_pid" -o ppid= 2>/dev/null || true)
        if [ "${parent//[[:space:]]/}" = "$$" ]; then
            kill "$worker_pid" 2>/dev/null || true
            wait "$worker_pid" 2>/dev/null || true
        fi
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
if [ "${SPOONDEV_COLLECT:-0}" = 1 ]; then
    python -m spoondev collect-auto >> data/collector.log 2>&1 &
    worker_pid=$!
    printf 'Automatic Dev collection enabled. Log: data/collector.log\n'
else
    printf 'Dev uses saved observations. Enable its separate collector with SPOONDEV_COLLECT=1.\n'
fi
printf 'Development only: http://127.0.0.1:8081\n'
python -m spoondev serve --auth --host 127.0.0.1 --port 8081 &
web_pid=$!
wait "$web_pid"
