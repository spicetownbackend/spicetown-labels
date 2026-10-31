"""
app/services/auto_print.py — Automatic label printing on price change.

Stage 6 built a manual review panel: a sync flags a price change as an
unreviewed PriceHistory row, and a human opens the Price Changes panel to
print (or dismiss) each one. This module is the hands-off counterpart: after
any bulk sync (nightly, the 30-minute periodic live sync, or a manual
refresh), print a fresh label for every real price change and mark it
reviewed automatically — reusing the exact same enqueue path
(routes.api._enqueue_for_product) and print-history bookkeeping the manual
panel already uses, so an auto-printed job is indistinguishable in
/api/print-history from one a human triggered (reason="price_change").

Deliberately NOT hooked into the per-scan cache-miss refresh
(CacheService._refresh_one) — that path fires while a staff member is
already standing at the scanner about to print that exact item, so
auto-printing there would just double-print. This only fires for proactive
bulk syncs, which is where an unattended price change is actually caught.

Controlled by config.AUTO_PRINT_PRICE_CHANGES (default True).

The same pass then prints labels for items a sync added (LabelEvent
"new_item") or found under a new barcode ("barcode_change"), when the
scanner page's "Automatic labels" switches allow it (Oct 31 2026). An item
with no real barcode prints without one. A new item at $0 waits for its
price (its price change would print it again otherwise); an item already
printed since the event isn't printed twice; at most
AUTO_PRINT_EVENTS_PER_PASS labels per pass, the rest after the next sync;
events older than 14 days, or arriving while a switch is off, are dropped.

Duplicate-label guards (the "label sync issue": a price change came out of
the printer twice, or once automatically and again by hand):
  * ONE label per product per pass, however many unreviewed PriceHistory
    rows it has (two rows for the same change - e.g. a live Toast row plus a
    price_changes.json import - used to print two labels; all of them are
    marked reviewed by the single label, which shows the current price).
  * Rows are CLAIMED with a conditional UPDATE (reviewed_at IS NULL) before
    the label is queued, and the whole pass runs under a process lock, so a
    scheduled sync and a /api/refresh finishing at the same moment can never
    both print the same change. A claim that fails to queue is released
    again so the next sync retries it.
  * If a label for the product was already queued (by anyone, not failed)
    after its newest price change, that label already shows the new price:
    the change is marked handled without printing another.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from flask import Flask

from ..extensions import db
from datetime import timedelta

from ..models import LabelEvent, PriceHistory, PrintJob, Product, utcnow
from .print_queue import QueueFull

logger = logging.getLogger("spicetown.autoprint")

# One auto-print pass at a time per process (scheduler thread vs. a manual
# /api/refresh request thread). A pass that finds the lock taken skips: the
# running pass is already printing every unreviewed change.
_pass_lock = threading.Lock()


def _claim(row_ids: list[int], now) -> int:
    """Atomically mark these rows reviewed if nobody else has; returns how
    many this call claimed (0 = another pass/print got there first)."""
    n = (
        db.session.query(PriceHistory)
        .filter(PriceHistory.id.in_(row_ids), PriceHistory.reviewed_at.is_(None))
        .update({"reviewed_at": now}, synchronize_session=False)
    )
    db.session.commit()
    return n


def _release(row_ids: list[int], now) -> None:
    """Undo a claim whose label could not be queued (next sync retries)."""
    (
        db.session.query(PriceHistory)
        .filter(PriceHistory.id.in_(row_ids), PriceHistory.reviewed_at == now)
        .update({"reviewed_at": None}, synchronize_session=False)
    )
    db.session.commit()


def _already_printed_since(product_id: int, since) -> PrintJob | None:
    """A non-failed label for this product queued at/after `since` - it was
    rendered from the current (post-change) price, so it covers the change."""
    return (
        db.session.query(PrintJob)
        .filter(
            PrintJob.product_id == product_id,
            PrintJob.status != "error",
            PrintJob.created_at >= since,
        )
        .order_by(PrintJob.id.desc())
        .first()
    )


def auto_print_price_changes(app: Flask) -> dict:
    """Print ONE fresh label per product with unreviewed price changes, then
    mark those changes reviewed. Must run inside `app.app_context()`.
    Returns {"printed": n, "failed": n, "skipped": bool,
    "already_printed": n, "merged": n} for the caller to log (`merged` =
    extra rows for a product folded into its single label)."""
    if not _pass_lock.acquire(blocking=False):
        logger.info("auto-print skipped: another pass is already running")
        return {"printed": 0, "failed": 0, "skipped": True}
    try:
        if app.config.get("AUTO_PRINT_PRICE_CHANGES", True):
            out = _run_pass()
        else:
            out = {"printed": 0, "failed": 0, "skipped": True}
        out["new_items"] = _run_events_pass(app)
        return out
    finally:
        _pass_lock.release()


# Every label field except the barcode: an item without a real barcode.
NO_BARCODE_FIELDS = "department,name,size,price"
EVENT_MAX_AGE = timedelta(days=14)


def _close_event(ev: LabelEvent, outcome: str, job_id: int | None = None) -> None:
    ev.handled_at = utcnow()
    ev.outcome = outcome
    ev.job_id = job_id
    db.session.commit()


def _run_events_pass(app: Flask) -> dict:
    """Print one label per open LabelEvent the switches allow."""
    from ..routes.api import _enqueue_for_product
    from .label_settings import auto_print_switches

    switches = auto_print_switches()
    cap = int(app.config.get("AUTO_PRINT_EVENTS_PER_PASS", 60))
    events = (
        db.session.query(LabelEvent)
        .filter(LabelEvent.handled_at.is_(None))
        .order_by(LabelEvent.id)
        .all()
    )
    counts = {"printed": 0, "failed": 0, "already_printed": 0, "waiting": 0, "dropped": 0}
    now = utcnow()
    for ev in events:
        product = db.session.get(Product, ev.product_id)
        allowed = switches["new_items"] if ev.kind == "new_item" else switches["barcode_changes"]
        if product is None or not allowed or now - ev.created_at > EVENT_MAX_AGE:
            _close_event(ev, "gone" if product is None else ("switched_off" if not allowed else "too_old"))
            counts["dropped"] += 1
            continue
        if not (product.effective_price() or 0) > 0:
            counts["waiting"] += 1  # no price yet: print once Toast has one
            continue
        prior = _already_printed_since(product.id, ev.created_at)
        if prior is not None:
            _close_event(ev, "already_printed", prior.id)
            counts["already_printed"] += 1
            continue
        if counts["printed"] >= cap:
            counts["waiting"] += 1
            continue
        claimed = (
            db.session.query(LabelEvent)
            .filter(LabelEvent.id == ev.id, LabelEvent.handled_at.is_(None))
            .update({"handled_at": now}, synchronize_session=False)
        )
        db.session.commit()
        if not claimed:
            continue
        try:
            res = _enqueue_for_product(
                product,
                variant=product.label_variant(),
                fields_csv=None if (product.sku or "").strip() else NO_BARCODE_FIELDS,
                reason=ev.kind,
            )
        except (RuntimeError, QueueFull) as exc:
            db.session.query(LabelEvent).filter(LabelEvent.id == ev.id).update(
                {"handled_at": None}, synchronize_session=False
            )
            db.session.commit()
            counts["failed"] += 1
            logger.warning("auto-print %s skipped for product %s: %s", ev.kind, product.id, exc)
            continue
        db.session.refresh(ev)
        _close_event(ev, "printed", getattr(res, "job_id", None))
        counts["printed"] += 1
    if counts["printed"] or counts["failed"]:
        logger.info("auto-print new items / barcode changes: %s", counts)
    return counts


def _run_pass() -> dict:
    # Imported here (not at module scope) to avoid a routes<->services import
    # cycle: routes.api already imports from this package's siblings.
    from ..routes.api import _enqueue_for_product

    rows = (
        db.session.query(PriceHistory)
        .filter(PriceHistory.reviewed_at.is_(None))
        .order_by(PriceHistory.id)
        .all()
    )
    by_product: "OrderedDict[int, list[PriceHistory]]" = OrderedDict()
    for row in rows:
        by_product.setdefault(row.product_id, []).append(row)

    printed = failed = already = merged = 0
    for product_id, group in by_product.items():
        product = db.session.get(Product, product_id) if product_id else None
        if product is None:
            continue
        ids = [r.id for r in group]
        newest = max(group, key=lambda r: (r.recorded_at, r.id))
        now = utcnow()
        if _claim(ids, now) == 0:
            continue  # handled by a concurrent print / pass

        prior = _already_printed_since(product.id, newest.recorded_at)
        if prior is not None:
            for r in group:
                db.session.refresh(r)
                if r.reviewed_at == now:
                    r.label_printed = True
            db.session.commit()
            already += 1
            logger.info(
                "auto-print: product %s (upc=%s) already has label job %s after its "
                "price change; not printing again",
                product.id, product.upc, prior.id,
            )
            continue

        try:
            _enqueue_for_product(
                product,
                variant=product.label_variant(),
                reason="price_change",
                price_history_id=newest.id,
            )
        except (RuntimeError, QueueFull) as exc:
            _release(ids, now)
            failed += 1
            logger.warning(
                "auto-print skipped for product %s (upc=%s): %s",
                product.id, product.upc, exc,
            )
            continue

        for r in group:
            db.session.refresh(r)
            if r.reviewed_at == now:
                r.label_printed = True
        db.session.commit()
        printed += 1
        merged += len(group) - 1

    if printed or failed or already:
        logger.info(
            "auto-print price changes: printed=%d failed=%d already_printed=%d merged=%d",
            printed, failed, already, merged,
        )
    return {
        "printed": printed,
        "failed": failed,
        "skipped": False,
        "already_printed": already,
        "merged": merged,
    }
