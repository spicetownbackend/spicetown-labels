#!/usr/bin/env python3
"""
scripts/test_print.py — Render (and optionally print) a single test label.

Built to verify a Brother QL-810W prints a 29x62mm die-cut label (DK-1209) in
LANDSCAPE at a chosen scale. It renders a real label image and, when run on a
machine that has CUPS + the printer, sends it with `lp` using your exact
options. In a sandbox without a printer it just writes the PNG(s) so you can
inspect them and copy the printed file to the Mac Mini.

Examples
--------
# Render only (no printer needed) — writes a preview + a print-ready file:
    python scripts/test_print.py --upc 711535509127

# Actually print on the Mac Mini with the requested settings:
    python scripts/test_print.py --upc 711535509127 --print \
        --printer Brother_QL_810W --media 29x62mm --landscape --scaling 123

Orientation note
----------------
macOS `lp -o landscape` rotates the page 90°. So the *print* file is rotated to
portrait on purpose: feeding a portrait image with `-o landscape` makes the text
read horizontally on the label. The *preview* file is the upright landscape view
for your eyes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Make `import app` / `app.services` work when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.label import LabelSpec, render_label  # noqa: E402


# A representative fallback product if no DB / UPC match is available.
_SAMPLE = {
    "upc": "711535509127",
    "name": "Saffron Threads Premium Grade A",
    "short_name": "Saffron Threads",
    "department": "Spices",
    "size": "1.0 g",
    "unit": "each",
    "price": 18.99,
    "sale_price": 14.99,
    "effective_price": 14.99,
    "on_sale": True,
    "clearance": False,
    "label_variant": "sale",
}


def _load_product(upc: str | None) -> dict:
    """Try to read the product from SQLite; fall back to the sample."""
    if not upc:
        return dict(_SAMPLE)
    try:
        from app import create_app
        from app.extensions import db
        from app.models import Product

        app = create_app(os.getenv("STL_ENV", "development"), start_background=False)
        with app.app_context():
            p = db.session.query(Product).filter_by(upc=upc.strip()).one_or_none()
            if p is not None:
                return p.to_dict()
        print(f"  (upc {upc} not in DB — using sample product)")
    except Exception as exc:  # pragma: no cover - convenience path
        print(f"  (could not query DB: {exc}; using sample product)")
    s = dict(_SAMPLE)
    s["upc"] = upc
    return s


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Render/print a test label.")
    ap.add_argument("--upc", help="UPC to look up (else a sample product)")
    ap.add_argument("--media", default="29x62mm",
                    help="CUPS media/PageSize name (default 29x62mm)")
    ap.add_argument("--printer", default=os.getenv("STL_CUPS_PRINTER_NAME", "Brother_QL_810W"))
    ap.add_argument("--scaling", type=int, default=123, help="lp scaling %% (default 123)")
    ap.add_argument("--landscape", action="store_true", default=True)
    ap.add_argument("--no-landscape", dest="landscape", action="store_false")
    ap.add_argument("--copies", type=int, default=1)
    ap.add_argument("--variant", help="override label variant (sale/clearance/standard)")
    ap.add_argument("--outdir", default="spool", help="where to write PNGs")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="actually send to the printer via lp")
    args = ap.parse_args(argv[1:])

    product = _load_product(args.upc)

    # 29x62mm DK-1209 in landscape reading orientation: 62mm wide x 29mm tall.
    spec = LabelSpec.for_media("29x62", dpi=args.dpi, compact=True)
    print(f"label geometry: {spec.width_px}x{spec.height_px}px @ {spec.dpi}dpi "
          f"(29x62mm landscape, compact)")

    landscape_img = render_label(product, spec, variant=args.variant)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    preview_path = outdir / "test_label_29x62_preview.png"
    landscape_img.save(preview_path, dpi=(args.dpi, args.dpi))

    # The file we actually print: rotate to portrait so `-o landscape` reads right.
    print_img = landscape_img.rotate(90, expand=True) if args.landscape else landscape_img
    print_path = outdir / "test_label_29x62_print.png"
    print_img.save(print_path, dpi=(args.dpi, args.dpi))

    print(f"preview (view this):     {preview_path}  ({landscape_img.size[0]}x{landscape_img.size[1]})")
    print(f"print file (send this):  {print_path}  ({print_img.size[0]}x{print_img.size[1]})")

    # Build the lp command (also printed so you can run it by hand on the Mac).
    cmd = ["lp", "-d", args.printer, "-n", str(args.copies),
           "-o", f"media={args.media}"]
    if args.landscape:
        cmd += ["-o", "landscape"]
    cmd += ["-o", f"scaling={args.scaling}", str(print_path)]
    print("\nlp command:\n  " + " ".join(cmd))

    if not args.do_print:
        print("\n(render-only; pass --print on a machine with the printer to send it)")
        return 0

    if shutil.which("lp") is None:
        print("\nERROR: `lp` not found — no CUPS on this machine. Copy the print "
              "file to the Mac Mini and run the command above.")
        return 2
    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(proc.stdout.strip() or "(submitted)")
    if proc.returncode != 0:
        print("lp error:", proc.stderr.strip())
        return proc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
