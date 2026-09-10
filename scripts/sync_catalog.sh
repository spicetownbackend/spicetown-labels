#!/usr/bin/env bash
# Pulls the latest committed catalog (products.csv + price_changes.json, written
# by .github/workflows/toast-sync.yml) and tells the running app to reload it.
# Render gets this for free via auto-deploy-on-push; this Mac needs its own
# periodic pull since nothing else here watches the git remote.
set -euo pipefail
cd "$(dirname "$0")/.."

BEFORE="$(git rev-parse HEAD)"
git fetch --quiet origin main
git merge --ff-only origin/main --quiet
AFTER="$(git rev-parse HEAD)"

if [ "$BEFORE" != "$AFTER" ]; then
  echo "$(date -u +%FT%TZ) catalog updated ${BEFORE:0:7} -> ${AFTER:0:7}, refreshing"
  curl -s -X POST "http://127.0.0.1:${STL_PORT:-8080}/api/refresh" -o /dev/null || true
fi
