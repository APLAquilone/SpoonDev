#!/usr/bin/env bash
# Run after stopping the public web process. Collectors may keep running.
set -euo pipefail
cd "$(dirname "$0")/.."
python - <<'PY'
from pathlib import Path
import subprocess,socket
source=Path.cwd().resolve()
if not source.name.endswith('-dev'): raise SystemExit('Run from SpoonDev-dev')
target=source.with_name(source.name[:-4])
def run(*args,cwd=source): return subprocess.check_output(args,cwd=cwd,text=True).strip()
for root in (source,target):
    if run('git','status','--porcelain',cwd=root): raise SystemExit(f'Commit changes first; uncommitted files in {root}')
with socket.socket() as s:
    try:s.bind(('127.0.0.1',8080))
    except OSError:raise SystemExit('Stop the public web server first. Collectors may keep running.')
commit=run('git','rev-parse','HEAD')
subprocess.run(['python','-m','unittest','discover','-s','tests','-q'],cwd=source,check=True)
subprocess.run(['git','fetch',str(source),commit],cwd=target,check=True)
subprocess.run(['git','merge-base','--is-ancestor','HEAD',commit],cwd=target,check=True)
previous=run('git','rev-parse','HEAD',cwd=target)
subprocess.run(['git','merge','--ff-only',commit],cwd=target,check=True)
print(f'Released {commit}; previous {previous}. Production data was preserved.')
print(f'cd {target}\nbash scripts/start-public-mac.sh')
PY
