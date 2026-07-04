"""
app/services/ratelimit.py — Thread-safe token-bucket rate limiter.

Wraps ALL external data-source calls (Toast API, etc.) so we never exceed the
upstream's tolerance. SQLite is the primary lookup layer; this limiter only
gates the rare cache-miss path that actually reaches out to the network.

Defaults (from config): max 10 req/s sustained, burst 20.

Design:
- Classic token bucket. The bucket holds up to `capacity` tokens and refills
  continuously at `rate` tokens/second. Each call consumes N tokens.
- `time.monotonic()` avoids wall-clock jumps (NTP/DST) skewing the refill.
- A single `threading.Lock` guards token state, so the limiter is safe to
  share across the Flask request threads, the print-queue worker, and the
  APScheduler refresh job.

Also included: `backoff_retry`, exponential backoff *with jitter* for 429 / 5xx
responses, which composes with the limiter on the external-call path.
"""

from __future__ import annotations

import functools
import logging
import random
import threading
import time
from typing import Callable, Iterable, TypeVar

logger = logging.getLogger("spicetown.ratelimit")

T = TypeVar("T")


class RateLimitExceeded(Exception):
    """Raised when tokens cannot be acquired within the allotted time."""


class TokenBucket:
    """A thread-safe token-bucket rate limiter.

    Parameters
    ----------
    rate:
        Sustained refill rate in tokens per second (e.g. 10.0).
    capacity:
        Maximum number of tokens the bucket can hold — the burst size (e.g. 20).
    initial_tokens:
        Starting token count. Defaults to a full bucket (`capacity`).
    name:
        Label used in log lines (handy when multiple buckets exist).
    """

    def __init__(
        self,
        rate: float,
        capacity: int,
        *,
        initial_tokens: float | None = None,
        name: str = "default",
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")

        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity if initial_tokens is None else initial_tokens)
        self._name = name
        self._lock = threading.Lock()
        self._last_refill = time.monotonic()

    # ── internals ─────────────────────────────────────────────────────────────
    def _refill_locked(self) -> None:
        """Add tokens accrued since the last refill. Caller must hold the lock."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    # ── public API ──────────────────────────────────────────────────────────
    @property
    def available_tokens(self) -> float:
        """Best-effort snapshot of currently available tokens (after refill)."""
        with self._lock:
            self._refill_locked()
            return self._tokens

    def try_acquire(self, tokens: int = 1) -> bool:
        """Non-blocking acquire. Returns True if tokens were consumed."""
        if tokens <= 0:
            return True
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(
        self,
        tokens: int = 1,
        *,
        blocking: bool = True,
        timeout: float | None = None,
    ) -> bool:
        """Acquire `tokens`, optionally blocking until they are available.

        Returns True on success. If `blocking` is False, returns False
        immediately when insufficient tokens. If `timeout` elapses while
        blocking, returns False (caller decides whether to raise).
        """
        if tokens <= 0:
            return True
        if tokens > self.capacity:
            # A request larger than the bucket could never be satisfied.
            raise ValueError(
                f"requested {tokens} tokens exceeds capacity {self.capacity}"
            )

        deadline = None if timeout is None else (time.monotonic() + timeout)

        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                # How long until enough tokens accrue?
                deficit = tokens - self._tokens
                wait = deficit / self.rate

            if not blocking:
                return False

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)

            # Sleep the *minimum* of (time-to-tokens, time-to-deadline). A small
            # cap keeps us responsive and avoids oversleeping past a refill.
            time.sleep(max(0.0, min(wait, 0.25)))

    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        """Use the bucket as a decorator: each call consumes one token."""

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not self.acquire(1, blocking=True):
                raise RateLimitExceeded(f"bucket '{self._name}' exhausted")
            return func(*args, **kwargs)

        return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Exponential backoff with jitter (429 / 5xx)
# ─────────────────────────────────────────────────────────────────────────────
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryableError(Exception):
    """Wrap a retryable upstream failure, optionally with a status code."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def compute_backoff(
    attempt: int,
    *,
    base: float,
    cap: float,
    jitter: str = "full",
) -> float:
    """Return the sleep duration for a given retry `attempt` (0-indexed).

    Uses exponential growth (base * 2**attempt) clamped to `cap`, then applies
    'full' jitter: a uniform random value in [0, computed]. Full jitter is the
    AWS-recommended strategy to avoid thundering-herd retries.
    """
    raw = min(cap, base * (2 ** attempt))
    if jitter == "full":
        return random.uniform(0.0, raw)
    if jitter == "equal":
        return raw / 2 + random.uniform(0.0, raw / 2)
    return raw  # "none"


def backoff_retry(
    *,
    max_retries: int,
    base: float,
    cap: float,
    retry_on: Iterable[type[BaseException]] = (RetryableError,),
    jitter: str = "full",
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: retry a function with exponential backoff + jitter.

    The wrapped function should raise one of `retry_on` (e.g. `RetryableError`
    with `.status` in {429, 5xx}) to signal a retryable failure. Non-listed
    exceptions propagate immediately.
    """
    retry_on = tuple(retry_on)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except retry_on as exc:  # type: ignore[misc]
                    if attempt >= max_retries:
                        logger.error(
                            "backoff: giving up after %d retries (%s)",
                            attempt,
                            exc,
                        )
                        raise
                    delay = compute_backoff(attempt, base=base, cap=cap, jitter=jitter)
                    status = getattr(exc, "status", None)
                    logger.warning(
                        "backoff: attempt %d failed (status=%s); sleeping %.3fs",
                        attempt + 1,
                        status,
                        delay,
                    )
                    time.sleep(delay)
                    attempt += 1

        return wrapper

    return decorator


# Process-wide default bucket. Initialised lazily from config so it is created
# exactly once and shared by every external-call site.
_default_bucket: TokenBucket | None = None
_default_lock = threading.Lock()


def get_default_bucket(rate: float | None = None, capacity: int | None = None) -> TokenBucket:
    """Return the shared external-API bucket, creating it on first use.

    The app factory calls this once with config values; subsequent callers omit
    the args and receive the same instance.
    """
    global _default_bucket
    with _default_lock:
        if _default_bucket is None:
            _default_bucket = TokenBucket(
                rate=rate if rate is not None else 10.0,
                capacity=capacity if capacity is not None else 20,
                name="external-api",
            )
        return _default_bucket


def reset_default_bucket() -> None:
    """Test helper: drop the shared bucket so the next call rebuilds it."""
    global _default_bucket
    with _default_lock:
        _default_bucket = None
