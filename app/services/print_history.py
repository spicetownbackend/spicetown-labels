"""
app/services/print_history.py — "When was this label last printed, and at
what price?"

Why this exists: every real price change is printed automatically by the
sync (services.auto_print, PrintJob.reason="price_change"). Staff scanning
the same item later had no way to see that, so they printed it again by
hand - two labels for one change. The apps now get, for each product they
look up, the last label that was printed for it (`last_print`), so they can
say "A label for this price was already printed today by the price update"
before staff print a duplicate.

Read-only helpers only; nothing here prints or writes.

Product identity: a product row is the logical (upc, name) pair the loader
upserts on, so `Product.id` is stable across syncs and print jobs pin it in
`PrintJob.product_id`. Legacy jobs without a product_id (queued before that
column existed) fall back to a UPC match.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Iterable

from sqlalchemy import and_, or_

from ..extensions import db
from ..models import PriceHistory, PrintJob, Product

_MONEY_EPSILON = 0.005

# Legacy price_change jobs (queued before PrintJob.price_history_id existed)
# are matched to the PriceHistory row the same sync marked reviewed: the
# auto-print / review panel set reviewed_at within a moment of queueing.
_LEGACY_MATCH_BEFORE = timedelta(seconds=5)
_LEGACY_MATCH_AFTER = timedelta(seconds=60)


def reason_key(reason: str | None) -> str:
    """Public reason name: "price_change" or "manual" (NULL/"" = manual)."""
    return "price_change" if reason == "price_change" else "manual"


def same_price(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < _MONEY_EPSILON


def _history_by_product(product_ids: Iterable[int]) -> dict[int, list[PriceHistory]]:
    ids = {i for i in product_ids if i is not None}
    if not ids:
        return {}
    out: dict[int, list[PriceHistory]] = defaultdict(list)
    rows = (
        db.session.query(PriceHistory)
        .filter(PriceHistory.product_id.in_(ids))
        .order_by(PriceHistory.recorded_at, PriceHistory.id)
        .all()
    )
    for r in rows:
        out[r.product_id].append(r)
    return out


def _match_legacy_change(job: PrintJob, history: list[PriceHistory]) -> PriceHistory | None:
    """PriceHistory row a legacy price_change job was printed for."""
    if job.created_at is None:
        return None
    best, best_gap = None, None
    for h in history:
        if not h.label_printed or h.reviewed_at is None:
            continue
        if not (
            job.created_at - _LEGACY_MATCH_BEFORE
            <= h.reviewed_at
            <= job.created_at + _LEGACY_MATCH_AFTER
        ):
            continue
        gap = abs((h.reviewed_at - job.created_at).total_seconds())
        if best_gap is None or gap < best_gap or (gap == best_gap and h.id > best.id):
            best, best_gap = h, gap
    return best


def _infer_price(job: PrintJob, product: Product | None, history: list[PriceHistory]) -> float | None:
    """Best guess of the price on a legacy label (no price_at_print stored):
    the base price in force when the job was queued, from the price log.
    Sale/clearance jobs use the product's sale price when it has one."""
    if product is None:
        return None
    if job.variant in ("sale", "clearance") and product.sale_price is not None:
        return product.sale_price
    when = job.created_at
    before = [h for h in history if when is not None and h.recorded_at <= when]
    if before:
        return before[-1].new_price
    after = [h for h in history if when is not None and h.recorded_at > when]
    if after and after[0].old_price is not None:
        return after[0].old_price
    return product.price


def describe_jobs(
    jobs: list[PrintJob],
    products: dict[int, Product] | None = None,
    history: dict[int, list[PriceHistory]] | None = None,
    product_for: dict[int, int] | None = None,
) -> dict[int, dict]:
    """Extra, display-ready fields for each job, keyed by job id:
    price_at_print (+ price_inferred), old_price/new_price for price-change
    labels (None for manual ones). Batched: two queries for any number of
    jobs. `product_for` maps job id -> product id for legacy jobs that carry
    no product_id of their own."""
    product_for = product_for or {}

    def pid_of(j: PrintJob) -> int | None:
        return product_for.get(j.id, j.product_id)

    product_ids = {pid_of(j) for j in jobs if pid_of(j) is not None}
    if products is None:
        products = {
            p.id: p
            for p in db.session.query(Product).filter(Product.id.in_(product_ids)).all()
        } if product_ids else {}
    if history is None:
        history = _history_by_product(product_ids)
    by_id = {h.id: h for rows in history.values() for h in rows}

    out: dict[int, dict] = {}
    for j in jobs:
        hist = history.get(pid_of(j), [])
        product = products.get(pid_of(j))
        change = None
        if j.reason == "price_change":
            if j.price_history_id is not None:
                change = by_id.get(j.price_history_id)
            if change is None:
                change = _match_legacy_change(j, hist)

        price = j.price_at_print
        inferred = False
        if price is None:
            inferred = True
            if change is not None and not (
                j.variant in ("sale", "clearance") and product is not None
                and product.sale_price is not None
            ):
                price = change.new_price
            else:
                price = _infer_price(j, product, hist)

        out[j.id] = {
            "price_at_print": price,
            "price_inferred": inferred if price is not None else False,
            "old_price": change.old_price if change is not None else None,
            "new_price": change.new_price if change is not None else None,
            "price_history_id": change.id if change is not None else j.price_history_id,
        }
    return out


def _last_print_dict(job: PrintJob, extra: dict, product: Product) -> dict:
    current = product.effective_price()
    return {
        "job_id": job.id,
        "at": job.created_at.isoformat() if job.created_at else None,
        "reason": reason_key(job.reason),
        "copies": job.copies,
        "status": job.status,
        "variant": job.variant,
        "price_at_print": extra["price_at_print"],
        "price_inferred": extra["price_inferred"],
        "old_price": extra["old_price"],
        "new_price": extra["new_price"],
        "current_price": current,
        "matches_current_price": same_price(extra["price_at_print"], current),
    }


def last_prints_for(products: Iterable[Product]) -> dict[int, dict | None]:
    """{product.id: last_print | None} for every given product.

    The last print is the newest job for the product that did not fail
    (status != "error" - a failed job produced no label; queued/printing
    count, since the label is on its way). Batched, no N+1.
    """
    products = [p for p in products if p is not None and p.id is not None]
    if not products:
        return {}
    pmap = {p.id: p for p in products}
    upc_to_ids: dict[str, list[int]] = defaultdict(list)
    for p in products:
        upc_to_ids[p.upc].append(p.id)

    jobs = (
        db.session.query(PrintJob)
        .filter(
            PrintJob.status != "error",
            or_(
                PrintJob.product_id.in_(pmap.keys()),
                and_(PrintJob.product_id.is_(None), PrintJob.upc.in_(upc_to_ids.keys())),
            ),
        )
        .order_by(PrintJob.id.desc())
        .all()
    )

    newest: dict[int, PrintJob] = {}
    for j in jobs:
        targets = [j.product_id] if j.product_id is not None else upc_to_ids.get(j.upc, [])
        for pid in targets:
            if pid in pmap and pid not in newest:
                newest[pid] = j

    history = _history_by_product(pmap.keys())
    out: dict[int, dict | None] = {pid: None for pid in pmap}
    for pid, job in newest.items():
        # A legacy UPC-matched job carries no product_id: describe it against
        # the product it was matched to.
        extra = describe_jobs([job], pmap, history, product_for={job.id: pid})[job.id]
        out[pid] = _last_print_dict(job, extra, pmap[pid])
    return out


def product_dict_with_last_print(product: Product, last: dict | None) -> dict:
    d = product.to_dict()
    d["last_print"] = last
    return d


def products_with_last_print(products: list[Product]) -> list[dict]:
    lp = last_prints_for(products)
    return [product_dict_with_last_print(p, lp.get(p.id)) for p in products]
