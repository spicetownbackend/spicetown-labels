"""
app/services/kloudc_auth.py - a second way to sign in: a KloudC session.

The labels site has no login of its own. Until now it only accepted the
older dashboard's session cookie (services/dashboard_auth.py). This module
adds KloudC: when a request carries a KloudC session token
(``<STORE_CODE>.<secret>``), the site asks the KloudC server who it is
(``GET /api/labels/session``) and remembers the answer for a short time.

Rules (the shared contract is docs/KLOUDC_AUTH.md):
  * Off unless STL_KLOUDC_AUTH_ENABLED is on AND the API base is on the
    built-in allow-list AND the store code is valid. Anything else logs one
    error and leaves KloudC sign-in off (fails closed).
  * A token for another store is refused without any outside call.
  * One attempt, no retries, redirects never followed, TLS verified,
    timeouts (2 s connect, STL_KLOUDC_TIMEOUT_SECONDS read).
  * Only these headers go out: Authorization, X-Store, Accept, User-Agent
    (plus Content-Type on the hand-off redeem). Never a Cookie or a
    forwarded-for header, and nothing from the environment (no proxy, no netrc).
  * The cache key is sha256(token). The raw token is never stored and never
    logged; logs show the first 8 characters of the hash and the username.
  * A yes is kept up to 60 s (at most 120 s, never past the session's end); a
    no is kept 30 s. When KloudC can't be reached, a token that was a yes in
    the last STALE_GRACE seconds still works. A 401/403 from KloudC always
    wins straight away: no grace after a no.
  * At most MAX_CHECKS_PER_MINUTE uncached calls a minute; above that the
    answer is 429 with no outside call (a recently-good token still gets the
    stale grace, so a flood of fake tokens can't lock real staff out). One
    caller address (CF-Connecting-IP behind the tunnel) may use at most half
    of that, so one stranger sending made-up tokens can't use it all up.
  * Nothing is written to any database.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union
from urllib.parse import urlsplit

import requests

logger = logging.getLogger("spicetown.kloudc_auth")

TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9_]{2,23}\.[A-Za-z0-9_-]{20,}$")
STORE_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_]{2,23}$")
# A hand-off code: 32 random bytes, URL-safe base64 (43 chars). Accept a range.
HANDOFF_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")

CONNECT_TIMEOUT_SECONDS = 2.0
MAX_CACHE_SECONDS = 120
MAX_CACHE_ENTRIES = 2000
MAX_TRACKED_CLIENTS = 5000
USER_AGENT = "labels-site-kloudc-auth/1"
DEFAULT_LOGIN_URL = "https://app.kloudc.com/#/label-printing/open"

SESSION_PATH = "/api/labels/session"
REDEEM_PATH = "/api/labels/handoff/redeem"

# Plain messages (contract section 2.3).
MSG_SIGN_IN_AGAIN = "Sign in again to use label printing."
MSG_TURNED_OFF = "Label printing is turned off for this store."
MSG_UNAVAILABLE = "Can't check your sign-in right now. Try again in a minute."
MSG_RATE_LIMITED = "Too many sign-in checks. Try again in a minute."

_LOCAL_BASE_RE = re.compile(r"^http://(127\.0\.0\.1|localhost):([0-9]{1,5})$")
_TAILNET_BASE_RE = re.compile(
    r"^https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\.ts\.net(?::([0-9]{1,5}))?$"
)


# ── Results ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Identity:
    """Who the KloudC server says this token belongs to."""

    store_code: str
    user_id: Any
    username: str
    display_name: str = ""
    is_admin: bool = False
    demo: bool = False
    can_print: bool = False
    options: dict = field(default_factory=dict, compare=False, hash=False)
    session_expires_at: Optional[dt.datetime] = None  # aware UTC; None = no end known
    cache_seconds: Optional[int] = None  # the server's hint; can only lower ours


@dataclass(frozen=True)
class Refusal:
    """A definite no. `status` is the HTTP status the labels site answers."""

    status: int
    error: str
    message: str

    def body(self) -> dict:
        return {"error": self.error, "message": self.message}


@dataclass(frozen=True)
class Unavailable:
    """KloudC couldn't be asked (timeout, connection error, 5xx, 429)."""

    status: int = 503
    error: str = "auth_unavailable"
    message: str = MSG_UNAVAILABLE

    def body(self) -> dict:
        return {"error": self.error, "message": self.message}


Result = Union[Identity, Refusal, Unavailable]

REFUSED_SIGN_IN = Refusal(401, "unauthorized", MSG_SIGN_IN_AGAIN)
REFUSED_OFF = Refusal(403, "label_printing_off", MSG_TURNED_OFF)
REFUSED_RATE = Refusal(429, "rate_limited", MSG_RATE_LIMITED)


# ── Small helpers ────────────────────────────────────────────────────────────
def is_kloudc_token(value: Optional[str]) -> bool:
    """True for a KloudC-shaped session token ``<CODE>.<secret>``. Tokens from
    the older system have no dot, so the two kinds never mix."""
    return bool(value) and isinstance(value, str) and bool(TOKEN_RE.match(value))


def token_store_code(token: str) -> str:
    return token.split(".", 1)[0]


def token_key(token: str) -> str:
    """The cache key: sha256 hex of the token. The raw token is never kept."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_api_base(value: Optional[str]) -> Optional[str]:
    """The API base normalised (no trailing slash) when it is on the
    built-in allow-list, else None."""
    base = (value or "").strip().rstrip("/")
    if not base:
        return None
    if base == "https://app.kloudc.com":
        return base
    m = _LOCAL_BASE_RE.match(base)
    if m and 1 <= int(m.group(2)) <= 65535:
        return base
    m = _TAILNET_BASE_RE.match(base)
    if m and (m.group(1) is None or 1 <= int(m.group(1)) <= 65535):
        return base
    return None


def _validate_login_url(value: Optional[str]) -> str:
    url = (value or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme != "https" or not parts.hostname or parts.username:
        if url:
            logger.error("kloudc_auth: STL_KLOUDC_LOGIN_URL must be an https address - using the default")
        return DEFAULT_LOGIN_URL
    return url


def _parse_time(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def identity_from_body(body: Any, store_code: str, now: float) -> Optional[Identity]:
    """An Identity when a KloudC 200 body passes every check, else None.

    All must hold: a JSON object, store_code matches, label_printing.enabled
    is exactly true, session_expires_at is in the future, and a user id and
    username are present. `can_print` is true only when the server says
    can_print true AND demo is not true (fails closed)."""
    if not isinstance(body, dict):
        return None
    if body.get("store_code") != store_code:
        return None
    lp = body.get("label_printing")
    if not isinstance(lp, dict) or lp.get("enabled") is not True:
        return None
    expires = _parse_time(body.get("session_expires_at"))
    if expires is None or expires.timestamp() <= now:
        return None
    user = body.get("user")
    if not isinstance(user, dict):
        return None
    user_id = user.get("id")
    username = user.get("username")
    if user_id is None or not isinstance(username, str) or not username:
        return None
    demo = body.get("demo") is not False  # missing/odd = demo (fails closed)
    can_print = body.get("can_print") is True and not demo
    options = lp.get("options") if isinstance(lp.get("options"), dict) else {}
    display = user.get("display_name")
    hint = body.get("cache_seconds")
    hint = hint if isinstance(hint, int) and not isinstance(hint, bool) and hint >= 0 else None
    return Identity(
        store_code=store_code,
        user_id=user_id,
        username=username,
        display_name=display if isinstance(display, str) else "",
        is_admin=body.get("is_admin") is True,
        demo=demo,
        can_print=can_print,
        options=options,
        session_expires_at=expires,
        cache_seconds=hint,
    )


def _body_error(resp) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, str):
            return err
        detail = data.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("error"), str):
            return detail["error"]
    return ""


# ── The checker ──────────────────────────────────────────────────────────────
class KloudcAuth:
    """Checks KloudC tokens against the KloudC server, with a small cache.

    One instance per app (app.extensions["kloudc_auth"]); thread safe.
    `http` is the function that does one HTTP call; tests may swap it.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        api_base: str = "",
        store_code: str = "",
        login_url: str = DEFAULT_LOGIN_URL,
        cache_seconds: int = 60,
        negative_cache_seconds: int = 30,
        stale_grace_seconds: int = 300,
        timeout_seconds: float = 3.0,
        max_checks_per_minute: int = 120,
        clock: Callable[[], float] = time.time,
        http: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.api_base = validate_api_base(api_base) or ""
        self.store_code = (store_code or "").strip()
        self.login_url = _validate_login_url(login_url)
        self.cache_seconds = max(0, min(int(cache_seconds), MAX_CACHE_SECONDS))
        self.negative_cache_seconds = max(0, int(negative_cache_seconds))
        self.stale_grace_seconds = max(0, int(stale_grace_seconds))
        self.timeout_seconds = float(timeout_seconds) if timeout_seconds and timeout_seconds > 0 else 3.0
        self.max_checks_per_minute = max(0, int(max_checks_per_minute))
        # One caller address may use at most half of the minute's checks.
        self.max_checks_per_client = max(1, self.max_checks_per_minute // 2)
        self._clock = clock
        self._http = http or _http_call
        self._lock = threading.Lock()
        self._cache: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self._calls: "collections.deque[float]" = collections.deque()
        self._client_calls: "collections.OrderedDict[str, collections.deque]" = collections.OrderedDict()

        self.enabled = False
        if not enabled:
            return
        if not self.api_base:
            logger.error(
                "kloudc_auth: STL_KLOUDC_API_BASE is not on the allow-list - KloudC sign-in stays off"
            )
            return
        if not STORE_CODE_RE.match(self.store_code):
            logger.error(
                "kloudc_auth: STL_KLOUDC_STORE_CODE is missing or not a valid store code - "
                "KloudC sign-in stays off"
            )
            return
        self.enabled = True
        logger.info("kloudc_auth: on (api=%s store=%s)", self.api_base, self.store_code)

    # ── public ──────────────────────────────────────────────────────────────
    def check(self, token: Optional[str], client: Optional[str] = None) -> Result:
        """Who is this KloudC token? Identity, Refusal or Unavailable.

        `client` is the caller's address when known (rate-limit key only)."""
        if not self.enabled or not is_kloudc_token(token):
            return REFUSED_SIGN_IN
        if token_store_code(token) != self.store_code:
            return REFUSED_SIGN_IN  # another store's token: no outside call
        key = token_key(token)
        now = self._clock()

        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                if "refused" in entry:
                    if now < entry["until"]:
                        return entry["refused"]
                elif now < entry["fresh_until"]:
                    return entry["identity"]
            if not self._take_call_slot(now, client):
                stale = self._stale_identity(entry, now)
                if stale is not None:
                    logger.info("kloudc_auth stale_grace (check cap) key=%s user=%s", key[:8], stale.username)
                    return stale
                logger.warning("kloudc_auth: check cap reached - refusing key=%s", key[:8])
                return REFUSED_RATE

        result = self._ask_session(token, key)
        now = self._clock()

        with self._lock:
            if isinstance(result, Identity):
                ttl = self.cache_seconds
                if result.cache_seconds is not None:
                    ttl = min(ttl, result.cache_seconds)
                fresh_until = now + ttl
                if result.session_expires_at is not None:
                    fresh_until = min(fresh_until, result.session_expires_at.timestamp())
                self._store(key, {"identity": result, "checked_at": now, "fresh_until": fresh_until})
                return result
            if isinstance(result, Refusal):
                self._store(key, {"refused": result, "until": now + self.negative_cache_seconds})
                return result
            # Unavailable: stale grace for a token that was a yes recently.
            stale = self._stale_identity(self._cache.get(key), now)
            if stale is not None:
                logger.info("kloudc_auth stale_grace key=%s user=%s", key[:8], stale.username)
                return stale
            return result

    def redeem(self, code: Optional[str], client: Optional[str] = None) -> Result:
        """Trade a one-time website hand-off code for an identity. Never cached."""
        if not self.enabled or not isinstance(code, str) or not HANDOFF_CODE_RE.match(code):
            return REFUSED_SIGN_IN
        with self._lock:
            if not self._take_call_slot(self._clock(), client):
                return REFUSED_RATE
        return self._ask(
            "POST",
            REDEEM_PATH,
            headers={"Content-Type": "application/json"},
            json_body={"code": code},
            log_key="handoff",
        )

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._calls.clear()
            self._client_calls.clear()

    def cache_keys(self) -> list:
        with self._lock:
            return list(self._cache.keys())

    # ── internals ───────────────────────────────────────────────────────────
    def _take_call_slot(self, now: float, client: Optional[str] = None) -> bool:
        """Count one uncached outside call against the per-minute ceiling and,
        when the caller's address is known, its own share. Caller holds the lock."""
        while self._calls and self._calls[0] <= now - 60:
            self._calls.popleft()
        if len(self._calls) >= self.max_checks_per_minute:
            return False
        mine = None
        if client:
            mine = self._client_calls.get(client)
            if mine is not None:
                while mine and mine[0] <= now - 60:
                    mine.popleft()
                if len(mine) >= self.max_checks_per_client:
                    return False
        self._calls.append(now)
        if client:
            if mine is None:
                mine = self._client_calls[client] = collections.deque()
            mine.append(now)
            self._client_calls.move_to_end(client)
            while len(self._client_calls) > MAX_TRACKED_CLIENTS:
                self._client_calls.popitem(last=False)
        return True

    def _stale_identity(self, entry: Optional[dict], now: float) -> Optional[Identity]:
        if not entry or "identity" not in entry:
            return None
        if now - entry["checked_at"] > self.stale_grace_seconds:
            return None
        ident = entry["identity"]
        if ident.session_expires_at is not None and ident.session_expires_at.timestamp() <= now:
            return None
        return ident

    def _store(self, key: str, value: dict) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)

    def _ask_session(self, token: str, key: str) -> Result:
        return self._ask(
            "GET",
            SESSION_PATH,
            headers={"Authorization": f"Bearer {token}"},
            json_body=None,
            log_key=key[:8],
        )

    def _ask(self, method: str, path: str, *, headers: dict, json_body, log_key: str) -> Result:
        out = {
            **headers,
            "X-Store": self.store_code,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        try:
            resp = self._http(
                method,
                self.api_base + path,
                headers=out,
                json_body=json_body,
                timeout=(CONNECT_TIMEOUT_SECONDS, self.timeout_seconds),
            )
        except requests.RequestException as exc:
            logger.warning("kloudc_auth: KloudC unreachable (%s) key=%s", type(exc).__name__, log_key)
            return Unavailable()
        except Exception as exc:  # noqa: BLE001 - any failure to ask = unavailable
            logger.warning("kloudc_auth: KloudC call failed (%s) key=%s", type(exc).__name__, log_key)
            return Unavailable()

        status = resp.status_code
        if status == 200:
            try:
                body = resp.json()
            except ValueError:
                body = None
            ident = identity_from_body(body, self.store_code, self._clock())
            if ident is None:
                logger.warning("kloudc_auth: KloudC answer failed the checks - refused key=%s", log_key)
                return REFUSED_SIGN_IN
            return ident
        if status == 429 or status >= 500:
            logger.warning("kloudc_auth: KloudC answered %d key=%s", status, log_key)
            return Unavailable()
        if status == 403 and _body_error(resp) == "label_printing_off":
            logger.info("kloudc_auth: label printing off key=%s", log_key)
            return REFUSED_OFF
        # 401, other 403/409, 3xx (never followed), 404/410 (hand-off), anything else.
        logger.info("kloudc_auth: KloudC refused (%d) key=%s", status, log_key)
        return REFUSED_SIGN_IN


def _http_call(method: str, url: str, *, headers: dict, json_body, timeout):
    """One HTTP call with nothing added: a fresh session that ignores the
    environment (no proxy, no netrc auth), keeps no cookies, sends no default
    headers, follows no redirects and verifies TLS."""
    with requests.Session() as session:
        session.trust_env = False
        session.headers.clear()
        return session.request(
            method,
            url,
            headers=headers,
            json=json_body,
            timeout=timeout,
            allow_redirects=False,
            verify=True,
        )


def kloudc_client_key(request) -> Optional[str]:
    """The caller's address for the per-caller check limit, or None (then
    only the overall limit applies, as before).

    Behind the Cloudflare tunnel every request comes from this machine
    (127.0.0.1 / ::1) and Cloudflare writes the real address in
    CF-Connecting-IP; only then is that header used. A request from any other
    peer is keyed by the peer itself. A bad header value means "unknown"."""
    import ipaddress

    peer = (getattr(request, "remote_addr", None) or "").strip()
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return None
    mapped = getattr(peer_ip, "ipv4_mapped", None)
    if mapped is not None:
        peer_ip = mapped  # ::ffff:127.0.0.1 is this machine too
    if not peer_ip.is_loopback:
        return str(peer_ip)
    header = (request.headers.get("CF-Connecting-IP") or "").strip()
    try:
        return str(ipaddress.ip_address(header))
    except ValueError:
        return None


def build_kloudc_auth(config) -> KloudcAuth:
    """KloudcAuth from a Flask config mapping (all STL_KLOUDC_* settings)."""
    get = config.get
    return KloudcAuth(
        enabled=bool(get("KLOUDC_AUTH_ENABLED", False)),
        api_base=get("KLOUDC_API_BASE", "") or "",
        store_code=get("KLOUDC_STORE_CODE", "") or "",
        login_url=get("KLOUDC_LOGIN_URL", DEFAULT_LOGIN_URL) or DEFAULT_LOGIN_URL,
        cache_seconds=get("KLOUDC_CACHE_SECONDS", 60),
        negative_cache_seconds=get("KLOUDC_NEGATIVE_CACHE_SECONDS", 30),
        stale_grace_seconds=get("KLOUDC_STALE_GRACE_SECONDS", 300),
        timeout_seconds=get("KLOUDC_TIMEOUT_SECONDS", 3.0),
        max_checks_per_minute=get("KLOUDC_MAX_CHECKS_PER_MINUTE", 120),
    )


# ── Website hand-off cookie (kc_labels) ──────────────────────────────────────
HANDOFF_COOKIE = "kc_labels"
HANDOFF_COOKIE_SALT = "kloudc-labels-v1"
HANDOFF_COOKIE_MAX_AGE = 7200


DEV_SECRET_KEY = "dev-only-change-me"


def handoff_secret(app) -> str:
    """The key that signs kc_labels, or "" when there is no usable one: blank,
    or the public dev default outside tests (anyone could forge a cookie
    signed with it, so the hand-off cookie is neither set nor accepted)."""
    secret = app.config.get("SECRET_KEY") or ""
    if not secret or (secret == DEV_SECRET_KEY and not app.testing):
        return ""
    return secret


def _serializer(secret_key: str):
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(secret_key, salt=HANDOFF_COOKIE_SALT)


def handoff_cookie_end(ident: Identity, now: Optional[float] = None) -> int:
    """When a kc_labels cookie for `ident` stops working: 2 hours from now,
    or the end of the KloudC session behind it when that comes sooner (a
    session with 10 minutes left must not open the site for 2 hours)."""
    iat = int(now if now is not None else time.time())
    end = iat + HANDOFF_COOKIE_MAX_AGE
    if ident.session_expires_at is not None:
        end = min(end, int(ident.session_expires_at.timestamp()))
    return end


def make_handoff_cookie(secret_key: str, ident: Identity, now: Optional[float] = None) -> str:
    iat = int(now if now is not None else time.time())
    payload = {
        "store": ident.store_code,
        "user_id": ident.user_id,
        "username": ident.username,
        "demo": bool(ident.demo),
        "can_print": bool(ident.can_print),
        "iat": iat,
        "exp": handoff_cookie_end(ident, iat),
    }
    return _serializer(secret_key).dumps(payload)


def read_handoff_cookie(
    secret_key: str, value: Optional[str], store_code: str, now: Optional[float] = None
) -> Optional[Identity]:
    """The Identity in a valid, unexpired kc_labels cookie for this store, else
    None (missing, tampered, signed with another key, over 2 h old, other store)."""
    if not value or not secret_key:
        return None
    from itsdangerous import BadData

    try:
        payload = _serializer(secret_key).loads(value, max_age=HANDOFF_COOKIE_MAX_AGE)
    except BadData:
        return None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict) or payload.get("store") != store_code:
        return None
    iat = payload.get("iat")
    current = now if now is not None else time.time()
    if not isinstance(iat, int) or iat > current + 60 or current - iat > HANDOFF_COOKIE_MAX_AGE:
        return None
    end = iat + HANDOFF_COOKIE_MAX_AGE
    exp = payload.get("exp", end)
    if not isinstance(exp, int) or isinstance(exp, bool) or current >= exp:
        return None  # the KloudC session behind the cookie has ended
    end = min(end, exp)
    username = payload.get("username")
    if not isinstance(username, str) or not username or payload.get("user_id") is None:
        return None
    demo = payload.get("demo") is not False
    return Identity(
        store_code=store_code,
        user_id=payload.get("user_id"),
        username=username,
        demo=demo,
        can_print=payload.get("can_print") is True and not demo,
        session_expires_at=dt.datetime.fromtimestamp(end, dt.timezone.utc),
    )
