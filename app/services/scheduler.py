"""
app/services/scheduler.py — Nightly bulk-refresh job (APScheduler).

Registers a single cron job that re-loads the full catalog from the active
DataProvider every night at the configured hour:minute. The job:
  * runs inside a fresh Flask app context (background thread),
  * uses `bulk_load_guarded` so it can never collide with a manual refresh,
  * scopes its own DB session and removes it afterwards.

A single in-process BackgroundScheduler is used (see app/extensions.py). Because
the WSGI server runs with ONE worker (`gunicorn -w 1`), exactly one scheduler
exists — no duplicate jobs.
"""

from __future__ import annotations

import logging

from apscheduler.triggers.cron import CronTrigger

from ..extensions import db, scheduler
from .loader import RefreshInProgress, bulk_load_guarded

logger = logging.getLogger("spicetown.scheduler")

NIGHTLY_JOB_ID = "nightly_bulk_refresh"


def run_nightly_refresh(app) -> None:
    """Job target: bulk-reload the catalog within an app context."""
    with app.app_context():
        provider = app.extensions["data_provider"]
        try:
            stats = bulk_load_guarded(
                provider,
                batch_size=app.config["BULK_LOAD_BATCH_SIZE"],
                price_change_threshold=app.config["PRICE_CHANGE_WARN_DELTA"],
                shorten_max_chars=app.config["LABEL_NAME_MAX_CHARS"],
            )
            logger.info("nightly refresh complete: %s", stats.as_dict())
        except RefreshInProgress:
            logger.warning("nightly refresh skipped: a refresh is already running")
        except Exception:
            logger.exception("nightly refresh failed")
        finally:
            # Background thread → release the scoped session.
            db.session.remove()


def init_scheduler(app) -> None:
    """Configure + start the background scheduler and register the cron job.

    Idempotent: safe across multiple create_app() calls in one process.
    """
    if not app.config.get("ENABLE_SCHEDULER", True):
        logger.info("scheduler disabled (ENABLE_SCHEDULER=false)")
        return

    hour = app.config["NIGHTLY_REFRESH_HOUR"]
    minute = app.config["NIGHTLY_REFRESH_MINUTE"]

    scheduler.add_job(
        func=run_nightly_refresh,
        trigger=CronTrigger(hour=hour, minute=minute),
        args=[app],
        id=NIGHTLY_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,  # still run if the box was briefly asleep
    )

    if not scheduler.running:
        scheduler.start()

    logger.info(
        "nightly refresh scheduled at %02d:%02d (%s)",
        hour,
        minute,
        scheduler.timezone,
    )


def shutdown_scheduler() -> None:
    """Stop the scheduler (used in tests / graceful shutdown)."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
