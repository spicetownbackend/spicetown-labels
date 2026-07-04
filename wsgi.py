"""
wsgi.py — Production WSGI entrypoint for gunicorn.

    gunicorn -w 1 --threads 4 -k gthread -b 0.0.0.0:8080 wsgi:app

IMPORTANT: run with a SINGLE worker process (-w 1). The print queue uses one
in-process worker thread and APScheduler runs one nightly job; multiple worker
processes would create duplicate queues/schedulers. Use --threads for
concurrency instead (the print work is offloaded to the queue, so request
threads stay responsive).
"""

from __future__ import annotations

import os

from app import create_app

app = create_app(os.getenv("STL_ENV", "production"))
