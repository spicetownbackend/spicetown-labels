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

Price-change review queue: the app's DB lives on Render's free-tier ephemeral
disk, so it gets wiped on every redeploy this sync's commit triggers — a diff
computed *inside* the app after that reload would only ever see inserts, never
an old-vs-new price to compare. So the diff is computed HERE instead, before
the CSV is overwritten, and written to data/price_changes.json (also
committed). That file always holds just THIS run's changes (not an
accumulating backlog) — see app/__init__.py's `_import_price_change_diffs`
for how the app turns it into review-queue rows on boot.

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
import json
import os
import sys
from datetime import datetime, timezone
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


def load_existing_overrides(path: Path) -> dict[tuple[str, str], dict]:
    """Read sale_price/clearance the store hand-set in the current CSV.

    Keyed by (upc, name): shared barcodes are allowed (e.g. "XYZ" and
    "XYZ B1G1"), so an override must target the exact variant row.
    """
    overrides: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return overrides
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            upc = (row.get("upc") or "").strip()
            name = (row.get("name") or "").strip()
            sale = (row.get("sale_price") or "").strip()
            clearance = (row.get("clearance") or "").strip()
            if upc and (sale or clearance):
                overrides[(upc, name)] = {"sale_price": sale, "clearance": clearance}
    return overrides


def load_existing_prices(path: Path) -> dict[tuple[str, str], float]:
    """Read the price the CSV had *before* this run overwrites it.

    Keyed by (upc, name), same logical identity as load_existing_overrides.
    """
    prices: dict[tuple[str, str], float] = {}
    if not path.exists():
        return prices
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            upc = (row.get("upc") or "").strip()
            name = (row.get("name") or "").strip()
            price = (row.get("price") or "").strip()
            if not upc or not price:
                continue
            try:
                prices[(upc, name)] = float(price)
            except ValueError:
                continue
    return prices


# Sub-cent differences are float noise, not a real price change.
_PRICE_EPSILON = 0.005

# Top-level Toast menus (NOT the item's "department", which is the leaf menu
# GROUP name — e.g. "Street Snacks" — and near-never equals the menu name
# itself) that never get labeled — made-to-order hot food, no shelf tag.
# Excluded entirely: never synced into products.csv, never printed, never
# shows up as a price change to review. Substring match (case-insensitive) so
# Toast naming variants like "Street Kitchen (GH)" are caught too.
EXCLUDED_MENUS = {"street kitchen", "catering"}


def _menu_excluded(menu: str) -> bool:
    menu = (menu or "").strip().lower()
    return any(ex in menu for ex in EXCLUDED_MENUS)


def compute_price_diffs(
    rows: list[dict], old_prices: dict[tuple[str, str], float]
) -> list[dict]:
    """Diff this run's rows against the previously-committed CSV prices."""
    now = datetime.now(timezone.utc).isoformat()
    diffs = []
    for row in rows:
        old = old_prices.get((row["upc"], row["name"]))
        if old is None:
            continue  # new item — nothing to compare against
        new = float(row["price"])
        if abs(new - old) >= _PRICE_EPSILON:
            diffs.append(
                {
                    "upc": row["upc"],
                    "name": row["name"],
                    "old_price": old,
                    "new_price": new,
                    "recorded_at": now,
                }
            )
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Toast catalog to CSV")
    parser.add_argument("--out", default=str(REPO_ROOT / "data" / "products.csv"))
    parser.add_argument("--dry-run", action="store_true", help="print stats, write nothing")
    parser.add_argument(
        "--list-menus",
        action="store_true",
        help="print each distinct top-level Toast menu name + item count, then exit "
        "(no CSV/diff written) — use this to audit exact menu names before adding "
        "them to EXCLUDED_MENUS",
    )
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

    if args.list_menus:
        counts: dict[str, int] = {}
        for rec in records:
            menu = ((rec.extra or {}).get("menu") or "(no menu)").strip()
            counts[menu] = counts.get(menu, 0) + 1
        for menu, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            flag = " [EXCLUDED]" if _menu_excluded(menu) else ""
            print(f"{n:5d}  {menu}{flag}")
        return 0

    overrides = load_existing_overrides(out_path)
    old_prices = load_existing_prices(out_path)
    kept_overrides = 0
    rows = []
    seen: set[tuple[str, str]] = set()
    dupes = 0
    excluded = 0
    for rec in records:
        menu = (rec.extra or {}).get("menu") or ""
        if _menu_excluded(menu):
            excluded += 1
            continue

        # Same rule as the app's loader (loader.py bulk_load): an exact
        # duplicate (same UPC *and* name) within one feed is a Toast data
        # issue (e.g. the item cross-listed in two menu groups with
        # inconsistent prices) — keep the first, skip the rest, rather than
        # emitting conflicting rows/diffs for the same logical item.
        key = (rec.upc, rec.name)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)

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
        if key in overrides:
            row.update(overrides[key])
            kept_overrides += 1
        rows.append(row)
    rows.sort(key=lambda r: (r["upc"], r["name"]))

    diffs = compute_price_diffs(rows, old_prices)
    print(
        f"toast sync: {len(rows)} items, {kept_overrides} sale/clearance override(s) preserved, "
        f"{excluded} Street Kitchen item(s) excluded, {dupes} exact duplicate(s) skipped, "
        f"{len(diffs)} price change(s)"
    )
    if args.dry_run:
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    price_changes_path = out_path.parent / "price_changes.json"
    price_changes_path.write_text(json.dumps(diffs, indent=2) + "\n")

    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
