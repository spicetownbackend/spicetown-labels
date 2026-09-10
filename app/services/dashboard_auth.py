"""
app/services/dashboard_auth.py — gate access on a valid spicetown-backend
dashboard login, instead of this app having its own separate login.

Both apps run on the same Mac. spicetown-backend widens its session cookie
to the shared parent domain (SESSION_COOKIE_DOMAIN=.spicetown.shop on that
app's side) so the SAME cookie a user gets from logging into
ops.spicetown.shop is sent here too, at labels.spicetown.shop. This module
validates that cookie by reading spicetown-backend's own `user_sessions`
table directly - a read-only SQLite connection to its DB file. The two apps
otherwise stay completely independent (separate schemas, separate
everything else) - this is the one narrow, deliberate integration point,
and it's read-only by construction (`mode=ro` in the connection URI), so
this app can never write to or corrupt the dashboard's database.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("spicetown.dashboard_auth")


def is_valid_dashboard_session(db_path: str, token: str | None) -> bool:
    """True if `token` is a real, unexpired spicetown-backend session token.

    Fails closed (returns False) on any missing token, missing/unreadable
    DB file, or DB error - a misconfigured path should lock people out, not
    silently let everyone in.
    """
    if not token or not db_path:
        return False
    path = Path(db_path)
    if not path.is_file():
        logger.error("dashboard DB not found at %s - denying access", db_path)
        return False

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT expires_at FROM user_sessions WHERE token = ?", (token,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("dashboard session lookup failed")
        return False

    if row is None:
        return False

    # Stored as naive-UTC text (spicetown-backend's UTCDateTime convention -
    # see that app's app/db.py) - fromisoformat parses its "YYYY-MM-DD
    # HH:MM:SS.ffffff" format directly, no 'T' needed.
    try:
        expires_at = dt.datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        logger.error("unparseable expires_at %r in dashboard session row", row[0])
        return False

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return expires_at > now
