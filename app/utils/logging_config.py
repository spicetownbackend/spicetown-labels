"""
app/utils/logging_config.py — Rotating file logging.

All errors and warnings go to rotating files (logging.handlers.RotatingFileHandler)
under the configured LOG_DIR, plus a console stream in development.

Two files:
  * spicetown.log  — everything at LOG_LEVEL and above (app activity).
  * errors.log     — WARNING and above only (quick triage feed).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Sentinel so repeated create_app() calls (tests) don't stack handlers.
_CONFIGURED = False


def configure_logging(config) -> logging.Logger:
    """Attach rotating file handlers to the root `spicetown` logger.

    Idempotent: safe to call once per process. Returns the package logger.
    """
    global _CONFIGURED

    log_dir = Path(config.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO)

    root = logging.getLogger("spicetown")
    root.setLevel(level)

    if _CONFIGURED and root.handlers:
        # Already configured this process: only refresh the level. Crucially we
        # do NOT touch `propagate` here so tests can opt into log capture.
        root.setLevel(level)
        return root

    # First-time configuration. Suppress propagation so records don't double
    # emit via the root logger in production.
    root.propagate = False

    # Clear any pre-existing handlers (defensive for re-imports under reload).
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # 1) Main rotating log — all activity at configured level.
    main_handler = RotatingFileHandler(
        log_dir / "spicetown.log",
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    main_handler.setLevel(level)
    main_handler.setFormatter(formatter)
    root.addHandler(main_handler)

    # 2) Errors-only rotating log — WARNING+ for fast triage.
    err_handler = RotatingFileHandler(
        log_dir / "errors.log",
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(formatter)
    root.addHandler(err_handler)

    # 3) Console (stdout) — useful in dev and under launchd's captured output.
    if getattr(config, "DEBUG", False):
        console = logging.StreamHandler(stream=sys.stdout)
        console.setLevel(level)
        console.setFormatter(formatter)
        root.addHandler(console)

    _CONFIGURED = True
    root.info(
        "logging configured: level=%s dir=%s maxBytes=%s backups=%s",
        config.LOG_LEVEL,
        log_dir,
        config.LOG_MAX_BYTES,
        config.LOG_BACKUP_COUNT,
    )
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger (e.g. get_logger('provider.file'))."""
    return logging.getLogger(f"spicetown.{name}")
