#!/usr/bin/env bash
# Update the independent Dev checkout from GitHub, without using its local origin.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(basename "$PWD")" != *-dev ]]; then
    echo 'Run this script inside the separate SpoonDev-dev checkout'; exit 1
fi
dev_branch="${1:-dev/v0.3.0}"
if [[ $# -gt 1 || ! "$dev_branch" =~ ^dev/v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'Specify a Dev version branch, for example dev/v0.3.0'; exit 1
fi
python - <<'PY'
import sys
if sys.version_info < (3,12):
    raise SystemExit('Activate the spoondev Python 3.12 environment')
PY
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo 'Save your tracked Dev changes before updating'; exit 1
fi
git fetch https://github.com/APLAquilone/SpoonDev.git "$dev_branch"
dev_commit=$(git rev-parse --verify 'FETCH_HEAD^{commit}')
if git show-ref --verify --quiet "refs/heads/$dev_branch"; then
    if ! git merge-base --is-ancestor "$dev_branch" "$dev_commit"; then
        echo 'Dev has local commits that cannot be updated by fast-forward; files were preserved'; exit 1
    fi
    git switch "$dev_branch"
    git merge --ff-only "$dev_commit"
else
    git switch -c "$dev_branch" "$dev_commit"
fi
python -m pip install -e .
python - "${dev_branch#dev/v}" <<'PY'
import sys
from spoondev import __version__
if __version__ != sys.argv[1]:
    raise SystemExit(f'Installed version {__version__} does not match {sys.argv[1]}; do not start Dev')
print(f'Development code updated to v{__version__}. Start with: bash scripts/start-dev-mac.sh')
PY
