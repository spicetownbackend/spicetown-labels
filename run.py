"""
run.py — Development entry point.

    python run.py                 # dev server on :8080
    STL_ENV=production python run.py

In production the app is served by gunicorn under launchd (see deploy/), e.g.:
    gunicorn -w 1 -b 127.0.0.1:8080 "app:create_app('production')"

NOTE: a single worker (-w 1) is intentional — the print queue uses one in-process
worker thread, so the WSGI process must be a singleton (Stage 3).
"""

from __future__ import annotations

import os

from app import create_app

app = create_app(os.getenv("STL_ENV", "development"))


if __name__ == "__main__":
    host = os.getenv("STL_HOST", "0.0.0.0")
    port = int(os.getenv("STL_PORT", "8080"))
    # debug reloader off by default so background threads aren't double-started.
    app.run(host=host, port=port, debug=app.config.get("DEBUG", False), use_reloader=False)
