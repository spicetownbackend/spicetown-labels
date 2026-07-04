"""
app/providers/toast_provider.py — ToastDataProvider (STUB).

This is a fully-wired *skeleton* for the Toast POS integration. The control
flow, rate limiting, and exponential backoff are real and correct; only the
actual HTTP/OAuth2 calls are stubbed and marked with TODO.

To activate Toast in production:
    1. Obtain OAuth2 client credentials from Toast (Integrations console).
    2. Set STL_TOAST_CLIENT_ID / STL_TOAST_CLIENT_SECRET / STL_TOAST_RESTAURANT_GUID.
    3. Set STL_DATA_PROVIDER=toast.
    4. Implement the TODO blocks below.

Every outbound call is gated by the shared token bucket (10 req/s, burst 20)
and retried with exponential backoff + jitter on 429 / 5xx. This guarantees we
never overrun Toast's limits regardless of how many scans arrive.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

from ..services.ratelimit import (
    RetryableError,
    RETRYABLE_STATUS,
    TokenBucket,
    backoff_retry,
    get_default_bucket,
)
from .base import DataProvider, ProductRecord

logger = logging.getLogger("spicetown.provider.toast")


class ToastDataProvider(DataProvider):
    """OAuth2-backed Toast POS provider — STUB implementation.

    The public methods mirror FileDataProvider so the rest of the system is
    agnostic to which provider is active.
    """

    name = "toast"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        api_base: str,
        restaurant_guid: str,
        bucket: TokenBucket | None = None,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        backoff_max_retries: int = 5,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = api_base.rstrip("/")
        self.restaurant_guid = restaurant_guid

        # Share the process-wide bucket so Toast + any other source collectively
        # respect the 10 req/s ceiling.
        self._bucket = bucket or get_default_bucket()
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._backoff_max_retries = backoff_max_retries

        # OAuth2 token cache.
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    # ── credentials ────────────────────────────────────────────────────────
    def _credentials_present(self) -> bool:
        return bool(self.client_id and self.client_secret and self.restaurant_guid)

    def _ensure_token(self) -> str:
        """Return a valid OAuth2 access token, refreshing if expired.

        TODO(toast-oauth): Implement the client-credentials grant:

            POST {api_base}/authentication/v1/authentication/login
            body = {
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
                "userAccessType": "TOAST_MACHINE_CLIENT",
            }
        Parse `token.accessToken` and `token.expiresIn`; cache both. Wrap the
        POST with `self._rate_limited_call(...)` so even auth respects limits.
        """
        now = time.monotonic()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        if not self._credentials_present():
            raise NotImplementedError(
                "ToastDataProvider is a stub: set STL_TOAST_CLIENT_ID / "
                "STL_TOAST_CLIENT_SECRET / STL_TOAST_RESTAURANT_GUID and "
                "implement _ensure_token()."
            )

        # TODO(toast-oauth): replace the line below with a real token request.
        raise NotImplementedError(
            "Toast OAuth2 client-credentials flow not yet implemented."
        )

    # ── transport ────────────────────────────────────────────────────────────
    def _rate_limited_call(self, method: str, path: str, **kwargs):
        """Single choke point for ALL Toast HTTP traffic.

        Acquires a token from the shared bucket (blocking up to a short timeout)
        and performs the request inside the backoff-retry wrapper. Non-2xx
        responses in RETRYABLE_STATUS raise RetryableError to trigger backoff.

        TODO(toast-http): Implement the actual `requests` call:

            import requests
            url = f"{self.api_base}{path}"
            headers = {
                "Authorization": f"Bearer {self._ensure_token()}",
                "Toast-Restaurant-External-ID": self.restaurant_guid,
            }
            resp = requests.request(method, url, headers=headers, timeout=10, **kwargs)
            if resp.status_code in RETRYABLE_STATUS:
                raise RetryableError(f"toast {resp.status_code}", status=resp.status_code)
            resp.raise_for_status()
            return resp.json()
        """

        @backoff_retry(
            max_retries=self._backoff_max_retries,
            base=self._backoff_base,
            cap=self._backoff_cap,
            retry_on=(RetryableError,),
            jitter="full",
        )
        def _do():
            # Token bucket gate — never exceed 10 req/s (burst 20).
            if not self._bucket.acquire(1, blocking=True, timeout=5.0):
                # Treat starvation as retryable so backoff smooths the spike.
                raise RetryableError("rate-limiter timeout", status=429)

            # TODO(toast-http): perform the real request here and return JSON.
            raise NotImplementedError(
                f"Toast HTTP transport not implemented ({method} {path})."
            )

        return _do()

    # ── DataProvider interface ────────────────────────────────────────────────
    def health_check(self) -> bool:
        """Cheap reachability probe.

        TODO(toast-health): GET {api_base}/.../config and return resp.ok.
        For the stub we simply report whether credentials are configured.
        """
        if not self._credentials_present():
            logger.warning("Toast credentials not configured; provider is a stub.")
            return False
        # TODO(toast-health): perform a lightweight authenticated GET.
        return False

    def fetch_all(self) -> Iterator[ProductRecord]:
        """Stream every menu item for the nightly bulk refresh.

        TODO(toast-menu): Page through the Toast Menus API:
            GET {api_base}/menus/v2/menus  (or config/v2/menuItems)
        Map each item -> ProductRecord(upc=..., name=..., price=..., ...).
        Yield records as pages arrive (do NOT build one giant list) so 10k+
        items stream with bounded memory. Each page fetch must go through
        self._rate_limited_call(...).
        """
        raise NotImplementedError(
            "ToastDataProvider.fetch_all() is a stub — implement Toast Menus paging."
        )
        # Unreachable, but documents intended shape for implementers:
        # yield ProductRecord(upc="...", name="...", price=0.0)

    def fetch_by_upc(self, upc: str) -> ProductRecord | None:
        """Look up one product by UPC (cache-miss fallback).

        TODO(toast-lookup): Query the Toast item endpoint filtered by UPC/SKU,
        e.g. GET {api_base}/config/v2/menuItems?upc={upc}. Return the mapped
        ProductRecord or None if not found. The call already flows through the
        rate limiter + backoff via _rate_limited_call.
        """
        _ = self._rate_limited_call  # referenced so implementers see the path
        raise NotImplementedError(
            "ToastDataProvider.fetch_by_upc() is a stub — implement Toast item lookup."
        )
