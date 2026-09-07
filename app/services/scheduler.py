"""
app/services/scheduler.py — Bulk-refresh jobs (APScheduler).

Registers two cron/interval jobs against the active DataProvider:
  * a full nightly refresh at the configured hour:minute (safety-net pass —
    catches anything a live source stopped reporting entirely, e.g. a
    discontinued item), and
  * a frequent periodic refresh (default every 30 minutes) that keeps prices
    live-synced through the day instead of waiting for the next night.
Both jobs:
  * run inside a fresh Flask app context (background thread),
  * use `bulk_load_guarded` so they can never collide with each other or a
    manual refresh (whichever gets there first wins; the other is skipped),
  * auto-print a fresh label for every real price change the sync finds
    (see services.auto_print), then scope/release their own DB session.
"""

from __future__ import annotations

import logging

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..extensions import db, scheduler
from .auto_print import auto_print_price_changes
from .loader import RefreshInProgress, bulk_load_guarded

logger = logging.getLogger("spicetown.scheduler")

NIGHTLY_JOB_ID = "nightly_bulk_refresh"
PERIODIC_JOB_ID = "periodic_bulk_refresh"


def _run_refresh(app, *, label: str) -> None:
    """Shared job body: bulk-reload the catalog, then auto-print whatever
    price changes it found. Used by both the nightly and periodic triggers."""
    with app.app_context():
        provider = app.extensions["data_provider"]
        try:
            stats = bulk_load_guarded(
                provider,
                batch_size=app.config["BULK_LOAD_BATCH_SIZE"],
                price_change_threshold=app.config["PRICE_CHANGE_WARN_DELTA"],
                shorten_max_chars=app.config["LABEL_NAME_MAX_CHARS"],
            )
            logger.info("%s refresh complete: %s", label, stats.as_dict())
            printed = auto_print_price_changes(app)
            if not printed.get("skipped"):
                logger.info("%s refresh auto-print: %s", label, printed)
        except RefreshInProgress:
            logger.warning("%s refresh skipped: a refresh is already running", label)
        except Exception:
            logger.exception("%s refresh failed", label)
        finally:
            # Background thread → release the scoped session.
            db.session.remove()


def run_nightly_refresh(app) -> None:
    """Job target: the once-nightly full safety-net refresh."""
    _run_refresh(app, label="nightly")


def run_periodic_refresh(app) -> None:
    """Job target: the frequent (default 30-minute) live sync."""
    _run_refresh(app, label="periodic")


def init_scheduler(app) -> None:
    """Configure + start the background scheduler and register both jobs.

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

    interval_minutes = app.config["REFRESH_INTERVAL_MINUTES"]
    if interval_minutes > 0:
        scheduler.add_job(
            func=run_periodic_refresh,
            trigger=IntervalTrigger(minutes=interval_minutes),
            args=[app],
            id=PERIODIC_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(60, interval_minutes * 60 - 30),
        )
    else:
        logger.info("periodic refresh disabled (STL_REFRESH_INTERVAL_MINUTES=0)")

    if not scheduler.running:
        scheduler.start()

    logger.info(
        "nightly refresh scheduled at %02d:%02d, periodic refresh every %s minute(s) (%s)",
        hour,
        minute,
        interval_minutes if interval_minutes > 0 else "disabled",
        scheduler.timezone,
    )


def shutdown_scheduler() -> None:
    """Stop the scheduler (used in tests / graceful shutdown)."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
