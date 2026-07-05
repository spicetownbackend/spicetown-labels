"""
app/services/print_queue.py — Request queue + single worker thread.

A `queue.Queue` decouples the HTTP response from print latency: the request
handler validates + enqueues a job and returns immediately, while ONE worker
thread renders the label and drives the printer. A single worker guarantees:
  * jobs print in submission order (no interleaved raster on the tape),
  * the Brother QL is never driven concurrently,
  * back-pressure via a bounded queue (returns "busy" instead of unbounded RAM).

Job lifecycle (persisted on the PrintJob row):
    queued -> printing -> done
                       -> error  (after retries with backoff+jitter)

The worker runs inside its own Flask app context so it can touch SQLite, and
removes the scoped session after each job.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from ..extensions import db
from ..models import PrintJob, Product, utcnow
from .label import LabelSpec, render_label
from .printer import PrinterError, PrinterTransport
from .ratelimit import compute_backoff

logger = logging.getLogger("spicetown.printq")

# Sentinel pushed to the queue to stop the worker cleanly.
_STOP = object()


def _resolve_product(product_id: int | None, upc: str) -> Product | None:
    """Product for a job: by pinned id first, else first UPC match (shared
    barcodes mean UPC alone may be ambiguous). Must run in an app context."""
    if product_id is not None:
        product = db.session.get(Product, product_id)
        if product is not None:
            return product
    return (
        db.session.query(Product)
        .filter_by(upc=upc)
        .order_by(Product.id)
        .first()
    )


@dataclass
class EnqueueResult:
    job_id: int
    status: str
    queue_depth: int


class QueueFull(Exception):
    """Raised when the bounded print queue is at capacity."""


class PrintQueue:
    """Single-worker, bounded print job queue."""

    def __init__(
        self,
        app,
        printer: PrinterTransport,
        spec: LabelSpec,
        *,
        maxsize: int = 100,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        backoff_cap: float = 5.0,
    ) -> None:
        self._app = app
        self._printer = printer
        self._spec = spec
        self._q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self._worker: threading.Thread | None = None
        self._started = threading.Event()
        self._stopping = threading.Event()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap

        # Lightweight metrics (thread-safe enough for counters under the GIL).
        self.printed = 0
        self.failed = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopping.clear()
        self._worker = threading.Thread(
            target=self._run, name="print-worker", daemon=True
        )
        self._worker.start()
        self._started.set()
        logger.info(
            "print worker started (transport=%s, maxsize=%d)",
            self._printer.name,
            self._q.maxsize,
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        if self._worker is None:
            return
        self._stopping.set()
        try:
            self._q.put_nowait(_STOP)
        except queue.Full:  # pragma: no cover - rare
            pass
        self._worker.join(timeout=timeout)
        logger.info("print worker stopped")

    @property
    def depth(self) -> int:
        return self._q.qsize()

    def is_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    # ── enqueue ───────────────────────────────────────────────────────────────
    def enqueue(
        self,
        upc: str,
        *,
        variant: str | None = None,
        copies: int = 1,
        product_id: int | None = None,
    ) -> EnqueueResult:
        """Create a PrintJob row (status=queued) and enqueue its id.

        Must be called within an app context (the request handler provides one).
        `product_id` pins the exact product when several share the barcode.
        If `variant` is None it is resolved from the product's computed variant
        (sale/clearance/standard) so labels reflect current pricing. Raises
        QueueFull if the bounded queue is saturated.
        """
        copies = max(1, min(int(copies), 50))  # clamp absurd values

        if variant is None:
            product = _resolve_product(product_id, upc)
            variant = product.label_variant() if product is not None else "standard"

        job = PrintJob(
            upc=upc,
            product_id=product_id,
            variant=variant,
            copies=copies,
            status="queued",
        )
        db.session.add(job)
        db.session.commit()

        try:
            self._q.put_nowait(job.id)
        except queue.Full:
            job.status = "error"
            job.error = "print queue full"
            db.session.commit()
            raise QueueFull("print queue is full")

        return EnqueueResult(job_id=job.id, status="queued", queue_depth=self.depth)

    # ── worker loop ───────────────────────────────────────────────────────────
    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is _STOP:
                    return
                with self._app.app_context():
                    self._process(item)
            except Exception:  # pragma: no cover - last-resort guard
                logger.exception("print worker: unexpected error on job %s", item)
            finally:
                self._q.task_done()
                # Release the scoped session created for this job.
                try:
                    with self._app.app_context():
                        db.session.remove()
                except Exception:
                    pass

    def _process(self, job_id: int) -> None:
        job = db.session.get(PrintJob, job_id)
        if job is None:
            logger.error("print worker: job %s vanished", job_id)
            return

        job.status = "printing"
        db.session.commit()

        product = _resolve_product(job.product_id, job.upc)
        if product is None:
            job.status = "error"
            job.error = f"UPC {job.upc} not in catalog at print time"
            job.completed_at = utcnow()
            db.session.commit()
            self.failed += 1
            logger.error("print job %s failed: %s", job_id, job.error)
            return

        variant = job.variant or product.label_variant()
        started = time.monotonic()

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                image = render_label(product.to_dict(), self._spec, variant=variant)
                result = self._printer.send(image, copies=job.copies, job_id=job.id)
                job.status = "done"
                job.error = None
                job.completed_at = utcnow()
                db.session.commit()
                self.printed += 1
                logger.info(
                    "print job %s done in %.3fs (variant=%s copies=%d) -> %s",
                    job_id,
                    time.monotonic() - started,
                    variant,
                    job.copies,
                    result,
                )
                return
            except PrinterError as exc:
                last_err = exc
                if attempt < self.max_retries:
                    delay = compute_backoff(
                        attempt, base=self.backoff_base, cap=self.backoff_cap
                    )
                    logger.warning(
                        "print job %s attempt %d failed (%s); retrying in %.2fs",
                        job_id, attempt + 1, exc, delay,
                    )
                    time.sleep(delay)
                else:
                    break
            except Exception as exc:  # rendering bug etc. — don't retry blindly
                last_err = exc
                logger.exception("print job %s rendering/send error", job_id)
                break

        job.status = "error"
        job.error = str(last_err)[:500] if last_err else "unknown print error"
        job.completed_at = utcnow()
        db.session.commit()
        self.failed += 1
        logger.error("print job %s failed after retries: %s", job_id, job.error)

    # ── helpers ───────────────────────────────────────────────────────────────
    def wait_for(self, job_id: int, *, timeout: float = 5.0, poll: float = 0.03) -> PrintJob | None:
        """Block until the job reaches a terminal state or timeout (optional).

        Used by /api/print?wait=1. Must run within an app context.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = db.session.get(PrintJob, job_id)
            if job is not None:
                db.session.refresh(job)
                if job.status in ("done", "error"):
                    return job
            time.sleep(poll)
        return db.session.get(PrintJob, job_id)

    def stats(self) -> dict:
        return {
            "transport": self._printer.name,
            "worker_alive": self.is_alive(),
            "queue_depth": self.depth,
            "queue_maxsize": self._q.maxsize,
            "printed": self.printed,
            "failed": self.failed,
        }
