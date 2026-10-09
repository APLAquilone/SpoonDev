#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python - <<'PY'
from pathlib import Path
import subprocess,sqlite3
source=Path.cwd().resolve(); target=source.with_name(source.name+'-dev')
if target.exists(): raise SystemExit(f'Already exists: {target}; existing development files were preserved')
subprocess.run(['git','clone','--no-hardlinks',str(source),str(target)],check=True)
# Keep the local clone source, and name the upstream explicitly for manual updates.
subprocess.run(['git','remote','add','github','https://github.com/APLAquilone/SpoonDev.git'],cwd=target,check=True)
# Snapshot only observations. Accounts/private registrations are never copied.
folder=target/'data';folder.mkdir()
source_db=source/'data/spoondev.sqlite3'
if source_db.is_file():
    with sqlite3.connect(source_db.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(folder/'spoondev.sqlite3') as dst:
        src.backup(dst)
        for table in ('registered_fans','fan_owners'):
            if dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone(): dst.execute(f'DELETE FROM {table}')
else:
    subprocess.run(['python','-m','spoondev','init-db'],cwd=target,check=True)
print(f'Development checkout: {target}')
print('origin is the local production checkout; use github or the explicit GitHub URL for Dev branches')
print('Create a separate development login there: python -m spoondev create-user kitomoya')
print('Start: bash scripts/start-dev-mac.sh')
PY
