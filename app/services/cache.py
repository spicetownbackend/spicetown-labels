"""
app/services/cache.py — SQLite-first cache with TTL + miss-rate monitoring.

Lookup policy ("SQLite is the primary lookup layer"):
  1. Read the product from SQLite by indexed UPC.
  2. If present AND fresh (within TTL) -> HIT, return immediately. No data
     source is ever contacted for a UPC that is cached and fresh.
  3. If absent or stale -> MISS. Optionally fetch the single UPC from the data
     provider (rate-limited inside the provider for external sources), upsert
     it, and return the refreshed row. If the upstream has nothing, return the
     stale row if we have one.

TTL (from config):
  * standard prices : 24h
  * flagged prices  : 1h   (Product.price_flagged -> volatile, re-verify sooner)

Miss-rate monitoring:
  A sliding 5-minute window tracks hit/miss events. When the miss ratio exceeds
  5% (and enough samples exist) a throttled WARNING is logged — a signal that
  the cache is being bypassed too often (cold cache, bad feed, scan of items
  not in catalog, etc.).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from ..extensions import db
from ..models import Product
from ..providers.base import DataProvider
from .loader import upsert_record

logger = logging.getLogger("spicetown.cache")

LookupOutcome = Literal["hit", "refreshed", "stale", "miss"]


@dataclass
class CacheResult:
    """Return value of CacheService.get()."""

    product: Product | None
    outcome: LookupOutcome

    @property
    def found(self) -> bool:
        return self.product is not None


class MissRateMonitor:
    """Thread-safe sliding-window monitor of cache hit/miss events.

    Logs a throttled WARNING when the miss ratio in the window exceeds the
    configured threshold. Uses a monotonic clock so it is immune to wall-clock
    jumps.
    """

    def __init__(
        self,
        *,
        window_seconds: int,
        warn_ratio: float,
        min_samples: int = 20,
        warn_cooldown_seconds: float = 60.0,
    ) -> None:
        self.window_seconds = window_seconds
        self.warn_ratio = warn_ratio
        self.min_samples = min_samples
        self.warn_cooldown_seconds = warn_cooldown_seconds

        self._events: deque[tuple[float, bool]] = deque()  # (ts, is_miss)
        self._lock = threading.Lock()
        self._last_warn = 0.0
        self._total_hits = 0
        self._total_misses = 0

    def _evict_locked(self, now: float) -> None:
        cutoff = now - self.window_seconds
        events = self._events
        while events and events[0][0] < cutoff:
            events.popleft()

    def record(self, is_hit: bool) -> float:
        """Record one event; return the current window miss ratio."""
        now = time.monotonic()
        with self._lock:
            if is_hit:
                self._total_hits += 1
            else:
                self._total_misses += 1

            self._events.append((now, not is_hit))
            self._evict_locked(now)

            total = len(self._events)
            misses = sum(1 for _, m in self._events if m)
            ratio = (misses / total) if total else 0.0

            if (
                total >= self.min_samples
                and ratio > self.warn_ratio
                and (now - self._last_warn) >= self.warn_cooldown_seconds
            ):
                self._last_warn = now
                logger.warning(
                    "cache miss rate %.1f%% over last %ds (%d misses / %d lookups) "
                    "exceeds %.1f%% threshold",
                    ratio * 100,
                    self.window_seconds,
                    misses,
                    total,
                    self.warn_ratio * 100,
                )
            return ratio

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            self._evict_locked(now)
            total = len(self._events)
            misses = sum(1 for _, m in self._events if m)
            return {
                "window_seconds": self.window_seconds,
                "warn_ratio": self.warn_ratio,
                "window_lookups": total,
                "window_misses": misses,
                "window_miss_ratio": round((misses / total) if total else 0.0, 4),
                "lifetime_hits": self._total_hits,
                "lifetime_misses": self._total_misses,
            }


class CacheService:
    """SQLite-first product cache."""

    def __init__(
        self,
        provider: DataProvider,
        *,
        standard_ttl: int,
        flagged_ttl: int,
        miss_window_seconds: int,
        miss_warn_ratio: float,
        price_change_threshold: float = 0.20,
        shorten_max_chars: int = 22,
    ) -> None:
        self.provider = provider
        self.standard_ttl = standard_ttl
        self.flagged_ttl = flagged_ttl
        self.price_change_threshold = price_change_threshold
        self.shorten_max_chars = shorten_max_chars
        self.monitor = MissRateMonitor(
            window_seconds=miss_window_seconds,
            warn_ratio=miss_warn_ratio,
        )

    # ── lookup ────────────────────────────────────────────────────────────────
    def get(self, upc: str, *, allow_remote: bool = True) -> CacheResult:
        """Look up a product by UPC. See module docstring for the policy."""
        upc = (upc or "").strip()
        product = db.session.query(Product).filter_by(upc=upc).one_or_none()

        # Fresh hit — never contact the data source.
        if product is not None and product.is_fresh(self.standard_ttl, self.flagged_ttl):
            self.monitor.record(is_hit=True)
            return CacheResult(product, "hit")

        # Miss: either absent or stale.
        self.monitor.record(is_hit=False)

        if not allow_remote:
            return CacheResult(product, "stale" if product is not None else "miss")

        refreshed = self._refresh_one(upc)
        if refreshed is not None:
            return CacheResult(refreshed, "refreshed")

        # Upstream had nothing: return the stale copy if we have one.
        return CacheResult(product, "stale" if product is not None else "miss")

    def _refresh_one(self, upc: str) -> Product | None:
        """Fetch a single UPC from the provider and upsert it.

        External providers gate this call through the token bucket + backoff
        internally; the file provider just reads disk. Errors are swallowed
        (logged) so a flaky upstream degrades to a stale/None result rather
        than failing the scan.
        """
        try:
            rec = self.provider.fetch_by_upc(upc)
        except NotImplementedError:
            # e.g. Toast stub before implementation — treat as "no upstream".
            logger.debug("provider %s cannot fetch_by_upc yet", self.provider.name)
            return None
        except Exception:
            logger.exception("provider %s fetch_by_upc(%s) failed", self.provider.name, upc)
            return None

        if rec is None:
            return None

        try:
            product = upsert_record(
                rec,
                source=self.provider.name,
                price_change_threshold=self.price_change_threshold,
                shorten_max_chars=self.shorten_max_chars,
            )
            db.session.commit()
            return product
        except Exception:
            db.session.rollback()
            logger.exception("cache upsert failed for upc=%s", upc)
            return None

    # ── metrics ────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        return self.monitor.snapshot()
