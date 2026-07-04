"""
tests/test_bridge.py — Remote print mode + print-bridge API tests.

Covers:
  * auth: missing/bad token -> 401; no token configured -> 503 (disabled)
  * remote /api/print: job stays queued (no in-process worker involvement)
  * claim-next: oldest-first, atomic queued->printing, 204 on empty queue
  * label.png + raster payloads for a claimed job
  * complete: done/error terminal states, idempotent re-reports, bad status
  * stale-claim recovery: printing job past BRIDGE_STALE_SECONDS re-queues
  * health endpoint reports remote mode + DB-backed queue depth
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import PrintJob, Product, utcnow
from config import TestingConfig

TOKEN = "test-bridge-token"


class RemoteTestingConfig(TestingConfig):
    PRINT_MODE = "remote"
    BRIDGE_TOKEN = TOKEN
    BRIDGE_STALE_SECONDS = 300


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


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed(upc="711535509127", name="Saffron Threads", price=18.99, **kw):
    p = Product(upc=upc, name=name, price=price, **kw)
    db.session.add(p)
    db.session.commit()
    return p


def _enqueue(client, upc="711535509127", **body):
    return client.post("/api/print", json={"upc": upc, **body})


# ── Auth ──────────────────────────────────────────────────────────────────────
def test_bridge_requires_token(client, ctx):
    assert client.get("/api/bridge/ping").status_code == 401
    assert client.get("/api/bridge/ping", headers=_auth("wrong")).status_code == 401
    assert client.post("/api/bridge/jobs/next", headers=_auth("wrong")).status_code == 401


def test_bridge_token_via_x_header(client, ctx):
    r = client.get("/api/bridge/ping", headers={"X-Bridge-Token": TOKEN})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_bridge_disabled_without_configured_token():
    class NoToken(RemoteTestingConfig):
        BRIDGE_TOKEN = ""

    app = create_app(config_object=NoToken, start_background=False)
    r = app.test_client().get("/api/bridge/ping", headers=_auth())
    assert r.status_code == 503
    assert r.get_json()["error"] == "bridge_disabled"


# ── Remote enqueue ────────────────────────────────────────────────────────────
def test_remote_print_queues_job_without_worker(client, ctx):
    _seed()
    r = _enqueue(client, copies=2)
    assert r.status_code == 202
    body = r.get_json()
    assert body["status"] == "queued"

    job = db.session.get(PrintJob, body["job_id"])
    assert job.status == "queued"
    assert job.copies == 2
    assert job.variant == "standard"
    assert job.claimed_at is None


def test_remote_health_reports_mode_and_depth(client, ctx):
    _seed()
    _enqueue(client)
    r = client.get("/api/health")
    body = r.get_json()
    assert body["print_mode"] == "remote"
    assert body["print_queue_depth"] == 1
    assert body["print_worker_alive"] is None


# ── Claiming ──────────────────────────────────────────────────────────────────
def test_claim_next_empty_queue_returns_204(client, ctx):
    assert client.post("/api/bridge/jobs/next", headers=_auth()).status_code == 204


def test_claim_next_is_oldest_first_and_sets_printing(client, ctx):
    _seed("111111111111", name="First")
    _seed("222222222222", name="Second")
    id1 = _enqueue(client, "111111111111").get_json()["job_id"]
    id2 = _enqueue(client, "222222222222").get_json()["job_id"]

    r = client.post("/api/bridge/jobs/next", headers=_auth())
    assert r.status_code == 200
    job = r.get_json()["job"]
    assert job["id"] == id1
    assert job["status"] == "printing"
    assert job["claimed_at"] is not None

    # Second claim gets the next job; third finds the queue drained.
    assert client.post("/api/bridge/jobs/next", headers=_auth()).get_json()["job"]["id"] == id2
    assert client.post("/api/bridge/jobs/next", headers=_auth()).status_code == 204


# ── Label payloads ────────────────────────────────────────────────────────────
def test_job_label_png(client, ctx):
    _seed()
    jid = _enqueue(client).get_json()["job_id"]
    r = client.get(f"/api/bridge/jobs/{jid}/label.png", headers=_auth())
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_job_raster_bytes(client, ctx):
    pytest.importorskip("brother_ql")
    _seed()
    jid = _enqueue(client, copies=1).get_json()["job_id"]
    r = client.get(f"/api/bridge/jobs/{jid}/raster", headers=_auth())
    assert r.status_code == 200
    assert r.mimetype == "application/octet-stream"
    assert len(r.data) > 1000  # real raster payload, not an error blob
    assert r.headers["X-Job-Id"] == str(jid)


def test_job_payload_404s(client, ctx):
    assert client.get("/api/bridge/jobs/999/label.png", headers=_auth()).status_code == 404
    assert client.get("/api/bridge/jobs/999/raster", headers=_auth()).status_code == 404


# ── Completion ────────────────────────────────────────────────────────────────
def test_complete_done_and_idempotent(client, ctx):
    _seed()
    jid = _enqueue(client).get_json()["job_id"]
    client.post("/api/bridge/jobs/next", headers=_auth())

    r = client.post(f"/api/bridge/jobs/{jid}/complete", headers=_auth(), json={"status": "done"})
    assert r.status_code == 200
    assert r.get_json()["job"]["status"] == "done"
    assert r.get_json()["job"]["completed_at"] is not None

    # A duplicate error report must NOT flip the settled job.
    r2 = client.post(
        f"/api/bridge/jobs/{jid}/complete",
        headers=_auth(),
        json={"status": "error", "error": "late duplicate"},
    )
    assert r2.get_json()["job"]["status"] == "done"


def test_complete_error_records_message(client, ctx):
    _seed()
    jid = _enqueue(client).get_json()["job_id"]
    client.post("/api/bridge/jobs/next", headers=_auth())
    r = client.post(
        f"/api/bridge/jobs/{jid}/complete",
        headers=_auth(),
        json={"status": "error", "error": "printer unreachable"},
    )
    job = r.get_json()["job"]
    assert job["status"] == "error"
    assert "unreachable" in job["error"]


def test_complete_rejects_bad_status(client, ctx):
    _seed()
    jid = _enqueue(client).get_json()["job_id"]
    r = client.post(f"/api/bridge/jobs/{jid}/complete", headers=_auth(), json={"status": "meh"})
    assert r.status_code == 400


# ── Stale-claim recovery ──────────────────────────────────────────────────────
def test_stale_claimed_job_is_requeued(client, ctx):
    _seed()
    jid = _enqueue(client).get_json()["job_id"]
    client.post("/api/bridge/jobs/next", headers=_auth())

    # Simulate a bridge that died 10 minutes ago mid-print.
    job = db.session.get(PrintJob, jid)
    job.claimed_at = utcnow() - timedelta(seconds=600)
    db.session.commit()

    r = client.post("/api/bridge/jobs/next", headers=_auth())
    assert r.status_code == 200
    assert r.get_json()["job"]["id"] == jid  # re-queued, then re-claimed


def test_fresh_claim_is_not_requeued(client, ctx):
    _seed()
    _enqueue(client)
    client.post("/api/bridge/jobs/next", headers=_auth())
    # Claim is recent -> nothing to hand out.
    assert client.post("/api/bridge/jobs/next", headers=_auth()).status_code == 204
