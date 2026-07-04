"""
Service layer.

Stage 1: rate limiter. Stage 2: cache + loader + scheduler.
Stage 3: label rendering + printer transports + single-worker print queue.
Stage 4: AI name shortening + rapidfuzz fuzzy search.
"""

from .cache import CacheResult, CacheService, MissRateMonitor
from .label import LabelSpec, render_label, render_to_png_bytes
from .loader import (
    LoadStats,
    RefreshInProgress,
    bulk_load,
    bulk_load_guarded,
    is_refresh_running,
    upsert_record,
)
from .print_queue import EnqueueResult, PrintQueue, QueueFull
from .printer import (
    BrotherQLTransport,
    CupsTransport,
    FileTransport,
    NullTransport,
    PrinterError,
    PrinterTransport,
    build_printer,
)
from .ratelimit import (
    RateLimitExceeded,
    RetryableError,
    RETRYABLE_STATUS,
    TokenBucket,
    backoff_retry,
    compute_backoff,
    get_default_bucket,
    reset_default_bucket,
)
from .scheduler import (
    NIGHTLY_JOB_ID,
    init_scheduler,
    run_nightly_refresh,
    shutdown_scheduler,
)
from .search import SearchHit, SearchService
from .shorten import DEFAULT_MAX_CHARS, shorten_name

__all__ = [
    # ratelimit
    "TokenBucket",
    "RateLimitExceeded",
    "RetryableError",
    "RETRYABLE_STATUS",
    "backoff_retry",
    "compute_backoff",
    "get_default_bucket",
    "reset_default_bucket",
    # loader
    "LoadStats",
    "RefreshInProgress",
    "bulk_load",
    "bulk_load_guarded",
    "is_refresh_running",
    "upsert_record",
    # cache
    "CacheService",
    "CacheResult",
    "MissRateMonitor",
    # scheduler
    "NIGHTLY_JOB_ID",
    "init_scheduler",
    "run_nightly_refresh",
    "shutdown_scheduler",
    # label
    "LabelSpec",
    "render_label",
    "render_to_png_bytes",
    # printer
    "PrinterTransport",
    "PrinterError",
    "NullTransport",
    "FileTransport",
    "CupsTransport",
    "BrotherQLTransport",
    "build_printer",
    # print queue
    "PrintQueue",
    "EnqueueResult",
    "QueueFull",
    # search + shorten (Stage 4)
    "SearchService",
    "SearchHit",
    "shorten_name",
    "DEFAULT_MAX_CHARS",
]
