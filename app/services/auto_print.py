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
from ..models import PriceHistory, PrintJob, Product, utcnow
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
    if not app.config.get("AUTO_PRINT_PRICE_CHANGES", True):
        return {"printed": 0, "failed": 0, "skipped": True}

    if not _pass_lock.acquire(blocking=False):
        logger.info("auto-print skipped: another pass is already running")
        return {"printed": 0, "failed": 0, "skipped": True}
    try:
        return _run_pass()
    finally:
        _pass_lock.release()


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
