"""
app/extensions.py — Shared extension singletons.

These objects are created *unbound* here and initialised against the Flask app
inside the application factory (app/__init__.py). Keeping them in their own
module avoids circular imports between models, routes, and the factory.
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

# Single ORM handle shared across models, services, and routes.
db = SQLAlchemy()

# Background scheduler for the nightly bulk refresh (wired up in the factory).
scheduler = BackgroundScheduler(timezone="America/New_York")


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
    """Tune SQLite on every new connection.

    - WAL journal: concurrent readers while the print-queue worker / nightly
      refresh writes, which keeps scan→lookup latency low.
    - NORMAL synchronous: safe with WAL, far faster than FULL.
    - foreign_keys ON: enforce relational integrity for future tables.
    - busy_timeout: block briefly instead of raising 'database is locked'.

    The guard ensures we only issue PRAGMAs against SQLite connections, so the
    same codebase works if the DB is ever pointed elsewhere.
    """
    # Only applies to the sqlite3 driver.
    if dbapi_connection.__class__.__module__.split(".")[0] != "sqlite3":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=5000;")  # 5s
    finally:
        cursor.close()
