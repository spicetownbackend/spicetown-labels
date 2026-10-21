"""
tests/test_label_dup_fix.py — the "label sync issue": price-change labels are
printed automatically, and the sync itself sometimes printed one change
twice (and staff then printed it again by hand).

Covers:
  * auto-print: ONE label per product per pass (several unreviewed rows for
    the same product are folded into it), linked to the newest change and
    stamped with the price on the label.
  * auto-print: a label already queued after the newest change (e.g. staff
    printed it by hand) means no second label - the change is just marked.
  * auto-print: concurrent passes can't double print (process lock + atomic
    claim); a claim that can't be queued is released for the next sync.
  * review-panel print: two selected changes for one product = one label.
  * price_changes.json import: skipped for a live provider, stale/duplicate
    diffs skipped for the file provider.
  * micro-migration adds the new columns to an existing (old-schema) DB.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from app import _import_price_change_diffs, create_app
from app.extensions import db
from app.models import PriceHistory, PrintJob, Product, utcnow
from app.services import auto_print as auto_print_mod
from app.services.auto_print import auto_print_price_changes
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


# ── auto-print ────────────────────────────────────────────────────────────────
def test_auto_print_one_label_per_product_for_duplicate_rows(ctx):
    p = _product(price=6.49)
    h1 = _change(p, 5.99, 6.49, ago_s=30, source="toast")
    h2 = _change(p, 5.99, 6.49, ago_s=10, source="toast_sync_file")
    other = _product(upc="222", name="Turmeric", price=3.0)
    _change(other, 2.5, 3.0)

    result = auto_print_price_changes(ctx)

    assert result["printed"] == 2
    assert result["merged"] == 1
    jobs = db.session.query(PrintJob).filter_by(product_id=p.id).all()
    assert len(jobs) == 1
    assert jobs[0].reason == "price_change"
    assert jobs[0].price_history_id == h2.id  # newest change
    assert jobs[0].price_at_print == pytest.approx(6.49)
    for h in (h1, h2):
        db.session.refresh(h)
        assert h.reviewed_at is not None and h.label_printed is True


def test_auto_print_price_at_print_is_sale_price_when_on_sale(ctx):
    p = _product(price=6.49, sale_price=4.99, on_sale=True)
    _change(p, 5.99, 6.49)
    auto_print_price_changes(ctx)
    job = db.session.query(PrintJob).one()
    assert job.price_at_print == pytest.approx(4.99)


def test_auto_print_skips_when_label_already_printed_after_change(ctx):
    p = _product(price=6.49)
    h = _change(p, 5.99, 6.49, ago_s=60)
    manual = _job(p, reason=None, ago_s=5, price=6.49)

    result = auto_print_price_changes(ctx)

    assert result["printed"] == 0
    assert result["already_printed"] == 1
    assert db.session.query(PrintJob).count() == 1  # only the manual one
    db.session.refresh(h)
    assert h.reviewed_at is not None and h.label_printed is True
    assert manual.id == db.session.query(PrintJob).one().id


def test_auto_print_prints_when_earlier_label_predates_change(ctx):
    p = _product(price=6.49)
    _job(p, reason=None, ago_s=600, price=5.99)  # old price label
    _change(p, 5.99, 6.49, ago_s=60)
    result = auto_print_price_changes(ctx)
    assert result["printed"] == 1


def test_auto_print_failed_earlier_job_does_not_count(ctx):
    p = _product(price=6.49)
    _change(p, 5.99, 6.49, ago_s=60)
    _job(p, reason=None, ago_s=5, status="error")
    assert auto_print_price_changes(ctx)["printed"] == 1


def test_auto_print_second_pass_prints_nothing(ctx):
    p = _product(price=6.49)
    _change(p, 5.99, 6.49)
    assert auto_print_price_changes(ctx)["printed"] == 1
    assert auto_print_price_changes(ctx)["printed"] == 0
    assert db.session.query(PrintJob).count() == 1


def test_auto_print_skips_while_another_pass_runs(ctx):
    p = _product(price=6.49)
    h = _change(p, 5.99, 6.49)
    assert auto_print_mod._pass_lock.acquire(blocking=False)
    try:
        result = auto_print_price_changes(ctx)
    finally:
        auto_print_mod._pass_lock.release()
    assert result["skipped"] is True
    assert db.session.query(PrintJob).count() == 0
    db.session.refresh(h)
    assert h.reviewed_at is None  # left for the running/next pass


def test_claim_is_atomic(ctx):
    p = _product(price=6.49)
    h = _change(p, 5.99, 6.49)
    now = utcnow()
    assert auto_print_mod._claim([h.id], now) == 1
    assert auto_print_mod._claim([h.id], utcnow()) == 0  # second claimer loses


def test_auto_print_releases_claim_when_queue_unavailable():
    class LocalNoWorker(TestingConfig):
        PRINT_MODE = "local"

    app = create_app(config_object=LocalNoWorker, start_background=False)
    with app.app_context():
        p = _product(price=6.49)
        h1 = _change(p, 5.99, 6.49, ago_s=5)
        h2 = _change(p, 5.99, 6.49)
        result = auto_print_price_changes(app)
        assert result["failed"] == 1 and result["printed"] == 0
        for h in (h1, h2):
            db.session.refresh(h)
            assert h.reviewed_at is None and h.label_printed is False


# ── review panel ─────────────────────────────────────────────────────────────
def test_review_panel_print_one_label_per_product(client, ctx):
    p = _product(price=6.49)
    h1 = _change(p, 5.99, 6.49, ago_s=30)
    h2 = _change(p, 5.99, 6.49, ago_s=10)
    body = client.post("/api/price-changes/print", json={"ids": [h1.id, h2.id]}).get_json()
    assert all(r["ok"] for r in body["results"])
    assert body["results"][1].get("merged") is True
    assert db.session.query(PrintJob).count() == 1
    job = db.session.query(PrintJob).one()
    assert job.price_history_id == h1.id
    assert job.price_at_print == pytest.approx(6.49)


# ── price_changes.json import ────────────────────────────────────────────────
def _write_diffs(tmp_path, diffs):
    (tmp_path / "products.csv").write_text("upc,name,price\n")
    (tmp_path / "price_changes.json").write_text(json.dumps(diffs))


def test_import_skipped_for_live_provider(app, tmp_path):
    app.config["PRODUCTS_FILE"] = str(tmp_path / "products.csv")
    app.config["DATA_PROVIDER"] = "toast"
    with app.app_context():
        p = _product(price=6.49)
        _change(p, 5.99, 6.49, source="toast")
    _write_diffs(tmp_path, [{"upc": "111", "name": "Cumin 200g", "old_price": 5.99, "new_price": 6.49}])
    _import_price_change_diffs(app)
    with app.app_context():
        assert db.session.query(PriceHistory).count() == 1


def test_import_file_provider_skips_duplicate_and_stale(app, tmp_path):
    app.config["PRODUCTS_FILE"] = str(tmp_path / "products.csv")
    app.config["DATA_PROVIDER"] = "file"
    with app.app_context():
        p = _product(price=6.49)
        _change(p, 5.99, 6.49, source="file")  # already logged by the loader
        _product(upc="222", name="Turmeric", price=3.25)  # moved on since
        _product(upc="333", name="Ajwain", price=2.0)  # genuinely new diff
    _write_diffs(
        tmp_path,
        [
            {"upc": "111", "name": "Cumin 200g", "old_price": 5.99, "new_price": 6.49},
            {"upc": "222", "name": "Turmeric", "old_price": 2.5, "new_price": 3.0},
            {"upc": "333", "name": "Ajwain", "old_price": 1.5, "new_price": 2.0},
        ],
    )
    _import_price_change_diffs(app)
    with app.app_context():
        rows = db.session.query(PriceHistory).filter_by(source="toast_sync_file").all()
        assert [r.new_price for r in rows] == [2.0]


# ── migration ────────────────────────────────────────────────────────────────
def test_migration_adds_columns_to_old_db(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE print_jobs (
            id INTEGER NOT NULL PRIMARY KEY, upc VARCHAR(32) NOT NULL,
            product_id INTEGER, variant VARCHAR(20) NOT NULL, copies INTEGER NOT NULL,
            fields VARCHAR(120), status VARCHAR(20) NOT NULL, error TEXT,
            reason VARCHAR(20), created_at DATETIME NOT NULL,
            claimed_at DATETIME, completed_at DATETIME);
        INSERT INTO print_jobs (upc, product_id, variant, copies, status, reason, created_at)
            VALUES ('111', 1, 'standard', 1, 'done', 'price_change', '2026-10-01 10:00:00');
        """
    )
    con.commit()
    con.close()

    class FileDb(RemoteConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{path}"

    create_app(config_object=FileDb, start_background=False)
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(print_jobs)")}
    idx = {r[1] for r in con.execute("PRAGMA index_list(print_jobs)")}
    old = con.execute("SELECT price_at_print, price_history_id, reason FROM print_jobs").fetchone()
    con.close()
    assert {"price_at_print", "price_history_id"} <= cols
    assert "ix_print_jobs_product" in idx
    assert old == (None, None, "price_change")  # existing rows untouched
