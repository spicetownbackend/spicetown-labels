"""Pytest path bootstrap so `import app` / `import config` work from repo root."""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# Isolate tests from any local .env and force the in-memory testing DB.
os.environ.setdefault("STL_ENV", "testing")


@pytest.fixture(autouse=True)
def _propagate_spicetown_logs():
    """Let pytest's `caplog` capture our logs.

    The app configures the `spicetown` logger with propagate=False (so prod
    logs don't double-emit). caplog attaches its handler to the root logger,
    which only sees records that propagate. Re-enable propagation per-test.
    """
    lg = logging.getLogger("spicetown")
    old = lg.propagate
    lg.propagate = True
    try:
        yield
    finally:
        lg.propagate = old
