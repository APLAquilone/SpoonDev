#!/usr/bin/env bash
# HTTPS trial tunnel; accounts and private data stay on this Mac.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v cloudflared >/dev/null || { echo 'Run: brew install cloudflared'; exit 1; }
python -c 'import sys; assert sys.version_info >= (3,12), "Activate the spoondev Python 3.12 environment"'
python - <<'PY'
import socket,sqlite3
from pathlib import Path
p=Path('data/accounts.sqlite3')
if not p.is_file(): raise SystemExit('First run: python -m spoondev create-user kitomoya --claim-local-data')
with sqlite3.connect(p) as c:
    if not c.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]: raise SystemExit('Create an account first')
with socket.socket() as s:
    try: s.bind(('127.0.0.1',8080))
    except OSError: raise SystemExit('Stop the existing serve process with Ctrl+C; keep collectors running')
PY
umask 077
public_tmp=$(mktemp -d)
web_pid=''; tunnel_pid=''; awake_pid=''
cleanup(){ for pid in "$web_pid" "$tunnel_pid" "$awake_pid"; do if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi; done; rm -rf "$public_tmp"; }
trap cleanup EXIT
trap 'exit 130' INT TERM
printf '{}\n' > "$public_tmp/config.yml"
cloudflared --config "$public_tmp/config.yml" tunnel --url http://127.0.0.1:8080 --no-autoupdate > "$public_tmp/tunnel.log" 2>&1 &
tunnel_pid=$!
public_url=$(python - "$public_tmp/tunnel.log" <<'PY'
import pathlib,re,sys,time
p=pathlib.Path(sys.argv[1])
for _ in range(120):
    text=p.read_text() if p.exists() else ''
    urls=re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com',text)
    if urls: print(urls[-1]); break
    time.sleep(.5)
else: raise SystemExit('Tunnel failed to start. Check network connectivity and retry.')
PY
)
python -m spoondev serve --auth --host 127.0.0.1 --port 8080 --public-url "$public_url" &
web_pid=$!
python - <<'PY'
import time,urllib.request,urllib.error
for _ in range(40):
    try: urllib.request.urlopen('http://127.0.0.1:8080/api/stats',timeout=1)
    except urllib.error.HTTPError as e:
        if e.code in (401,403): break
    except OSError: pass
    time.sleep(.25)
else: raise SystemExit('Authenticated web server did not become ready')
PY
if command -v caffeinate >/dev/null; then caffeinate -i -w "$web_pid" & awake_pid=$!; fi
printf '\nOpen this URL and log in: %s\nKeep this terminal open. Ctrl+C stops public access.\n' "$public_url"
wait "$web_pid"
