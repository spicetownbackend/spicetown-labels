"""
tests/test_stage2.py — Stage 2 acceptance tests.

Covers:
  * upsert: insert vs update, synced_at refresh
  * duplicate-UPC detection in the feed
  * >20% price-change flagging (+ PriceHistory, flag set/clear, TTL)
  * bulk_load stats + batched commits
  * bulk_load_guarded concurrency lock
  * CacheService: hit / refreshed / stale / miss + remote toggle
  * MissRateMonitor: window ratio + threshold warning
  * API: /api/lookup outcomes, /api/refresh, /api/stats
"""

from __future__ import annotations

import logging
import time

import pytest

from app import create_app
from app.extensions import db
from app.models import PriceHistory, Product, utcnow
from app.providers.base import DataProvider, ProductRecord
from app.services.cache import CacheService, MissRateMonitor
from app.services.loader import (
    RefreshInProgress,
    bulk_load,
    bulk_load_guarded,
    upsert_record,
    _refresh_lock,
)
from config import TestingConfig


# ── A controllable in-memory provider for tests ──────────────────────────────
class FakeProvider(DataProvider):
    name = "fake"

    def __init__(self, records):
        self._records = list(records)
        self.fetch_all_calls = 0
        self.fetch_by_upc_calls = 0

    def health_check(self):
        return True

    def fetch_all(self):
        self.fetch_all_calls += 1
        yield from self._records

    def fetch_by_upc(self, upc):
        self.fetch_by_upc_calls += 1
        for r in self._records:
            if r.upc == str(upc).strip():
                return r
        return None


def rec(upc, name="Item", price=5.0, **kw):
    return ProductRecord(upc=upc, name=name, price=price, **kw)


@pytest.fixture()
def app():
    # No background load/scheduler; we drive the loader/cache explicitly.
    app = create_app(config_object=TestingConfig, start_background=False)
    yield app


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield app


# ── upsert ───────────────────────────────────────────────────────────────────
def test_upsert_insert_then_update(ctx):
    upsert_record(rec("111", "Cumin", 4.99), source="file")
    db.session.commit()
    p = db.session.query(Product).filter_by(upc="111").one()
    assert p.price == 4.99
    first_sync = p.synced_at

    time.sleep(0.01)
    upsert_record(rec("111", "Cumin", 5.25), source="file")
    db.session.commit()
    p = db.session.query(Product).filter_by(upc="111").one()
    assert p.price == 5.25
    assert p.synced_at >= first_sync
    assert db.session.query(Product).count() == 1  # updated, not duplicated


def test_price_change_under_threshold_not_flagged(ctx):
    upsert_record(rec("222", "Paprika", 10.0), source="file")
    db.session.commit()
    upsert_record(rec("222", "Paprika", 11.5), source="file")  # +15%
    db.session.commit()
    p = db.session.query(Product).filter_by(upc="222").one()
    assert p.price_flagged is False
    hist = db.session.query(PriceHistory).filter_by(product_id=p.id).all()
    assert len(hist) == 1
    assert hist[0].flagged is False


def test_price_change_over_threshold_flagged(ctx, caplog):
    upsert_record(rec("333", "Saffron", 10.0), source="file")
    db.session.commit()
    with caplog.at_level(logging.WARNING, logger="spicetown.loader"):
        upsert_record(rec("333", "Saffron", 13.0), source="file")  # +30%
        db.session.commit()
    p = db.session.query(Product).filter_by(upc="333").one()
    assert p.price_flagged is True
    hist = db.session.query(PriceHistory).filter_by(product_id=p.id).one()
    assert hist.flagged is True
    assert abs(hist.delta_ratio - 0.30) < 1e-6
    assert any("SUSPICIOUS price change" in r.message for r in caplog.records)


def test_flag_clears_when_price_stabilizes(ctx):
    upsert_record(rec("444", "Clove", 10.0), source="file")
    db.session.commit()
    upsert_record(rec("444", "Clove", 20.0), source="file")  # +100% -> flagged
    db.session.commit()
    assert db.session.query(Product).filter_by(upc="444").one().price_flagged is True
    # Same price next sync -> stabilized, flag cleared.
    upsert_record(rec("444", "Clove", 20.0), source="file")
    db.session.commit()
    assert db.session.query(Product).filter_by(upc="444").one().price_flagged is False


def test_flagged_ttl_is_shorter(ctx):
    p = Product(upc="555", name="X", price=1.0, price_flagged=True,
                synced_at=utcnow())
    db.session.add(p)
    db.session.commit()
    # 1h TTL flagged vs 24h standard: 2h-old flagged row is stale.
    from datetime import timedelta
    p.synced_at = utcnow() - timedelta(hours=2)
    db.session.commit()
    assert p.is_fresh(standard_ttl=86400, flagged_ttl=3600) is False
    p.price_flagged = False
    assert p.is_fresh(standard_ttl=86400, flagged_ttl=3600) is True


# ── bulk_load ─────────────────────────────────────────────────────────────────
def test_bulk_load_inserts_and_dedupes(ctx):
    provider = FakeProvider([
        rec("a1", "One", 1.0),
        rec("a2", "Two", 2.0),
        rec("a1", "One", 9.0),       # exact duplicate (same upc+name) in feed
        rec("a1", "One B1G1", 0.5),  # shared barcode, different product → allowed
        rec("", "NoUPC", 3.0),       # invalid
    ])
    stats = bulk_load(provider, batch_size=2)
    assert stats.inserted == 3
    assert stats.duplicates == 1
    assert "a1" in stats.duplicate_upcs
    assert stats.skipped_invalid == 1
    assert db.session.query(Product).count() == 3
    # exact dup: first occurrence kept (price 1.0, not 9.0)
    assert (
        db.session.query(Product).filter_by(upc="a1", name="One").one().price == 1.0
    )
    # shared barcode: both variants live under the same UPC
    assert db.session.query(Product).filter_by(upc="a1").count() == 2


def test_bulk_load_updates_existing(ctx):
    bulk_load(FakeProvider([rec("b1", "X", 1.0)]))
    stats = bulk_load(FakeProvider([rec("b1", "X", 1.10), rec("b2", "Y", 2.0)]))
    assert stats.inserted == 1   # b2
    assert stats.updated == 1    # b1 price changed
    assert db.session.query(Product).count() == 2


def test_bulk_load_guarded_blocks_concurrent(ctx):
    # Simulate an in-progress refresh by holding the lock.
    assert _refresh_lock.acquire(blocking=False)
    try:
        with pytest.raises(RefreshInProgress):
            bulk_load_guarded(FakeProvider([rec("c1", "Z", 1.0)]))
    finally:
        _refresh_lock.release()


# ── CacheService ─────────────────────────────────────────────────────────────
def _cache(provider):
    return CacheService(
        provider,
        standard_ttl=86400,
        flagged_ttl=3600,
        miss_window_seconds=300,
        miss_warn_ratio=0.05,
    )


def test_cache_hit_does_not_touch_provider(ctx):
    provider = FakeProvider([rec("h1", "Cumin", 4.0)])
    bulk_load(provider)  # warms SQLite (fetch_all)
    cache = _cache(provider)
    provider.fetch_by_upc_calls = 0
    result = cache.get("h1")
    assert result.outcome == "hit"
    assert result.product.name == "Cumin"
    assert provider.fetch_by_upc_calls == 0  # never hit the source


def test_cache_miss_refreshes_from_provider(ctx):
    provider = FakeProvider([rec("m1", "New Item", 7.0)])
    cache = _cache(provider)
    result = cache.get("m1")
    assert result.outcome == "refreshed"
    assert result.product.price == 7.0
    assert provider.fetch_by_upc_calls == 1
    assert db.session.query(Product).filter_by(upc="m1").one().price == 7.0


def test_cache_miss_unknown_returns_miss(ctx):
    provider = FakeProvider([])
    cache = _cache(provider)
    result = cache.get("nope")
    assert result.outcome == "miss"
    assert result.found is False


def test_cache_remote_disabled_returns_stale_or_miss(ctx):
    provider = FakeProvider([rec("s1", "Item", 1.0)])
    cache = _cache(provider)
    # absent + remote disabled -> miss without touching provider
    r = cache.get("s1", allow_remote=False)
    assert r.outcome == "miss"
    assert provider.fetch_by_upc_calls == 0


def test_cache_stale_then_refresh(ctx):
    from datetime import timedelta
    provider = FakeProvider([rec("t1", "Item", 1.0)])
    bulk_load(provider)
    p = db.session.query(Product).filter_by(upc="t1").one()
    p.synced_at = utcnow() - timedelta(days=2)  # force stale
    db.session.commit()
    cache = _cache(provider)
    provider.fetch_by_upc_calls = 0
    r = cache.get("t1")
    assert r.outcome == "refreshed"
    assert provider.fetch_by_upc_calls == 1


# ── MissRateMonitor ──────────────────────────────────────────────────────────
def test_miss_monitor_warns_above_threshold(caplog):
    mon = MissRateMonitor(
        window_seconds=300, warn_ratio=0.05, min_samples=10, warn_cooldown_seconds=0
    )
    with caplog.at_level(logging.WARNING, logger="spicetown.cache"):
        for _ in range(8):
            mon.record(is_hit=True)
        for _ in range(5):
            mon.record(is_hit=False)  # 5/13 ≈ 38% > 5%
    assert any("cache miss rate" in r.message for r in caplog.records)
    snap = mon.snapshot()
    assert snap["window_misses"] == 5
    assert snap["lifetime_hits"] == 8


def test_miss_monitor_quiet_below_threshold(caplog):
    mon = MissRateMonitor(window_seconds=300, warn_ratio=0.5, min_samples=5)
    with caplog.at_level(logging.WARNING, logger="spicetown.cache"):
        for _ in range(20):
            mon.record(is_hit=True)
    assert not any("cache miss rate" in r.message for r in caplog.records)


# ── API ──────────────────────────────────────────────────────────────────────
@pytest.fixture()
def client(app):
    return app.test_client()


def test_api_refresh_and_lookup(app, client):
    # Swap in a controllable provider + matching cache for deterministic results.
    provider = FakeProvider([rec("api1", "Coriander", 3.5), rec("api2", "Mace", 9.0)])
    app.extensions["data_provider"] = provider
    app.extensions["cache"] = _cache(provider)

    r = client.post("/api/refresh")
    assert r.status_code == 200
    body = r.get_json()
    assert body["stats"]["inserted"] == 2

    look = client.get("/api/lookup/api1")
    assert look.status_code == 200
    assert look.get_json()["outcome"] == "hit"
    assert look.get_json()["product"]["name"] == "Coriander"

    missing = client.get("/api/lookup/zzz?remote=0")
    assert missing.status_code == 404
    assert missing.get_json()["outcome"] == "miss"


def test_api_stats(app, client):
    provider = FakeProvider([rec("st1", "Dill", 2.0)])
    app.extensions["data_provider"] = provider
    app.extensions["cache"] = _cache(provider)
    client.post("/api/refresh")
    s = client.get("/api/stats")
    assert s.status_code == 200
    body = s.get_json()
    assert body["product_count"] == 1
    assert "cache" in body


def test_api_refresh_conflict_when_locked(app, client):
    assert _refresh_lock.acquire(blocking=False)
    try:
        r = client.post("/api/refresh")
        assert r.status_code == 409
    finally:
        _refresh_lock.release()
