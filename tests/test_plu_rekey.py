"""A supplier name in Toast's PLU is no longer an item's barcode (Oct 31 2026).

Items keyed on it before are moved to the new internal code on the next sync,
keeping their price and print history; nothing is duplicated.
"""

from __future__ import annotations

import pytest

from app import create_app
from app.extensions import db
from app.models import PriceHistory, PrintJob, Product
from app.providers.base import ProductRecord
from app.services.loader import upsert_record
from config import TestingConfig


@pytest.fixture()
def ctx():
    app = create_app(config_object=TestingConfig, start_background=False)
    with app.app_context():
        yield app


def _rec(upc, name, price, legacy=None):
    extra = {"legacy_upc": legacy} if legacy else {}
    return ProductRecord(upc=upc, name=name, price=price, extra=extra)


def test_old_supplier_key_moves_to_the_new_code_with_its_history(ctx):
    old = upsert_record(_rec("RBest", "Bananas", 0.69), source="toast")
    db.session.commit()
    db.session.add(PrintJob(product_id=old.id, upc="RBest", copies=1, status="done", reason="manual"))
    db.session.commit()

    moved = upsert_record(_rec("TG-ABCDEF012345", "Bananas", 0.79, legacy="RBest"), source="toast")
    db.session.commit()

    rows = db.session.query(Product).filter_by(name="Bananas").all()
    assert [r.upc for r in rows] == ["TG-ABCDEF012345"]
    assert moved.id == old.id
    assert db.session.query(PrintJob).filter_by(product_id=old.id).count() == 1
    # The price move is still caught (and auto-prints the new code's label).
    assert [(h.old_price, h.new_price) for h in db.session.query(PriceHistory).filter_by(product_id=old.id)] == [(0.69, 0.79)]
    assert db.session.query(Product).filter_by(upc="RBest").count() == 0


def test_only_the_same_name_moves(ctx):
    upsert_record(_rec("Vdyas", "Ragi Flour", 3.0), source="toast")
    upsert_record(_rec("Vdyas", "Jowar Flour", 3.0), source="toast")
    db.session.commit()

    upsert_record(_rec("TG-111111111111", "Ragi Flour", 3.0, legacy="Vdyas"), source="toast")
    db.session.commit()

    assert {(p.upc, p.name) for p in db.session.query(Product)} == {
        ("TG-111111111111", "Ragi Flour"),
        ("Vdyas", "Jowar Flour"),
    }


def test_no_legacy_row_inserts_normally(ctx):
    upsert_record(_rec("TG-222222222222", "New Item", 1.0, legacy="Chetak"), source="toast")
    db.session.commit()
    assert [(p.upc, p.name) for p in db.session.query(Product)] == [("TG-222222222222", "New Item")]


def test_leftover_supplier_keys_are_retired_but_codes_are_kept(ctx):
    from app.services.loader import retire_supplier_keys

    stale = upsert_record(_rec("Vdyas", "Old Flour", 2.0), source="toast")
    code = upsert_record(_rec("RIM87230", "Rice Flakes", 2.0), source="toast")
    real = upsert_record(ProductRecord(upc="041196910184", name="Cumin", price=4.99, sku="041196910184"), source="toast")
    db.session.commit()

    assert retire_supplier_keys() == 1
    assert stale.upc == f"TG-OLD{stale.id:08d}"
    assert code.upc == "RIM87230"
    assert real.upc == "041196910184"
    assert retire_supplier_keys() == 0  # nothing left the second time
