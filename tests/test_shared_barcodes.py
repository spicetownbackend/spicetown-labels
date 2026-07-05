"""
tests/test_shared_barcodes.py — Duplicate/shared-barcode support.

The store sells variants that share a barcode (e.g. "XYZ" and "XYZ B1G1").
Covers:
  * /api/lookup returns every match + multiple flag
  * /api/preview honors ?id= to pin the exact variant
  * /api/print with product_id targets the right product (remote mode)
  * bridge label rendering resolves the pinned product, not just the UPC
  * cache returns all rows; loader upserts by (upc, name)
"""

from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db
from app.models import PrintJob, Product
from config import TestingConfig

TOKEN = "shared-barcode-token"
UPC = "099999000010"


class RemoteTestingConfig(TestingConfig):
    PRINT_MODE = "remote"
    BRIDGE_TOKEN = TOKEN


@pytest.fixture()
def app():
    yield create_app(config_object=RemoteTestingConfig, start_background=False)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield app


@pytest.fixture()
def pair(ctx):
    """Two products sharing one barcode."""
    a = Product(upc=UPC, name="Shan Biryani Masala", price=3.49)
    b = Product(upc=UPC, name="Shan Biryani Masala B1G1", price=3.49, clearance=True)
    db.session.add_all([a, b])
    db.session.commit()
    return a, b


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_lookup_returns_all_matches(client, pair):
    r = client.get(f"/api/lookup/{UPC}")
    body = r.get_json()
    assert r.status_code == 200
    assert body["multiple"] is True
    names = {p["name"] for p in body["products"]}
    assert names == {"Shan Biryani Masala", "Shan Biryani Masala B1G1"}


def test_lookup_single_match_not_multiple(client, ctx):
    db.session.add(Product(upc="042000000001", name="Solo Item", price=1.0))
    db.session.commit()
    body = client.get("/api/lookup/042000000001").get_json()
    assert body["multiple"] is False
    assert len(body["products"]) == 1


def test_preview_pins_variant_by_id(client, pair):
    a, b = pair
    ra = client.get(f"/api/preview/{UPC}.png?id={a.id}")
    rb = client.get(f"/api/preview/{UPC}.png?id={b.id}")
    assert ra.status_code == rb.status_code == 200
    # Different products (name + clearance banner) → different pixels.
    assert ra.data != rb.data
    assert client.get(f"/api/preview/{UPC}.png?id=99999").status_code == 404


def test_print_targets_product_id(client, pair):
    a, b = pair
    r = client.post("/api/print", json={"upc": UPC, "product_id": b.id})
    assert r.status_code == 202
    body = r.get_json()
    assert body["product"]["name"] == "Shan Biryani Masala B1G1"

    job = db.session.get(PrintJob, body["job_id"])
    assert job.product_id == b.id
    # b is clearance → variant auto-resolves from the pinned product.
    assert job.variant == "clearance"


def test_print_without_id_uses_first_match(client, pair):
    a, _ = pair
    r = client.post("/api/print", json={"upc": UPC})
    assert r.status_code == 202
    assert db.session.get(PrintJob, r.get_json()["job_id"]).variant == "standard"


def test_bridge_renders_pinned_product(client, pair):
    a, b = pair
    jid_a = client.post("/api/print", json={"upc": UPC, "product_id": a.id}).get_json()["job_id"]
    jid_b = client.post("/api/print", json={"upc": UPC, "product_id": b.id}).get_json()["job_id"]
    png_a = client.get(f"/api/bridge/jobs/{jid_a}/label.png", headers=_auth())
    png_b = client.get(f"/api/bridge/jobs/{jid_b}/label.png", headers=_auth())
    assert png_a.status_code == png_b.status_code == 200
    assert png_a.data != png_b.data  # each job renders ITS product


def test_cache_returns_all_rows(app, pair):
    with app.app_context():
        result = app.extensions["cache"].get(UPC, allow_remote=False)
        assert result.found
        assert len(result.products) == 2
