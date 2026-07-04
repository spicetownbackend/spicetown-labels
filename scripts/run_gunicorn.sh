#!/usr/bin/env bash
# Start the production server (gunicorn, single worker + threads).
# Single worker keeps the print queue + scheduler singular.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[ -d .venv ] && source .venv/bin/activate

export STL_ENV="${STL_ENV:-production}"
HOST="${STL_HOST:-0.0.0.0}"
PORT="${STL_PORT:-8080}"

exec gunicorn -w 1 --threads 4 -k gthread \
  -b "${HOST}:${PORT}" --timeout 120 \
  --access-logfile - --error-logfile - \
  wsgi:app
