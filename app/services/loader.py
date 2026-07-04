"""
app/services/loader.py — Bulk loader + upsert engine.

Responsibilities (Stage 2):
  * Bulk-load every product from the active DataProvider into SQLite at startup
    and on the nightly refresh — NOT on per-scan demand.
  * Upsert by UPC (insert new / update existing), keeping SQLite authoritative.
  * Duplicate-UPC detection on cache writes (within the incoming feed).
  * Suspicious price-change flagging: a >20% delta logs a WARNING, appends a
    PriceHistory row, and sets `price_flagged=True` (which shortens the cache
    TTL to 1h so the volatile price is re-verified sooner).
  * Batched commits so a 10,000+ row load stays memory- and WAL-friendly.

A module-level lock guarantees only ONE bulk load runs at a time (the nightly
cron and a manual /api/refresh can never collide).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from ..extensions import db
from ..models import PriceHistory, Product, utcnow
from ..providers.base import DataProvider, ProductRecord
from .shorten import DEFAULT_MAX_CHARS, shorten_name

logger = logging.getLogger("spicetown.loader")

# Only one bulk load may run process-wide at any moment.
_refresh_lock = threading.Lock()

# Treat sub-cent differences as "no change" to avoid float-noise churn.
_MONEY_EPSILON = 0.005


@dataclass
class LoadStats:
    """Outcome of a bulk load / upsert pass."""

    total_seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    flagged: int = 0
    skipped_invalid: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    duplicate_upcs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Cap the duplicate list in the API payload so it can't balloon.
        d["duplicate_upcs"] = self.duplicate_upcs[:50]
        return d


def _money_changed(old: float | None, new: float | None) -> bool:
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return abs(float(old) - float(new)) >= _MONEY_EPSILON


def _record_fingerprint(p: Product, rec: ProductRecord) -> bool:
    """Return True if any tracked field on `rec` differs from product `p`."""
    return (
        p.name != rec.name
        or _money_changed(p.price, rec.price)
        or _money_changed(p.sale_price, rec.sale_price)
        or bool(p.on_sale) != bool(rec.on_sale)
        or bool(p.clearance) != bool(rec.clearance)
        or (p.sku or None) != (rec.sku or None)
        or (p.department or None) != (rec.department or None)
        or (p.size or None) != (rec.size or None)
        or (p.unit or None) != (rec.unit or None)
    )


def upsert_record(
    rec: ProductRecord,
    *,
    source: str,
    price_change_threshold: float = 0.20,
    shorten_max_chars: int = DEFAULT_MAX_CHARS,
    existing: Product | None = None,
    stats: LoadStats | None = None,
) -> Product:
    """Insert or update a single product from a ProductRecord.

    Parameters
    ----------
    rec:
        Normalized product DTO from a provider.
    source:
        Provider name persisted on the row ("file" / "toast").
    price_change_threshold:
        Fractional delta above which a price change is flagged (0.20 = 20%).
    shorten_max_chars:
        Character budget for the AI-shortened `short_name` (label fit).
    existing:
        Pre-fetched Product to update (bulk path passes this to avoid N queries).
        When None, the function queries by UPC itself (single-lookup path).
    stats:
        Optional LoadStats to tally into.

    The caller is responsible for committing the session (callers batch commits).
    """
    if existing is None:
        existing = db.session.query(Product).filter_by(upc=rec.upc).one_or_none()

    now = utcnow()
    short = shorten_name(rec.name, max_chars=shorten_max_chars)

    # ── INSERT ────────────────────────────────────────────────────────────────
    if existing is None:
        product = Product(
            upc=rec.upc,
            name=rec.name,
            short_name=short,
            sku=rec.sku,
            department=rec.department,
            size=rec.size,
            unit=rec.unit,
            price=rec.price,
            sale_price=rec.sale_price,
            on_sale=rec.on_sale,
            clearance=rec.clearance,
            price_flagged=False,
            source=source,
            synced_at=now,
        )
        db.session.add(product)
        if stats is not None:
            stats.inserted += 1
        return product

    # ── UPDATE ──────────────────────────────────────────────────────────────
    old_price = existing.price
    price_moved = _money_changed(old_price, rec.price)

    # Compute the relative delta for flagging (guard divide-by-zero / $0 base).
    flagged_now = False
    if price_moved and old_price and old_price > 0:
        delta_ratio = abs(rec.price - old_price) / old_price
        if delta_ratio > price_change_threshold:
            flagged_now = True
    else:
        delta_ratio = 0.0

    anything_changed = _record_fingerprint(existing, rec)

    # Apply incoming field values.
    existing.name = rec.name
    existing.short_name = short
    existing.sku = rec.sku
    existing.department = rec.department
    existing.size = rec.size
    existing.unit = rec.unit
    existing.price = rec.price
    existing.sale_price = rec.sale_price
    existing.on_sale = rec.on_sale
    existing.clearance = rec.clearance
    existing.source = source
    existing.synced_at = now  # always refresh TTL window on a successful sync

    if price_moved:
        # Append to the append-only price log for audit + future analytics.
        db.session.add(
            PriceHistory(
                product=existing,
                old_price=old_price,
                new_price=rec.price,
                delta_ratio=delta_ratio,
                flagged=flagged_now,
                source=source,
            )
        )
        if flagged_now:
            existing.price_flagged = True
            logger.warning(
                "SUSPICIOUS price change upc=%s '%s' %.2f -> %.2f (%.0f%% > %.0f%%)",
                existing.upc,
                existing.name,
                old_price,
                rec.price,
                delta_ratio * 100,
                price_change_threshold * 100,
            )
            if stats is not None:
                stats.flagged += 1
        else:
            # A normal-sized change: the price is considered stable again.
            existing.price_flagged = False
    else:
        # Price unchanged → if it had been flagged, it has now stabilized.
        if existing.price_flagged:
            existing.price_flagged = False

    if stats is not None:
        if anything_changed:
            stats.updated += 1
        else:
            stats.unchanged += 1

    return existing


def bulk_load(
    provider: DataProvider,
    *,
    batch_size: int = 500,
    price_change_threshold: float = 0.20,
    shorten_max_chars: int = DEFAULT_MAX_CHARS,
    source: str | None = None,
) -> LoadStats:
    """Load every product from `provider` into SQLite (startup / nightly).

    Pre-loads existing rows into an in-memory {upc: Product} map so each record
    is resolved without an extra query (one SELECT instead of N). Commits every
    `batch_size` records to bound the transaction size for 10,000+ catalogs.

    Returns a LoadStats summary. Raises if another load is already in progress
    is avoided by skipping (see `bulk_load_guarded`).
    """
    stats = LoadStats()
    started = time.monotonic()
    source = source or provider.name

    # Pre-load existing products keyed by UPC (single query).
    existing_map: dict[str, Product] = {
        p.upc: p for p in db.session.query(Product).all()
    }
    logger.info(
        "bulk_load start: provider=%s existing_rows=%d batch_size=%d",
        provider.name,
        len(existing_map),
        batch_size,
    )

    seen: set[str] = set()
    pending = 0

    try:
        for rec in provider.fetch_all():
            stats.total_seen += 1

            # Invalid / incomplete record.
            if not rec.upc or not rec.name:
                stats.skipped_invalid += 1
                continue

            # Duplicate UPC within the incoming feed → data error.
            if rec.upc in seen:
                stats.duplicates += 1
                if len(stats.duplicate_upcs) < 1000:
                    stats.duplicate_upcs.append(rec.upc)
                logger.warning(
                    "duplicate UPC in feed: %s ('%s') — keeping first, skipping dup",
                    rec.upc,
                    rec.name,
                )
                continue
            seen.add(rec.upc)

            try:
                upsert_record(
                    rec,
                    source=source,
                    price_change_threshold=price_change_threshold,
                    shorten_max_chars=shorten_max_chars,
                    existing=existing_map.get(rec.upc),
                    stats=stats,
                )
            except Exception:  # pragma: no cover - per-record resilience
                stats.errors += 1
                logger.exception("upsert failed for upc=%s", rec.upc)
                continue

            pending += 1
            if pending >= batch_size:
                db.session.commit()
                pending = 0

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("bulk_load aborted; rolled back uncommitted batch")
        raise
    finally:
        stats.duration_seconds = round(time.monotonic() - started, 3)

    logger.info(
        "bulk_load done: seen=%d inserted=%d updated=%d unchanged=%d "
        "flagged=%d duplicates=%d invalid=%d errors=%d in %.3fs",
        stats.total_seen,
        stats.inserted,
        stats.updated,
        stats.unchanged,
        stats.flagged,
        stats.duplicates,
        stats.skipped_invalid,
        stats.errors,
        stats.duration_seconds,
    )
    return stats


class RefreshInProgress(Exception):
    """Raised by bulk_load_guarded when a load is already running."""


def bulk_load_guarded(provider: DataProvider, **kwargs) -> LoadStats:
    """Run bulk_load under the process-wide lock (non-blocking).

    Raises RefreshInProgress immediately if another load holds the lock, so the
    nightly cron and a manual /api/refresh never run concurrently.
    """
    acquired = _refresh_lock.acquire(blocking=False)
    if not acquired:
        raise RefreshInProgress("a bulk load/refresh is already in progress")
    try:
        return bulk_load(provider, **kwargs)
    finally:
        _refresh_lock.release()


def is_refresh_running() -> bool:
    """Best-effort check used by health/stats endpoints."""
    if _refresh_lock.acquire(blocking=False):
        _refresh_lock.release()
        return False
    return True
