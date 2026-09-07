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
"""

from __future__ import annotations

import logging

from flask import Flask

from ..extensions import db
from ..models import PriceHistory, Product, utcnow
from .print_queue import QueueFull

logger = logging.getLogger("spicetown.autoprint")


def auto_print_price_changes(app: Flask) -> dict:
    """Print a fresh label for every unreviewed price change, then mark it
    reviewed. Must run inside `app.app_context()`. Returns
    {"printed": n, "failed": n, "skipped": bool} for the caller to log.
    """
    if not app.config.get("AUTO_PRINT_PRICE_CHANGES", True):
        return {"printed": 0, "failed": 0, "skipped": True}

    # Imported here (not at module scope) to avoid a routes<->services import
    # cycle: routes.api already imports from this package's siblings.
    from ..routes.api import _enqueue_for_product

    rows = (
        db.session.query(PriceHistory)
        .filter(PriceHistory.reviewed_at.is_(None))
        .order_by(PriceHistory.id)
        .all()
    )

    printed = 0
    failed = 0
    for row in rows:
        product = db.session.get(Product, row.product_id) if row.product_id else None
        if product is None:
            continue
        try:
            _enqueue_for_product(product, variant=product.label_variant(), reason="price_change")
        except (RuntimeError, QueueFull) as exc:
            failed += 1
            logger.warning(
                "auto-print skipped for product %s (upc=%s): %s",
                product.id, product.upc, exc,
            )
            continue

        row.reviewed_at = utcnow()
        row.label_printed = True
        db.session.commit()
        printed += 1

    if printed or failed:
        logger.info("auto-print price changes: printed=%d failed=%d", printed, failed)
    return {"printed": printed, "failed": failed, "skipped": False}
