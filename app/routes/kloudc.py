"""
app/routes/kloudc.py - the website hand-off from KloudC.

GET /kloudc/enter?code=<one-time code>

The KloudC website never puts a session token in a URL. It asks KloudC for a
one-time code (valid 60 s, used once) and sends the browser here. This route
trades the code with KloudC (POST /api/labels/handoff/redeem), then sets this
site's own signed cookie `kc_labels` (2 hours) and sends the browser to `/`.
Any failure goes back to the KloudC sign-in page (STL_KLOUDC_LOGIN_URL).

Works only when KloudC sign-in is on; otherwise 404. Exempt from the auth
gate (app/__init__.py).
"""

from __future__ import annotations

import logging
import time

from flask import Blueprint, current_app, jsonify, redirect, request

from ..services.kloudc_auth import (
    HANDOFF_COOKIE,
    Identity,
    handoff_cookie_end,
    handoff_secret,
    kloudc_client_key,
    make_handoff_cookie,
)

bp = Blueprint("kloudc", __name__)

logger = logging.getLogger("spicetown.kloudc_auth")


def _no_store(resp):
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.get("/kloudc/enter")
def enter():
    kloudc = current_app.extensions.get("kloudc_auth")
    if kloudc is None or not kloudc.enabled:
        return jsonify({"error": "not_found", "message": "Not found"}), 404

    secret = handoff_secret(current_app)
    if not secret:
        logger.error("kloudc_auth: STL_SECRET_KEY is not set - website hand-off refused")
        return _no_store(redirect(kloudc.login_url, code=303))

    result = kloudc.redeem(request.args.get("code", ""), client=kloudc_client_key(request))
    if not isinstance(result, Identity):
        return _no_store(redirect(kloudc.login_url, code=303))

    # 2 hours, or less when the KloudC session behind it ends sooner.
    now = int(time.time())
    max_age = handoff_cookie_end(result, now) - now
    if max_age <= 0:
        return _no_store(redirect(kloudc.login_url, code=303))
    logger.info("kloudc_auth: website hand-off ok user=%s", result.username)
    resp = redirect("/", code=303)
    resp.set_cookie(
        HANDOFF_COOKIE,
        make_handoff_cookie(secret, result, now),
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return _no_store(resp)
