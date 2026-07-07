"""Tests for the barcode-free 62x29mm "shelf" label variant."""

import pytest

from app import create_app
from app.extensions import db
from app.models import Product
from app.services.label import (
    SHELF_LENGTH_MM,
    LabelSpec,
    render_label,
    render_to_png_bytes,
)
from config import TestingConfig


@pytest.fixture()
def app():
    yield create_app(config_object=TestingConfig, start_background=False)


@pytest.fixture()
def client(app):
    return app.test_client()


def _product(**over):
    p = {
        "name": "Tetley Tea Bags",
        "short_name": "Tetley Tea",
        "price": 5.99,
        "sale_price": None,
        "effective_price": 5.99,
        "on_sale": False,
        "clearance": False,
        "size": "216 ct",
        "unit": "box",
        "department": "Beverages",
        "upc": "011156000134",
        "label_variant": "standard",
    }
    p.update(over)
    return p


def _spec():
    return LabelSpec.for_media("62")


def test_shelf_is_29mm_tall():
    img = render_label(_product(), _spec(), variant="shelf")
    expected_h = int(round(SHELF_LENGTH_MM / 25.4 * 300))
    assert img.height == expected_h
    assert img.width == 696


def test_shelf_has_no_barcode():
    # A barcode band would put long black bar runs in the lower half.
    # Instead just verify shelf output differs from standard and the lower
    # half is mostly white except the price text on the left.
    img = render_label(_product(), _spec(), variant="shelf")
    # right third, lower half: no barcode there → nearly all white
    w, h = img.size
    region = img.crop((int(w * 0.7), int(h * 0.55), w - 12, h - 12))
    px = list(region.convert("L").getdata())
    dark = sum(1 for v in px if v < 128)
    assert dark / len(px) < 0.02


def test_shelf_long_name_renders():
    long = _product(name="Shan Special Bombay Biryani Masala Family Pack B1G1")
    img = render_label(long, _spec(), variant="shelf")
    assert img.width == 696  # no crash, geometry intact


def test_shelf_png_bytes():
    png = render_to_png_bytes(_product(), _spec(), variant="shelf")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_shelf_via_preview_endpoint(app, client):
    with app.app_context():
        db.session.add(
            Product(upc="099999000099", name="Tetley Tea", price=4.99,
                    department="Beverages")
        )
        db.session.commit()
    r = client.get("/api/preview/099999000099.png?variant=shelf")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
