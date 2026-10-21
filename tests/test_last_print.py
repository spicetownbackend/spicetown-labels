"""
tests/test_last_print.py — "was this label already printed, at this price?"

Covers:
  * /api/lookup + /api/search: every product carries `last_print`.
  * /api/products/<id>/last-print: the dedicated pre-print check.
  * /api/print: records price_at_print, returns previous_print.
  * /api/print-history: old/new price on price-change rows, inferred price
    for legacy rows, counts per reason, product_id filter.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import PriceHistory, PrintJob, Product, utcnow
from app.services.auto_print import auto_print_price_changes
from app.services.print_history import last_prints_for
from config import TestingConfig


class RemoteConfig(TestingConfig):
    # Remote mode queues jobs in SQLite with no worker/printer involved, so
    # nothing here can ever reach a real printer.
    PRINT_MODE = "remote"
    BRIDGE_TOKEN = "t"


@pytest.fixture()
def app():
    yield create_app(config_object=RemoteConfig, start_background=False)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield app


def _product(upc="111", name="Cumin 200g", price=5.99, **kw):
    p = Product(upc=upc, name=name, price=price, **kw)
    db.session.add(p)
    db.session.commit()
    return p


def _change(p, old, new, *, ago_s=0, source="toast"):
    h = PriceHistory(
        product_id=p.id,
        old_price=old,
        new_price=new,
        source=source,
        recorded_at=utcnow() - timedelta(seconds=ago_s),
    )
    db.session.add(h)
    db.session.commit()
    return h


def _job(p, *, reason=None, status="done", ago_s=0, price=None, variant="standard", ph=None):
    j = PrintJob(
        upc=p.upc,
        product_id=p.id,
        variant=variant,
        copies=1,
        status=status,
        reason=reason,
        price_at_print=price,
        price_history_id=ph,
        created_at=utcnow() - timedelta(seconds=ago_s),
    )
    db.session.add(j)
    db.session.commit()
    return j


# ── last_print on lookup / search / dedicated endpoint ──────────────────────
def test_lookup_last_print_none_when_never_printed(client, ctx):
    _product()
    body = client.get("/api/lookup/111?remote=0").get_json()
    assert body["product"]["last_print"] is None
    assert body["products"][0]["last_print"] is None


def test_lookup_last_print_after_auto_print(client, ctx):
    p = _product(price=6.49)
    h = _change(p, 5.99, 6.49)
    auto_print_price_changes(ctx)

    lp = client.get("/api/lookup/111?remote=0").get_json()["product"]["last_print"]
    assert lp["reason"] == "price_change"
    assert lp["price_at_print"] == pytest.approx(6.49)
    assert lp["matches_current_price"] is True
    assert lp["old_price"] == pytest.approx(5.99)
    assert lp["new_price"] == pytest.approx(6.49)
    assert lp["status"] == "queued"
    assert lp["copies"] == 1
    assert lp["at"]
    assert lp["price_inferred"] is False
    assert h.id  # linked row exists


def test_lookup_last_print_price_moved_since(client, ctx):
    p = _product(price=7.49)
    _job(p, reason=None, price=6.49)
    lp = client.get("/api/lookup/111?remote=0").get_json()["product"]["last_print"]
    assert lp["reason"] == "manual"
    assert lp["matches_current_price"] is False
    assert lp["current_price"] == pytest.approx(7.49)


def test_last_print_ignores_failed_jobs(ctx):
    p = _product()
    ok = _job(p, ago_s=100, price=5.99)
    _job(p, status="error", price=5.99)
    assert last_prints_for([p])[p.id]["job_id"] == ok.id


def test_last_print_is_per_product_for_shared_barcode(client, ctx):
    a = _product(upc="999", name="Ghee 500g", price=9.99)
    b = _product(upc="999", name="Ghee 500g B1G1", price=9.99)
    _job(b, price=9.99)
    body = client.get("/api/lookup/999?remote=0").get_json()
    by_id = {d["id"]: d for d in body["products"]}
    assert by_id[a.id]["last_print"] is None
    assert by_id[b.id]["last_print"]["reason"] == "manual"


def test_search_results_carry_last_print(client, ctx):
    p = _product(name="Kashmiri Chili Powder")
    _job(p, price=5.99)
    body = client.get("/api/search?q=Kashmiri Chili").get_json()
    assert body["count"] >= 1
    assert body["results"][0]["product"]["last_print"]["reason"] == "manual"


def test_last_print_endpoint(client, ctx):
    p = _product(price=6.49)
    _change(p, 5.99, 6.49, ago_s=30)
    auto_print_price_changes(ctx)

    body = client.get(f"/api/products/{p.id}/last-print").get_json()
    assert body["printed_at_current_price"] is True
    assert body["last_print"]["reason"] == "price_change"
    assert body["current_price"] == pytest.approx(6.49)
    assert len(body["recent"]) == 1
    assert body["recent"][0]["old_price"] == pytest.approx(5.99)

    assert client.get("/api/products/999999/last-print").status_code == 404


def test_last_print_endpoint_never_printed(client, ctx):
    p = _product()
    body = client.get(f"/api/products/{p.id}/last-print").get_json()
    assert body["last_print"] is None
    assert body["printed_at_current_price"] is False
    assert body["recent"] == []


# ── /api/print ────────────────────────────────────────────────────────────────
def test_print_records_price_and_returns_previous_print(client, ctx):
    p = _product(price=6.49)
    _change(p, 5.99, 6.49)
    auto_print_price_changes(ctx)

    r = client.post("/api/print", json={"product_id": p.id})
    assert r.status_code == 202
    body = r.get_json()
    assert body["previous_print"]["reason"] == "price_change"
    assert body["previous_print"]["matches_current_price"] is True
    job = db.session.get(PrintJob, body["job_id"])
    assert job.price_at_print == pytest.approx(6.49)
    assert job.reason is None


def test_print_previous_print_none_first_time(client, ctx):
    p = _product()
    body = client.post("/api/print", json={"product_id": p.id}).get_json()
    assert body["previous_print"] is None


# ── /api/print-history ────────────────────────────────────────────────────────
def test_print_history_price_change_rows_and_counts(client, ctx):
    p = _product(price=6.49)
    _change(p, 5.99, 6.49, ago_s=30)
    auto_print_price_changes(ctx)
    q = _product(upc="222", name="Turmeric", price=3.0)
    _job(q, price=3.0)
    _job(q, price=3.0)

    body = client.get("/api/print-history").get_json()
    assert body["counts"] == {"price_change": 1, "manual": 2, "total": 3}
    pc = [j for j in body["jobs"] if j["reason"] == "price_change"][0]
    assert pc["old_price"] == pytest.approx(5.99)
    assert pc["new_price"] == pytest.approx(6.49)
    assert pc["price_at_print"] == pytest.approx(6.49)
    assert pc["name"] == "Cumin 200g"
    manual = [j for j in body["jobs"] if j["reason"] is None][0]
    assert manual["old_price"] is None and manual["new_price"] is None
    assert manual["price_at_print"] == pytest.approx(3.0)

    only_pc = client.get("/api/print-history?reason=price_change").get_json()
    assert only_pc["count"] == 1
    assert only_pc["counts"]["total"] == 3  # counts ignore the reason filter

    by_product = client.get(f"/api/print-history?product_id={q.id}").get_json()
    assert by_product["count"] == 2
    assert by_product["counts"] == {"price_change": 0, "manual": 2, "total": 2}


def test_print_history_legacy_rows_get_inferred_values(client, ctx):
    p = _product(price=6.49)
    h = _change(p, 5.99, 6.49, ago_s=3600)
    # Legacy auto-print job: no price_at_print / price_history_id; the old
    # code marked the row reviewed right after queueing it.
    job = _job(p, reason="price_change", ago_s=3000)
    h.reviewed_at = job.created_at + timedelta(milliseconds=5)
    h.label_printed = True
    db.session.commit()
    # Legacy manual print from before the change.
    _job(p, reason=None, ago_s=7200)

    rows = client.get("/api/print-history").get_json()["jobs"]
    pc = [r for r in rows if r["reason"] == "price_change"][0]
    assert pc["old_price"] == pytest.approx(5.99)
    assert pc["new_price"] == pytest.approx(6.49)
    assert pc["price_at_print"] == pytest.approx(6.49)
    assert pc["price_inferred"] is True
    manual = [r for r in rows if r["reason"] is None][0]
    assert manual["price_at_print"] == pytest.approx(5.99)  # price before the change
    assert manual["price_inferred"] is True


def test_print_history_date_range_counts(client, ctx):
    p = _product()
    _job(p, price=5.99)
    old = _job(p, price=5.99)
    old.created_at = utcnow() - timedelta(days=10)
    db.session.commit()
    today = (utcnow() - timedelta(hours=5)).date().isoformat()
    body = client.get(f"/api/print-history?start={today}&end={today}").get_json()
    assert body["counts"]["total"] <= 1


