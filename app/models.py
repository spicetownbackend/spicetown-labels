"""
app/models.py — SQLAlchemy schema.

The `Product` table is the primary lookup layer ("SQLite is authoritative").
Its `upc` column is UNIQUE and INDEXED so per-scan lookups are O(log n) and
remain fast at 10,000+ products.

Supporting tables:
- `PrintJob`    : audit trail / status for queued label print jobs (Stage 3+).
- `PriceHistory`: append-only log enabling >20% delta flagging (Stage 4+).

Stage 1 ships the full schema so later stages need no migrations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db


def utcnow() -> datetime:
    """Timezone-aware UTC now (stored as naive UTC in SQLite columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Product(db.Model):
    """A single sellable product, keyed by UPC.

    Cache freshness is tracked via `synced_at` + `price_flagged`:
      * standard rows expire after CACHE_TTL_STANDARD_SECONDS (24h)
      * flagged rows expire after CACHE_TTL_FLAGGED_SECONDS  (1h)
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # ── Identity ──────────────────────────────────────────────────────────────
    # UPC is the hot path: INDEXED (non-unique — the store sells variants that
    # share a barcode, e.g. "XYZ" and "XYZ B1G1"; a scan of a shared barcode
    # surfaces a picker in the UI). Stored as TEXT to preserve leading zeros
    # and support EAN-13 / Code128 payloads. Logical identity is (upc, name).
    upc: Mapped[str] = mapped_column(String(32), nullable=False)

    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Descriptive ──────────────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # AI auto-shortened name pre-computed to fit label width (Stage 4 fills it).
    short_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    department: Mapped[str | None] = mapped_column(String(80), nullable=True)
    size: Mapped[str | None] = mapped_column(String(40), nullable=True)  # "16 oz"
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "each"

    # ── Pricing ────────────────────────────────────────────────────────────────
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    on_sale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    clearance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── Cache / freshness bookkeeping ────────────────────────────────────────
    # True when a recent suspicious change was detected -> shorter TTL (1h).
    price_flagged: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)  # "file"/"toast"
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    # When the row was last reconciled with the data source.
    synced_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        # Explicit named index on the hot lookup column (non-unique: shared
        # barcodes are allowed; duplicates resolve via a UI picker).
        Index("ix_products_upc", "upc"),
        # Secondary indexes that help nightly refresh + sale/clearance queries.
        Index("ix_products_synced_at", "synced_at"),
        Index("ix_products_flags", "on_sale", "clearance"),
    )

    # ── Convenience helpers ───────────────────────────────────────────────────
    def is_fresh(self, standard_ttl: int, flagged_ttl: int) -> bool:
        """Return True if the cached row is still within its TTL window."""
        ttl = flagged_ttl if self.price_flagged else standard_ttl
        return utcnow() - self.synced_at < timedelta(seconds=ttl)

    def effective_price(self) -> float:
        """Price the customer pays (sale/clearance override base price)."""
        if (self.on_sale or self.clearance) and self.sale_price is not None:
            return self.sale_price
        return self.price

    def label_variant(self) -> str:
        """Which label template to render: 'clearance' | 'sale' | 'standard'."""
        if self.clearance:
            return "clearance"
        if self.on_sale:
            return "sale"
        return "standard"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "upc": self.upc,
            "sku": self.sku,
            "name": self.name,
            "short_name": self.short_name,
            "department": self.department,
            "size": self.size,
            "unit": self.unit,
            "price": self.price,
            "sale_price": self.sale_price,
            "on_sale": self.on_sale,
            "clearance": self.clearance,
            "effective_price": self.effective_price(),
            "label_variant": self.label_variant(),
            "price_flagged": self.price_flagged,
            "source": self.source,
            "synced_at": self.synced_at.isoformat() if self.synced_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Product upc={self.upc!r} name={self.name!r} ${self.price}>"


class PriceHistory(db.Model):
    """Append-only price log. Powers >20% suspicious-change flagging (Stage 4)."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    old_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_price: Mapped[float] = mapped_column(Float, nullable=False)
    delta_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    product: Mapped["Product"] = relationship(back_populates="price_history")

    __table_args__ = (Index("ix_price_history_product", "product_id"),)


class PrintJob(db.Model):
    """Tracks a queued/processed label print job (decoupled worker, Stage 3)."""

    __tablename__ = "print_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upc: Mapped[str] = mapped_column(String(32), nullable=False)
    # Specific product to print (required to disambiguate shared barcodes).
    # Nullable for legacy rows; resolution falls back to the first UPC match.
    product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variant: Mapped[str] = mapped_column(String(20), default="standard")
    copies: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Comma-separated label fields to include (see label.ALL_LABEL_FIELDS);
    # NULL means "all fields" (the default).
    fields: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # queued | printing | done | error
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    # Set when a remote print bridge claims the job (queued -> printing). Used
    # to re-queue jobs whose bridge died mid-print (BRIDGE_STALE_SECONDS).
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_print_jobs_status", "status"),
        UniqueConstraint("id", name="uq_print_jobs_id"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "upc": self.upc,
            "product_id": self.product_id,
            "variant": self.variant,
            "copies": self.copies,
            "fields": self.fields,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }
