"""
app/services/label_settings.py — The scanner page's "Automatic labels" switches.

Stored in app_settings (key -> "1"/"0"); a key never saved falls back to
config.py (STL_AUTO_PRINT_NEW_ITEMS / STL_AUTO_PRINT_BARCODE_CHANGES, both on).
"""

from __future__ import annotations

from flask import current_app

from ..extensions import db
from ..models import AppSetting

# switch -> (app_settings key, config default)
SWITCHES = {
    "new_items": ("auto_print_new_items", "AUTO_PRINT_NEW_ITEMS"),
    "barcode_changes": ("auto_print_barcode_changes", "AUTO_PRINT_BARCODE_CHANGES"),
}


def auto_print_switches() -> dict:
    stored = {s.key: s.value for s in db.session.query(AppSetting).all()}
    out = {}
    for name, (key, cfg) in SWITCHES.items():
        out[name] = stored[key] == "1" if key in stored else bool(current_app.config.get(cfg, True))
    return out


def set_auto_print_switches(values: dict) -> dict:
    """Saves the switches given (unknown names ignored); returns all of them."""
    for name, on in values.items():
        if name not in SWITCHES or not isinstance(on, bool):
            continue
        key = SWITCHES[name][0]
        row = db.session.get(AppSetting, key)
        if row is None:
            db.session.add(AppSetting(key=key, value="1" if on else "0"))
        else:
            row.value = "1" if on else "0"
    db.session.commit()
    return auto_print_switches()
