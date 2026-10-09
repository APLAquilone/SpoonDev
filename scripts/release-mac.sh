#!/usr/bin/env bash
# Release committed, tested Dev code; keep a verified running tunnel alive.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python scripts/manage-release.py release "$@"
