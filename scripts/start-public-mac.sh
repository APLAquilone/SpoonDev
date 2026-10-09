#!/usr/bin/env bash
# HTTPS trial tunnel; accounts and private data stay on this Mac.
set -euo pipefail
cd "$(dirname "$0")/.."
case "${SPOONDEV_COLLECT:-1}" in 0|1) ;; *) echo 'SPOONDEV_COLLECT must be 0 or 1'; exit 1 ;; esac
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
web_pid=''; tunnel_pid=''; awake_pid=''; worker_pid=''
cleanup(){
    for pid in "$web_pid" "$tunnel_pid" "$awake_pid"; do if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi; done
    if [ -n "$worker_pid" ]; then
        parent=$(ps -p "$worker_pid" -o ppid= 2>/dev/null || true)
        if [ "${parent//[[:space:]]/}" = "$$" ]; then
            kill "$worker_pid" 2>/dev/null || true
            wait "$worker_pid" 2>/dev/null || true
        fi
    fi
    rm -rf "$public_tmp"; rm -f data/public-run.json
}
trap cleanup EXIT
trap 'exit 130' INT TERM
if [ -f data/public-tunnel.json ]; then
    public_url=$(python - <<'PYCONFIG'
import json,re
from pathlib import Path
from urllib.parse import urlsplit
c=json.loads(Path('data/public-tunnel.json').read_text())
u=urlsplit(c['url'])
if u.scheme!='https' or not u.hostname or u.path not in ('','/') or u.query or u.fragment or u.username or u.port: raise SystemExit('Invalid HTTPS origin')
if not re.fullmatch(r'[0-9a-fA-F-]{36}',c['tunnel_id']): raise SystemExit('Invalid tunnel UUID')
p=Path(c['credentials_file']).expanduser().resolve()
if not p.is_file(): raise SystemExit('Tunnel credentials file missing')
config={'tunnel':c['tunnel_id'],'credentials-file':str(p),'ingress':[{'hostname':u.hostname,'service':'http://127.0.0.1:8080'},{'service':'http_status:404'}]}
Path('data/named-tunnel-config.json').write_text(json.dumps(config));Path('data/named-tunnel-config.json').chmod(0o600)
print('https://'+u.netloc)
PYCONFIG
)
    cloudflared --config data/named-tunnel-config.json tunnel run > "$public_tmp/tunnel.log" 2>&1 &
    tunnel_pid=$!
else
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
fi
start_web(){
    python -m spoondev serve --auth --host 127.0.0.1 --port 8080 --public-url "$public_url" &
    web_pid=$!
}
start_web
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
if [ "${SPOONDEV_COLLECT:-1}" = 1 ]; then
    python -m spoondev collect-auto >> data/collector.log 2>&1 &
    worker_pid=$!
    printf 'Automatic collection enabled. Log: data/collector.log\n'
else
    printf 'Automatic collection disabled; existing manual collectors were kept running.\n'
fi
if command -v caffeinate >/dev/null; then caffeinate -i -w "$web_pid" & awake_pid=$!; fi
printf '\nOpen this URL and log in: %s\nKeep this terminal open. Ctrl+C stops public access.\n' "$public_url"
reload_requested=0
request_reload(){ reload_requested=1; kill "$web_pid" 2>/dev/null || true; }
trap request_reload HUP
python - "$$" "$public_url" <<'PYSTATE'
import json,subprocess,sys
from pathlib import Path
pid=sys.argv[1]
state={'pid':int(pid),'started':subprocess.check_output(['ps','-p',pid,'-o','lstart='],text=True).strip(),'root':str(Path.cwd().resolve()),'url':sys.argv[2]}
Path('data/public-run.json').write_text(json.dumps(state));Path('data/public-run.json').chmod(0o600)
PYSTATE
while true; do
    status=0
    wait "$web_pid" || status=$?
    if [ "$reload_requested" -eq 1 ]; then
        # Replace only the web server; the collector and tunnel stay running.
        wait "$web_pid" 2>/dev/null || true
        reload_requested=0
        start_web
        if command -v caffeinate >/dev/null; then caffeinate -i -w "$web_pid" & awake_pid=$!; fi
        printf '\nWeb reloaded. Public URL unchanged: %s\n' "$public_url"
    else
        exit "$status"
    fi
done
