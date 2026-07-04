"""
app/__init__.py — Flask application factory.

create_app() wires together: config -> logging -> DB -> data provider ->
rate limiter -> blueprints. Later stages register the cache service, the print
queue worker, and the APScheduler nightly-refresh job here as well (clearly
marked TODO).

Usage:
    from app import create_app
    app = create_app("production")
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify

from config import Config, get_config
from .extensions import db
from .providers import build_provider
from .services.cache import CacheService
from .services.label import LabelSpec
from .services.loader import bulk_load_guarded, RefreshInProgress
from .services.print_queue import PrintQueue
from .services.printer import build_printer
from .services.ratelimit import get_default_bucket
from .services.scheduler import init_scheduler
from .services.search import SearchService
from .utils.logging_config import configure_logging


def create_app(
    config_name: str | None = None,
    config_object: type[Config] | None = None,
    *,
    start_background: bool = True,
) -> Flask:
    """Build and return a configured Flask app.

    Parameters
    ----------
    config_name:
        One of 'development' | 'production' | 'testing'. Resolved via
        config.get_config() if `config_object` is not provided.
    config_object:
        Explicit Config subclass (used by tests to inject overrides).
    start_background:
        When False, skip the startup bulk load and the APScheduler nightly job.
        CLI tasks (manage.py) pass False so a quick `count` doesn't trigger a
        full catalog load or spin up the scheduler thread.
    """
    app = Flask(__name__)

    cfg = config_object or get_config(config_name)
    app.config.from_object(cfg)

    # 1) Logging first so everything below is captured.
    configure_logging(cfg)
    app.logger = logging.getLogger("spicetown.flask")
    app.logger.info("Starting Spice Town Labels (env=%s)", getattr(cfg, "ENV", "?"))

    # 2) Database.
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _apply_micro_migrations(app)
        app.logger.info("SQLite schema ensured at %s", cfg.SQLALCHEMY_DATABASE_URI)

    # 3) Shared token-bucket rate limiter (10 req/s, burst 20 by default).
    bucket = get_default_bucket(cfg.RATE_LIMIT_RATE, cfg.RATE_LIMIT_BURST)
    app.extensions["rate_bucket"] = bucket
    app.logger.info(
        "Rate limiter ready: rate=%.1f/s burst=%d",
        cfg.RATE_LIMIT_RATE,
        cfg.RATE_LIMIT_BURST,
    )

    # 4) Data provider (file | toast) selected purely by config.
    provider = build_provider(cfg)
    app.extensions["data_provider"] = provider
    app.logger.info("Data provider initialised: %s", provider.name)

    # 5) SQLite-first cache service (TTL + miss-rate monitoring).
    cache = CacheService(
        provider,
        standard_ttl=cfg.CACHE_TTL_STANDARD_SECONDS,
        flagged_ttl=cfg.CACHE_TTL_FLAGGED_SECONDS,
        miss_window_seconds=cfg.CACHE_MISS_WINDOW_SECONDS,
        miss_warn_ratio=cfg.CACHE_MISS_WARN_RATIO,
        price_change_threshold=cfg.PRICE_CHANGE_WARN_DELTA,
        shorten_max_chars=cfg.LABEL_NAME_MAX_CHARS,
    )
    app.extensions["cache"] = cache
    app.logger.info(
        "Cache ready: TTL standard=%ds flagged=%ds; miss-warn=%.0f%% over %ds",
        cfg.CACHE_TTL_STANDARD_SECONDS,
        cfg.CACHE_TTL_FLAGGED_SECONDS,
        cfg.CACHE_MISS_WARN_RATIO * 100,
        cfg.CACHE_MISS_WINDOW_SECONDS,
    )

    # 5b) Fuzzy search service (rapidfuzz) for unrecognized barcodes (Stage 4).
    search = SearchService(
        limit=cfg.SEARCH_LIMIT,
        name_score_cutoff=cfg.SEARCH_NAME_SCORE_CUTOFF,
        upc_score_cutoff=cfg.SEARCH_UPC_SCORE_CUTOFF,
    )
    app.extensions["search"] = search
    app.logger.info(
        "Fuzzy search ready: limit=%d name_cutoff=%.0f upc_cutoff=%.0f",
        cfg.SEARCH_LIMIT,
        cfg.SEARCH_NAME_SCORE_CUTOFF,
        cfg.SEARCH_UPC_SCORE_CUTOFF,
    )

    # 6) Blueprints.
    from .routes.api import bp as api_bp
    from .routes.bridge import bp as bridge_bp
    from .routes.views import bp as views_bp

    app.register_blueprint(api_bp)
    app.register_blueprint(bridge_bp)
    app.register_blueprint(views_bp)

    # 7) Error handlers -> JSON for API, logged to rotating files.
    _register_error_handlers(app)

    # 8) Printing: transport + label spec + single-worker print queue.
    printer = build_printer(cfg)
    app.extensions["printer"] = printer
    label_spec = LabelSpec.for_media(
        cfg.LABEL_SIZE,
        dpi=cfg.LABEL_DPI,
        length_px=cfg.LABEL_LENGTH_PX,
    )
    app.extensions["label_spec"] = label_spec
    print_queue = PrintQueue(
        app,
        printer,
        label_spec,
        maxsize=cfg.PRINT_QUEUE_MAXSIZE,
        max_retries=cfg.PRINT_MAX_RETRIES,
        backoff_base=cfg.PRINT_RETRY_BASE,
        backoff_cap=cfg.PRINT_RETRY_CAP,
    )
    app.extensions["print_queue"] = print_queue
    app.logger.info(
        "Print pipeline ready: transport=%s label=%s (%dx%d px @ %ddpi)",
        printer.name,
        cfg.LABEL_SIZE,
        label_spec.width_px,
        label_spec.height_px,
        label_spec.dpi,
    )

    # 9) Background work: startup bulk load + nightly refresh + print worker.
    # In remote print mode the store's bridge agent drains the job queue via
    # /api/bridge, so no in-process worker is started.
    remote_mode = getattr(cfg, "PRINT_MODE", "local") == "remote"
    if remote_mode:
        app.logger.info(
            "print mode=remote: jobs queue in SQLite for the store print bridge%s",
            "" if getattr(cfg, "BRIDGE_TOKEN", "") else
            " (WARNING: STL_BRIDGE_TOKEN unset — bridge API disabled)",
        )
    if start_background:
        _startup_bulk_load(app, provider)
        init_scheduler(app)
        if cfg.ENABLE_PRINT_WORKER and not remote_mode:
            print_queue.start()
            _register_print_worker_shutdown(app, print_queue)

    return app


def _apply_micro_migrations(app: Flask) -> None:
    """Additive column migrations create_all() can't do on existing tables."""
    from sqlalchemy import inspect, text

    try:
        cols = {c["name"] for c in inspect(db.engine).get_columns("print_jobs")}
        if "claimed_at" not in cols:
            db.session.execute(
                text("ALTER TABLE print_jobs ADD COLUMN claimed_at DATETIME")
            )
            db.session.commit()
            app.logger.info("migrated: print_jobs.claimed_at added")
    except Exception:  # pragma: no cover - defensive
        app.logger.exception("micro-migration check failed")


def _register_print_worker_shutdown(app: Flask, print_queue) -> None:
    """Stop the worker cleanly on interpreter exit."""
    import atexit

    atexit.register(print_queue.stop)


def _startup_bulk_load(app: Flask, provider) -> None:
    """Warm SQLite from the data source at boot (per the rate-limit strategy:
    bulk-load at startup, NOT on per-scan demand)."""
    if not app.config.get("LOAD_ON_STARTUP", True):
        app.logger.info("startup bulk load disabled (LOAD_ON_STARTUP=false)")
        return
    with app.app_context():
        try:
            stats = bulk_load_guarded(
                provider,
                batch_size=app.config["BULK_LOAD_BATCH_SIZE"],
                price_change_threshold=app.config["PRICE_CHANGE_WARN_DELTA"],
                shorten_max_chars=app.config["LABEL_NAME_MAX_CHARS"],
            )
            app.logger.info("startup bulk load: %s", stats.as_dict())
        except RefreshInProgress:
            app.logger.warning("startup bulk load skipped: refresh already running")
        except FileNotFoundError as exc:
            app.logger.error("startup bulk load skipped: %s", exc)
        except Exception:
            app.logger.exception("startup bulk load failed")


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(404)
    def _not_found(err):  # noqa: ANN001
        return jsonify({"error": "not_found", "message": str(err)}), 404

    @app.errorhandler(500)
    def _server_error(err):  # noqa: ANN001
        app.logger.exception("unhandled 500: %s", err)
        return jsonify({"error": "server_error", "message": "internal error"}), 500
