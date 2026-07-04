"""
app/services/shorten.py — AI-assisted product-name auto-shortening.

Long product names won't fit on a 62mm label at a readable font size. This
module produces a compact `short_name` that preserves meaning, used by the
label renderer (and stored on `Product.short_name` at load time).

Strategy (applied progressively, stops as soon as the name fits the budget):
  1. Normalise whitespace.
  2. Drop trailing qualifiers after a comma  ("X, Authentic Style" -> "X").
  3. Strip parenthetical asides            ("X (12 ct)" -> "X").
  4. Apply a grocery/spice abbreviation map ("Organic Ground" -> "Org Grd").
  5. Drop filler words                      ("Authentic", "Style", ...).
  6. Hard-truncate with an ellipsis as a last resort.

The function is deterministic and side-effect free, so it is trivially testable
and safe to call on every load.
"""

from __future__ import annotations

import re

# Default character budget. ~22 chars renders comfortably bold on 62mm @ 300dpi.
DEFAULT_MAX_CHARS = 22

# Recognisable abbreviations for common grocery / spice vocabulary.
# Keys are lowercase; values keep nice display casing.
ABBREVIATIONS: dict[str, str] = {
    "organic": "Org",
    "ground": "Grd",
    "premium": "Prem",
    "powder": "Pwd",
    "powdered": "Pwd",
    "roasted": "Rstd",
    "smoked": "Smkd",
    "crushed": "Crshd",
    "seasoning": "Seas",
    "seasonings": "Seas",
    "original": "Orig",
    "traditional": "Trad",
    "imported": "Imp",
    "natural": "Nat",
    "threads": "Thrd",
    "extract": "Ext",
    "package": "Pkg",
    "container": "Cont",
    "grinder": "Grndr",
    "bottle": "Btl",
    "large": "Lg",
    "medium": "Md",
    "small": "Sm",
    "double": "Dbl",
    "concentrate": "Conc",
    "concentrated": "Conc",
    "vegetable": "Veg",
    "international": "Intl",
    "professional": "Pro",
    "assorted": "Asst",
    "with": "w/",
    "and": "&",
}

# True filler words that can be dropped without losing the product identity.
FILLER_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "authentic",
        "style",
        "brand",
        "genuine",
        "finest",
        "quality",
        "selection",
        "value",
        "premium-quality",
        "real",
        "pure",
    }
)

_WS_RE = re.compile(r"\s+")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _apply_abbreviations(text: str) -> str:
    out: list[str] = []
    for word in text.split(" "):
        # Preserve any trailing punctuation lightly; spices rarely need it.
        key = word.lower()
        out.append(ABBREVIATIONS.get(key, word))
    return _collapse_ws(" ".join(out))


def _drop_filler(text: str) -> str:
    words = [w for w in text.split(" ") if w.lower() not in FILLER_WORDS]
    return _collapse_ws(" ".join(words)) or text


def shorten_name(name: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Return a compact label-friendly version of `name`.

    Always returns a non-empty string (unless `name` is empty). Idempotent for
    names already within budget.
    """
    name = _collapse_ws(name)
    if not name:
        return ""
    if len(name) <= max_chars:
        return name

    # 2) drop trailing qualifier after the first comma
    head = name.split(",", 1)[0].strip()
    if head and len(head) <= max_chars:
        return head
    candidate = head or name

    # 3) strip parenthetical asides
    candidate = _collapse_ws(_PAREN_RE.sub("", candidate))
    if len(candidate) <= max_chars:
        return candidate

    # 4) apply abbreviations
    abbreviated = _apply_abbreviations(candidate)
    if len(abbreviated) <= max_chars:
        return abbreviated
    candidate = abbreviated

    # 5) drop filler words
    candidate = _drop_filler(candidate)
    if len(candidate) <= max_chars:
        return candidate

    # 6) hard truncate on a word boundary where possible, else mid-word
    if max_chars <= 1:
        return candidate[:max_chars]
    budget = max_chars - 1  # leave room for the ellipsis
    truncated = candidate[:budget].rstrip()
    # prefer cutting at the last space to avoid a chopped word, if reasonable
    space = truncated.rfind(" ")
    if space >= budget * 0.6:
        truncated = truncated[:space].rstrip()
    return (truncated + "…") if truncated else (candidate[:budget] + "…")
