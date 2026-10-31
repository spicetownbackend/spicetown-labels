"""Automatic labels for new items and barcode changes (Oct 31 2026).

Owner: "print labels for newly added items, if there is no barcode, print them
without barcode, and also print the items if the barcode is updated."
"""

from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db
from app.models import LabelEvent, PrintJob, Product
from app.providers.base import DataProvider, ProductRecord
from app.services.auto_print import NO_BARCODE_FIELDS, auto_print_price_changes
from app.services.loader import bulk_load
from config import TestingConfig


class RemoteTestingConfig(TestingConfig):
    PRINT_MODE = "remote"  # jobs are rows in print_jobs; no printer needed


class FakeProvider(DataProvider):
    name = "fake"

    def __init__(self, records):
        self._records = list(records)

    def health_check(self):
        return True

    def fetch_all(self):
        yield from self._records

    def fetch_by_upc(self, upc):
        return next((r for r in self._records if r.upc == upc), None)


def rec(upc, name, price=5.0, sku=None):
    return ProductRecord(upc=upc, name=name, price=price, sku=sku)


@pytest.fixture()
def app():
    yield create_app(config_object=RemoteTestingConfig, start_background=False)


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield app


CATALOG = [rec("041196910184", "Ground Cumin", 4.99, sku="041196910184"), rec("TG-AAAAAAAAAAAA", "Veg Samosa", 1.99)]


def _sync(app, records):
    bulk_load(FakeProvider(records))
    return auto_print_price_changes(app)


def _jobs():
    return db.session.query(PrintJob).order_by(PrintJob.id).all()


def test_first_load_into_an_empty_catalog_prints_nothing(ctx):
    _sync(ctx, CATALOG)
    assert db.session.query(LabelEvent).count() == 0
    assert _jobs() == []


def test_new_item_with_a_barcode_prints_a_full_label(ctx):
    _sync(ctx, CATALOG)
    out = _sync(ctx, CATALOG + [rec("8901764051258", "Limca 300ml", 0.99, sku="8901764051258")])
    assert out["new_items"]["printed"] == 1
    (job,) = _jobs()
    assert (job.upc, job.reason, job.fields) == ("8901764051258", "new_item", None)


def test_new_item_without_a_barcode_prints_without_one(ctx):
    _sync(ctx, CATALOG)
    _sync(ctx, CATALOG + [rec("TG-BBBBBBBBBBBB", "Masala Dosa Batter", 6.99)])
    (job,) = _jobs()
    assert job.reason == "new_item"
    assert job.fields == NO_BARCODE_FIELDS
    assert "barcode" not in job.fields


def test_barcode_added_to_an_item_prints_its_new_label(ctx):
    _sync(ctx, CATALOG)
    # Toast now has a barcode for Veg Samosa: same name, new key.
    _sync(ctx, CATALOG[:1] + [rec("8906001234567", "Veg Samosa", 1.99, sku="8906001234567")])
    (job,) = _jobs()
    assert (job.upc, job.reason, job.fields) == ("8906001234567", "barcode_change", None)


def test_each_event_prints_once(ctx):
    _sync(ctx, CATALOG)
    new = CATALOG + [rec("TG-BBBBBBBBBBBB", "Masala Dosa Batter", 6.99)]
    _sync(ctx, new)
    out = _sync(ctx, new)
    assert len(_jobs()) == 1
    assert out["new_items"]["printed"] == 0


def test_an_item_with_no_price_waits_for_one_and_prints_once(ctx):
    _sync(ctx, CATALOG)
    out = _sync(ctx, CATALOG + [rec("TG-CCCCCCCCCCCC", "Diwali Gift Box", 0.0)])
    assert out["new_items"]["waiting"] == 1
    assert _jobs() == []
    # The price arrives: the price-change label covers it; no second label.
    _sync(ctx, CATALOG + [rec("TG-CCCCCCCCCCCC", "Diwali Gift Box", 24.99)])
    jobs = _jobs()
    assert len(jobs) == 1
    assert db.session.query(LabelEvent).one().outcome in ("printed", "already_printed")


def test_switched_off_prints_nothing_and_does_not_pile_up(ctx, app):
    from app.services.label_settings import set_auto_print_switches

    _sync(ctx, CATALOG)
    set_auto_print_switches({"new_items": False})
    _sync(ctx, CATALOG + [rec("TG-BBBBBBBBBBBB", "Masala Dosa Batter", 6.99)])
    assert _jobs() == []
    assert db.session.query(LabelEvent).one().outcome == "switched_off"
    # Turned back on later: the old event doesn't print.
    set_auto_print_switches({"new_items": True})
    auto_print_price_changes(app)
    assert _jobs() == []


def test_at_most_the_cap_per_sync_rest_next_time(ctx):
    ctx.config["AUTO_PRINT_EVENTS_PER_PASS"] = 2
    _sync(ctx, CATALOG)
    more = CATALOG + [rec(f"TG-D{i:011d}", f"New Item {i}", 1.0) for i in range(5)]
    out = _sync(ctx, more)
    assert (out["new_items"]["printed"], out["new_items"]["waiting"]) == (2, 3)
    _sync(ctx, more)
    _sync(ctx, more)
    assert len(_jobs()) == 5


def test_switch_endpoints_and_print_history_filter(app):
    client = app.test_client()
    assert client.get("/api/auto-print-settings").get_json() == {
        "new_items": True, "barcode_changes": True, "price_changes": True,
    }
    r = client.post("/api/auto-print-settings", json={"barcode_changes": False, "junk": True})
    assert r.get_json()["barcode_changes"] is False
    assert client.get("/api/auto-print-settings").get_json()["new_items"] is True

    with app.app_context():
        _sync(app, CATALOG)
        _sync(app, CATALOG + [rec("TG-BBBBBBBBBBBB", "Masala Dosa Batter", 6.99)])
    body = client.get("/api/print-history?reason=new_item").get_json()
    assert [j["reason"] for j in body["jobs"]] == ["new_item"]
    assert body["counts"]["new_item"] == 1
    assert body["counts"]["manual"] == 0
