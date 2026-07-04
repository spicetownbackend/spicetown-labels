"""
app/routes/bridge.py — Print-bridge API (remote print mode).

When the app is hosted in the cloud (STL_PRINT_MODE=remote) it cannot reach the
store's Brother QL-810W. Instead, /api/print leaves jobs `queued` in SQLite and
a small agent at the store (scripts/print_bridge.py) polls this blueprint:

    POST /api/bridge/jobs/next        -> atomically claim the oldest queued job
                                         (204 when there is nothing to print)
    GET  /api/bridge/jobs/<id>/label.png -> rendered label PNG for the job
    GET  /api/bridge/jobs/<id>/raster    -> Brother QL raster bytes, ready to
                                            stream to the printer's port 9100
                                            (copies + cut already encoded)
    POST /api/bridge/jobs/<id>/complete  -> report {"status": "done"|"error"}
    GET  /api/bridge/ping             -> auth check + pending count (also keeps
                                         a free-tier dyno awake during polling)

Auth: every endpoint requires the shared token from STL_BRIDGE_TOKEN, sent as
`Authorization: Bearer <token>` or `X-Bridge-Token: <token>`. If no token is
configured the whole blueprint answers 503 (bridge disabled).

Claiming is a single atomic UPDATE (status queued -> printing) so two bridges
can never print the same job. A claimed job whose bridge dies is re-queued
after BRIDGE_STALE_SECONDS.
"""

from __future__ import annotations

import hmac
from datetime import timedelta

from flask import Blueprint, Response, current_app, jsonify, request

from ..extensions import db
from ..models import PrintJob, Product, utcnow
from ..services.label import render_label, render_to_png_bytes

bp = Blueprint("bridge", __name__, url_prefix="/api/bridge")

TERMINAL_STATUSES = {"done", "error"}


# ── Auth ──────────────────────────────────────────────────────────────────────
def _presented_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Bridge-Token", "").strip()


@bp.before_request
def _require_token():
    configured = (current_app.config.get("BRIDGE_TOKEN") or "").strip()
    if not configured:
        return (
            jsonify(
                {
                    "error": "bridge_disabled",
                    "message": "STL_BRIDGE_TOKEN is not configured on the server",
                }
            ),
            503,
        )
    if not hmac.compare_digest(_presented_token(), configured):
        return jsonify({"error": "unauthorized", "message": "bad bridge token"}), 401
    return None


# ── Stale-claim recovery ──────────────────────────────────────────────────────
def _requeue_stale_jobs() -> int:
    """Re-queue jobs a bridge claimed but never completed (bridge died)."""
    stale_secs = int(current_app.config.get("BRIDGE_STALE_SECONDS", 300))
    cutoff = utcnow() - timedelta(seconds=stale_secs)
    n = (
        db.session.query(PrintJob)
        .filter(
            PrintJob.status == "printing",
            PrintJob.claimed_at.isnot(None),
            PrintJob.claimed_at < cutoff,
        )
        .update({"status": "queued", "claimed_at": None}, synchronize_session=False)
    )
    if n:
        db.session.commit()
        current_app.logger.warning("bridge: re-queued %d stale claimed job(s)", n)
    return n


# ── Endpoints ─────────────────────────────────────────────────────────────────
@bp.get("/ping")
def ping():
    """Auth probe + queue depth. The bridge polls this to keep the dyno warm."""
    pending = db.session.query(PrintJob).filter_by(status="queued").count()
    return jsonify({"ok": True, "pending": pending})


@bp.post("/jobs/next")
def claim_next():
    """Atomically claim the oldest queued job for this bridge.

    Returns 200 with the job payload, or 204 when the queue is empty.
    """
    _requeue_stale_jobs()

    # Oldest-first so labels come out in scan order, like the local worker.
    candidate = (
        db.session.query(PrintJob)
        .filter_by(status="queued")
        .order_by(PrintJob.id.asc())
        .first()
    )
    if candidate is None:
        return "", 204

    # Atomic claim: only wins if the row is still queued (guards concurrent
    # bridges and gthread workers).
    claimed = (
        db.session.query(PrintJob)
        .filter(PrintJob.id == candidate.id, PrintJob.status == "queued")
        .update(
            {"status": "printing", "claimed_at": utcnow()},
            synchronize_session=False,
        )
    )
    db.session.commit()
    if not claimed:  # lost the race; caller just polls again
        return "", 204

    job = db.session.get(PrintJob, candidate.id)
    current_app.logger.info(
        "bridge: job %d claimed (upc=%s variant=%s copies=%d)",
        job.id, job.upc, job.variant, job.copies,
    )
    return jsonify({"job": job.to_dict()})


def _job_or_404(job_id: int) -> tuple[PrintJob | None, Response | None]:
    job = db.session.get(PrintJob, job_id)
    if job is None:
        return None, (jsonify({"error": "not_found", "job_id": job_id}), 404)
    return job, None


def _render_job_image(job: PrintJob):
    """Render the label image for a job (or None if the UPC vanished)."""
    product = db.session.query(Product).filter_by(upc=job.upc).one_or_none()
    if product is None:
        return None
    spec = current_app.extensions["label_spec"]
    return render_label(product.to_dict(), spec, variant=job.variant)


@bp.get("/jobs/<int:job_id>/label.png")
def job_label_png(job_id: int):
    """The job's rendered label as a PNG (for bridges that convert locally)."""
    job, err = _job_or_404(job_id)
    if err:
        return err
    product = db.session.query(Product).filter_by(upc=job.upc).one_or_none()
    if product is None:
        return jsonify({"error": "not_found", "upc": job.upc}), 404
    spec = current_app.extensions["label_spec"]
    png = render_to_png_bytes(product.to_dict(), spec, variant=job.variant)
    return Response(png, mimetype="image/png")


@bp.get("/jobs/<int:job_id>/raster")
def job_raster(job_id: int):
    """Brother QL raster instructions for the job — copies + cut included.

    The bridge can stream these bytes verbatim to the printer's TCP port 9100,
    which keeps the store agent dependency-free (no Pillow/brother_ql on the
    tablet). Conversion happens here, where brother_ql is installed.
    """
    job, err = _job_or_404(job_id)
    if err:
        return err
    image = _render_job_image(job)
    if image is None:
        return jsonify({"error": "not_found", "upc": job.upc}), 404

    try:
        data = _convert_to_raster(
            image,
            model=current_app.config.get("PRINTER_MODEL", "QL-810W"),
            label_size=str(current_app.config.get("LABEL_SIZE", "62")),
            copies=job.copies,
        )
    except Exception as exc:
        current_app.logger.exception("bridge: raster conversion failed for job %d", job_id)
        return jsonify({"error": "raster_failed", "message": str(exc)}), 500

    return Response(
        data,
        mimetype="application/octet-stream",
        headers={"X-Job-Id": str(job_id), "X-Copies": str(job.copies)},
    )


def _convert_to_raster(image, *, model: str, label_size: str, copies: int) -> bytes:
    from PIL import Image

    # brother_ql 0.9.4 still references the Image.ANTIALIAS alias that
    # Pillow 10 removed; restore it before conversion.
    if not hasattr(Image, "ANTIALIAS"):  # pragma: no cover - Pillow>=10 only
        Image.ANTIALIAS = Image.LANCZOS

    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(model)
    qlr.exception_on_warning = True
    return convert(
        qlr=qlr,
        images=[image] * max(1, copies),
        label=label_size,
        rotate="auto",
        threshold=70.0,
        dither=False,
        red=False,
        cut=True,
    )


@bp.post("/jobs/<int:job_id>/complete")
def complete_job(job_id: int):
    """Bridge reports the terminal outcome: {"status": "done"|"error", "error"?}."""
    job, err = _job_or_404(job_id)
    if err:
        return err

    body = request.get_json(silent=True) or {}
    status = str(body.get("status", "")).strip().lower()
    if status not in TERMINAL_STATUSES:
        return (
            jsonify(
                {"error": "bad_request", "message": "status must be 'done' or 'error'"}
            ),
            400,
        )
    if job.status in TERMINAL_STATUSES:
        # Idempotent: a retried completion report doesn't flip a settled job.
        return jsonify({"job": job.to_dict(), "note": "already terminal"})

    job.status = status
    job.error = (str(body.get("error", ""))[:500] or None) if status == "error" else None
    job.completed_at = utcnow()
    db.session.commit()
    log = current_app.logger.info if status == "done" else current_app.logger.error
    log("bridge: job %d reported %s%s", job_id, status, f" ({job.error})" if job.error else "")
    return jsonify({"job": job.to_dict()})
