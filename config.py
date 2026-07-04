"""
config.py — Central configuration for the Spice Town label system.

Every tunable lives here so that switching data providers, changing cache
TTLs, or pointing at a different printer is a *config change only* — no code
edits required (per the rate-limit + adapter strategy).

Values are read from environment variables (optionally via a .env file) with
sensible defaults for the Mac Mini M1 deployment.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    # python-dotenv is optional at import time; load .env if present.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience only
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("STL_DATA_DIR", BASE_DIR / "data"))
LOG_DIR = Path(os.getenv("STL_LOG_DIR", BASE_DIR / "logs"))

# Ensure runtime directories exist (idempotent, cheap).
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class Config:
    """Base configuration. Subclasses tweak per-environment behaviour."""

    # ── Flask core ───────────────────────────────────────────────────────────
    SECRET_KEY = os.getenv("STL_SECRET_KEY", "dev-only-change-me")
    JSON_SORT_KEYS = False

    # ── Database (SQLite primary lookup layer) ────────────────────────────────
    # Single file DB on the Mac Mini's local disk. WAL mode is enabled at
    # connect-time (see app/extensions.py) for concurrent read performance.
    DB_PATH = Path(os.getenv("STL_DB_PATH", BASE_DIR / "spicetown.db"))
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "STL_DATABASE_URI", f"sqlite:///{DB_PATH}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        # SQLite + threads: required because the print-queue worker and the
        # APScheduler refresh job run on separate threads.
        "connect_args": {"check_same_thread": False},
        "pool_pre_ping": True,
    }

    # ── Data provider selection (adapter pattern) ─────────────────────────────
    # "file"  -> FileDataProvider  (reads products.csv / products.json)
    # "toast" -> ToastDataProvider  (OAuth2 stub; Stage 2+)
    DATA_PROVIDER = os.getenv("STL_DATA_PROVIDER", "file").strip().lower()

    # FileDataProvider settings
    PRODUCTS_FILE = Path(
        os.getenv("STL_PRODUCTS_FILE", DATA_DIR / "products.csv")
    )

    # ToastDataProvider settings (stub — see app/providers/toast_provider.py)
    TOAST_CLIENT_ID = os.getenv("STL_TOAST_CLIENT_ID", "")
    TOAST_CLIENT_SECRET = os.getenv("STL_TOAST_CLIENT_SECRET", "")
    TOAST_API_BASE = os.getenv(
        "STL_TOAST_API_BASE", "https://ws-api.toasttab.com"
    )
    TOAST_RESTAURANT_GUID = os.getenv("STL_TOAST_RESTAURANT_GUID", "")

    # ── Cache TTL strategy ────────────────────────────────────────────────────
    # SQLite is authoritative; the data source is only consulted when a UPC is
    # missing or its cached record has expired.
    CACHE_TTL_STANDARD_SECONDS = _as_int(
        os.getenv("STL_CACHE_TTL_STANDARD"), 24 * 60 * 60  # 24 hours
    )
    CACHE_TTL_FLAGGED_SECONDS = _as_int(
        os.getenv("STL_CACHE_TTL_FLAGGED"), 60 * 60  # 1 hour
    )

    # Cache-miss monitoring: warn if miss-rate exceeds threshold in a window.
    CACHE_MISS_WINDOW_SECONDS = _as_int(
        os.getenv("STL_CACHE_MISS_WINDOW"), 5 * 60  # 5 minutes
    )
    CACHE_MISS_WARN_RATIO = _as_float(
        os.getenv("STL_CACHE_MISS_WARN_RATIO"), 0.05  # 5%
    )

    # ── Token-bucket rate limiter (wraps ALL external API calls) ──────────────
    RATE_LIMIT_RATE = _as_float(os.getenv("STL_RATE_LIMIT_RATE"), 10.0)  # req/s
    RATE_LIMIT_BURST = _as_int(os.getenv("STL_RATE_LIMIT_BURST"), 20)  # bucket cap

    # Exponential backoff with jitter (429 / 5xx responses).
    BACKOFF_BASE_SECONDS = _as_float(os.getenv("STL_BACKOFF_BASE"), 0.5)
    BACKOFF_MAX_SECONDS = _as_float(os.getenv("STL_BACKOFF_MAX"), 30.0)
    BACKOFF_MAX_RETRIES = _as_int(os.getenv("STL_BACKOFF_MAX_RETRIES"), 5)

    # ── Suspicious price-change flagging ──────────────────────────────────────
    PRICE_CHANGE_WARN_DELTA = _as_float(
        os.getenv("STL_PRICE_CHANGE_WARN_DELTA"), 0.20  # 20%
    )

    # ── Print mode: local (in-process worker) vs remote (store print bridge) ──
    # "local"  -> jobs are printed by this process (cups/brother_ql/file/null).
    # "remote" -> jobs stay `queued` in SQLite; a print-bridge agent running at
    #             the store polls /api/bridge, fetches the rendered label, and
    #             drives the printer. Use this when hosting in the cloud.
    PRINT_MODE = os.getenv("STL_PRINT_MODE", "local").strip().lower()
    # Shared secret for the bridge API (required in remote mode; requests must
    # send it as `Authorization: Bearer <token>` or `X-Bridge-Token`).
    BRIDGE_TOKEN = os.getenv("STL_BRIDGE_TOKEN", "")
    # A job claimed by a bridge that never reports back is re-queued after this.
    BRIDGE_STALE_SECONDS = _as_int(os.getenv("STL_BRIDGE_STALE_SECONDS"), 300)

    # ── Printing (Brother QL-810W) ────────────────────────────────────────────
    # Transport: "cups" | "brother_ql" | "file" | "null".
    #   cups       -> `lp -d <printer>` (default on the Mac Mini via CUPS driver)
    #   brother_ql -> raster over USB/network using the brother_ql library
    #   file       -> write label PNGs to PRINT_SPOOL_DIR (hardware-free dev)
    #   null       -> discard (unit tests)
    PRINT_TRANSPORT = os.getenv("STL_PRINT_TRANSPORT", "cups").strip().lower()
    CUPS_PRINTER_NAME = os.getenv("STL_CUPS_PRINTER_NAME", "Brother_QL_810W")
    CUPS_LP_OPTIONS = [
        o for o in os.getenv("STL_CUPS_LP_OPTIONS", "").split(",") if o.strip()
    ]
    # Default `lp -o fit-to-page`. Set False when using an explicit `scaling=`
    # lp option (auto-disabled too when CUPS_LP_OPTIONS contains a scaling).
    CUPS_FIT_TO_PAGE = _as_bool(os.getenv("STL_CUPS_FIT_TO_PAGE"), True)
    LABEL_SIZE = os.getenv("STL_LABEL_SIZE", "62")  # 62mm continuous default
    PRINTER_MODEL = os.getenv("STL_PRINTER_MODEL", "QL-810W")
    PRINTER_BACKEND = os.getenv("STL_PRINTER_BACKEND", "linux_kernel")
    PRINTER_DEVICE = os.getenv("STL_PRINTER_DEVICE", "/dev/usb/lp0")
    PRINT_SPOOL_DIR = Path(os.getenv("STL_PRINT_SPOOL_DIR", BASE_DIR / "spool"))

    # Label rendering geometry.
    LABEL_DPI = _as_int(os.getenv("STL_LABEL_DPI"), 300)
    LABEL_LENGTH_PX = _as_int(os.getenv("STL_LABEL_LENGTH_PX"), 390)  # continuous len

    # ── AI features (Stage 4) ─────────────────────────────────────────────────
    # Character budget for the auto-shortened product name printed on labels.
    LABEL_NAME_MAX_CHARS = _as_int(os.getenv("STL_LABEL_NAME_MAX_CHARS"), 22)
    # Fuzzy search (rapidfuzz) for unrecognized barcodes.
    SEARCH_LIMIT = _as_int(os.getenv("STL_SEARCH_LIMIT"), 5)
    SEARCH_NAME_SCORE_CUTOFF = _as_float(os.getenv("STL_SEARCH_NAME_CUTOFF"), 60.0)
    SEARCH_UPC_SCORE_CUTOFF = _as_float(os.getenv("STL_SEARCH_UPC_CUTOFF"), 70.0)

    # Print job queue worker target: <2s from scan to printed label.
    PRINT_QUEUE_MAXSIZE = _as_int(os.getenv("STL_PRINT_QUEUE_MAXSIZE"), 100)
    PRINT_JOB_TIMEOUT_SECONDS = _as_float(os.getenv("STL_PRINT_TIMEOUT"), 20.0)
    PRINT_MAX_RETRIES = _as_int(os.getenv("STL_PRINT_MAX_RETRIES"), 2)
    PRINT_RETRY_BASE = _as_float(os.getenv("STL_PRINT_RETRY_BASE"), 0.5)
    PRINT_RETRY_CAP = _as_float(os.getenv("STL_PRINT_RETRY_CAP"), 5.0)
    # Start the print worker thread with the app (False for CLI/tests as needed).
    ENABLE_PRINT_WORKER = _as_bool(os.getenv("STL_ENABLE_PRINT_WORKER"), True)

    # ── Bulk load / nightly refresh schedule (APScheduler) ────────────────────
    # Run a full bulk load when the server boots so SQLite is warm immediately.
    LOAD_ON_STARTUP = _as_bool(os.getenv("STL_LOAD_ON_STARTUP"), True)
    # Commit the bulk load in batches of this size (bounds transaction/WAL size).
    BULK_LOAD_BATCH_SIZE = _as_int(os.getenv("STL_BULK_LOAD_BATCH_SIZE"), 500)
    NIGHTLY_REFRESH_HOUR = _as_int(os.getenv("STL_REFRESH_HOUR"), 3)  # 03:00
    NIGHTLY_REFRESH_MINUTE = _as_int(os.getenv("STL_REFRESH_MINUTE"), 0)
    ENABLE_SCHEDULER = _as_bool(os.getenv("STL_ENABLE_SCHEDULER"), True)
    SCHEDULER_TIMEZONE = os.getenv("STL_SCHEDULER_TIMEZONE", "America/New_York")

    # ── Logging (rotating file handlers) ──────────────────────────────────────
    LOG_DIR = LOG_DIR
    LOG_LEVEL = os.getenv("STL_LOG_LEVEL", "INFO").upper()
    LOG_MAX_BYTES = _as_int(os.getenv("STL_LOG_MAX_BYTES"), 5 * 1024 * 1024)  # 5MB
    LOG_BACKUP_COUNT = _as_int(os.getenv("STL_LOG_BACKUP_COUNT"), 7)


class DevelopmentConfig(Config):
    DEBUG = True
    ENV = "development"


class ProductionConfig(Config):
    DEBUG = False
    ENV = "production"


class TestingConfig(Config):
    TESTING = True
    DEBUG = True
    ENV = "testing"
    # In-memory DB + no scheduler/startup-load for fast, isolated tests.
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    ENABLE_SCHEDULER = False
    LOAD_ON_STARTUP = False
    # Discard prints by default; tests opt into 'file' with a tmp spool dir.
    PRINT_TRANSPORT = "null"
    ENABLE_PRINT_WORKER = False


# Registry used by the app factory: create_app("production"), etc.
CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}


def get_config(name: str | None = None) -> type[Config]:
    """Resolve a config class by name (falls back to STL_ENV then 'default')."""
    name = (name or os.getenv("STL_ENV") or "default").strip().lower()
    return CONFIG_MAP.get(name, CONFIG_MAP["default"])
