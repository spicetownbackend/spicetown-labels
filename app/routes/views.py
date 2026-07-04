"""
app/routes/views.py — HTML views blueprint (Stage 5: scanner UI).

Serves the mobile scanner page (QuaggaJS camera scanning) plus a small health
page. The page talks to the JSON API (/api/lookup, /api/preview, /api/print,
/api/search) that the earlier stages deliver.
"""

from __future__ import annotations

from flask import Blueprint, current_app, render_template

bp = Blueprint("views", __name__)


@bp.get("/")
def index():
    """Mobile scanner UI."""
    return render_template(
        "scanner.html",
        provider=current_app.config.get("DATA_PROVIDER"),
        printer=current_app.extensions.get("printer").name
        if current_app.extensions.get("printer")
        else None,
        label_size=current_app.config.get("LABEL_SIZE"),
        store_name="Spice Town Grocery",
    )


@bp.get("/healthz")
def healthz():
    """Plain-text liveness for load balancers / uptime checks."""
    return "ok", 200, {"Content-Type": "text/plain"}
