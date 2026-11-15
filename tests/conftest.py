"""Process-wide caches and the environment are reset per test, so nothing leaks between tests.

An entrypoint under test loads the developer's `.env`; without the reset its warehouse
settings would switch every later test to that backend.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from secure_query.auth import principal_registry
from secure_query.engine.runtime import get_runtime
from secure_query.planner.llm import get_client

CHINOOK_DB = Path("data/chinook.duckdb")
requires_chinook = pytest.mark.skipif(
    not CHINOOK_DB.exists(), reason="Chinook DuckDB missing; run load_chinook"
)


@pytest.fixture(autouse=True)
def _clear_process_caches():
    environment = dict(os.environ)
    for cached in (get_runtime, get_client, principal_registry):
        cached.cache_clear()
    yield
    os.environ.clear()
    os.environ.update(environment)
    for cached in (get_runtime, get_client, principal_registry):
        cached.cache_clear()
