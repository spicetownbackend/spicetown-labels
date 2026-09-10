"""Pytest path bootstrap so `import app` / `import config` work from repo root."""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# Isolate tests from any local .env and force the in-memory testing DB.
# STL_ENV alone doesn't do this - config.py's `load_dotenv()` still fills in
# every other STL_* var from a real .env if one exists (e.g. a production
# label size), since dotenv only skips vars already present. Pin every
# printer/label setting the test suite's own assertions assume, so a real
# local .env can't change what the tests expect.
os.environ.setdefault("STL_ENV", "testing")
os.environ.setdefault("STL_LABEL_SIZE", "62")
os.environ.setdefault("STL_LABEL_LENGTH_PX", "390")
os.environ.setdefault("STL_CUPS_LP_OPTIONS", "")
os.environ.setdefault("STL_PRINT_TRANSPORT", "null")
os.environ.setdefault("STL_PRINT_MODE", "local")


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
