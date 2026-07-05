"""
tests/test_toast_provider.py — ToastDataProvider against a mocked Toast API.

Covers:
  * OAuth2 login: token fetched once, cached, and reused across calls
  * fetch_all: nested menu-group traversal, sku->upc mapping, department
    inheritance, skipping items without sku / name / positive price
  * fetch_by_upc: hit + miss, menus-document reuse within the TTL
  * retry behaviour: 429 then success; 401 invalidates the token and re-logins
  * health_check: True on 200 metadata, False on auth failure / no credentials
"""

from __future__ import annotations

import json

import pytest

from app.providers.toast_provider import ToastDataProvider
from app.services.ratelimit import TokenBucket


MENUS_DOC = {
    "menus": [
        {
            "name": "Grocery",
            "menuGroups": [
                {
                    "name": "Spices",
                    "menuItems": [
                        {"guid": "g1", "name": "Ground Cumin", "sku": "041196910184", "price": 4.99},
                        {"guid": "g2", "name": "No Barcode Item", "sku": "", "price": 3.99},
                        {"guid": "g3", "name": "Open Priced", "sku": "111", "price": 0},
                    ],
                    "menuGroups": [
                        {
                            "name": "Organic Spices",
                            "menuItems": [
                                {"guid": "g4", "name": "Organic Turmeric", "sku": "085239014288", "price": 7.99},
                            ],
                        }
                    ],
                }
            ],
        }
    ]
}

LOGIN_OK = {"token": {"accessToken": "tok-123", "expiresIn": 3600}}


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.content = b"" if payload is None else json.dumps(payload).encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"http {self.status_code}")


class FakeToast:
    """Programmable stand-in for requests.request."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        # Optional queue of responses for specific paths (popped in order).
        self.scripted: dict[str, list[FakeResponse]] = {}

    def __call__(self, method, url, headers=None, timeout=None, **kwargs):
        path = url.split("toasttab.com")[-1]
        self.calls.append((method, path, headers or {}))
        if path in self.scripted and self.scripted[path]:
            return self.scripted[path].pop(0)
        if path.endswith("/authentication/login"):
            return FakeResponse(200, LOGIN_OK)
        if path.endswith("/menus/v2/menus"):
            return FakeResponse(200, MENUS_DOC)
        if path.endswith("/menus/v2/metadata"):
            return FakeResponse(200, {"lastUpdated": "2026-07-04"})
        return FakeResponse(404, {"message": "not found"})

    def count(self, suffix: str) -> int:
        return sum(1 for _, p, _ in self.calls if p.endswith(suffix))


@pytest.fixture()
def fake(monkeypatch):
    fake = FakeToast()
    monkeypatch.setattr("app.providers.toast_provider.requests.request", fake)
    return fake


def _provider(**kw) -> ToastDataProvider:
    return ToastDataProvider(
        client_id="cid",
        client_secret="secret",
        api_base="https://ws-api.toasttab.com",
        restaurant_guid="r-guid",
        bucket=TokenBucket(rate=1000, capacity=1000),
        backoff_base=0.001,
        backoff_cap=0.002,
        backoff_max_retries=3,
        **kw,
    )


# ── OAuth ─────────────────────────────────────────────────────────────────────
def test_token_fetched_once_and_reused(fake):
    p = _provider()
    list(p.fetch_all())
    p.health_check()
    assert fake.count("/authentication/login") == 1
    # Authenticated calls carry the bearer + restaurant headers.
    _, _, headers = fake.calls[-1]
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["Toast-Restaurant-External-ID"] == "r-guid"


def test_missing_credentials_fail_health(fake):
    p = ToastDataProvider(
        client_id="",
        client_secret="",
        api_base="https://ws-api.toasttab.com",
        restaurant_guid="",
        bucket=TokenBucket(rate=1000, capacity=1000),
    )
    assert p.health_check() is False
    assert fake.calls == []  # never even tried the network


# ── Catalog mapping ───────────────────────────────────────────────────────────
def test_fetch_all_maps_and_skips(fake):
    recs = {r.upc: r for r in _provider().fetch_all()}
    # 2 valid items; blank-sku and zero-price items skipped.
    assert set(recs) == {"041196910184", "085239014288"}
    cumin = recs["041196910184"]
    assert cumin.name == "Ground Cumin"
    assert cumin.price == 4.99
    assert cumin.department == "Spices"
    assert cumin.sku == "041196910184"
    # Nested group inherits its own (deepest) group name as department.
    assert recs["085239014288"].department == "Organic Spices"


def test_fetch_by_upc_hit_miss_and_doc_reuse(fake):
    p = _provider()
    hit = p.fetch_by_upc("085239014288")
    assert hit is not None and hit.name == "Organic Turmeric"
    assert p.fetch_by_upc("000000000000") is None
    # Both lookups within the TTL -> menus downloaded only once.
    assert fake.count("/menus/v2/menus") == 1


# ── Retry / token refresh ─────────────────────────────────────────────────────
def test_retries_on_429_then_succeeds(fake):
    fake.scripted["/menus/v2/menus"] = [FakeResponse(429), FakeResponse(200, MENUS_DOC)]
    recs = list(_provider().fetch_all())
    assert len(recs) == 2
    assert fake.count("/menus/v2/menus") == 2


def test_401_invalidates_token_and_relogins(fake):
    fake.scripted["/menus/v2/metadata"] = [
        FakeResponse(401, {"message": "expired"}),
        FakeResponse(200, {"lastUpdated": "x"}),
    ]
    p = _provider()
    assert p.health_check() is True
    # First login, 401, re-login, success.
    assert fake.count("/authentication/login") == 2


def test_health_false_when_login_rejected(fake):
    fake.scripted["/authentication/v1/authentication/login"] = [
        FakeResponse(200, {"status": "FORBIDDEN"})  # no token in payload
    ]
    assert _provider().health_check() is False
