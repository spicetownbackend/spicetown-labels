#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/setup_mac.sh — one-shot setup for the Mac Mini (the printing host).
#
# Fixes the "wrong Python version" problem automatically: ensures Python 3.12 is
# present (via Homebrew), builds a clean virtualenv with it, installs deps, and
# initialises + loads the catalog.
#
#   bash scripts/setup_mac.sh
#
# Then start it with:  bash scripts/run_gunicorn.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "▶ Project: $ROOT"

# 1) Ensure Homebrew --------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  echo "✗ Homebrew not found."
  echo "  Install it once with:"
  echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  echo "  then re-run this script."
  exit 1
fi

# 2) Ensure Python 3.12 -----------------------------------------------------
if ! brew list python@3.12 >/dev/null 2>&1; then
  echo "▶ Installing python@3.12 via Homebrew…"
  brew install python@3.12
fi
PY="$(brew --prefix)/bin/python3.12"
if [ ! -x "$PY" ]; then
  # Apple Silicon vs Intel prefix fallback
  PY="$(command -v python3.12 || true)"
fi
[ -x "$PY" ] || { echo "✗ python3.12 not found after install"; exit 1; }
echo "▶ Using $($PY --version) at $PY"

# 3) Fresh virtualenv -------------------------------------------------------
echo "▶ Creating .venv …"
rm -rf .venv
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python --version

# 4) Dependencies -----------------------------------------------------------
echo "▶ Installing dependencies…"
pip install --upgrade pip
pip install -r requirements.txt

# 5) Initialise + load ------------------------------------------------------
echo "▶ Initialising database + loading sample catalog…"
python manage.py init-db
python manage.py load || echo "  (load skipped — provide data/products.csv to load real products)"

cat <<EOF

✓ Setup complete.

Next:
  • Dev run:        source .venv/bin/activate && python run.py
                    then open http://localhost:8080
  • Production run: bash scripts/run_gunicorn.sh
  • Printer:        confirm the queue name with 'lpstat -p' and set
                    STL_CUPS_PRINTER_NAME / STL_LABEL_SIZE in a .env file
                    (copy from .env.example). See DEPLOY.md.
EOF
