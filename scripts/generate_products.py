#!/usr/bin/env python3
"""
scripts/generate_products.py — Synthetic catalog generator for load testing.

Generates a CSV with N rows of realistic spice/grocery products so you can
verify the 10,000+ product target and bulk-load timing.

    python scripts/generate_products.py 10000 data/products_10k.csv

Then point the app at it:
    STL_PRODUCTS_FILE=data/products_10k.csv python manage.py load
"""

from __future__ import annotations

import csv
import random
import sys
from pathlib import Path

ADJECTIVES = [
    "Organic", "Smoked", "Ground", "Whole", "Toasted", "Premium", "Roasted",
    "Cracked", "Wild", "Sun-Dried", "Fine", "Coarse", "Hot", "Mild", "Sweet",
]
SPICES = [
    "Cumin", "Paprika", "Turmeric", "Cinnamon", "Cardamom", "Coriander",
    "Saffron", "Nutmeg", "Clove", "Ginger", "Fennel", "Mustard Seed",
    "Black Pepper", "Chili Flakes", "Garam Masala", "Curry Powder", "Sumac",
    "Za'atar", "Allspice", "Bay Leaf", "Oregano", "Thyme", "Rosemary", "Sage",
]
SIZES = ["1.0 oz", "1.5 oz", "2.0 oz", "2.2 oz", "4.0 oz", "8.0 oz", "1.0 lb"]
DEPARTMENTS = ["Spices", "Baking", "International", "Bulk", "Seasonings"]


def gen_upc(i: int) -> str:
    # 12-digit numeric UPC-A-ish, zero-padded; deterministic per index.
    base = 100000000000 + i
    return f"{base:012d}"


def main(argv: list[str]) -> int:
    n = int(argv[1]) if len(argv) > 1 else 10000
    out = Path(argv[2]) if len(argv) > 2 else Path("data/products_10k.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    rng = random.Random(42)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["upc", "name", "price", "sku", "department", "size", "unit",
             "sale_price", "clearance"]
        )
        for i in range(n):
            name = f"{rng.choice(ADJECTIVES)} {rng.choice(SPICES)}"
            price = round(rng.uniform(1.99, 24.99), 2)
            on_sale = rng.random() < 0.10
            clearance = rng.random() < 0.03
            sale_price = round(price * rng.uniform(0.6, 0.9), 2) if (on_sale or clearance) else ""
            w.writerow(
                [
                    gen_upc(i),
                    name,
                    price,
                    f"SPC-{i:06d}",
                    rng.choice(DEPARTMENTS),
                    rng.choice(SIZES),
                    "each",
                    sale_price,
                    1 if clearance else "",
                ]
            )
    print(f"wrote {n} products -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
