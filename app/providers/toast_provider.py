"""
app/providers/toast_provider.py — ToastDataProvider (Toast standard API).

Pulls the product catalog from Toast's Menus API so the label system syncs
straight from the POS (startup bulk load + nightly refresh — no CSV edits).

Mapping (per the store's Toast setup):
  * ProductRecord.upc   <- menu item `sku` (the store keeps the barcode there)
  * ProductRecord.name  <- item `name`
  * ProductRecord.price <- item `price` (fixed-price items)
  * department          <- the menu group's name (falls back to the menu name)
Items without a name or any stable key (sku/PLU/GUID) are skipped (logged once
per sync). Zero/open-priced items ARE kept — their labels omit the price area.

Activation (config only):
    1. Set STL_TOAST_CLIENT_ID / STL_TOAST_CLIENT_SECRET /
       STL_TOAST_RESTAURANT_GUID (Render: Environment tab — never in git).
    2. Set STL_DATA_PROVIDER=toast.
    3. Verify: `python manage.py provider-check` -> provider=toast healthy=True

Every outbound call is gated by the shared token bucket (10 req/s, burst 20)
and retried with exponential backoff + jitter on 429 / 5xx. An expired OAuth
token (401 mid-flight) is refreshed transparently and the call retried.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator

import requests

from ..services.ratelimit import (
    RetryableError,
    RETRYABLE_STATUS,
    TokenBucket,
    backoff_retry,
    get_default_bucket,
)
from .base import DataProvider, ProductRecord

logger = logging.getLogger("spicetown.provider.toast")

# Refresh the OAuth token this many seconds before Toast says it expires.
_TOKEN_SAFETY_MARGIN = 60.0
# fetch_by_upc reuses the last menus document for this long (the cache-miss
# path is rare; this avoids re-downloading the catalog for burst misses).
_MENUS_DOC_TTL = 60.0
_HTTP_TIMEOUT = 30.0


class ToastAuthError(Exception):
    """Raised when Toast rejects the client credentials."""


class ToastDataProvider(DataProvider):
    """OAuth2-backed Toast POS provider (Menus API)."""

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

        # OAuth2 token cache (lock: refresh may race between the scheduler
        # thread and a request-handler cache miss).
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

        # Short-lived menus-document cache for the per-UPC miss path.
        self._menus_doc: Any = None
        self._menus_doc_at: float = 0.0

    # ── credentials / OAuth2 ──────────────────────────────────────────────────
    def _credentials_present(self) -> bool:
        return bool(self.client_id and self.client_secret and self.restaurant_guid)

    def _ensure_token(self) -> str:
        """Return a valid OAuth2 access token, refreshing if (nearly) expired."""
        with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires_at:
                return self._access_token

            if not self._credentials_present():
                raise ToastAuthError(
                    "Toast credentials missing: set STL_TOAST_CLIENT_ID / "
                    "STL_TOAST_CLIENT_SECRET / STL_TOAST_RESTAURANT_GUID"
                )

            payload = self._http_json(
                "POST",
                "/authentication/v1/authentication/login",
                authenticated=False,
                json={
                    "clientId": self.client_id,
                    "clientSecret": self.client_secret,
                    "userAccessType": "TOAST_MACHINE_CLIENT",
                },
            )
            token = (payload or {}).get("token") or {}
            access = token.get("accessToken")
            expires_in = token.get("expiresIn") or 0
            if not access:
                raise ToastAuthError(f"Toast login returned no token: {payload!r}")

            self._access_token = access
            self._token_expires_at = (
                time.monotonic() + max(float(expires_in) - _TOKEN_SAFETY_MARGIN, 30.0)
            )
            logger.info("Toast OAuth token refreshed (expires_in=%ss)", expires_in)
            return access

    def _invalidate_token(self) -> None:
        with self._token_lock:
            self._access_token = None
            self._token_expires_at = 0.0

    # ── transport ────────────────────────────────────────────────────────────
    def _http_json(self, method: str, path: str, *, authenticated: bool = True, **kwargs):
        """Single choke point for ALL Toast HTTP traffic.

        Acquires a token from the shared bucket and performs the request inside
        the backoff-retry wrapper. 429/5xx raise RetryableError -> backoff with
        jitter. A 401 on an authenticated call invalidates the cached OAuth
        token and retries (fresh login) via the same backoff path.
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

            headers = {"Toast-Restaurant-External-ID": self.restaurant_guid}
            if authenticated:
                headers["Authorization"] = f"Bearer {self._ensure_token()}"

            url = f"{self.api_base}{path}"
            try:
                resp = requests.request(
                    method, url, headers=headers, timeout=_HTTP_TIMEOUT, **kwargs
                )
            except requests.RequestException as exc:
                # Network blips are retryable; DNS/refused resolve on retry too.
                raise RetryableError(f"toast network error: {exc}", status=503) from exc

            if resp.status_code in RETRYABLE_STATUS:
                raise RetryableError(
                    f"toast {resp.status_code} on {path}", status=resp.status_code
                )
            if authenticated and resp.status_code == 401:
                # Token expired server-side — drop it and retry with a new one.
                self._invalidate_token()
                raise RetryableError("toast 401 (token expired)", status=401)
            resp.raise_for_status()
            return resp.json() if resp.content else None

        return _do()

    # ── Menus document → ProductRecords ───────────────────────────────────────
    def _get_menus_document(self, *, max_age: float = 0.0) -> Any:
        """Fetch the published menus (optionally reusing a recent copy)."""
        if (
            max_age > 0
            and self._menus_doc is not None
            and (time.monotonic() - self._menus_doc_at) < max_age
        ):
            return self._menus_doc
        doc = self._http_json("GET", "/menus/v2/menus")
        self._menus_doc = doc
        self._menus_doc_at = time.monotonic()
        return doc

    def _iter_items(self, doc: Any) -> Iterator[tuple[dict, str]]:
        """Yield (menu_item, department) walking menus -> groups (recursively)."""

        def walk_group(group: dict, department: str) -> Iterator[tuple[dict, str]]:
            dept = (group.get("name") or department or "").strip() or department
            for item in group.get("menuItems") or []:
                yield item, dept
            for sub in group.get("menuGroups") or []:
                yield from walk_group(sub, dept)

        menus = (doc or {}).get("menus") or []
        for menu in menus:
            menu_name = (menu.get("name") or "").strip()
            for group in menu.get("menuGroups") or []:
                yield from walk_group(group, menu_name)

    def _to_record(self, item: dict, department: str) -> ProductRecord | None:
        """Map one Toast menu item to a ProductRecord (None -> skip + reason)."""
        name = (item.get("name") or "").strip()
        if not name:
            return None
        try:
            price = float(item.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        # Open-priced / unpriced items (price 0) are kept: their labels simply
        # omit the price area, so staff can still print name+barcode tags.

        # Barcode key: sku (the store keeps the UPC there), else PLU, else a
        # stable code derived from the Toast GUID. Items without a real
        # barcode are still loadable — staff find them by NAME SEARCH in the
        # scanner UI, and the printed label carries the internal code as a
        # scannable Code128, so the label itself scans from then on.
        sku = (str(item.get("sku") or "")).strip()
        plu = (str(item.get("plu") or "")).strip()
        guid = (str(item.get("guid") or "")).strip()
        upc = sku or plu or (f"TG-{guid.replace('-', '')[:12].upper()}" if guid else "")
        if not upc:
            return None  # nothing stable to key on

        return ProductRecord(
            upc=upc,
            name=name,
            price=price,
            sku=sku or None,
            department=department or None,
            extra={"toast_guid": item.get("guid")},
        )

    # ── DataProvider interface ────────────────────────────────────────────────
    def health_check(self) -> bool:
        """Authenticated probe against the lightweight menus metadata endpoint."""
        if not self._credentials_present():
            logger.warning("Toast credentials not configured.")
            return False
        try:
            self._http_json("GET", "/menus/v2/metadata")
            return True
        except Exception as exc:
            logger.error("Toast health check failed: %s", exc)
            return False

    def fetch_all(self) -> Iterator[ProductRecord]:
        """Stream every sellable item for the startup load / nightly refresh."""
        doc = self._get_menus_document()
        seen = 0
        skipped = 0
        for item, department in self._iter_items(doc):
            rec = self._to_record(item, department)
            if rec is None:
                skipped += 1
                continue
            seen += 1
            yield rec
        logger.info(
            "toast fetch_all: yielded=%d skipped=%d (no name/key)",
            seen,
            skipped,
        )

    def fetch_by_upc(self, upc: str) -> ProductRecord | None:
        """Look up one product by UPC (cache-miss fallback).

        Toast's standard API has no sku-filter endpoint, so this scans the
        menus document (reusing a copy fetched within the last minute — the
        miss path is rare and bursts shouldn't re-download the catalog).
        """
        upc = (upc or "").strip()
        if not upc:
            return None
        doc = self._get_menus_document(max_age=_MENUS_DOC_TTL)
        for item, department in self._iter_items(doc):
            rec = self._to_record(item, department)
            if rec is not None and rec.upc == upc:
                return rec
        return None
