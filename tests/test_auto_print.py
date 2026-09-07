"""
tests/test_auto_print.py — automatic label printing on price change.

Covers:
  * auto_print_price_changes() enqueues a price_change-tagged print job for
    every unreviewed PriceHistory row and marks it reviewed.
  * it's a clean no-op when there's nothing unreviewed.
  * AUTO_PRINT_PRICE_CHANGES=False disables it entirely (nothing touched).
  * a row with no resolvable product is skipped, not crashed on.
  * the print worker being down leaves the row unreviewed (so the next sync
    retries it) instead of silently marking it handled.
  * scheduler wiring: both the nightly and periodic jobs get registered, and
    STL_REFRESH_INTERVAL_MINUTES=0 disables the periodic job.
"""

from __future__ import annotations

import time

import pytest

from app import create_app
from app.extensions import db, scheduler
from app.models import PriceHistory, PrintJob, Product
from app.services.auto_print import auto_print_price_changes
from app.services.print_queue import PrintQueue
from app.services.printer import NullTransport
from app.services.scheduler import NIGHTLY_JOB_ID, PERIODIC_JOB_ID, init_scheduler
from config import TestingConfig


@pytest.fixture()
def app():
    app = create_app(config_object=TestingConfig, start_background=False)
    yield app


def _seed_with_change(app, upc="ap1", name="Cumin", old=4.99, new=5.99, reviewed=False):
    with app.app_context():
        p = Product(upc=upc, name=name, price=new)
        db.session.add(p)
        db.session.commit()
        hist = PriceHistory(product_id=p.id, old_price=old, new_price=new)
        db.session.add(hist)
        db.session.commit()
        return p.id, hist.id


def _wait_status(app, job_id, target=("done", "error"), timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with app.app_context():
            job = db.session.get(PrintJob, job_id)
            if job and job.status in target:
                return job.status
        time.sleep(0.02)
    return "timeout"


# ── core behavior ───────────────────────────────────────────────────────────
def test_auto_print_enqueues_and_marks_reviewed(app):
    _, hist_id = _seed_with_change(app)
    pq = PrintQueue(app, NullTransport(), app.extensions["label_spec"], maxsize=10)
    app.extensions["print_queue"] = pq
    pq.start()
    try:
        with app.app_context():
            result = auto_print_price_changes(app)
        assert result == {"printed": 1, "failed": 0, "skipped": False}

        with app.app_context():
            row = db.session.get(PriceHistory, hist_id)
            assert row.reviewed_at is not None
            assert row.label_printed is True
            job = (
                db.session.query(PrintJob)
                .filter_by(reason="price_change")
                .order_by(PrintJob.id.desc())
                .first()
            )
        assert job is not None
        assert _wait_status(app, job.id) == "done"
    finally:
        pq.stop()


def test_auto_print_noop_when_nothing_unreviewed(app):
    with app.app_context():
        result = auto_print_price_changes(app)
    assert result == {"printed": 0, "failed": 0, "skipped": False}


def test_auto_print_disabled_via_config(app):
    app.config["AUTO_PRINT_PRICE_CHANGES"] = False
    _, hist_id = _seed_with_change(app)
    with app.app_context():
        result = auto_print_price_changes(app)
    assert result == {"printed": 0, "failed": 0, "skipped": True}
    with app.app_context():
        row = db.session.get(PriceHistory, hist_id)
        assert row.reviewed_at is None  # untouched


def test_auto_print_skips_row_with_no_product(app):
    # PriceHistory.product_id has an ON DELETE CASCADE foreign key (and this
    # app runs with SQLite FK enforcement on), so a real orphaned row can't
    # be produced by deleting the product - the FK removes the history row
    # right along with it. Simulate the defensive branch directly instead:
    # a row whose product_id points at an id that was never a real product.
    with app.app_context():
        db.session.execute(db.text("PRAGMA foreign_keys=OFF"))
        hist = PriceHistory(product_id=999999, old_price=1.0, new_price=2.0)
        db.session.add(hist)
        db.session.commit()
        hist_id = hist.id
    with app.app_context():
        result = auto_print_price_changes(app)
    assert result == {"printed": 0, "failed": 0, "skipped": False}
    with app.app_context():
        row = db.session.get(PriceHistory, hist_id)
        assert row is not None
        assert row.reviewed_at is None  # left for a human, not silently dropped


def test_auto_print_worker_down_leaves_row_unreviewed_for_retry(app):
    # print_queue extension exists (from create_app) but its worker thread was
    # never started (start_background=False) -> RuntimeError from
    # _enqueue_for_product, which auto_print_price_changes must swallow into
    # a failed count rather than crash the sync, and must NOT mark reviewed
    # (so the next periodic sync gets another chance).
    _, hist_id = _seed_with_change(app)
    with app.app_context():
        result = auto_print_price_changes(app)
    assert result == {"printed": 0, "failed": 1, "skipped": False}
    with app.app_context():
        row = db.session.get(PriceHistory, hist_id)
        assert row.reviewed_at is None
        assert row.label_printed is False


def test_auto_print_multiple_changes_all_handled(app):
    _seed_with_change(app, upc="m1", name="A", new=1.5)
    _seed_with_change(app, upc="m2", name="B", new=2.5)
    _seed_with_change(app, upc="m3", name="C", new=3.5)
    pq = PrintQueue(app, NullTransport(), app.extensions["label_spec"], maxsize=10)
    app.extensions["print_queue"] = pq
    pq.start()
    try:
        with app.app_context():
            result = auto_print_price_changes(app)
        assert result == {"printed": 3, "failed": 0, "skipped": False}
        with app.app_context():
            still_unreviewed = (
                db.session.query(PriceHistory)
                .filter(PriceHistory.reviewed_at.is_(None))
                .count()
            )
        assert still_unreviewed == 0
    finally:
        pq.stop()


# ── scheduler wiring ──────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_scheduler():
    """The scheduler singleton (app/extensions.py) is process-wide; make sure
    a previous test's jobs don't leak into the next one's assertions."""
    yield
    for job_id in (NIGHTLY_JOB_ID, PERIODIC_JOB_ID):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    if scheduler.running:
        scheduler.shutdown(wait=False)


def test_init_scheduler_registers_both_jobs(app):
    app.config["ENABLE_SCHEDULER"] = True
    app.config["REFRESH_INTERVAL_MINUTES"] = 30
    init_scheduler(app)
    ids = {j.id for j in scheduler.get_jobs()}
    assert NIGHTLY_JOB_ID in ids
    assert PERIODIC_JOB_ID in ids
    periodic = scheduler.get_job(PERIODIC_JOB_ID)
    assert periodic.trigger.interval.total_seconds() == 30 * 60


def test_init_scheduler_periodic_disabled_when_interval_zero(app):
    app.config["ENABLE_SCHEDULER"] = True
    app.config["REFRESH_INTERVAL_MINUTES"] = 0
    init_scheduler(app)
    ids = {j.id for j in scheduler.get_jobs()}
    assert NIGHTLY_JOB_ID in ids
    assert PERIODIC_JOB_ID not in ids


def test_init_scheduler_noop_when_scheduler_disabled(app):
    app.config["ENABLE_SCHEDULER"] = False
    init_scheduler(app)
    ids = {j.id for j in scheduler.get_jobs()}
    assert NIGHTLY_JOB_ID not in ids
    assert PERIODIC_JOB_ID not in ids
