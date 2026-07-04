"""
app/providers/file_provider.py — FileDataProvider.

Reads products from a local CSV or JSON file (products.csv / products.json),
auto-detecting the format from the file extension. This is the default
provider for Spice Town's initial deployment and the reference implementation
of the DataProvider interface.

Because it reads from local disk, FileDataProvider does NOT consume the
external-API rate limiter — there is no upstream to protect. It still streams
records so a 10k+ row file loads with bounded memory.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from .base import DataProvider, ProductRecord

logger = logging.getLogger("spicetown.provider.file")


# Map of canonical field -> the various header spellings we accept from the
# wild. Keeps Spice Town's spreadsheet flexible without code changes.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "upc": ("upc", "barcode", "ean", "gtin", "code"),
    "name": ("name", "product", "product_name", "description", "title"),
    "price": ("price", "retail", "retail_price", "list_price", "unit_price"),
    "sku": ("sku", "item_number", "item_no", "plu"),
    "department": ("department", "dept", "category", "aisle"),
    "size": ("size", "pack_size", "package", "weight"),
    "unit": ("unit", "uom", "unit_of_measure"),
    "sale_price": ("sale_price", "sale", "promo_price", "promo"),
    "on_sale": ("on_sale", "is_sale", "sale_flag"),
    "clearance": ("clearance", "is_clearance", "clearance_flag"),
}

_TRUTHY = {"1", "true", "yes", "y", "t", "on", "sale", "clearance"}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        # Tolerate "$1.99" / "1,299.00" style strings.
        cleaned = str(value).replace("$", "").replace(",", "").strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def _build_alias_index(headers: list[str]) -> dict[str, str]:
    """Return {canonical_field: actual_header} using case-insensitive aliases."""
    lowered = {h.lower().strip(): h for h in headers}
    resolved: dict[str, str] = {}
    for canonical, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                resolved[canonical] = lowered[alias]
                break
    return resolved


def _record_from_mapping(row: dict[str, Any], idx: dict[str, str]) -> ProductRecord | None:
    """Build a ProductRecord from a raw row using the resolved alias index."""

    def get(field: str, default: Any = None) -> Any:
        header = idx.get(field)
        return row.get(header, default) if header else default

    upc = get("upc")
    name = get("name")
    if upc in (None, "") or name in (None, ""):
        return None  # skip incomplete rows (logged by caller)

    price = _coerce_float(get("price")) or 0.0
    sale_price = _coerce_float(get("sale_price"))
    on_sale = _coerce_bool(get("on_sale")) or (sale_price is not None)
    clearance = _coerce_bool(get("clearance"))

    # Anything not mapped is preserved under `extra` for debugging/future use.
    known_headers = set(idx.values())
    extra = {k: v for k, v in row.items() if k not in known_headers}

    return ProductRecord(
        upc=str(upc),
        name=str(name),
        price=price,
        sku=(str(get("sku")) if get("sku") not in (None, "") else None),
        department=(str(get("department")) if get("department") not in (None, "") else None),
        size=(str(get("size")) if get("size") not in (None, "") else None),
        unit=(str(get("unit")) if get("unit") not in (None, "") else None),
        sale_price=sale_price,
        on_sale=on_sale,
        clearance=clearance,
        extra=extra,
    )


class FileDataProvider(DataProvider):
    """DataProvider backed by a local CSV or JSON file."""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ── interface ────────────────────────────────────────────────────────────
    def health_check(self) -> bool:
        ok = self.path.exists() and self.path.is_file()
        if not ok:
            logger.error("products file not found: %s", self.path)
        return ok

    def fetch_all(self) -> Iterator[ProductRecord]:
        if not self.health_check():
            raise FileNotFoundError(f"products file not found: {self.path}")

        suffix = self.path.suffix.lower()
        if suffix == ".csv":
            yield from self._iter_csv()
        elif suffix == ".json":
            yield from self._iter_json()
        else:
            raise ValueError(
                f"unsupported products file type {suffix!r} (use .csv or .json)"
            )

    def fetch_by_upc(self, upc: str) -> ProductRecord | None:
        """Linear scan of the file for a single UPC.

        This is the cache-miss fallback. In normal operation it is rarely hit
        because the startup/nightly bulk load keeps SQLite authoritative.
        """
        target = str(upc).strip()
        for rec in self.fetch_all():
            if rec.upc == target:
                return rec
        return None

    # ── format readers ───────────────────────────────────────────────────────
    def _iter_csv(self) -> Iterator[ProductRecord]:
        with self.path.open("r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                logger.error("CSV %s has no header row", self.path)
                return
            idx = _build_alias_index(list(reader.fieldnames))
            if "upc" not in idx or "name" not in idx or "price" not in idx:
                logger.error(
                    "CSV %s missing required columns; resolved=%s",
                    self.path,
                    idx,
                )
            skipped = 0
            for line_no, row in enumerate(reader, start=2):
                rec = _record_from_mapping(row, idx)
                if rec is None:
                    skipped += 1
                    continue
                yield rec
            if skipped:
                logger.warning("CSV %s: skipped %d incomplete rows", self.path, skipped)

    def _iter_json(self) -> Iterator[ProductRecord]:
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        # Accept either a top-level list or {"products": [...]}.
        if isinstance(data, dict):
            data = data.get("products") or data.get("items") or []
        if not isinstance(data, list):
            logger.error("JSON %s did not contain a product list", self.path)
            return

        if not data:
            return
        # Derive alias index from the union of keys across the first few rows.
        sample_keys: list[str] = []
        for row in data[:25]:
            if isinstance(row, dict):
                for k in row:
                    if k not in sample_keys:
                        sample_keys.append(k)
        idx = _build_alias_index(sample_keys)

        skipped = 0
        for row in data:
            if not isinstance(row, dict):
                skipped += 1
                continue
            rec = _record_from_mapping(row, idx)
            if rec is None:
                skipped += 1
                continue
            yield rec
        if skipped:
            logger.warning("JSON %s: skipped %d incomplete rows", self.path, skipped)
