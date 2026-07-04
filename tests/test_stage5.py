"""
tests/test_stage5.py — Stage 5 (scanner UI) + Stage 6 (deploy artifacts).

Covers:
  * scanner page renders, references QuaggaJS + static assets
  * static JS/CSS are served
  * /healthz plain-text liveness
  * wsgi:app importable (gunicorn entrypoint)
  * deployment files exist (Dockerfile, compose, launchd, render.yaml, scripts)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app
from config import TestingConfig

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app():
    return create_app(config_object=TestingConfig, start_background=False)


@pytest.fixture()
def client(app):
    return app.test_client()


# ── Scanner UI ────────────────────────────────────────────────────────────────
def test_scanner_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Spice Town" in html
    assert "scanner.js" in html
    assert "quagga" in html.lower()        # camera scanning library
    assert "/api/" not in html or "preview" not in html  # logic lives in JS, not inline


def test_static_assets_served(client):
    js = client.get("/static/js/scanner.js")
    css = client.get("/static/css/app.css")
    assert js.status_code == 200 and len(js.data) > 500
    assert css.status_code == 200 and len(css.data) > 200
    # the JS wires the real API endpoints
    body = js.data.decode()
    assert "/api/lookup/" in body
    assert "/api/print" in body
    assert "/api/preview/" in body
    assert "/api/search" in body


def test_healthz_plaintext(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.data == b"ok"


# ── Deployment artifacts ─────────────────────────────────────────────────────
def test_wsgi_entrypoint_importable():
    import wsgi  # noqa: F401

    assert hasattr(wsgi, "app")


@pytest.mark.parametrize(
    "relpath",
    [
        "Dockerfile",
        ".dockerignore",
        "docker-compose.yml",
        "wsgi.py",
        "render.yaml",
        "Procfile",
        ".python-version",
        "DEPLOY.md",
        "deploy/com.spicetown.labels.plist",
        "scripts/setup_mac.sh",
        "scripts/run_gunicorn.sh",
    ],
)
def test_deploy_artifact_exists(relpath):
    assert (ROOT / relpath).exists(), f"missing deploy artifact: {relpath}"


def test_procfile_single_worker():
    # single worker keeps the print queue + scheduler singular
    proc = (ROOT / "Procfile").read_text()
    assert "-w 1" in proc
    assert "wsgi:app" in proc
