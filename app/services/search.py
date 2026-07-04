"""
app/services/search.py — Fuzzy search (rapidfuzz) for unrecognized barcodes.

Two complementary modes:

  * search_by_name(query)  — a cashier types part of a product name when a
    barcode is missing/damaged; returns the best-matching catalog products.

  * search_by_upc(scanned) — a scanner misreads a digit (transposition/OCR);
    fuzz-match the scanned digit string against known UPCs to suggest the most
    likely intended product. Surfaced on 404 lookups.

SQLite remains authoritative: this only ever ranks rows already in the cache.
rapidfuzz is C-fast, so for the rare unrecognized-barcode path we simply read
the current catalog from SQLite and rank in-process (measured well under the
scan-latency budget even at 10k+ products).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from ..extensions import db
from ..models import Product

logger = logging.getLogger("spicetown.search")


@dataclass
class SearchHit:
    product: Product
    score: float
    matched_on: str  # "name" | "upc"

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "matched_on": self.matched_on,
            "product": self.product.to_dict(),
        }


class SearchService:
    """Fuzzy lookup over the cached catalog."""

    def __init__(
        self,
        *,
        limit: int = 5,
        name_score_cutoff: float = 60.0,
        upc_score_cutoff: float = 70.0,
    ) -> None:
        self.limit = limit
        self.name_score_cutoff = name_score_cutoff
        self.upc_score_cutoff = upc_score_cutoff

    # ── data access ───────────────────────────────────────────────────────────
    def _name_index(self) -> list[tuple[int, str, str | None]]:
        """Lightweight (id, name, short_name) rows — avoids ORM hydration.

        Ranking over column tuples is far cheaper than loading full Product
        objects; we re-fetch only the handful of matched rows afterwards.
        """
        return db.session.query(
            Product.id, Product.name, Product.short_name
        ).all()

    def _upc_index(self) -> list[tuple[int, str]]:
        return db.session.query(Product.id, Product.upc).all()

    def _fetch_products(self, ids: list[int]) -> dict[int, Product]:
        if not ids:
            return {}
        rows = db.session.query(Product).filter(Product.id.in_(ids)).all()
        return {p.id: p for p in rows}

    # ── name search ───────────────────────────────────────────────────────────
    def search_by_name(
        self,
        query: str,
        *,
        limit: int | None = None,
        score_cutoff: float | None = None,
    ) -> list[SearchHit]:
        query = (query or "").strip()
        if not query:
            return []
        limit = limit or self.limit
        cutoff = self.name_score_cutoff if score_cutoff is None else score_cutoff

        rows = self._name_index()
        if not rows:
            return []

        # Index each product by both its full and shortened name; take the best.
        choices: list[str] = []
        owner_ids: list[int] = []
        for pid, name, short in rows:
            choices.append(name or "")
            owner_ids.append(pid)
            if short and short != name:
                choices.append(short)
                owner_ids.append(pid)

        # WRatio handles partial / token-order / typo'd queries robustly.
        matches = process.extract(
            query,
            choices,
            scorer=fuzz.WRatio,
            limit=limit * 3,  # over-fetch; we dedupe owners below
            score_cutoff=cutoff,
        )

        best: dict[int, float] = {}
        for _text, score, idx in matches:
            pid = owner_ids[idx]
            if pid not in best or score > best[pid]:
                best[pid] = float(score)

        top = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        products = self._fetch_products([pid for pid, _ in top])
        return [
            SearchHit(products[pid], score, "name")
            for pid, score in top
            if pid in products
        ]

    # ── upc search (misread barcode) ──────────────────────────────────────────
    def search_by_upc(
        self,
        scanned_upc: str,
        *,
        limit: int | None = None,
        score_cutoff: float | None = None,
    ) -> list[SearchHit]:
        scanned_upc = (scanned_upc or "").strip()
        if not scanned_upc:
            return []
        limit = limit or self.limit
        cutoff = self.upc_score_cutoff if score_cutoff is None else score_cutoff

        rows = self._upc_index()
        if not rows:
            return []

        upcs = [upc for _pid, upc in rows]
        # Ratio (edit-distance based) is ideal for catching a single transposed
        # or mis-scanned digit in an otherwise-correct code.
        matches = process.extract(
            scanned_upc,
            upcs,
            scorer=fuzz.ratio,
            limit=limit,
            score_cutoff=cutoff,
        )
        ranked = [(rows[idx][0], float(score)) for _text, score, idx in matches]
        products = self._fetch_products([pid for pid, _ in ranked])
        return [
            SearchHit(products[pid], score, "upc")
            for pid, score in ranked
            if pid in products
        ]

    # ── convenience for 404 suggestions ───────────────────────────────────────
    def suggestions_for(self, scanned: str) -> list[dict]:
        """Best-effort suggestions for an unrecognized scan (upc + name)."""
        scanned = (scanned or "").strip()
        if not scanned:
            return []
        hits = self.search_by_upc(scanned)
        # If the scan looks like text (a typed query), also try name matching.
        if not scanned.isdigit():
            hits = self.search_by_name(scanned) or hits
        return [h.to_dict() for h in hits]
