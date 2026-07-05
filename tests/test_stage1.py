"""
tests/test_stage1.py — Stage 1 acceptance tests.

Covers:
  * token-bucket limiter: burst, sustained refill, blocking, decorator
  * exponential backoff helper math + retry decorator
  * FileDataProvider: CSV + JSON parsing, alias headers, UPC normalization
  * app factory: schema creation, /api/health, /api/lookup
"""

from __future__ import annotations

import time

import pytest

from app import create_app
from app.extensions import db
from app.models import Product
from app.providers.file_provider import FileDataProvider
from app.services.ratelimit import (
    RetryableError,
    TokenBucket,
    backoff_retry,
    compute_backoff,
)
from config import TestingConfig


# ── Rate limiter ─────────────────────────────────────────────────────────────
def test_bucket_burst_then_empty():
    b = TokenBucket(rate=10, capacity=20)
    # Burst of 20 should succeed immediately, the 21st should fail (non-blocking).
    assert all(b.try_acquire(1) for _ in range(20))
    assert b.try_acquire(1) is False


def test_bucket_refills_over_time():
    b = TokenBucket(rate=10, capacity=20, initial_tokens=0)
    assert b.try_acquire(1) is False
    time.sleep(0.25)  # ~2.5 tokens accrue at 10/s
    assert b.try_acquire(1) is True


def test_bucket_blocking_acquire_waits():
    b = TokenBucket(rate=20, capacity=1, initial_tokens=0)
    start = time.monotonic()
    assert b.acquire(1, blocking=True, timeout=1.0) is True
    assert time.monotonic() - start >= 0.02  # waited for a refill


def test_bucket_timeout_returns_false():
    b = TokenBucket(rate=1, capacity=1, initial_tokens=0)
    assert b.acquire(1, blocking=True, timeout=0.05) is False


def test_bucket_rejects_oversized_request():
    b = TokenBucket(rate=10, capacity=20)
    with pytest.raises(ValueError):
        b.acquire(21)


def test_bucket_decorator():
    b = TokenBucket(rate=100, capacity=5)
    calls = []

    @b
    def work(x):
        calls.append(x)
        return x * 2

    assert work(3) == 6
    assert calls == [3]


# ── Backoff ──────────────────────────────────────────────────────────────────
def test_compute_backoff_is_bounded():
    for attempt in range(10):
        d = compute_backoff(attempt, base=0.5, cap=30, jitter="full")
        assert 0.0 <= d <= 30.0


def test_backoff_retry_eventually_succeeds():
    state = {"n": 0}

    @backoff_retry(max_retries=5, base=0.001, cap=0.01)
    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise RetryableError("boom", status=503)
        return "ok"

    assert flaky() == "ok"
    assert state["n"] == 3


def test_backoff_retry_gives_up():
    @backoff_retry(max_retries=2, base=0.001, cap=0.01)
    def always_fail():
        raise RetryableError("nope", status=429)

    with pytest.raises(RetryableError):
        always_fail()


# ── FileDataProvider ─────────────────────────────────────────────────────────
def test_file_provider_csv(tmp_path):
    csv_file = tmp_path / "products.csv"
    csv_file.write_text(
        "barcode,product,retail,clearance\n"
        "012345678905,Test Item,1.99,1\n"
        "  012000000010 ,Second Item,$2.50,\n",
        encoding="utf-8",
    )
    p = FileDataProvider(csv_file)
    assert p.health_check() is True
    recs = list(p.fetch_all())
    assert len(recs) == 2
    assert recs[0].upc == "012345678905"
    assert recs[0].clearance is True
    assert recs[1].upc == "012000000010"  # trimmed
    assert recs[1].price == 2.5  # "$2.50" coerced


def test_file_provider_json(tmp_path):
    json_file = tmp_path / "products.json"
    json_file.write_text(
        '{"products":[{"ean":"099","title":"X","list_price":3.0,"promo":2.0}]}',
        encoding="utf-8",
    )
    p = FileDataProvider(json_file)
    recs = list(p.fetch_all())
    assert len(recs) == 1
    assert recs[0].upc == "099"
    assert recs[0].sale_price == 2.0
    assert recs[0].on_sale is True  # inferred from promo price


def test_file_provider_fetch_by_upc(tmp_path):
    csv_file = tmp_path / "p.csv"
    csv_file.write_text("upc,name,price\n111,A,1\n222,B,2\n", encoding="utf-8")
    p = FileDataProvider(csv_file)
    assert p.fetch_by_upc("222").name == "B"
    assert p.fetch_by_upc("999") is None


# ── App factory / routes ─────────────────────────────────────────────────────
@pytest.fixture()
def app():
    app = create_app(config_object=TestingConfig)
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def test_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True


def test_lookup_found_and_missing(app, client):
    with app.app_context():
        db.session.add(Product(upc="555000111", name="Cardamom Pods", price=12.0))
        db.session.commit()

    ok = client.get("/api/lookup/555000111")
    assert ok.status_code == 200
    assert ok.get_json()["product"]["name"] == "Cardamom Pods"

    missing = client.get("/api/lookup/000000000")
    assert missing.status_code == 404
    assert missing.get_json()["found"] is False


def test_indexed_upc_allows_shared_barcodes(app):
    # The store sells variants sharing a barcode (e.g. "XYZ" / "XYZ B1G1"),
    # so upc is indexed but intentionally NOT unique.
    with app.app_context():
        db.session.add(Product(upc="dup-123", name="One", price=1.0))
        db.session.commit()
        db.session.add(Product(upc="dup-123", name="Two", price=2.0))
        db.session.commit()
        assert db.session.query(Product).filter_by(upc="dup-123").count() == 2


def test_stage_stubs_return_501(client):
    # All previously-stubbed endpoints are now implemented:
    #   /api/refresh (Stage 2), /api/print (Stage 3), /api/search (Stage 4).
    # /api/search now returns 200 (empty catalog -> empty results), not 501.
    resp = client.get("/api/search?q=cumin")
    assert resp.status_code == 200
    assert resp.get_json()["results"] == []
