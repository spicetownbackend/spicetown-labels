"""
app/providers/base.py — DataProvider adapter interface.

The rest of the system depends only on this abstraction, never on a concrete
source. Switching from the local file to the Toast POS is a *config change*
(STL_DATA_PROVIDER=file|toast); no calling code changes.

Providers return normalized `ProductRecord` DTOs (plain dataclasses), keeping
them decoupled from SQLAlchemy. The bulk loader maps DTOs -> ORM rows.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Iterable, Iterator


@dataclass(slots=True)
class ProductRecord:
    """Source-agnostic product DTO produced by every provider."""

    upc: str
    name: str
    price: float
    sku: str | None = None
    department: str | None = None
    size: str | None = None
    unit: str | None = None
    sale_price: float | None = None
    on_sale: bool = False
    clearance: bool = False
    extra: dict = field(default_factory=dict)

    def normalized_upc(self) -> str:
        """Trim whitespace and strip a trailing '.0' some spreadsheets add."""
        upc = (self.upc or "").strip()
        if upc.endswith(".0") and upc[:-2].isdigit():
            upc = upc[:-2]
        return upc

    def __post_init__(self) -> None:
        # Defensive coercion: sources are messy (CSV strings, JSON nulls, ...).
        self.upc = self.normalized_upc()
        self.name = (self.name or "").strip()
        try:
            self.price = float(self.price)
        except (TypeError, ValueError):
            self.price = 0.0
        if self.sale_price is not None:
            try:
                self.sale_price = float(self.sale_price)
            except (TypeError, ValueError):
                self.sale_price = None


class DataProvider(abc.ABC):
    """Abstract base every concrete data source implements."""

    #: Short identifier persisted on rows (Product.source) and used in logs.
    name: str = "base"

    @abc.abstractmethod
    def health_check(self) -> bool:
        """Return True if the source is reachable/usable right now."""
        raise NotImplementedError

    @abc.abstractmethod
    def fetch_all(self) -> Iterator[ProductRecord]:
        """Yield every product for the startup load / nightly bulk refresh.

        Implementations SHOULD stream (yield) rather than build a giant list so
        10,000+ products load with bounded memory.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def fetch_by_upc(self, upc: str) -> ProductRecord | None:
        """Fetch a single product by UPC (the rare cache-miss path).

        For external sources this MUST go through the token-bucket limiter and
        backoff-retry wrapper. Returns None if the UPC is unknown.
        """
        raise NotImplementedError

    # Optional capability — providers that can't batch fall back to per-UPC.
    def fetch_many(self, upcs: Iterable[str]) -> dict[str, ProductRecord]:
        """Fetch several UPCs at once. Default: loop fetch_by_upc."""
        out: dict[str, ProductRecord] = {}
        for upc in upcs:
            rec = self.fetch_by_upc(upc)
            if rec is not None:
                out[rec.upc] = rec
        return out

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} name={self.name!r}>"
