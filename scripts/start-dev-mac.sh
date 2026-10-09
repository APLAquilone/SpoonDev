#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$(basename "$PWD")" != *-dev ]]; then echo 'Run this script inside the separate SpoonDev-dev checkout'; exit 1; fi
printf 'Development only: http://127.0.0.1:8081\n'
exec python -m spoondev serve --auth --host 127.0.0.1 --port 8081
