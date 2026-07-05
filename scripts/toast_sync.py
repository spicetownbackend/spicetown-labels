#!/usr/bin/env python3
"""
scripts/toast_sync.py — Sync the Toast POS catalog into data/products.csv.

Runs nightly in GitHub Actions (.github/workflows/toast-sync.yml): pulls every
sellable item from Toast (sku = barcode/UPC), rewrites the CSV, and the
workflow commits it — which triggers Render's auto-deploy so the label app
reloads the new catalog. The app itself stays on the simple `file` provider
and never needs Toast credentials.

Sale/clearance handling: Toast has no sale-price concept, so the sync
PRESERVES any `sale_price` / `clearance` values already present in the CSV for
UPCs that still exist in Toast. Hand-edit those columns to put an item on
sale; the nightly sync keeps your edit while updating name/price from Toast.

Env (GitHub Actions Secrets): TOAST_CLIENT_ID, TOAST_CLIENT_SECRET,
TOAST_RESTAURANT_GUID, optional TOAST_API_BASE.

Usage:
    python scripts/toast_sync.py [--out data/products.csv] [--dry-run]

Exit codes: 0 = wrote CSV (or dry-run OK), 1 = config/API error,
            2 = safety stop (Toast returned suspiciously few items).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.providers.toast_provider import ToastDataProvider  # noqa: E402

FIELDS = [
    "upc", "name", "price", "sku", "department",
    "size", "unit", "sale_price", "clearance",
]

# Refuse to clobber a healthy catalog with a nearly-empty one (misconfigured
# restaurant GUID, Toast outage returning a stub menu, etc.).
MIN_EXPECTED_ITEMS = 1


def load_existing_overrides(path: Path) -> dict[str, dict]:
    """Read sale_price/clearance the store hand-set in the current CSV."""
    overrides: dict[str, dict] = {}
    if not path.exists():
        return overrides
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            upc = (row.get("upc") or "").strip()
            sale = (row.get("sale_price") or "").strip()
            clearance = (row.get("clearance") or "").strip()
            if upc and (sale or clearance):
                overrides[upc] = {"sale_price": sale, "clearance": clearance}
    return overrides


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Toast catalog to CSV")
    parser.add_argument("--out", default=str(REPO_ROOT / "data" / "products.csv"))
    parser.add_argument("--dry-run", action="store_true", help="print stats, write nothing")
    args = parser.parse_args()
    out_path = Path(args.out)

    client_id = os.getenv("TOAST_CLIENT_ID", "")
    client_secret = os.getenv("TOAST_CLIENT_SECRET", "")
    guid = os.getenv("TOAST_RESTAURANT_GUID", "")
    api_base = os.getenv("TOAST_API_BASE", "https://ws-api.toasttab.com")
    if not (client_id and client_secret and guid):
        print("error: TOAST_CLIENT_ID / TOAST_CLIENT_SECRET / TOAST_RESTAURANT_GUID not set")
        return 1

    provider = ToastDataProvider(
        client_id=client_id,
        client_secret=client_secret,
        api_base=api_base,
        restaurant_guid=guid,
    )

    try:
        records = list(provider.fetch_all())
    except Exception as exc:
        print(f"error: Toast fetch failed: {exc}")
        return 1

    if len(records) < MIN_EXPECTED_ITEMS:
        print(f"safety stop: Toast returned only {len(records)} item(s); not overwriting CSV")
        return 2

    overrides = load_existing_overrides(out_path)
    kept_overrides = 0
    rows = []
    for rec in records:
        row = {
            "upc": rec.upc,
            "name": rec.name,
            "price": f"{rec.price:.2f}",
            "sku": rec.sku or "",
            "department": rec.department or "",
            "size": rec.size or "",
            "unit": rec.unit or "",
            "sale_price": "",
            "clearance": "",
        }
        if rec.upc in overrides:
            row.update(overrides[rec.upc])
            kept_overrides += 1
        rows.append(row)
    rows.sort(key=lambda r: r["upc"])

    print(f"toast sync: {len(rows)} items, {kept_overrides} sale/clearance override(s) preserved")
    if args.dry_run:
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
