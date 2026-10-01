"""
tests/test_dashboard_auth.py — dashboard-shared login gate.

Covers:
  * is_valid_dashboard_session(): a real token/expiry match, an expired one,
    an unknown one, a missing DB file, a blank token.
  * The Flask before_request gate end-to-end: no cookie / expired cookie ->
    redirect (page) or 401 JSON (api); a valid cookie passes through;
    /healthz, /api/bridge/*, /static/* stay exempt regardless of cookie.
  * REQUIRE_DASHBOARD_LOGIN=False (the default) never gates anything, even
    with no dashboard DB configured at all - this is what every other test
    in this suite already relies on.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from app import create_app
from app.services.dashboard_auth import is_valid_dashboard_session
from config import TestingConfig


def _make_dashboard_db(tmp_path, *, valid_token=None, expired_token=None):
    """A tiny standalone sqlite file mirroring spicetown-backend's
    user_sessions table shape (token PK, user_id, expires_at as naive-UTC
    ISO text) - just enough for is_valid_dashboard_session to query."""
    db_path = tmp_path / "dashboard.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE user_sessions (token TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT)"
    )
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if valid_token:
        future = (now + dt.timedelta(days=1)).isoformat(sep=" ")
        conn.execute("INSERT INTO user_sessions VALUES (?, 1, ?)", (valid_token, future))
    if expired_token:
        past = (now - dt.timedelta(days=1)).isoformat(sep=" ")
        conn.execute("INSERT INTO user_sessions VALUES (?, 1, ?)", (expired_token, past))
    conn.commit()
    conn.close()
    return str(db_path)


# ── is_valid_dashboard_session() directly ─────────────────────────────────────
def test_valid_token_passes(tmp_path):
    db_path = _make_dashboard_db(tmp_path, valid_token="realtoken123")
    assert is_valid_dashboard_session(db_path, "realtoken123") is True


def test_expired_token_fails(tmp_path):
    db_path = _make_dashboard_db(tmp_path, expired_token="oldtoken456")
    assert is_valid_dashboard_session(db_path, "oldtoken456") is False


def test_unknown_token_fails(tmp_path):
    db_path = _make_dashboard_db(tmp_path, valid_token="realtoken123")
    assert is_valid_dashboard_session(db_path, "not-a-real-token") is False


def test_blank_token_fails(tmp_path):
    db_path = _make_dashboard_db(tmp_path, valid_token="realtoken123")
    assert is_valid_dashboard_session(db_path, "") is False
    assert is_valid_dashboard_session(db_path, None) is False


def test_missing_db_file_fails_closed(tmp_path):
    assert is_valid_dashboard_session(str(tmp_path / "does_not_exist.db"), "anytoken") is False


def test_blank_db_path_fails_closed():
    assert is_valid_dashboard_session("", "anytoken") is False


# ── Flask before_request gate ──────────────────────────────────────────────────
@pytest.fixture()
def gated_app(tmp_path):
    db_path = _make_dashboard_db(tmp_path, valid_token="goodcookie", expired_token="staledcookie")

    class GatedTestConfig(TestingConfig):
        REQUIRE_DASHBOARD_LOGIN = True
        DASHBOARD_DB_PATH = db_path
        DASHBOARD_SESSION_COOKIE_NAME = "session_token"
        DASHBOARD_LOGIN_URL = "https://ops.spicetown.shop/"

    app = create_app(config_object=GatedTestConfig, start_background=False)
    yield app


@pytest.fixture()
def client(gated_app):
    return gated_app.test_client()


def test_page_no_cookie_redirects_to_dashboard(client):
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["Location"] == "https://ops.spicetown.shop/"


def test_page_expired_cookie_redirects(client):
    client.set_cookie("session_token", "staledcookie")
    r = client.get("/")
    assert r.status_code == 302


def test_page_valid_cookie_passes_through(client):
    client.set_cookie("session_token", "goodcookie")
    r = client.get("/")
    assert r.status_code == 200


def test_api_no_cookie_returns_401_json(client):
    r = client.get("/api/health")
    assert r.status_code == 401
    assert r.get_json()["error"] == "unauthorized"


def test_api_valid_cookie_passes_through(client):
    client.set_cookie("session_token", "goodcookie")
    r = client.get("/api/health")
    assert r.status_code == 200


def test_healthz_always_exempt(client):
    # No cookie at all - still reachable, for uptime monitoring.
    assert client.get("/healthz").status_code == 200


def test_api_bridge_exempt_from_dashboard_gate(client):
    # No dashboard cookie set - bridge has its own separate STL_BRIDGE_TOKEN
    # auth (routes/bridge.py), so this must reach that check (401/403), not
    # get redirected/blocked by the dashboard gate first.
    r = client.get("/api/bridge/next-job")
    assert r.status_code != 302
    assert r.get_json().get("error") != "unauthorized"


# ── Default (REQUIRE_DASHBOARD_LOGIN=False) never gates ───────────────────────
def test_gate_disabled_by_default_even_with_no_dashboard_db_configured():
    app = create_app(config_object=TestingConfig, start_background=False)
    client = app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/health").status_code == 200


# ── App Review demo login: printing refused (build 7 contract §1) ──────────────
from app.services.dashboard_auth import dashboard_session_username, is_demo_session  # noqa: E402

DEMO_ERROR = {"error": "Printing is turned off for the demo account."}


def _make_db_with_users(tmp_path):
    """user_sessions + users, as in spicetown-backend: a demo login, a real
    one, and an expired demo session."""
    db_path = tmp_path / "dashboard_users.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE user_sessions (token TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT)")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO users VALUES (8, 'App_Store_Review'), (2, 'ravi')")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    future = (now + dt.timedelta(days=1)).isoformat(sep=" ")
    past = (now - dt.timedelta(days=1)).isoformat(sep=" ")
    conn.execute("INSERT INTO user_sessions VALUES ('demotoken', 8, ?)", (future,))
    conn.execute("INSERT INTO user_sessions VALUES ('stafftoken', 2, ?)", (future,))
    conn.execute("INSERT INTO user_sessions VALUES ('olddemo', 8, ?)", (past,))
    conn.commit()
    conn.close()
    return str(db_path)


def test_session_username_lookup(tmp_path):
    db_path = _make_db_with_users(tmp_path)
    assert dashboard_session_username(db_path, "demotoken") == "App_Store_Review"
    assert dashboard_session_username(db_path, "stafftoken") == "ravi"
    assert dashboard_session_username(db_path, "olddemo") is None
    assert dashboard_session_username(db_path, "nope") is None
    assert dashboard_session_username(db_path, None) is None
    assert dashboard_session_username(str(tmp_path / "missing.db"), "demotoken") is None


def test_is_demo_session(tmp_path):
    db_path = _make_db_with_users(tmp_path)
    names = "app_store_review,appreview"
    assert is_demo_session(db_path, "demotoken", names) is True
    assert is_demo_session(db_path, "stafftoken", names) is False
    assert is_demo_session(db_path, "olddemo", names) is False
    assert is_demo_session(db_path, "demotoken", "") is False


@pytest.fixture(params=[True, False], ids=["gate_on", "gate_off"])
def demo_app_client(request, tmp_path):
    db_path = _make_db_with_users(tmp_path)

    class DemoConfig(TestingConfig):
        REQUIRE_DASHBOARD_LOGIN = request.param
        DASHBOARD_DB_PATH = db_path
        DASHBOARD_SESSION_COOKIE_NAME = "session_token"

    app = create_app(config_object=DemoConfig, start_background=False)
    return app.test_client()


def test_demo_session_cannot_print(demo_app_client):
    demo_app_client.set_cookie("session_token", "demotoken")
    r = demo_app_client.post("/api/print", json={"upc": "012345678905"})
    assert r.status_code == 403 and r.get_json() == DEMO_ERROR
    r = demo_app_client.post("/api/price-changes/print", json={"ids": [1]})
    assert r.status_code == 403 and r.get_json() == DEMO_ERROR


def test_demo_bearer_token_cannot_print(demo_app_client):
    r = demo_app_client.post(
        "/api/print", json={"upc": "012345678905"}, headers={"Authorization": "Bearer demotoken"}
    )
    # With the gate on, a Bearer-only request is stopped by the login gate
    # (401) before it gets here; with it off, the demo check refuses it.
    assert r.status_code in (401, 403)
    assert r.status_code == 401 or r.get_json() == DEMO_ERROR


def test_real_session_still_reaches_printing(demo_app_client):
    demo_app_client.set_cookie("session_token", "stafftoken")
    r = demo_app_client.post("/api/print", json={})
    assert r.status_code == 400  # past the demo check: "upc is required"
    r = demo_app_client.post("/api/price-changes/print", json={"ids": "x"})
    assert r.status_code == 400


def test_demo_session_can_still_look_things_up(demo_app_client):
    demo_app_client.set_cookie("session_token", "demotoken")
    assert demo_app_client.get("/api/health").status_code == 200


def test_demo_refresh_never_auto_prints(demo_app_client, monkeypatch):
    """A manual catalog refresh normally prints labels for unreviewed price
    changes straight away - not when a demo login pressed it."""
    import app.routes.api as api

    calls = []
    monkeypatch.setattr(api, "auto_print_price_changes", lambda app: calls.append(1) or {"printed": 1})
    monkeypatch.setattr(api, "bulk_load_guarded", lambda *a, **k: type("S", (), {"as_dict": lambda self: {}})())
    demo_app_client.set_cookie("session_token", "demotoken")
    r = demo_app_client.post("/api/refresh")
    assert r.status_code == 200
    assert r.get_json()["auto_printed"] == {"printed": 0, "failed": 0, "skipped": True}
    assert calls == []

    demo_app_client.set_cookie("session_token", "stafftoken")
    r = demo_app_client.post("/api/refresh")
    assert r.status_code == 200 and calls == [1]
