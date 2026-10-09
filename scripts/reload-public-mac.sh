#!/usr/bin/env bash
# Reload the web process while the tunnel and URL stay alive.
set -euo pipefail
cd "$(dirname "$0")/.."
python - <<'PY'
import json,os,signal,subprocess
from pathlib import Path
p=Path('data/public-run.json')
if not p.is_file(): raise SystemExit('The running launcher does not support reload yet. Stop it with Ctrl+C and start start-public-mac.sh once; a Quick Tunnel URL will change on that first restart.')
s=json.loads(p.read_text());pid=s['pid']
if type(pid) is not int or pid<=1 or s['root']!=str(Path.cwd().resolve()): raise SystemExit('Invalid launcher metadata')
try:
    start=subprocess.check_output(['ps','-p',str(pid),'-o','lstart='],text=True).strip()
    command=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True).strip()
except subprocess.CalledProcessError: raise SystemExit('Launcher is not running')
if start!=s['started'] or 'start-public-mac.sh' not in command: raise SystemExit('Launcher identity mismatch; no process was signaled')
os.kill(pid,signal.SIGHUP)
print('Requested web reload; tunnel remains active: '+s['url'])
print('Check the public terminal for successful web startup.')
PY
