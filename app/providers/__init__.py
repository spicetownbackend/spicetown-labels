"""Data provider adapters (file, Toast). Import via the factory."""

from .base import DataProvider, ProductRecord
from .factory import build_provider
from .file_provider import FileDataProvider
from .toast_provider import ToastDataProvider

__all__ = [
    "DataProvider",
    "ProductRecord",
    "FileDataProvider",
    "ToastDataProvider",
    "build_provider",
]
