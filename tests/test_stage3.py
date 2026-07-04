"""
tests/test_stage3.py — Stage 3 acceptance tests (printing pipeline).

Covers:
  * label rendering: dimensions, variants, PNG output, long-name auto-fit
  * printer transports: null + file (no hardware required)
  * print queue: enqueue -> worker -> done, status transitions, copies
  * error path: unknown UPC at print time -> job error
  * retry path: transient PrinterError then success
  * queue full -> QueueFull / 503
  * API: /api/print (queued + wait), /api/print/<id>, /api/preview/<upc>.png
"""

from __future__ import annotations

import io
import time

import pytest
from PIL import Image

from app import create_app
from app.extensions import db
from app.models import PrintJob, Product
from app.services.label import LabelSpec, render_label, render_to_png_bytes
from app.services.print_queue import PrintQueue, QueueFull
from app.services.printer import (
    FileTransport,
    NullTransport,
    PrinterError,
    build_printer,
)
from config import TestingConfig


SAMPLE = {
    "upc": "711535509127",
    "name": "Saffron Threads Premium Grade A",
    "department": "Spices",
    "size": "1.0 g",
    "unit": "each",
    "price": 18.99,
    "sale_price": 14.99,
    "effective_price": 14.99,
    "on_sale": True,
    "clearance": False,
    "label_variant": "sale",
}


@pytest.fixture()
def app():
    app = create_app(config_object=TestingConfig, start_background=False)
    yield app


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield app


def _spec():
    return LabelSpec.for_media("62", dpi=300, length_px=390)


def _seed(upc, name="Test Item", price=5.0, **kw):
    p = Product(upc=upc, name=name, price=price, **kw)
    db.session.add(p)
    db.session.commit()
    return p


# ── Label rendering ───────────────────────────────────────────────────────────
def test_render_label_dimensions_62mm():
    img = render_label(SAMPLE, _spec())
    assert isinstance(img, Image.Image)
    assert img.size == (696, 390)  # 62mm @ 300dpi, default length
    assert img.mode == "RGB"


def test_render_png_bytes_is_valid_png():
    png = render_to_png_bytes(SAMPLE, _spec())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    reopened = Image.open(io.BytesIO(png))
    assert reopened.size == (696, 390)


def test_render_variants_differ():
    std = render_label({**SAMPLE, "label_variant": "standard"}, _spec())
    sale = render_label({**SAMPLE, "label_variant": "sale"}, _spec())
    clr = render_label({**SAMPLE, "label_variant": "clearance"}, _spec())
    # Different variants must produce visibly different pixels (banner/border).
    assert std.tobytes() != sale.tobytes()
    assert sale.tobytes() != clr.tobytes()


def test_render_long_name_does_not_overflow():
    long_name = "Super Extra Premium Authentic Himalayan Pink Rock Salt Fine Grind"
    img = render_label({**SAMPLE, "name": long_name}, _spec())
    assert img.size == (696, 390)  # renderer fit/truncated rather than crashing


def test_render_die_cut_size_fixed_height():
    spec = LabelSpec.for_media("62x29", dpi=300)
    img = render_label(SAMPLE, spec)
    assert img.size == (696, 271)


# ── Transports ────────────────────────────────────────────────────────────────
def test_null_transport_counts():
    t = NullTransport()
    img = render_label(SAMPLE, _spec())
    assert t.health_check() is True
    t.send(img, copies=3, job_id=1)
    assert t.sent == 3
    assert t.last_size == (696, 390)


def test_file_transport_writes_png(tmp_path):
    t = FileTransport(tmp_path)
    img = render_label(SAMPLE, _spec())
    out = t.send(img, copies=1, job_id=7)
    assert out.endswith(".png")
    files = list(tmp_path.glob("*.png"))
    assert len(files) == 1
    assert Image.open(files[0]).size == (696, 390)


def test_build_printer_from_config():
    class C:
        PRINT_TRANSPORT = "null"
    assert build_printer(C).name == "null"

    class C2:
        PRINT_TRANSPORT = "file"
        PRINT_SPOOL_DIR = "/tmp/stl_spool_test"
    assert build_printer(C2).name == "file"


# ── Print queue (threaded worker) ─────────────────────────────────────────────
def _started_queue(app, printer=None):
    pq = PrintQueue(app, printer or NullTransport(), _spec(), maxsize=10)
    pq.start()
    return pq


def _wait_status(app, job_id, target=("done", "error"), timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with app.app_context():
            job = db.session.get(PrintJob, job_id)
            if job and job.status in target:
                return job.status
        time.sleep(0.02)
    return "timeout"


def test_queue_enqueue_and_print(app):
    with app.app_context():
        _seed("q1", "Cumin", 4.99)
    pq = _started_queue(app)
    try:
        with app.app_context():
            enq = pq.enqueue("q1", copies=2)
        assert _wait_status(app, enq.job_id) == "done"
        with app.app_context():
            job = db.session.get(PrintJob, enq.job_id)
            assert job.status == "done"
            assert job.copies == 2
            assert job.completed_at is not None
        assert pq.printed >= 1
    finally:
        pq.stop()


def test_queue_unknown_upc_errors(app):
    pq = _started_queue(app)
    try:
        with app.app_context():
            enq = pq.enqueue("does-not-exist", copies=1)
        assert _wait_status(app, enq.job_id) == "error"
        with app.app_context():
            job = db.session.get(PrintJob, enq.job_id)
            assert "not in catalog" in (job.error or "")
    finally:
        pq.stop()


def test_queue_retries_then_succeeds(app):
    with app.app_context():
        _seed("r1", "Paprika", 6.49)

    class FlakyPrinter(NullTransport):
        name = "flaky"

        def __init__(self):
            super().__init__()
            self.calls = 0

        def send(self, image, *, copies=1, job_id=None):
            self.calls += 1
            if self.calls < 2:
                raise PrinterError("transient jam")
            return super().send(image, copies=copies, job_id=job_id)

    flaky = FlakyPrinter()
    pq = PrintQueue(app, flaky, _spec(), maxsize=10, max_retries=2, backoff_base=0.01, backoff_cap=0.02)
    pq.start()
    try:
        with app.app_context():
            enq = pq.enqueue("r1")
        assert _wait_status(app, enq.job_id) == "done"
        assert flaky.calls == 2  # failed once, succeeded on retry
    finally:
        pq.stop()


def test_queue_full_raises(app):
    # Block the worker by NOT starting it, fill the queue past maxsize.
    with app.app_context():
        _seed("f1", "Item", 1.0)
    pq = PrintQueue(app, NullTransport(), _spec(), maxsize=1)
    # worker not started -> items stay queued
    with app.app_context():
        pq.enqueue("f1")  # fills the single slot
        with pytest.raises(QueueFull):
            pq.enqueue("f1")  # second one overflows


# ── API ───────────────────────────────────────────────────────────────────────
@pytest.fixture()
def client(app):
    return app.test_client()


def test_api_print_requires_upc(client):
    assert client.post("/api/print", json={}).status_code == 400


def test_api_print_unknown_upc_404(app, client):
    # worker must be alive for the endpoint to reach the lookup-then-enqueue path
    app.extensions["print_queue"].start()
    try:
        r = client.post("/api/print", json={"upc": "nope"})
        assert r.status_code == 404
    finally:
        app.extensions["print_queue"].stop()


def test_api_print_queued_then_status(app, client):
    with app.app_context():
        _seed("apipq", "Turmeric", 7.99)
    app.extensions["print_queue"].start()
    try:
        r = client.post("/api/print", json={"upc": "apipq", "copies": 1})
        assert r.status_code == 202
        job_id = r.get_json()["job_id"]
        # poll the status endpoint until terminal
        for _ in range(100):
            s = client.get(f"/api/print/{job_id}").get_json()["job"]
            if s["status"] in ("done", "error"):
                break
            time.sleep(0.02)
        assert s["status"] == "done"
    finally:
        app.extensions["print_queue"].stop()


def test_api_print_wait_true_returns_done(app, client):
    with app.app_context():
        _seed("apiwait", "Clove", 9.49)
    app.extensions["print_queue"].start()
    try:
        r = client.post("/api/print", json={"upc": "apiwait", "wait": True})
        assert r.status_code == 200
        assert r.get_json()["job"]["status"] == "done"
    finally:
        app.extensions["print_queue"].stop()


def test_api_print_worker_down_503(app, client):
    with app.app_context():
        _seed("down1", "Item", 1.0)
    # worker not started
    r = client.post("/api/print", json={"upc": "down1"})
    assert r.status_code == 503


def test_api_preview_png(app, client):
    with app.app_context():
        _seed("prev1", "Saffron", 18.99, sale_price=14.99, on_sale=True)
    r = client.get("/api/preview/prev1.png")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(io.BytesIO(r.data)).size == (696, 390)


def test_api_preview_unknown_404(client):
    assert client.get("/api/preview/zzzzz.png").status_code == 404


# ── 29x62mm landscape die-cut (DK-1209) + CUPS scaling options ────────────────
def test_label_29x62_landscape_geometry():
    spec = LabelSpec.for_media("29x62", dpi=300, compact=True)
    assert (spec.width_px, spec.height_px) == (732, 306)
    assert spec.compact is True
    img = render_label(SAMPLE, spec)
    assert img.size == (732, 306)  # renders cleanly, compact layout


def test_compact_label_renders_essentials():
    # compact should still render (name/price/barcode) without overflow/crash
    spec = LabelSpec.for_media("29x62", dpi=300, compact=True)
    std = render_label({**SAMPLE, "label_variant": "standard"}, spec)
    sale = render_label({**SAMPLE, "label_variant": "sale"}, spec)
    assert std.size == (732, 306)
    assert std.tobytes() != sale.tobytes()  # banner makes them differ


def test_cups_cmd_scaling_suppresses_fit_to_page():
    from app.services.printer import CupsTransport

    t = CupsTransport(
        "Brother_QL_810W",
        lp_options=["media=29x62mm", "landscape", "scaling=123"],
        fit_to_page=True,
    )
    cmd = t._build_lp_cmd("/tmp/x.png", 2)
    assert "fit-to-page" not in cmd          # scaling present -> no fit-to-page
    assert "scaling=123" in cmd
    assert "landscape" in cmd
    assert "media=29x62mm" in cmd
    assert cmd[:5] == ["lp", "-d", "Brother_QL_810W", "-n", "2"]


def test_cups_cmd_default_uses_fit_to_page():
    from app.services.printer import CupsTransport

    t = CupsTransport("Brother_QL_810W")  # no lp_options
    cmd = t._build_lp_cmd("/tmp/x.png", 1)
    assert "fit-to-page" in cmd


def test_cups_cmd_fit_to_page_disabled():
    from app.services.printer import CupsTransport

    t = CupsTransport("Brother_QL_810W", fit_to_page=False)
    cmd = t._build_lp_cmd("/tmp/x.png", 1)
    assert "fit-to-page" not in cmd
