"""
app/services/label.py — Label image rendering (Pillow).

Generates a print-ready label image for a product, sized for the Brother
QL-810W media. The output is a 1-bit-friendly RGB PIL image at 300 DPI that the
printer transports (CUPS / brother_ql) convert to device raster.

Layout (62mm continuous default, 696 px wide @ 300 DPI):

    ┌────────────────────────────────────────┐  ← variant border
    │ DEPARTMENT                       SALE   │
    │ Product Name (auto-fit to width)        │
    │ size · unit                             │
    │ $14.99            ̶$̶1̶8̶.̶9̶9̶  (was, if sale) │
    │ ▌▌ ▌ ▌▌▌ ▌ ▌▌  Code128 barcode          │
    │ 7 11535 50912 7                         │
    └────────────────────────────────────────┘

Variant styling: sale/clearance colored borders + banners (Stage 3). The name
is rendered from the AI-shortened `short_name` when available, with a width-
aware abbreviation fallback before any ellipsis (Stage 4).
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .shorten import shorten_name

logger = logging.getLogger("spicetown.label")


# ── Media geometry ────────────────────────────────────────────────────────────
# Brother QL printable width in dots (pixels) at 300 DPI, keyed by media label.
# Continuous tapes have a flexible length; die-cut sizes are fixed W x H.
# Source: brother_ql label specs.
QL_MEDIA_PX: dict[str, tuple[int, int | None]] = {
    # continuous (width, None=variable length)
    "12": (106, None),
    "29": (306, None),
    "38": (413, None),
    "50": (554, None),
    "54": (590, None),
    "62": (696, None),
    # die-cut (width, height)
    "17x54": (165, 566),
    "17x87": (165, 956),
    "23x23": (202, 202),
    "29x42": (306, 425),
    "29x90": (306, 991),
    "38x90": (413, 991),
    "39x48": (425, 495),
    "52x29": (578, 271),
    "62x29": (696, 271),
    "62x100": (696, 1109),
    # DK-1209 small address labels (29mm tape x 62mm long) rendered in LANDSCAPE
    # reading orientation: 62mm wide x 29mm tall @ 300 DPI. Height is a hair
    # under the true 29mm (342px) to leave a safety margin against the
    # die-cutter's edge tolerance, while still using nearly the full tape.
    "29x62": (732, 336),
}

# Default printed length (dots) for a continuous-tape price label (~33mm).
DEFAULT_CONTINUOUS_LENGTH_PX = 390

# Variant → (border RGB, banner text, banner RGB). Black border is the default.
VARIANT_STYLE: dict[str, dict[str, Any]] = {
    "standard": {"border": (0, 0, 0), "banner": None, "banner_bg": None},
    "sale": {"border": (200, 0, 0), "banner": "SALE", "banner_bg": (200, 0, 0)},
    "clearance": {
        "border": (200, 0, 0),
        "banner": "CLEARANCE",
        "banner_bg": (0, 0, 0),
    },
    # "shelf" is a barcode-free 62x29mm tag (department + name + price only);
    # it uses a dedicated layout in _render_shelf(), not this style table.
    "shelf": {"border": (0, 0, 0), "banner": None, "banner_bg": None},
}

# Physical length of the shelf-tag variant on continuous tape, in millimetres.
SHELF_LENGTH_MM = 29

# Label content blocks staff can toggle per print (default: all included).
ALL_LABEL_FIELDS: tuple[str, ...] = ("department", "name", "size", "price", "barcode")


def parse_fields(value) -> frozenset[str] | None:
    """Normalize a field selection (list or comma-separated string).

    Returns None for "all fields" (empty/absent input, or nothing valid left
    after filtering unknown names) so callers can treat None as the default.
    """
    if not value:
        return None
    items = value.split(",") if isinstance(value, str) else list(value)
    sel = frozenset(
        f.strip().lower() for f in items if isinstance(f, str) and f.strip()
    )
    sel = frozenset(f for f in sel if f in ALL_LABEL_FIELDS)
    if not sel or sel == frozenset(ALL_LABEL_FIELDS):
        return None
    return sel


# ── Font resolution (portable: macOS + Linux) ─────────────────────────────────
_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _first_existing(paths: list[str], override: str | None = None) -> str | None:
    if override and os.path.exists(override):
        return override
    for p in paths:
        if os.path.exists(p):
            return p
    return None


@lru_cache(maxsize=64)
def _load_font(path: str | None, size: int, bold: bool) -> ImageFont.FreeTypeFont:
    """Load a TrueType font at `size`, falling back to Pillow's default."""
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR
    resolved = _first_existing(candidates, override=path)
    if resolved:
        try:
            return ImageFont.truetype(resolved, size=size)
        except Exception:  # pragma: no cover - corrupt/unsupported font
            logger.warning("failed to load font %s; using default", resolved)
    # Last resort: Pillow's built-in bitmap font (small, but keeps us running).
    return ImageFont.load_default()


# ── Config DTO ────────────────────────────────────────────────────────────────
@dataclass
class LabelSpec:
    """Resolved geometry + style knobs for the renderer."""

    width_px: int
    height_px: int
    dpi: int = 300
    margin: int = 18
    border_width: int = 6
    font_path_regular: str | None = None
    font_path_bold: str | None = None
    draw_barcode: bool = True
    store_name: str = "Spice Town Grocery"
    # Compact mode drops the department header + size line so the essentials
    # (name, price, barcode) fit a short label (e.g. 29mm-tall die-cut).
    compact: bool = False
    # Blank white margin added to the TOP and BOTTOM only (each side gets
    # this many px), outside the design area, before the image is handed to
    # the printer. See the comment on `for_media` below for why this exists.
    bleed_px: int = 0

    @classmethod
    def for_media(
        cls,
        label_size: str,
        *,
        dpi: int = 300,
        length_px: int | None = None,
        **kwargs,
    ) -> "LabelSpec":
        w, h = QL_MEDIA_PX.get(str(label_size), (696, None))
        if h is None:  # continuous tape → choose a length
            h = length_px or DEFAULT_CONTINUOUS_LENGTH_PX
            # Continuous tape has a non-printable margin at the leading/
            # trailing edge of each cut (the FEED direction — our label's
            # height) that nothing else accounts for. Compare: QL_MEDIA_PX's
            # continuous "62" entry is already 696px wide, not the full
            # 732px (62mm) a naive px/mm conversion would give — that 36px
            # gap is Brother's own documented printable-width margin for
            # 62mm tape (~1.5mm/side). The WIDTH axis bakes this in; nothing
            # upstream ever did the equivalent for HEIGHT, so a border drawn
            # flush to the top/bottom edge (by design, see render_label) was
            # landing exactly in the printer's dead zone and getting cut
            # off. Add a matching ~1.5mm/side (18px) safety margin here, as
            # blank bleed outside the design area rather than by shrinking
            # the design itself. This must stay paired with a matching bump
            # to the CUPS page height (STL_CUPS_LP_OPTIONS' media=...) or
            # fit-to-page will just scale the extra canvas back away.
            kwargs.setdefault("bleed_px", 18)
        # Short die-cut media (e.g. 29x62's 306px/~26mm) doesn't have room for
        # a department header + size line on top of name/price/barcode; drop
        # them automatically so the name and barcode get the vertical space.
        kwargs.setdefault("compact", h < 340)
        return cls(width_px=w, height_px=h, dpi=dpi, **kwargs)


# ── Text helpers ──────────────────────────────────────────────────────────────
def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return (r - l, b - t)


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str | None,
    bold: bool,
    max_width: int,
    start_size: int,
    min_size: int = 14,
) -> ImageFont.FreeTypeFont:
    """Shrink the font until `text` fits within `max_width` (single line).

    This is the lightweight Stage 3 fitter. Stage 4 adds smarter abbreviation
    of long product names (rapidfuzz/heuristics) before falling back to shrink.
    """
    size = start_size
    while size > min_size:
        font = _load_font(font_path, size, bold)
        w, _ = _text_size(draw, text, font)
        if w <= max_width:
            return font
        size -= 2
    return _load_font(font_path, min_size, bold)


def _fit_single(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str | None,
    bold: bool,
    max_width: int,
    start_size: int,
    min_size: int,
) -> ImageFont.FreeTypeFont | None:
    """Like _fit_font, but returns None when the text can't fit at min_size."""
    size = start_size
    while size >= min_size:
        font = _load_font(font_path, size, bold)
        if _text_size(draw, text, font)[0] <= max_width:
            return font
        size -= 2
    return None


def _wrap_two_lines(
    draw: ImageDraw.ImageDraw, text: str, font, max_width: int
) -> tuple[str, str] | None:
    """Split `text` into two lines that both fit `max_width`, or None.

    Picks the most balanced feasible split so neither line looks orphaned
    (e.g. "Shan Biryani Masala" / "B1G1" beats cramming three words up top).
    """
    words = text.split(" ")
    if len(words) < 2:
        return None
    best: tuple[int, tuple[str, str]] | None = None
    for i in range(1, len(words)):
        l1, l2 = " ".join(words[:i]), " ".join(words[i:])
        w1 = _text_size(draw, l1, font)[0]
        w2 = _text_size(draw, l2, font)[0]
        if w1 <= max_width and w2 <= max_width:
            widest = max(w1, w2)
            if best is None or widest < best[0]:
                best = (widest, (l1, l2))
    return best[1] if best else None


def _fit_two_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str | None,
    bold: bool,
    max_width: int,
    start_size: int,
    min_size: int,
) -> tuple[ImageFont.FreeTypeFont, str, str] | None:
    """Find the largest font at which `text` wraps onto two fitting lines."""
    size = start_size
    while size >= min_size:
        font = _load_font(font_path, size, bold)
        lines = _wrap_two_lines(draw, text, font, max_width)
        if lines is not None:
            return font, lines[0], lines[1]
        size -= 2
    return None


def _truncate_to_width(
    draw: ImageDraw.ImageDraw, text: str, font, max_width: int
) -> str:
    """Ellipsize text that still doesn't fit at the minimum font size."""
    if _text_size(draw, text, font)[0] <= max_width:
        return text
    ell = "…"
    while text and _text_size(draw, text + ell, font)[0] > max_width:
        text = text[:-1]
    return (text + ell) if text else ell


# ── Barcode ───────────────────────────────────────────────────────────────────
def _render_barcode(
    upc: str, max_width: int, target_height: int, dpi: int
) -> Image.Image | None:
    """Render `upc` as a Code128 barcode, scaled to ~target_height.

    Code128 encodes arbitrary digits without checksum constraints, so it never
    fails on the store's mixed UPC formats. (Stage 4 can opt into EAN/UPC-A when
    payloads validate.) We scale by HEIGHT (keeping bars crisp) and only
    downscale width to fit — never upscale width, which would distort modules
    and hurt scannability.
    """
    try:
        import barcode
        from barcode.writer import ImageWriter

        code = barcode.get("code128", upc, writer=ImageWriter())
        bio = io.BytesIO()
        code.write(
            bio,
            options={
                "module_height": 7.0,   # mm
                "module_width": 0.33,   # mm per narrow bar
                "quiet_zone": 2.0,      # mm
                "write_text": False,
                "dpi": dpi,
            },
        )
        bio.seek(0)
        img = Image.open(bio).convert("RGB")

        # Resize to the target height WITHOUT touching width: bar module width
        # is the X-dimension that actually encodes data, so it must stay
        # pixel-accurate for a scanner to decode it. A 1D barcode tolerates
        # vertical compression fine (the scanner just needs the beam to sweep
        # across it) — an aspect-locked resize was previously shrinking module
        # width in lockstep with height on short labels, which is what made
        # small labels' barcodes unscannable.
        if img.height != target_height and img.height > 0:
            img = img.resize((img.width, target_height), Image.NEAREST)
        # If still wider than the label, downscale width only as a last
        # resort (this does soften scannability, so it's worth sizing labels
        # to avoid hitting this path for normal-length UPCs).
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize(
                (max_width, img.height), Image.NEAREST
            )
        return img
    except Exception:
        logger.exception("barcode render failed for upc=%s", upc)
        return None


# ── Main entry point ──────────────────────────────────────────────────────────
def _money(v: float | None) -> str:
    return "" if v is None else f"${v:,.2f}"


def _render_shelf(
    product: dict, spec: LabelSpec, *, fields: frozenset[str] | None = None
) -> Image.Image:
    """Barcode-free shelf tag: department, name, price. 62mm x 29mm.

    Width follows the configured tape (696 px on 62mm media); the length is
    fixed at ~29mm regardless of the app-wide label length, so the same
    continuous roll yields a short tag.
    """
    def _has(f: str) -> bool:
        return fields is None or f in fields

    W = spec.width_px
    H = int(round(SHELF_LENGTH_MM / 25.4 * spec.dpi))
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    bw = min(spec.border_width, 4)  # thin border; the tag is short
    for i in range(bw):
        draw.rectangle([i, i, W - 1 - i, H - 1 - i], outline=(0, 0, 0))

    m = spec.margin + bw
    inner_w = W - 2 * m
    y = m

    # Department (category) header.
    if _has("department"):
        head_font = _load_font(spec.font_path_regular, 24, bold=False)
        dept = (product.get("department") or spec.store_name).upper()
        dept = _truncate_to_width(draw, dept, head_font, inner_w)
        draw.text((m, y), dept, font=head_font, fill=(60, 60, 60))
        y += _text_size(draw, dept, head_font)[1] + 10

    # Reserve room at the bottom for the price; the name gets the band between.
    # Zero/open-priced items (or a deselected price field) render no price.
    eff = product.get("effective_price", product.get("price", 0.0))
    show_price = _has("price") and eff is not None and eff > 0
    price_str = _money(eff) if show_price else ""
    if show_price:
        price_font = _fit_font(
            draw, price_str, spec.font_path_bold, True, inner_w, 84, min_size=48
        )
        pw, ph = _text_size(draw, price_str, price_font)
    else:
        price_font, pw, ph = None, 0, 0

    name_band = (H - m - ph - 4) - y - 8
    full_name = (product.get("name") or "") if _has("name") else ""
    short_name = product.get("short_name") or full_name

    name_lines: list[str] = []
    name_font = None
    if full_name:
        name_font = _fit_single(
            draw, full_name, spec.font_path_bold, True, inner_w, 46, 26
        )
    if name_font is not None:
        name_lines = [full_name]
    elif full_name and name_band >= 90:
        two = _fit_two_lines(
            draw, full_name, spec.font_path_bold, True, inner_w, 32, 20
        )
        if two is not None:
            name_font, l1, l2 = two
            name_lines = [l1, l2]
    if full_name and not name_lines:
        name_font = _fit_single(
            draw, short_name, spec.font_path_bold, True, inner_w, 46, 22
        )
        if name_font is not None:
            name_lines = [short_name]
    if full_name and not name_lines:
        abbreviated = shorten_name(full_name, max_chars=24)
        name_font = _fit_single(
            draw, abbreviated, spec.font_path_bold, True, inner_w, 46, 22
        )
        if name_font is not None:
            name_lines = [abbreviated]
    if full_name and not name_lines:
        name_font = _load_font(spec.font_path_bold, 22, True)
        name_lines = [_truncate_to_width(draw, short_name, name_font, inner_w)]

    for line in name_lines:
        lw = _text_size(draw, line, name_font)[0]
        draw.text((m + (inner_w - lw) // 2, y), line, font=name_font, fill="black")
        y += _text_size(draw, line, name_font)[1] + 4

    # Price directly under the name, centered in the remaining space.
    if show_price:
        price_y = y + max(6, ((H - m) - y - ph) // 2)
        draw.text((m + (inner_w - pw) // 2, price_y), price_str,
                  font=price_font, fill="black")

    return img


def _add_bleed(img: Image.Image, spec: LabelSpec) -> Image.Image:
    """Pad `img` with `spec.bleed_px` of blank white margin top and bottom.

    This runs *after* the design is fully drawn, so it never competes with
    the layout budget (name/price/barcode sizing above is untouched) — it
    just wraps the finished label in the safety margin continuous tape
    needs. See the comment on `LabelSpec.for_media` for why this exists.
    """
    if spec.bleed_px <= 0:
        return img
    padded = Image.new("RGB", (img.width, img.height + 2 * spec.bleed_px), "white")
    padded.paste(img, (0, spec.bleed_px))
    return padded


def render_label(
    product: dict,
    spec: LabelSpec,
    *,
    variant: str | None = None,
    fields: frozenset[str] | None = None,
) -> Image.Image:
    """Render a label for `product` (a Product.to_dict()) and return a PIL image.

    Parameters
    ----------
    product:
        Mapping with keys: name, price, sale_price, effective_price, on_sale,
        clearance, size, unit, department, upc, label_variant.
    spec:
        LabelSpec describing geometry + fonts.
    variant:
        Override the variant; defaults to product["label_variant"].
    fields:
        Subset of ALL_LABEL_FIELDS to include (see parse_fields); None = all.
    """
    def _has(f: str) -> bool:
        return fields is None or f in fields

    variant = variant or product.get("label_variant", "standard")
    if variant == "shelf":
        return _add_bleed(_render_shelf(product, spec, fields=fields), spec)
    style = VARIANT_STYLE.get(variant, VARIANT_STYLE["standard"])

    W, H = spec.width_px, spec.height_px
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    # Outer variant border. Compact (short die-cut) labels use a thinner
    # border + margin, like the shelf-tag layout, since every pixel of the
    # ~26mm-tall canvas is needed for name/price/barcode.
    bw = min(spec.border_width, 4) if spec.compact else spec.border_width
    for i in range(bw):
        draw.rectangle([i, i, W - 1 - i, H - 1 - i], outline=style["border"])

    m = (10 if spec.compact else spec.margin) + bw
    inner_w = W - 2 * m
    y = m

    # ── Header row: department (left) + variant banner (right) ────────────────
    # Compact labels (short die-cut) skip the department text to save height,
    # but still show the sale/clearance banner (smaller, to save height).
    head_font = _load_font(spec.font_path_regular, 26, bold=False)
    if not spec.compact and _has("department"):
        dept = (product.get("department") or spec.store_name).upper()
        dept = _truncate_to_width(draw, dept, head_font, int(inner_w * 0.62))
        draw.text((m, y), dept, font=head_font, fill=(60, 60, 60))

    if style["banner"]:
        banner_font = _load_font(spec.font_path_bold, 18 if spec.compact else 26, bold=True)
        bw_w, bw_h = _text_size(draw, style["banner"], banner_font)
        pad = 5 if spec.compact else 8
        bx1 = W - m - bw_w - 2 * pad
        by1 = y - 2
        draw.rectangle(
            [bx1, by1, W - m, by1 + bw_h + 2 * pad],
            fill=style["banner_bg"],
        )
        draw.text((bx1 + pad, by1 + pad), style["banner"], font=banner_font, fill="white")
    # Advance past the header band (smaller when compact).
    if spec.compact:
        y += 26 if style["banner"] else 4
    else:
        y += 38

    # ── Product name — NEVER truncate if avoidable. Cascade:
    #   1. full name, one line, shrinking 58 -> 28
    #   2. full name, TWO lines, shrinking 40 -> 22 (tall labels only)
    #   3. AI short_name, one line, 58 -> 24
    #   4. abbreviated full name (AI shortener), one line
    #   5. last resort: min-size + ellipsis
    full_name = (product.get("name") or "") if _has("name") else ""
    short_name = product.get("short_name") or full_name
    # Two-line mode needs vertical room. Compact labels drop the department +
    # size lines specifically to free up room for this, so they're eligible
    # too (name legibility matters more than a header on a small price tag).
    allow_two_lines = spec.height_px >= 260
    # Compact labels have plenty of WIDTH (732px on 29x62) but very little
    # HEIGHT, so width-based font fitting alone would happily pick a huge
    # font that fits one line's width while starving the price/barcode below
    # it. Cap the starting sizes much lower than the continuous-roll label.
    single_start, single_min = (42, 26) if spec.compact else (58, 28)
    two_start, two_min = (30, 20) if spec.compact else (40, 22)
    fallback_start, fallback_min = (34, 18) if spec.compact else (58, 24)

    name_lines: list[str] = []
    name_font = None
    if full_name:
        name_font = _fit_single(
            draw, full_name, spec.font_path_bold, True, inner_w, single_start, single_min
        )
    if name_font is not None:
        name_lines = [full_name]
    elif full_name and allow_two_lines:
        two = _fit_two_lines(
            draw, full_name, spec.font_path_bold, True, inner_w, two_start, two_min
        )
        if two is not None:
            name_font, l1, l2 = two
            name_lines = [l1, l2]
    if full_name and not name_lines:
        name_font = _fit_single(
            draw, short_name, spec.font_path_bold, True, inner_w, fallback_start, fallback_min
        )
        if name_font is not None:
            name_lines = [short_name]
    if full_name and not name_lines:
        abbreviated = shorten_name(full_name, max_chars=24)
        name_font = _fit_single(
            draw, abbreviated, spec.font_path_bold, True, inner_w, fallback_start, fallback_min
        )
        if name_font is not None:
            name_lines = [abbreviated]
    if full_name and not name_lines:
        name_font = _load_font(spec.font_path_bold, fallback_min, True)
        name_lines = [_truncate_to_width(draw, short_name, name_font, inner_w)]

    for line in name_lines:
        draw.text((m, y), line, font=name_font, fill="black")
        y += _text_size(draw, line, name_font)[1] + (3 if spec.compact else 6)
    y += 3 if spec.compact else 8

    # ── Size / unit line (skipped in compact mode) ────────────────────────────
    sub_bits = [b for b in [product.get("size"), product.get("unit")] if b]
    if sub_bits and not spec.compact and _has("size"):
        sub_font = _load_font(spec.font_path_regular, 28, bold=False)
        draw.text((m, y), " · ".join(sub_bits), font=sub_font, fill=(80, 80, 80))
        y += _text_size(draw, sub_bits[0], sub_font)[1] + 12

    # ── Price line (sale strikethrough of original) ───────────────────────────
    # Zero/open-priced items (or a deselected price field) render no price.
    eff = product.get("effective_price", product.get("price", 0.0))
    if _has("price") and eff is not None and eff > 0:
        # Compact (short die-cut) labels can't spare the full 72pt price the
        # continuous roll uses, but the price should still read as the most
        # prominent bold element on the label after the name.
        price_font = _load_font(spec.font_path_bold, 58 if spec.compact else 72, bold=True)
        price_str = _money(eff)
        draw.text((m, y), price_str, font=price_font, fill="black")
        pw, ph = _text_size(draw, price_str, price_font)

        if variant in ("sale", "clearance") and product.get("sale_price") is not None:
            was_font = _load_font(spec.font_path_regular, 30, bold=False)
            was_str = _money(product.get("price"))
            wx = m + pw + 18
            wy = y + (ph - _text_size(draw, was_str, was_font)[1])
            draw.text((wx, wy), was_str, font=was_font, fill=(120, 120, 120))
            ww, wh = _text_size(draw, was_str, was_font)
            # strike-through the original price
            draw.line([wx, wy + wh // 2, wx + ww, wy + wh // 2], fill=(120, 120, 120), width=3)
        y += ph + (6 if spec.compact else 16)

    # ── Barcode + human-readable UPC, bottom-aligned ──────────────────────────
    # A price label with NO barcode can't be scanned/re-priced at all, so this
    # must degrade gracefully (shrink) under a tight layout rather than ever
    # disappear outright. band_h used to be floored at a "legible minimum"
    # (70px) even when the REAL remaining space was smaller — which handed
    # _render_barcode a target taller than what was actually available, so the
    # honest fit-check right below it would then correctly reject it and drop
    # the barcode entirely. The floor must never exceed the true budget.
    upc = str(product.get("upc", ""))
    if spec.draw_barcode and upc and _has("barcode"):
        upc_font = _load_font(spec.font_path_regular, 24, bold=False)
        upc_text_h = _text_size(draw, upc, upc_font)[1]
        avail = max(0, (H - m) - y - 4)  # real remaining vertical space
        band_h = min(130, avail)
        bc_height = max(24, band_h - upc_text_h - 6)
        if avail > upc_text_h + 30:  # enough for a real (if small) barcode
            bc = _render_barcode(upc, inner_w, bc_height, spec.dpi)
            if bc is not None:
                bc_x = m + max(0, (inner_w - bc.width) // 2)  # center horizontally
                bc_y = H - m - bc.height - upc_text_h - 4
                bc_y = max(bc_y, y)  # never overlap the price line
                img.paste(bc, (bc_x, bc_y))
                uw, _ = _text_size(draw, upc, upc_font)
                draw.text(
                    ((W - uw) // 2, bc_y + bc.height + 2),
                    upc,
                    font=upc_font,
                    fill="black",
                )
        else:
            logger.warning(
                "barcode dropped: no vertical space left (upc=%s, avail=%dpx)",
                upc, avail,
            )

    return _add_bleed(img, spec)


def render_to_png_bytes(
    product: dict,
    spec: LabelSpec,
    *,
    variant: str | None = None,
    fields: frozenset[str] | None = None,
) -> bytes:
    """Render and return PNG bytes (used by the preview endpoint)."""
    img = render_label(product, spec, variant=variant, fields=fields)
    bio = io.BytesIO()
    img.save(bio, format="PNG", dpi=(spec.dpi, spec.dpi))
    return bio.getvalue()
