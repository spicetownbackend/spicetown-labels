"""
tests/test_stage4.py — Stage 4 acceptance tests (AI features).

Covers:
  * shorten_name: passthrough, comma/paren drop, abbreviation, filler drop,
    truncation, idempotence, empty input
  * loader populates Product.short_name on insert + update
  * SearchService.search_by_name: ranking, score cutoff, short_name indexing
  * SearchService.search_by_upc: catches a transposed/misread digit
  * /api/search: by=name / by=upc / auto, empty q -> 400, no match -> []
  * /api/lookup 404 attaches fuzzy suggestions
  * label renderer uses short_name and shortens overlong names safely
"""

from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db
from app.models import Product
from app.providers.base import DataProvider, ProductRecord
from app.services.label import LabelSpec, render_label
from app.services.loader import bulk_load, upsert_record
from app.services.search import SearchService
from app.services.shorten import DEFAULT_MAX_CHARS, shorten_name
from config import TestingConfig


# ── shorten_name ──────────────────────────────────────────────────────────────
def test_shorten_passthrough_when_short():
    assert shorten_name("Cumin", max_chars=22) == "Cumin"


def test_shorten_empty():
    assert shorten_name("") == ""
    assert shorten_name("   ") == ""


def test_shorten_drops_after_comma():
    out = shorten_name("Garam Masala Blend, Authentic North Indian Style", max_chars=22)
    assert out == "Garam Masala Blend"


def test_shorten_strips_parens():
    out = shorten_name("Cinnamon Sticks (12 count jar here)", max_chars=18)
    assert "(" not in out and ")" not in out
    assert out.startswith("Cinnamon")


def test_shorten_applies_abbreviations():
    out = shorten_name("Organic Ground Turmeric Powder Premium", max_chars=22)
    assert len(out) <= 22
    # abbreviations should appear for the long form
    assert "Org" in out or "Grd" in out or "Pwd" in out


def test_shorten_truncates_as_last_resort():
    out = shorten_name("Supercalifragilisticexpialidocious Spice Mix Deluxe", max_chars=20)
    assert len(out) <= 20
    assert out.endswith("…")


def test_shorten_idempotent():
    once = shorten_name("Organic Ground Turmeric Powder Premium", max_chars=22)
    twice = shorten_name(once, max_chars=22)
    assert once == twice


def test_shorten_default_constant():
    assert DEFAULT_MAX_CHARS == 22


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def app():
    return create_app(config_object=TestingConfig, start_background=False)


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield app


@pytest.fixture()
def client(app):
    return app.test_client()


class FakeProvider(DataProvider):
    name = "fake"

    def __init__(self, records):
        self._records = list(records)

    def health_check(self):
        return True

    def fetch_all(self):
        yield from self._records

    def fetch_by_upc(self, upc):
        for r in self._records:
            if r.upc == str(upc).strip():
                return r
        return None


def rec(upc, name, price=5.0, **kw):
    return ProductRecord(upc=upc, name=name, price=price, **kw)


# ── loader populates short_name ───────────────────────────────────────────────
def test_loader_sets_short_name_on_insert(ctx):
    upsert_record(
        rec("111", "Organic Ground Turmeric Powder Premium"),
        source="file",
        shorten_max_chars=22,
    )
    db.session.commit()
    p = db.session.query(Product).filter_by(upc="111").one()
    assert p.short_name
    assert len(p.short_name) <= 22


def test_loader_updates_short_name_on_name_change(ctx):
    upsert_record(rec("222", "Cumin"), source="file")
    db.session.commit()
    assert db.session.query(Product).filter_by(upc="222").one().short_name == "Cumin"
    upsert_record(
        rec("222", "Organic Ground Cumin Premium Authentic Style Jar"),
        source="file",
        shorten_max_chars=22,
    )
    db.session.commit()
    p = db.session.query(Product).filter_by(upc="222").one()
    assert len(p.short_name) <= 22
    assert p.short_name != "Cumin"


# ── SearchService ─────────────────────────────────────────────────────────────
def _seed_catalog(ctx):
    bulk_load(
        FakeProvider(
            [
                rec("036000291452", "Cinnamon Ground Saigon"),
                rec("711535509127", "Saffron Threads Premium Grade A"),
                rec("041196910184", "McCormick Ground Cumin"),
                rec("085239014288", "Organic Turmeric Powder"),
                rec("099482448257", "Crushed Red Pepper Flakes"),
            ]
        )
    )


def test_search_by_name_ranks_best_match(ctx):
    _seed_catalog(ctx)
    svc = SearchService(limit=3, name_score_cutoff=50)
    hits = svc.search_by_name("turmeric")
    assert hits
    assert hits[0].product.upc == "085239014288"
    assert hits[0].matched_on == "name"


def test_search_by_name_typo_tolerant(ctx):
    _seed_catalog(ctx)
    svc = SearchService(limit=3, name_score_cutoff=50)
    hits = svc.search_by_name("safron")  # missing an 'f'
    assert hits and hits[0].product.upc == "711535509127"


def test_search_by_name_cutoff_filters(ctx):
    _seed_catalog(ctx)
    svc = SearchService(limit=5, name_score_cutoff=95)
    assert svc.search_by_name("xyzzy nonsense") == []


def test_search_by_upc_catches_transposed_digit(ctx):
    _seed_catalog(ctx)
    svc = SearchService(limit=3, upc_score_cutoff=70)
    # swap two adjacent digits of the cumin UPC 041196910184 -> 041196910148
    hits = svc.search_by_upc("041196910148")
    assert hits
    assert hits[0].product.upc == "041196910184"


def test_search_empty_query_returns_empty(ctx):
    _seed_catalog(ctx)
    svc = SearchService()
    assert svc.search_by_name("") == []
    assert svc.search_by_upc("") == []


# ── API: /api/search ──────────────────────────────────────────────────────────
def test_api_search_by_name(app, client):
    with app.app_context():
        _seed_catalog(app)
    r = client.get("/api/search?q=turmeric")
    assert r.status_code == 200
    body = r.get_json()
    assert body["by"] == "name"
    assert body["count"] >= 1
    assert body["results"][0]["product"]["upc"] == "085239014288"
    assert body["results"][0]["matched_on"] == "name"


def test_api_search_auto_detects_upc(app, client):
    with app.app_context():
        _seed_catalog(app)
    r = client.get("/api/search?q=041196910148")  # misread digits
    assert r.status_code == 200
    body = r.get_json()
    assert body["by"] == "upc"
    assert body["results"][0]["product"]["upc"] == "041196910184"


def test_api_search_requires_q(client):
    r = client.get("/api/search?q=")
    assert r.status_code == 400


def test_api_search_no_match_empty_list(app, client):
    with app.app_context():
        _seed_catalog(app)
    r = client.get("/api/search?q=zzzzzzzzz&cutoff=95")
    assert r.status_code == 200
    assert r.get_json()["results"] == []


# ── API: 404 lookup attaches suggestions ──────────────────────────────────────
def test_api_lookup_404_includes_suggestions(app, client):
    with app.app_context():
        _seed_catalog(app)
    # a near-miss of the cumin UPC should suggest it
    r = client.get("/api/lookup/041196910148?remote=0")
    assert r.status_code == 404
    body = r.get_json()
    assert body["found"] is False
    assert "suggestions" in body
    assert any(s["product"]["upc"] == "041196910184" for s in body["suggestions"])


def test_api_lookup_404_suggest_disabled(app, client):
    with app.app_context():
        _seed_catalog(app)
    r = client.get("/api/lookup/041196910148?remote=0&suggest=0")
    assert r.status_code == 404
    assert r.get_json()["suggestions"] == []


# ── Label renderer uses short_name / shortens long names ──────────────────────
def test_label_uses_short_name():
    spec = LabelSpec.for_media("62", dpi=300, length_px=390)
    product = {
        "upc": "111",
        "name": "Organic Ground Turmeric Powder Premium Authentic",
        "short_name": "Org Grd Turmeric",
        "price": 7.99,
        "effective_price": 7.99,
        "label_variant": "standard",
    }
    img = render_label(product, spec)
    assert img.size == (696, 390)  # renders cleanly with the short name


def test_label_shortens_when_no_short_name():
    spec = LabelSpec.for_media("62", dpi=300, length_px=390)
    product = {
        "upc": "222",
        "name": "Super Extra Premium Authentic Himalayan Pink Rock Salt Fine Grind Deluxe",
        "price": 3.99,
        "effective_price": 3.99,
        "label_variant": "standard",
    }
    img = render_label(product, spec)  # must not raise / overflow
    assert img.size == (696, 390)
