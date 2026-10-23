"""
tests/test_kloudc_auth.py - KloudC sign-in (services/kloudc_auth.py,
routes/kloudc.py, the gate in app/__init__.py, the demo check in api.py).

A fake KloudC server runs on 127.0.0.1 (a real HTTP server, so the real
`requests` call is exercised: headers, no redirects, timeouts). Nothing
leaves this machine. The numbered sections follow the contract's L tests.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import create_app
from app.services import kloudc_auth as ka
from config import TestingConfig

STORE = "TESTSTORE"
SECRET = "s" * 43
GOOD = f"{STORE}.good{SECRET}"
DEMO_TOKEN = f"{STORE}.demo{SECRET}"
NOPRINT = f"{STORE}.nopr{SECRET}"
OTHER_STORE = f"OTHERSTORE.good{SECRET}"
LOGIN_URL = "https://app.kloudc.com/#/label-printing/open"
DASH_LOGIN = "https://ops.example.test/"
KC_HOST = "https://labels-teststore.kloudc.com"
UPC = "012345678905"


# ── Fake KloudC server ──────────────────────────────────────────────────────
def _future(seconds=3600):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def session_body(**over):
    body = {
        "store_code": STORE,
        "user": {"id": 12, "username": "maria", "display_name": "Maria"},
        "is_admin": False,
        "demo": False,
        "can_print": True,
        "label_printing": {"enabled": True, "options": {"label_size": "62x33.5"}},
        "session_expires_at": _future(),
        "cache_seconds": 60,
    }
    body.update(over)
    return body


class FakeKloudC:
    """Answers per token (session) or per code (hand-off redeem)."""

    def __init__(self):
        self.calls = []
        self.session = {}  # token -> (status, body or raw bytes, extra headers)
        self.default = (401, {"error": "unauthorized", "message": "no"}, {})
        self.redeem = {}  # code -> (status, body)
        self.delay = 0.0
        self.lock = threading.Lock()

    def answer(self, token, status=200, body=None, headers=None):
        self.session[token] = (status, session_body() if body is None and status == 200 else body, headers or {})


@pytest.fixture()
def fake():
    state = FakeKloudC()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, status, body, headers=None):
            raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            with state.lock:
                state.calls.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
            if state.delay:
                time.sleep(state.delay)
            if self.path != "/api/labels/session":
                return self._send(404, {"error": "not_found"})
            token = (self.headers.get("Authorization") or "").partition(" ")[2]
            status, body, headers = state.session.get(token, state.default)
            self._send(status, body, headers)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            with state.lock:
                state.calls.append(
                    {"method": "POST", "path": self.path, "headers": dict(self.headers), "json": data}
                )
            if self.path != "/api/labels/handoff/redeem":
                return self._send(404, {"error": "not_found"})
            status, body = state.redeem.get(data.get("code"), (404, {"error": "code_unknown"}))
            self._send(status, body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def _dashboard_db(tmp_path):
    db_path = tmp_path / "dashboard.db"
    if db_path.exists():
        return str(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE user_sessions (token TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT)")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO users VALUES (2, 'ravi')")
    future = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(days=1)).isoformat(sep=" ")
    conn.execute("INSERT INTO user_sessions VALUES ('legacytoken', 2, ?)", (future,))
    conn.commit()
    conn.close()
    return str(db_path)


def _make_app(tmp_path, base, **over):
    attrs = dict(
        REQUIRE_DASHBOARD_LOGIN=True,
        DASHBOARD_DB_PATH=_dashboard_db(tmp_path),
        DASHBOARD_SESSION_COOKIE_NAME="session_token",
        DASHBOARD_LOGIN_URL=DASH_LOGIN,
        KLOUDC_AUTH_ENABLED=True,
        KLOUDC_API_BASE=base,
        KLOUDC_STORE_CODE=STORE,
        KLOUDC_LOGIN_URL=LOGIN_URL,
        KLOUDC_TIMEOUT_SECONDS=3.0,
        KLOUDC_MAX_CHECKS_PER_MINUTE=120,
    )
    attrs.update(over)
    cfg = type("KloudcTestConfig", (TestingConfig,), attrs)
    app = create_app(config_object=cfg, start_background=False)
    with app.app_context():
        from app.extensions import db
        from app.models import Product

        db.session.add(Product(upc=UPC, name="Basmati Rice 10 lb", price=12.99, department="Grocery"))
        db.session.commit()
    return app


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def app(tmp_path, fake, clock):
    a = _make_app(tmp_path, fake.base)
    a.extensions["kloudc_auth"]._clock = clock
    fake.answer(GOOD)
    fake.answer(DEMO_TOKEN, body=session_body(demo=True, can_print=False))
    fake.answer(NOPRINT, body=session_body(can_print=False))
    return a


@pytest.fixture()
def client(app):
    return app.test_client()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def session_calls(fake):
    return [c for c in fake.calls if c["path"] == "/api/labels/session"]


# ── 1. KloudC sign-in off: the site behaves exactly as before ───────────────
def test_1_off_by_default_and_kloudc_token_gets_existing_401(tmp_path, fake):
    # Settings unset (TestingConfig defaults) with the gate on.
    cfg = type(
        "Plain",
        (TestingConfig,),
        dict(REQUIRE_DASHBOARD_LOGIN=True, DASHBOARD_DB_PATH=_dashboard_db(tmp_path), DASHBOARD_LOGIN_URL=DASH_LOGIN),
    )
    a = create_app(config_object=cfg, start_background=False)
    assert a.extensions["kloudc_auth"].enabled is False
    c = a.test_client()
    fake.answer(GOOD)
    r = c.get("/api/health", headers=bearer(GOOD))
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized", "message": "Log into the dashboard first"}
    c.set_cookie("session_token", GOOD)
    assert c.get("/api/health").status_code == 401
    r = c.get("/", headers=bearer(GOOD), base_url=KC_HOST)
    assert r.status_code == 302 and r.headers["Location"] == DASH_LOGIN
    assert c.get("/kloudc/enter?code=" + "a" * 43, base_url=KC_HOST).status_code == 302  # gated, as any page
    assert fake.calls == []


def test_1b_enabled_without_settings_stays_off(tmp_path, fake):
    a = _make_app(tmp_path, fake.base, KLOUDC_STORE_CODE="")
    assert a.extensions["kloudc_auth"].enabled is False
    a = _make_app(tmp_path, "", KLOUDC_STORE_CODE=STORE)
    assert a.extensions["kloudc_auth"].enabled is False
    a = _make_app(tmp_path, fake.base, KLOUDC_STORE_CODE="lower_case")
    assert a.extensions["kloudc_auth"].enabled is False


# ── 2. The legacy cookie still passes with zero outbound calls ─────────────
def test_2_legacy_cookie_passes_without_calling_kloudc(client, fake):
    client.set_cookie("session_token", "legacytoken")
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.post("/api/print", json={}).status_code == 400  # past the demo check
    assert fake.calls == []


# ── 3. A valid Bearer token: reads work; one call, then cached for 60 s ────
def test_3_valid_bearer_reads_and_cache(client, fake, clock):
    h = bearer(GOOD)
    assert client.get(f"/api/lookup/{UPC}", headers=h).status_code == 200
    assert client.get("/api/search?q=basmati", headers=h).status_code == 200
    r = client.get(f"/api/preview/{UPC}.png", headers=h)
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert client.get("/api/print-history", headers=h).status_code == 200
    assert len(session_calls(fake)) == 1
    clock.now += 59
    assert client.get("/api/health", headers=h).status_code == 200
    assert len(session_calls(fake)) == 1
    clock.now += 2  # past 60 s: asks again
    assert client.get("/api/health", headers=h).status_code == 200
    assert len(session_calls(fake)) == 2


def test_3b_exact_headers_sent(client, fake):
    client.set_cookie("session_token", "not-a-legacy-session")
    client.get("/api/health", headers={**bearer(GOOD), "X-Forwarded-For": "1.2.3.4", "CF-Connecting-IP": "1.2.3.4"})
    (call,) = session_calls(fake)
    names = {k.lower() for k in call["headers"]}
    assert names <= {"authorization", "x-store", "accept", "user-agent", "host", "accept-encoding", "connection"}
    assert "cookie" not in names and "x-forwarded-for" not in names and "cf-connecting-ip" not in names
    assert call["headers"]["Authorization"] == f"Bearer {GOOD}"
    assert call["headers"]["X-Store"] == STORE
    assert call["headers"]["Accept"] == "application/json"
    assert call["headers"]["User-Agent"] == "labels-site-kloudc-auth/1"


def test_3c_cache_never_outlives_the_session(client, fake, clock):
    fake.answer(GOOD, body=session_body(session_expires_at=_future(10)))
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 200
    clock.now += 11
    fake.answer(GOOD, status=401, body={"error": "session_expired"})
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 401
    assert len(session_calls(fake)) == 2


# ── 4. A KloudC-shaped session_token cookie (installed app builds) ─────────
def test_4_kloudc_cookie_accepted(client, fake):
    client.set_cookie("session_token", GOOD)
    assert client.get(f"/api/lookup/{UPC}").status_code == 200
    assert client.get("/").status_code == 200
    assert len(session_calls(fake)) == 1


# ── 5. Another store's token: refused with no outside call ─────────────────
def test_5_other_store_prefix_refused_without_call(client, fake):
    r = client.get("/api/health", headers=bearer(OTHER_STORE))
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized", "message": "Sign in again to use label printing."}
    client.set_cookie("session_token", OTHER_STORE)
    assert client.get("/api/health").status_code == 401
    assert fake.calls == []


def test_5b_non_kloudc_bearer_gets_the_existing_401(client, fake):
    r = client.get("/api/health", headers=bearer("legacy-shaped-token-without-dot"))
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized", "message": "Log into the dashboard first"}
    assert fake.calls == []


# ── 6. Fake tokens: one call, 30 s "no" cache, flood capped then 429 ───────
def test_6_fake_token_negative_cache(client, fake, clock):
    fakeweb = f"{STORE}.fake{SECRET}"
    assert client.get("/api/health", headers=bearer(fakeweb)).status_code == 401
    assert client.get("/api/health", headers=bearer(fakeweb)).status_code == 401
    assert len(session_calls(fake)) == 1
    clock.now += 29
    assert client.get("/api/health", headers=bearer(fakeweb)).status_code == 401
    assert len(session_calls(fake)) == 1
    clock.now += 2
    assert client.get("/api/health", headers=bearer(fakeweb)).status_code == 401
    assert len(session_calls(fake)) == 2


def test_6b_flood_is_capped_then_429(client, fake):
    codes = []
    for i in range(200):
        token = f"{STORE}.flood{i:04d}{SECRET}"
        codes.append(client.get("/api/health", headers=bearer(token)).status_code)
    assert len(session_calls(fake)) <= 120
    assert codes.count(429) == 200 - len(session_calls(fake))
    r = client.get("/api/health", headers=bearer(f"{STORE}.another{SECRET}"))
    assert r.status_code == 429
    assert r.get_json() == {"error": "rate_limited", "message": "Too many sign-in checks. Try again in a minute."}


def test_6c_known_good_token_survives_a_flood_within_grace(client, fake, clock):
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 200
    for i in range(130):
        client.get("/api/health", headers=bearer(f"{STORE}.flood{i:04d}{SECRET}"))
    clock.now += 61  # cache expired, cap still full
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 200


# ── 7. A 200 that fails a check is a refusal ───────────────────────────────
@pytest.mark.parametrize(
    "body",
    [
        session_body(store_code="OTHERSTORE"),
        session_body(label_printing={"enabled": False, "options": {}}),
        session_body(label_printing={"enabled": "true"}),
        session_body(session_expires_at="2020-01-01T00:00:00Z"),
        session_body(session_expires_at="soon"),
        session_body(user={"id": 1}),
        b"<html>not json</html>",
        b"[1, 2]",
    ],
    ids=["store", "disabled", "enabled_str", "expired", "bad_time", "no_username", "not_json", "list"],
)
def test_7_bad_200_bodies_refused(client, fake, body):
    fake.answer(GOOD, status=200, body=body)
    assert client.get(f"/api/lookup/{UPC}", headers=bearer(GOOD)).status_code == 401


def test_7b_label_printing_off_is_403(client, fake):
    fake.answer(GOOD, status=403, body={"error": "label_printing_off", "message": "x"})
    r = client.get("/api/health", headers=bearer(GOOD))
    assert r.status_code == 403
    assert r.get_json() == {"error": "label_printing_off", "message": "Label printing is turned off for this store."}
    diff = f"{STORE}.diff{SECRET}"
    fake.answer(diff, status=409, body={"error": "store_mismatch"})
    assert client.get("/api/health", headers=bearer(diff)).status_code == 401
    other = f"{STORE}.othr{SECRET}"
    fake.answer(other, status=403, body={"error": "store_suspended"})
    r = client.get("/api/health", headers=bearer(other))
    assert r.status_code == 401 and r.get_json()["error"] == "unauthorized"


# ── 8. A redirect from KloudC is not followed ──────────────────────────────
def test_8_redirect_not_followed(client, fake):
    fake.answer(GOOD, status=302, body={}, headers={"Location": fake.base + "/elsewhere"})
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 401
    assert [c["path"] for c in fake.calls] == ["/api/labels/session"]


# ── 9. KloudC unreachable: stale grace after a success, else 503 ───────────
def test_9_5xx_without_history_is_503(client, fake):
    fake.answer(GOOD, status=502, body={"error": "bad_gateway"})
    r = client.get("/api/health", headers=bearer(GOOD))
    assert r.status_code == 503
    assert r.get_json() == {
        "error": "auth_unavailable",
        "message": "Can't check your sign-in right now. Try again in a minute.",
    }
    fake.answer(GOOD, status=429, body={"error": "rate_limited"})
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 503


def test_9b_stale_grace_then_expires(client, fake, clock, caplog):
    caplog.set_level(logging.INFO)
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 200
    fake.answer(GOOD, status=503, body={})
    clock.now += 120
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 200
    assert "kloudc_auth stale_grace" in caplog.text
    clock.now += 200  # 320 s after the last yes: grace over
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 503


def test_9c_connection_error_and_timeout(tmp_path, fake, clock):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    a = _make_app(tmp_path, f"http://127.0.0.1:{closed_port}")
    a.extensions["kloudc_auth"]._clock = clock
    assert a.test_client().get("/api/health", headers=bearer(GOOD)).status_code == 503

    b = _make_app(tmp_path, fake.base, KLOUDC_TIMEOUT_SECONDS=0.3)
    b.extensions["kloudc_auth"]._clock = clock
    fake.answer(GOOD)
    fake.delay = 1.0
    started = time.monotonic()
    assert b.test_client().get("/api/health", headers=bearer(GOOD)).status_code == 503
    assert time.monotonic() - started < 0.9
    assert len(session_calls(fake)) == 1  # one attempt, no retries


def test_9d_401_after_success_wins_at_once(client, fake, clock):
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 200
    clock.now += 61
    fake.answer(GOOD, status=401, body={"error": "session_expired"})
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 401
    fake.answer(GOOD, status=503, body={})  # even if KloudC then goes down: no grace after a no
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 401
    clock.now += 31
    assert client.get("/api/health", headers=bearer(GOOD)).status_code == 503


# ── 10. Demo logins and can_print false: reads yes, prints and writes no ───
@pytest.mark.parametrize("token", [DEMO_TOKEN, NOPRINT], ids=["demo", "can_print_false"])
def test_10_demo_can_read_not_print(client, token):
    h = bearer(token)
    assert client.get(f"/api/lookup/{UPC}", headers=h).status_code == 200
    assert client.get(f"/api/preview/{UPC}.png", headers=h).status_code == 200
    assert client.get("/api/print-history", headers=h).status_code == 200
    for path, body in (
        ("/api/print", {"upc": UPC}),
        ("/api/price-changes/print", {"ids": [1]}),
        ("/api/products/custom", {"name": "Reviewer Item"}),
        ("/api/price-changes/dismiss", {"ids": [1]}),
    ):
        r = client.post(path, json=body, headers=h)
        assert r.status_code == 403, path
    from app.extensions import db
    from app.models import Product, PrintJob

    with client.application.app_context():
        assert db.session.query(Product).filter_by(name="Reviewer Item").count() == 0
        assert db.session.query(PrintJob).count() == 0


def test_10b_real_kloudc_user_reaches_printing_and_is_logged(client, caplog):
    caplog.set_level(logging.INFO)
    h = bearer(GOOD)
    assert client.post("/api/print", json={}, headers=h).status_code == 400  # past the demo check
    r = client.post("/api/print", json={"upc": UPC}, headers=h)
    assert r.status_code == 503  # null transport, worker off in tests: past the demo check
    assert "printed_by=maria" in caplog.text
    assert client.post("/api/products/custom", json={"name": "Staff Item"}, headers=h).status_code == 201


def test_10c_identity_without_demo_flag_fails_closed():
    now = time.time()
    body = session_body()
    body.pop("demo")
    ident = ka.identity_from_body(body, STORE, now)
    assert ident is not None and ident.demo is True and ident.can_print is False


# ── 11. API base allow-list ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "base",
    [
        "https://evil.example",
        "http://10.0.0.5:8900",
        "https://app.kloudc.com.evil.example",
        "http://app.kloudc.com",
        "https://app.kloudc.com/api",
        "http://127.0.0.1",
        "http://127.0.0.1:8900@evil.example",
        "https://user@app.kloudc.com",
        "https://x.ts.net",
        "https://a.b.ts.net.evil.example",
    ],
)
def test_11_api_base_not_on_allow_list_stays_off(tmp_path, base, fake):
    a = _make_app(tmp_path, base)
    assert a.extensions["kloudc_auth"].enabled is False
    assert a.test_client().get("/api/health", headers=bearer(GOOD)).status_code == 401
    assert fake.calls == []


@pytest.mark.parametrize(
    "base",
    ["https://app.kloudc.com", "https://app.kloudc.com/", "http://127.0.0.1:8900", "http://localhost:8900",
     "https://mac-mini.tail1234.ts.net", "https://mac-mini.tail1234.ts.net:8443"],
)
def test_11b_allowed_bases(base):
    assert ka.validate_api_base(base) == base.rstrip("/")


# ── 12. The token never appears in logs or cache keys ──────────────────────
def test_12_token_not_logged_or_cached_raw(app, client, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fakeweb = f"{STORE}.fake{SECRET}"
    client.get("/api/health", headers=bearer(GOOD))
    client.get("/api/health", headers=bearer(fakeweb))
    fake.answer(DEMO_TOKEN, status=503, body={})
    client.get("/api/health", headers=bearer(DEMO_TOKEN))
    client.post("/api/print", json={}, headers=bearer(GOOD))
    for t in (GOOD, fakeweb, DEMO_TOKEN):
        assert t not in caplog.text
        assert t.split(".", 1)[1] not in caplog.text
    keys = app.extensions["kloudc_auth"].cache_keys()
    assert keys and all(len(k) == 64 for k in keys)
    assert ka.token_key(GOOD) in keys
    assert not any(GOOD in k or SECRET in k for k in keys)


# ── 13. Website hand-off: /kloudc/enter and the kc_labels cookie ───────────
CODE_OK = "c" * 43
CODE_USED = "u" * 43
CODE_EXPIRED = "e" * 43


def test_13_handoff_good_code_sets_cookie(client, fake):
    fake.redeem[CODE_OK] = (200, session_body(session_expires_at=_future(3 * 3600)))
    r = client.get(f"/kloudc/enter?code={CODE_OK}", base_url=KC_HOST)
    assert r.status_code == 303 and r.headers["Location"] == "/"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["Cache-Control"] == "no-store"
    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith("kc_labels=")
    for flag in ("Max-Age=7200", "HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert flag in cookie
    assert "Domain=" not in cookie
    (call,) = fake.calls
    assert call["method"] == "POST" and call["json"] == {"code": CODE_OK}
    assert call["headers"]["X-Store"] == STORE and "Authorization" not in call["headers"]
    # The cookie now signs the browser in - no further KloudC call.
    assert client.get("/", base_url=KC_HOST).status_code == 200
    assert client.get(f"/api/lookup/{UPC}", base_url=KC_HOST).status_code == 200
    assert len(fake.calls) == 1


def test_13b_demo_handoff_cookie_cannot_print(client, fake):
    fake.redeem[CODE_OK] = (200, session_body(demo=True, can_print=False))
    client.get(f"/kloudc/enter?code={CODE_OK}", base_url=KC_HOST)
    assert client.get(f"/api/lookup/{UPC}", base_url=KC_HOST).status_code == 200
    assert client.post("/api/print", json={"upc": UPC}, base_url=KC_HOST).status_code == 403


@pytest.mark.parametrize(
    "code,answer",
    [
        (CODE_USED, (410, {"error": "code_used"})),
        ("x" * 43, None),  # unknown: fake answers 404
        (CODE_EXPIRED, (410, {"error": "code_expired"})),
        (CODE_OK, (200, session_body(store_code="OTHERSTORE"))),
        ("short", None),  # not code-shaped: no call at all
    ],
    ids=["used", "unknown", "expired", "other_store", "malformed"],
)
def test_13c_bad_codes_go_to_login(client, fake, code, answer):
    if answer:
        fake.redeem[code] = answer
    r = client.get(f"/kloudc/enter?code={code}", base_url=KC_HOST)
    assert r.status_code == 303 and r.headers["Location"] == LOGIN_URL
    assert "Set-Cookie" not in r.headers
    assert r.headers["Cache-Control"] == "no-store"
    if code == "short":
        assert fake.calls == []


def test_13d_tampered_and_expired_cookies_refused(client, app, monkeypatch):
    secret = app.config["SECRET_KEY"]
    ident = ka.Identity(store_code=STORE, user_id=12, username="maria", demo=False, can_print=True)
    good = ka.make_handoff_cookie(secret, ident)
    client.set_cookie("kc_labels", good, domain="labels-teststore.kloudc.com")
    assert client.get("/api/health", base_url=KC_HOST).status_code == 200

    tampered = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    client.set_cookie("kc_labels", tampered, domain="labels-teststore.kloudc.com")
    r = client.get("/", base_url=KC_HOST)
    assert r.status_code == 302 and r.headers["Location"] == LOGIN_URL
    assert client.get("/api/health", base_url=KC_HOST).status_code == 401

    other_key = ka.make_handoff_cookie("another-secret", ident)
    client.set_cookie("kc_labels", other_key, domain="labels-teststore.kloudc.com")
    assert client.get("/api/health", base_url=KC_HOST).status_code == 401

    other_store = ka.make_handoff_cookie(secret, ka.Identity(store_code="OTHERSTORE", user_id=1, username="x"))
    client.set_cookie("kc_labels", other_store, domain="labels-teststore.kloudc.com")
    assert client.get("/api/health", base_url=KC_HOST).status_code == 401

    # Over 2 hours old: refused by the signature age and by iat.
    import itsdangerous.timed

    real_time = time.time
    monkeypatch.setattr(itsdangerous.timed.time, "time", lambda: real_time() - 7300)
    old = ka.make_handoff_cookie(secret, ident, now=real_time() - 7300)
    monkeypatch.undo()
    client.set_cookie("kc_labels", old, domain="labels-teststore.kloudc.com")
    assert client.get("/api/health", base_url=KC_HOST).status_code == 401
    old_iat = ka.make_handoff_cookie(secret, ident, now=real_time() - 7300)
    assert ka.read_handoff_cookie(secret, old_iat, STORE) is None


def test_13e_enter_is_404_when_off(tmp_path, fake):
    a = _make_app(tmp_path, fake.base, KLOUDC_AUTH_ENABLED=False, REQUIRE_DASHBOARD_LOGIN=False)
    assert a.test_client().get(f"/kloudc/enter?code={CODE_OK}").status_code == 404
    assert fake.calls == []


# ── 14. Refused page requests: KloudC hosts go to the KloudC sign-in ───────
def test_14_page_redirects_by_host(client, fake):
    r = client.get("/", base_url=KC_HOST)
    assert r.status_code == 302 and r.headers["Location"] == LOGIN_URL
    r = client.get("/", base_url="https://labels.example.test")
    assert r.status_code == 302 and r.headers["Location"] == DASH_LOGIN
    fake.answer(GOOD, status=401, body={})
    r = client.get("/", headers=bearer(GOOD), base_url=KC_HOST)
    assert r.status_code == 302 and r.headers["Location"] == LOGIN_URL
    r = client.get("/", headers=bearer(f"{STORE}.diff{SECRET}"), base_url="https://labels.example.test")
    assert r.status_code == 302 and r.headers["Location"] == DASH_LOGIN
    # API requests never redirect.
    assert client.get("/api/health", base_url=KC_HOST).status_code == 401


def test_14b_exempt_paths_unchanged(client, fake):
    assert client.get("/healthz", base_url=KC_HOST).status_code == 200
    assert client.get("/api/bridge/next-job", base_url=KC_HOST).status_code != 302
    assert fake.calls == []


# ── Unit: token shape and options pass-through ─────────────────────────────
def test_token_shape():
    assert ka.is_kloudc_token(GOOD)
    assert not ka.is_kloudc_token("legacytokenwithoutdot")
    assert not ka.is_kloudc_token("teststore." + SECRET)  # lower-case prefix
    assert not ka.is_kloudc_token(f"{STORE}.short")
    assert not ka.is_kloudc_token(f"{STORE}.{SECRET} ")
    assert not ka.is_kloudc_token(None)


def test_options_and_cache_seconds_lowered(app, fake, clock):
    kc = app.extensions["kloudc_auth"]
    fake.answer(GOOD, body=session_body(cache_seconds=5))
    ident = kc.check(GOOD)
    assert isinstance(ident, ka.Identity) and ident.options == {"label_size": "62x33.5"}
    assert ident.username == "maria" and ident.can_print is True
    clock.now += 4
    kc.check(GOOD)
    assert len(session_calls(fake)) == 1
    clock.now += 2  # the server's 5 s lowered our 60 s
    kc.check(GOOD)
    assert len(session_calls(fake)) == 2
    # A larger hint can't raise it, and our own setting is capped at 120 s.
    fake.answer(GOOD, body=session_body(cache_seconds=900))
    clock.now += 10
    kc.check(GOOD)
    clock.now += 61
    kc.check(GOOD)
    assert len(session_calls(fake)) == 4
    assert ka.KloudcAuth(enabled=False, cache_seconds=500).cache_seconds == 120


def test_13f_dev_secret_never_signs_or_accepts_cookies(tmp_path, fake):
    """Outside tests, the public dev SECRET_KEY can't be used for kc_labels:
    /kloudc/enter refuses before asking KloudC, and a cookie signed with it
    is not accepted."""
    a = _make_app(tmp_path, fake.base, TESTING=False, SECRET_KEY=ka.DEV_SECRET_KEY)
    c = a.test_client()
    fake.redeem[CODE_OK] = (200, session_body())
    r = c.get(f"/kloudc/enter?code={CODE_OK}", base_url=KC_HOST)
    assert r.status_code == 303 and r.headers["Location"] == LOGIN_URL and "Set-Cookie" not in r.headers
    assert fake.calls == []
    forged = ka.make_handoff_cookie(
        ka.DEV_SECRET_KEY, ka.Identity(store_code=STORE, user_id=1, username="x", demo=False, can_print=True)
    )
    c.set_cookie("kc_labels", forged, domain="labels-teststore.kloudc.com")
    assert c.get("/api/health", base_url=KC_HOST).status_code == 401


# ── Security check (2026-10-22) ─────────────────────────────────────────────
def _from(ip):
    """Headers for a request that came through the tunnel from `ip`."""
    return {"CF-Connecting-IP": ip}


def test_sec_one_address_cannot_use_up_every_check(client, fake):
    # A stranger sends made-up tokens through the tunnel from one address.
    for i in range(200):
        client.get("/api/health", headers={**bearer(f"{STORE}.flood{i:04d}{SECRET}"), **_from("203.0.113.9")})
    assert len(session_calls(fake)) == 60  # half of the minute's 120
    r = client.get("/api/health", headers={**bearer(f"{STORE}.more{SECRET}"), **_from("203.0.113.9")})
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited"
    # Staff in the store (another address), never checked before, still get in.
    r = client.get(f"/api/lookup/{UPC}", headers={**bearer(GOOD), **_from("198.51.100.7")})
    assert r.status_code == 200


def test_sec_caller_address_rule():
    class Req:
        def __init__(self, peer, header=None):
            self.remote_addr = peer
            self.headers = {"CF-Connecting-IP": header} if header is not None else {}

    assert ka.kloudc_client_key(Req("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    assert ka.kloudc_client_key(Req("::1", "2001:db8::1")) == "2001:db8::1"
    assert ka.kloudc_client_key(Req("::ffff:127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    # Not from the tunnel: the header is ignored, the peer is the key.
    assert ka.kloudc_client_key(Req("192.168.1.20", "203.0.113.9")) == "192.168.1.20"
    # From the tunnel with no or a bad header: unknown (overall limit only).
    assert ka.kloudc_client_key(Req("127.0.0.1")) is None
    assert ka.kloudc_client_key(Req("127.0.0.1", "1.2.3.4, 5.6.7.8")) is None
    assert ka.kloudc_client_key(Req("127.0.0.1", "evil\r\nX: y")) is None
    assert ka.kloudc_client_key(Req(None)) is None


def test_sec_no_address_keeps_the_overall_limit(client, fake):
    # Without CF-Connecting-IP (e.g. a call on this machine) only the overall
    # 120 a minute applies, exactly as before.
    for i in range(130):
        client.get("/api/health", headers=bearer(f"{STORE}.flood{i:04d}{SECRET}"))
    assert len(session_calls(fake)) == 120


def test_sec_handoff_cookie_never_outlives_the_kloudc_session(client, fake):
    fake.redeem[CODE_OK] = (200, session_body(session_expires_at=_future(600)))
    r = client.get(f"/kloudc/enter?code={CODE_OK}", base_url=KC_HOST)
    assert r.status_code == 303 and r.headers["Location"] == "/"
    cookie = r.headers["Set-Cookie"]
    max_age = int(cookie.split("Max-Age=", 1)[1].split(";", 1)[0])
    assert 590 <= max_age <= 600
    value = cookie.split("kc_labels=", 1)[1].split(";", 1)[0]
    secret = client.application.config["SECRET_KEY"]
    now = time.time()
    assert ka.read_handoff_cookie(secret, value, STORE, now=now + 500) is not None
    assert ka.read_handoff_cookie(secret, value, STORE, now=now + 601) is None


def test_sec_cookie_with_ended_exp_refused(app):
    secret = app.config["SECRET_KEY"]
    now = time.time()
    ident = ka.Identity(
        store_code=STORE, user_id=12, username="maria", demo=False, can_print=True,
        session_expires_at=dt.datetime.fromtimestamp(now + 30, dt.timezone.utc),
    )
    value = ka.make_handoff_cookie(secret, ident, now=now)
    got = ka.read_handoff_cookie(secret, value, STORE, now=now + 10)
    assert got is not None and got.session_expires_at.timestamp() <= now + 30
    assert ka.read_handoff_cookie(secret, value, STORE, now=now + 31) is None


@pytest.mark.parametrize(
    "token",
    [
        f"{STORE}.abc\tX-Evil: 1{SECRET}",
        f"{STORE}.{SECRET} extra",
        f"{STORE.lower()}.{SECRET}",
        f"{STORE}.short",
        f"{STORE}..{SECRET}",
        f"{STORE}.{SECRET}/../../admin",
        f"{STORE}.{SECRET}@evil.example",
        "PLATFORM." + SECRET,
    ],
    ids=["tab", "space", "lower", "short", "double_dot", "path", "at", "other_prefix"],
)
def test_sec_odd_tokens_never_reach_kloudc(client, fake, token):
    r = client.get(f"/api/lookup/{UPC}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert session_calls(fake) == []


def test_sec_kloudc_answer_for_another_user_store_is_not_trusted(client, fake):
    # KloudC (or something in the middle) answering for another store, or
    # with the feature off, never signs anyone in - and nothing is cached as a yes.
    fake.answer(GOOD, body=session_body(store_code="OTHERSTORE"))
    assert client.get(f"/api/lookup/{UPC}", headers=bearer(GOOD)).status_code == 401
    assert client.post("/api/print", json={"upc": UPC}, headers=bearer(GOOD)).status_code == 401


def test_sec_legacy_cookie_unchanged_when_kloudc_is_on(client, fake):
    # The existing dashboard cookie: a bad one is still refused with the old
    # body and never sent to KloudC (it isn't KloudC-shaped).
    client.set_cookie("session_token", "not-a-real-session")
    r = client.get(f"/api/lookup/{UPC}")
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized", "message": "Log into the dashboard first"}
    client.set_cookie("session_token", "legacytoken")
    assert client.get(f"/api/lookup/{UPC}").status_code == 200
    assert fake.calls == []
