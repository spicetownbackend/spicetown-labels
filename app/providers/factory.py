"""
app/providers/factory.py — Provider selection.

Reads the active configuration and returns the matching DataProvider instance.
Adding a new source later is a two-line change here plus a new module — calling
code keeps depending only on the DataProvider interface.
"""

from __future__ import annotations

import logging

from .base import DataProvider
from .file_provider import FileDataProvider
from .toast_provider import ToastDataProvider

logger = logging.getLogger("spicetown.provider.factory")


def build_provider(config) -> DataProvider:
    """Instantiate the DataProvider named by `config.DATA_PROVIDER`.

    Parameters
    ----------
    config:
        A Config class/instance exposing the STL_* settings (see config.py).
    """
    kind = (getattr(config, "DATA_PROVIDER", "file") or "file").strip().lower()

    if kind == "file":
        provider: DataProvider = FileDataProvider(path=config.PRODUCTS_FILE)
        logger.info("DataProvider=file path=%s", config.PRODUCTS_FILE)
        return provider

    if kind == "toast":
        provider = ToastDataProvider(
            client_id=config.TOAST_CLIENT_ID,
            client_secret=config.TOAST_CLIENT_SECRET,
            api_base=config.TOAST_API_BASE,
            restaurant_guid=config.TOAST_RESTAURANT_GUID,
            backoff_base=config.BACKOFF_BASE_SECONDS,
            backoff_cap=config.BACKOFF_MAX_SECONDS,
            backoff_max_retries=config.BACKOFF_MAX_RETRIES,
        )
        logger.info("DataProvider=toast api_base=%s", config.TOAST_API_BASE)
        return provider

    raise ValueError(
        f"unknown STL_DATA_PROVIDER={kind!r} (expected 'file' or 'toast')"
    )
