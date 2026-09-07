"""
app/routes/api.py — JSON API blueprint.

Implemented (Stage 1-4):
  GET  /api/health             -> liveness + provider/db/cache/printer status
  GET  /api/lookup/<upc>       -> SQLite-first lookup (+ fuzzy suggestions on 404)
  GET  /api/stats              -> cache + print-queue metrics
  POST /api/refresh            -> trigger a manual bulk refresh (guarded)
  POST /api/print              -> enqueue a label print job (single-worker queue)
  GET  /api/print/<job_id>     -> poll print job status
  GET  /api/print-history      -> print jobs (manual + price-change), optional date range
  GET  /api/preview/<upc>.png  -> render label PNG (no print) for the scanner UI
  GET  /api/search?q=...       -> fuzzy search (rapidfuzz) for bad barcodes
  GET  /api/price-changes      -> unreviewed price changes needing a fresh label
  POST /api/price-changes/print   -> print labels for selected changes, mark reviewed
  POST /api/price-changes/dismiss -> mark selected changes reviewed without printing
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, jsonify, request

from ..extensions import db
from ..models import PriceHistory, PrintJob, Product, utcnow
from ..services.label import parse_fields, render_to_png_bytes
from ..services.loader import (
    RefreshInProgress,
    bulk_load_guarded,
    is_refresh_running,
)
from ..services.print_queue import QueueFull

bp = Blueprint("api", __name__, url_prefix="/api")

STORE_TZ = ZoneInfo("America/New_York")


def _local_day_bounds_utc(day_str: str) -> tuple[datetime, datetime]:
    """Given a store-local calendar date (YYYY-MM-DD), return that day's
    [start, end] as naive UTC datetimes, matching how PrintJob timestamps are
    stored (naive UTC via utcnow()). Mirrors the scanner UI's own convention
    of always rendering/filtering print history in America/New_York, so a
    "today" filter matches what's actually shown on screen."""
    d = date.fromisoformat(day_str)
    start_local = datetime.combine(d, time.min, tzinfo=STORE_TZ)
    end_local = datetime.combine(d, time.max, tzinfo=STORE_TZ)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


@bp.get("/health")
def health():
    """Liveness probe used by launchd / monitoring."""
    provider = current_app.config.get("DATA_PROVIDER")
    try:
        product_count = db.session.query(Product).count()
        db_ok = True
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("health check DB error: %s", exc)
        product_count = None
        db_ok = False

    status = 200 if db_ok else 503
    pq = current_app.extensions.get("print_queue")
    printer = current_app.extensions.get("printer")
    print_mode = current_app.config.get("PRINT_MODE", "local")
    if print_mode == "remote":
        # Jobs are drained by the store's print bridge, not an in-process worker.
        try:
            depth = db.session.query(PrintJob).filter_by(status="queued").count()
        except Exception:
            depth = None
        worker_alive = None
    else:
        depth = pq.depth if pq else None
        worker_alive = pq.is_alive() if pq else False
    return (
        jsonify(
            {
                "status": "ok" if db_ok else "degraded",
                "provider": provider,
                "db_ok": db_ok,
                "product_count": product_count,
                "refresh_running": is_refresh_running(),
                "printer": printer.name if printer else None,
                "print_mode": print_mode,
                "print_worker_alive": worker_alive,
                "print_queue_depth": depth,
            }
        ),
        status,
    )


@bp.get("/lookup/<upc>")
def lookup(upc: str):
    """Look up a product by UPC through the SQLite-first cache.

    Query params:
      remote=0   -> cache-only (do NOT contact the data source on a miss).

    The cache returns one of: hit | refreshed | stale | miss.
    """
    upc = (upc or "").strip()
    allow_remote = request.args.get("remote", "1") not in ("0", "false", "no")

    cache = current_app.extensions["cache"]
    result = cache.get(upc, allow_remote=allow_remote)

    if not result.found:
        # Stage 4: attach fuzzy suggestions (catch a misread/transposed digit).
        suggest = request.args.get("suggest", "1") not in ("0", "false", "no")
        suggestions = []
        if suggest:
            try:
                suggestions = current_app.extensions["search"].suggestions_for(upc)
            except Exception:  # pragma: no cover - suggestions are best-effort
                current_app.logger.exception("suggestion lookup failed for %s", upc)
        return (
            jsonify(
                {
                    "found": False,
                    "upc": upc,
                    "outcome": result.outcome,
                    "message": "UPC not in catalog",
                    "suggestions": suggestions,
                }
            ),
            404,
        )

    products = result.products or [result.product]
    return jsonify(
        {
            "found": True,
            "outcome": result.outcome,  # hit | refreshed | stale
            "product": result.product.to_dict(),
            # Shared barcodes (e.g. "XYZ" vs "XYZ B1G1") return every match;
            # the UI shows a picker when there is more than one.
            "products": [p.to_dict() for p in products],
            "multiple": len(products) > 1,
        }
    )


@bp.get("/stats")
def stats():
    """Cache + catalog + print-queue metrics for monitoring dashboards."""
    cache = current_app.extensions["cache"]
    pq = current_app.extensions.get("print_queue")
    product_count = db.session.query(Product).count()
    flagged_count = (
        db.session.query(Product).filter(Product.price_flagged.is_(True)).count()
    )
    return jsonify(
        {
            "product_count": product_count,
            "flagged_count": flagged_count,
            "refresh_running": is_refresh_running(),
            "cache": cache.stats(),
            "print_queue": pq.stats() if pq else None,
        }
    )


@bp.post("/refresh")
def manual_refresh():
    """Trigger a manual bulk refresh.

    Body (optional JSON): {"async": true} to run in a background thread and
    return 202 immediately; otherwise runs synchronously and returns stats.
    A 409 is returned if a refresh (manual or nightly) is already running.
    """
    provider = current_app.extensions["data_provider"]
    batch_size = current_app.config["BULK_LOAD_BATCH_SIZE"]
    threshold = current_app.config["PRICE_CHANGE_WARN_DELTA"]
    short_chars = current_app.config["LABEL_NAME_MAX_CHARS"]

    body = request.get_json(silent=True) or {}
    run_async = bool(body.get("async", False))

    if run_async:
        app = current_app._get_current_object()

        def _bg():
            with app.app_context():
                try:
                    bulk_load_guarded(
                        provider,
                        batch_size=batch_size,
                        price_change_threshold=threshold,
                        shorten_max_chars=short_chars,
                    )
                except RefreshInProgress:
                    app.logger.warning("async refresh skipped: already running")
                except Exception:
                    app.logger.exception("async refresh failed")
                finally:
                    db.session.remove()

        if is_refresh_running():
            return jsonify({"status": "busy", "message": "refresh already running"}), 409
        threading.Thread(target=_bg, name="manual-refresh", daemon=True).start()
        return jsonify({"status": "accepted", "async": True}), 202

    try:
        stats = bulk_load_guarded(
            provider,
            batch_size=batch_size,
            price_change_threshold=threshold,
            shorten_max_chars=short_chars,
        )
    except RefreshInProgress:
        return jsonify({"status": "busy", "message": "refresh already running"}), 409

    return jsonify({"status": "ok", "stats": stats.as_dict()})


@bp.post("/products/custom")
def create_custom_product():
    """Create (or update) a hand-entered product so it can be labelled.

    Body (JSON): {"name": "...", "price"?: 4.99, "size"?, "unit"?,
                  "department"?, "upc"?}
      - name  : required.
      - price : optional; 0/absent = open-priced (label omits the price area).
      - upc   : optional; auto-generated ("CU-…") when blank, so the printed
                Code128 barcode scans back to this product from then on.

    Rows are stored with source="custom"; the catalog sync only upserts
    provider rows, so custom products survive refreshes. Re-posting the same
    upc+name updates the row instead of duplicating it.
    """
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    if not name:
        return jsonify({"error": "bad_request", "message": "name is required"}), 400
    try:
        price = float(body.get("price") or 0.0)
    except (TypeError, ValueError):
        return jsonify({"error": "bad_request", "message": "price must be a number"}), 400
    if price < 0:
        return jsonify({"error": "bad_request", "message": "price must be >= 0"}), 400

    upc = str(body.get("upc", "")).strip()
    if not upc:
        upc = "CU-" + uuid.uuid4().hex[:10].upper()
    size = str(body.get("size", "")).strip() or None
    unit = str(body.get("unit", "")).strip() or None
    department = str(body.get("department", "")).strip() or None

    product = db.session.query(Product).filter_by(upc=upc, name=name).first()
    created = product is None
    if created:
        product = Product(upc=upc, name=name, source="custom")
        db.session.add(product)
    product.price = price
    product.size = size
    product.unit = unit
    product.department = department
    db.session.commit()

    current_app.logger.info(
        "custom product %s: %r upc=%s price=%.2f",
        "created" if created else "updated", name, upc, price,
    )
    return jsonify({"product": product.to_dict(), "created": created}), (
        201 if created else 200
    )


def _enqueue_for_product(
    product: Product,
    *,
    variant: str | None = None,
    copies: int = 1,
    fields_csv: str | None = None,
    reason: str | None = None,
):
    """Queue a print job for `product`. Shared by /api/print and the
    price-change review/print flow.

    `reason` tags why the job exists (None = manual scan/search print,
    "price_change" = queued from the price-change review panel) so the
    print-history view can distinguish them.

    Returns a SimpleNamespace(job_id, status, queue_depth). Raises QueueFull
    (bounded local queue) or RuntimeError (local worker not running).
    """
    pq = current_app.extensions["print_queue"]
    remote_mode = current_app.config.get("PRINT_MODE", "local") == "remote"

    if remote_mode:
        copies_n = max(1, min(int(copies), 50))
        job = PrintJob(
            upc=product.upc,
            product_id=product.id,
            variant=variant or product.label_variant(),
            copies=copies_n,
            fields=fields_csv,
            status="queued",
            reason=reason,
        )
        db.session.add(job)
        db.session.commit()
        return SimpleNamespace(
            job_id=job.id,
            status="queued",
            queue_depth=db.session.query(PrintJob).filter_by(status="queued").count(),
        )

    if not pq.is_alive():
        raise RuntimeError("print worker not running")
    return pq.enqueue(
        product.upc,
        variant=variant,
        copies=copies,
        product_id=product.id,
        fields=fields_csv,
        reason=reason,
    )


# ── Printing (Stage 3) ────────────────────────────────────────────────────────
@bp.post("/print")
def enqueue_print():
    """Enqueue a label print job and return immediately (decoupled worker).

    Body (JSON): {"upc": "...", "copies": 1, "variant": "sale"?, "wait": false?,
                  "fields": ["name", "price", ...]?}
      - upc      : required; must resolve via the cache (404 if unknown).
      - copies   : optional, default 1 (clamped 1..50).
      - variant  : optional override of the product's computed variant.
      - fields   : optional label-field subset (default: all fields).
      - wait     : optional; if true, block briefly for the terminal status.

    Returns 202 with the job id (or 200 with the final status when wait=true).
    Returns 503 if the bounded queue is full; 404 if the UPC is unknown.
    """
    body = request.get_json(silent=True) or {}
    upc = str(body.get("upc", "")).strip()
    product_id = body.get("product_id")
    if not upc and not product_id:
        return jsonify({"error": "bad_request", "message": "upc is required"}), 400

    copies = body.get("copies", 1)
    variant = body.get("variant")
    wait = bool(body.get("wait", False))
    # Normalize the field selection to a stored CSV (None = all fields).
    fields_sel = parse_fields(body.get("fields"))
    fields_csv = ",".join(sorted(fields_sel)) if fields_sel else None

    # Resolve the exact product: by id when given (disambiguates shared
    # barcodes), else the first catalog match for the UPC.
    if product_id is not None:
        product = db.session.get(Product, int(product_id))
        if product is None:
            return (
                jsonify({"error": "not_found", "product_id": product_id}),
                404,
            )
        upc = product.upc
    else:
        cache = current_app.extensions["cache"]
        result = cache.get(upc, allow_remote=True)
        if not result.found:
            return (
                jsonify({"error": "not_found", "upc": upc, "message": "UPC not in catalog"}),
                404,
            )
        product = result.product

    pq = current_app.extensions["print_queue"]
    try:
        enq = _enqueue_for_product(
            product, variant=variant, copies=copies, fields_csv=fields_csv
        )
    except RuntimeError:
        # Worker disabled/not started — fail loudly rather than silently drop.
        return (
            jsonify({"error": "unavailable", "message": "print worker not running"}),
            503,
        )
    except QueueFull:
        return jsonify({"error": "busy", "message": "print queue full"}), 503

    # A normal scan-and-print (this endpoint) is just as much "handling" a
    # pending price change as the review panel's dedicated print action is —
    # printing *any* fresh label for this product means whatever price is on
    # it is now current, so it shouldn't keep sitting in the review queue.
    # Mirrors the reviewed_at/label_printed bookkeeping in
    # print_price_changes() below; without this, only prints that go through
    # the review panel ever clear the queue, and everyday scanner prints
    # leave stale rows behind indefinitely.
    unreviewed = (
        db.session.query(PriceHistory)
        .filter(PriceHistory.product_id == product.id, PriceHistory.reviewed_at.is_(None))
        .all()
    )
    if unreviewed:
        now = utcnow()
        for row in unreviewed:
            row.reviewed_at = now
            row.label_printed = True
        db.session.commit()

    if wait:
        timeout = float(current_app.config.get("PRINT_JOB_TIMEOUT_SECONDS", 20.0))
        job = pq.wait_for(enq.job_id, timeout=timeout)
        payload = job.to_dict() if job else {"id": enq.job_id, "status": "unknown"}
        code = 200 if (job and job.status == "done") else 202
        return jsonify({"job": payload}), code

    return (
        jsonify(
            {
                "status": "queued",
                "job_id": enq.job_id,
                "queue_depth": enq.queue_depth,
                "product": product.to_dict(),
            }
        ),
        202,
    )


@bp.get("/print/<int:job_id>")
def print_status(job_id: int):
    """Poll the status of a print job: queued | printing | done | error."""
    job = db.session.get(PrintJob, job_id)
    if job is None:
        return jsonify({"error": "not_found", "job_id": job_id}), 404
    return jsonify({"job": job.to_dict()})


@bp.get("/print-history")
def print_history():
    """Print jobs (manual + price-change), most recent first.

    Query params:
      limit  : default 100 (default 1000 when start/end given), max 500
               (max 2000 when start/end given).
      reason : filter to "price_change" or "manual" (manual = reason is NULL).
      start, end : optional inclusive local-calendar-date range (YYYY-MM-DD,
                   store timezone America/New_York) filtered on created_at.
                   Both must be given together.
    """
    reason = request.args.get("reason")
    start = request.args.get("start")
    end = request.args.get("end")

    ranged = bool(start and end)
    default_limit = 1000 if ranged else 100
    max_limit = 2000 if ranged else 500
    limit = min(request.args.get("limit", default_limit, type=int) or default_limit, max_limit)

    q = db.session.query(PrintJob)
    if reason == "price_change":
        q = q.filter(PrintJob.reason == "price_change")
    elif reason == "manual":
        q = q.filter(PrintJob.reason.is_(None))

    if ranged:
        utc_start, _ = _local_day_bounds_utc(start)
        _, utc_end = _local_day_bounds_utc(end)
        q = q.filter(PrintJob.created_at >= utc_start, PrintJob.created_at <= utc_end)

    rows = q.order_by(PrintJob.id.desc()).limit(limit).all()

    # PrintJob has no name column — attach it from the product for display.
    product_ids = {r.product_id for r in rows if r.product_id is not None}
    names = {}
    if product_ids:
        names = {
            p.id: p.name
            for p in db.session.query(Product).filter(Product.id.in_(product_ids)).all()
        }

    out = []
    for r in rows:
        d = r.to_dict()
        d["name"] = names.get(r.product_id)
        out.append(d)

    return jsonify({"count": len(out), "jobs": out})


# ── Price-change review (Stage 6) ───────────────────────────────────────────
# Every Toast sync / refresh upserts through loader.upsert_record, which
# already appends a PriceHistory row whenever a price actually moves. These
# endpoints surface the unreviewed ones so staff can print fresh labels for
# exactly what changed instead of silently auto-printing or re-scanning the
# whole store.
@bp.get("/price-changes")
def list_price_changes():
    """Unreviewed price changes, most recent first.

    Query params: limit (default 200, max 500).
    """
    limit = min(request.args.get("limit", 200, type=int) or 200, 500)
    rows = (
        db.session.query(PriceHistory)
        .filter(PriceHistory.reviewed_at.is_(None))
        .order_by(PriceHistory.recorded_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify({"count": len(rows), "changes": [r.to_dict() for r in rows]})


def _resolve_price_change_ids() -> tuple[list[int], dict | None]:
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return [], {"error": "bad_request", "message": "ids (non-empty list) is required"}
    try:
        return [int(i) for i in ids], None
    except (TypeError, ValueError):
        return [], {"error": "bad_request", "message": "ids must be integers"}


@bp.post("/price-changes/print")
def print_price_changes():
    """Print a fresh label for each selected price change, then mark reviewed.

    Body (JSON): {"ids": [<price_history id>, ...]}
    Returns per-id results; partial failures don't block the rest.
    """
    ids, err = _resolve_price_change_ids()
    if err:
        return jsonify(err), 400

    results = []
    for pid in ids:
        row = db.session.get(PriceHistory, pid)
        if row is None or row.reviewed_at is not None:
            results.append({"id": pid, "ok": False, "error": "not_found"})
            continue
        product = db.session.get(Product, row.product_id) if row.product_id else None
        if product is None:
            results.append({"id": pid, "ok": False, "error": "product_missing"})
            continue
        try:
            enq = _enqueue_for_product(
                product, variant=product.label_variant(), reason="price_change"
            )
        except RuntimeError:
            results.append({"id": pid, "ok": False, "error": "unavailable"})
            continue
        except QueueFull:
            results.append({"id": pid, "ok": False, "error": "busy"})
            continue
        row.reviewed_at = utcnow()
        row.label_printed = True
        db.session.commit()
        results.append({"id": pid, "ok": True, "job_id": enq.job_id})

    return jsonify({"results": results})


@bp.post("/price-changes/dismiss")
def dismiss_price_changes():
    """Mark selected price changes reviewed without printing a label.

    Body (JSON): {"ids": [<price_history id>, ...]}
    """
    ids, err = _resolve_price_change_ids()
    if err:
        return jsonify(err), 400

    n = (
        db.session.query(PriceHistory)
        .filter(PriceHistory.id.in_(ids), PriceHistory.reviewed_at.is_(None))
        .update({"reviewed_at": utcnow()}, synchronize_session=False)
    )
    db.session.commit()
    return jsonify({"dismissed": n})


@bp.get("/preview/<upc>.png")
def preview_label(upc: str):
    """Render and return the label PNG for `upc` WITHOUT printing.

    Powers the scanner UI's on-screen preview (Stage 5). Query param
    `variant=` overrides the computed variant; `fields=` (comma-separated)
    limits which label blocks are drawn (default: all).
    """
    upc = (upc or "").strip()
    # `id` pins the exact product when several share the barcode.
    product_id = request.args.get("id", type=int)
    if product_id is not None:
        product = db.session.get(Product, product_id)
        if product is None:
            return jsonify({"error": "not_found", "product_id": product_id}), 404
    else:
        cache = current_app.extensions["cache"]
        result = cache.get(upc, allow_remote=True)
        if not result.found:
            return jsonify({"error": "not_found", "upc": upc}), 404
        product = result.product

    spec = current_app.extensions["label_spec"]
    variant = request.args.get("variant")
    fields = parse_fields(request.args.get("fields"))
    png = render_to_png_bytes(product.to_dict(), spec, variant=variant, fields=fields)
    return Response(png, mimetype="image/png")


@bp.get("/search")
def fuzzy_search():
    """Fuzzy-search the catalog for an unrecognized barcode (rapidfuzz).

    Query params:
      q       : required search text (a product name, or a scanned UPC string)
      by      : "name" | "upc" | "auto" (default: auto — digits→upc, else name)
      limit   : optional max results (defaults to config SEARCH_LIMIT)
      cutoff  : optional score cutoff override (0-100)

    Returns 200 with a ranked `results` list (may be empty); 400 if q is blank.
    """
    q = (request.args.get("q", "") or "").strip()
    if not q:
        return jsonify({"error": "bad_request", "message": "q is required"}), 400

    by = (request.args.get("by", "auto") or "auto").strip().lower()
    limit = request.args.get("limit", type=int)
    cutoff = request.args.get("cutoff", type=float)

    search = current_app.extensions["search"]
    if by == "auto":
        by = "upc" if q.isdigit() else "name"

    if by == "upc":
        hits = search.search_by_upc(q, limit=limit, score_cutoff=cutoff)
    else:
        hits = search.search_by_name(q, limit=limit, score_cutoff=cutoff)

    return jsonify(
        {
            "query": q,
            "by": by,
            "count": len(hits),
            "results": [h.to_dict() for h in hits],
        }
    )

