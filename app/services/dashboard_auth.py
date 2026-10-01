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


def dashboard_session_username(db_path: str, token: str | None) -> str | None:
    """The username behind a real, unexpired spicetown-backend session, or
    None (no token, unknown/expired session, missing DB, any DB error).
    Same read-only connection as is_valid_dashboard_session."""
    if not token or not db_path:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT u.username, s.expires_at FROM user_sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                (token,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("dashboard session user lookup failed")
        return None
    if row is None:
        return None
    try:
        expires_at = dt.datetime.fromisoformat(row[1])
    except (TypeError, ValueError):
        return None
    if expires_at <= dt.datetime.now(dt.timezone.utc).replace(tzinfo=None):
        return None
    return row[0]


def parse_demo_usernames(raw: str | None) -> set[str]:
    return {n.strip().lower() for n in (raw or "").split(",") if n.strip()}


def is_demo_session(db_path: str, token: str | None, demo_usernames: str | None) -> bool:
    """True when the session belongs to an App Review demo login
    (spicetown-backend's DEMO_USERNAMES - keep STL_DEMO_USERNAMES in step).
    Printing is refused for those: a reviewer must never put a real label on
    the store printer."""
    names = parse_demo_usernames(demo_usernames)
    if not names:
        return False
    username = dashboard_session_username(db_path, token)
    return bool(username) and username.strip().lower() in names
