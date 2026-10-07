"""Process-wide caches are cleared per test, so env changes in one test never leak."""

from __future__ import annotations

import pytest

from secure_query.auth import principal_registry
from secure_query.engine.runtime import get_runtime
from secure_query.planner.llm import get_client


@pytest.fixture(autouse=True)
def _clear_process_caches():
    for cached in (get_runtime, get_client, principal_registry):
        cached.cache_clear()
    yield
    for cached in (get_runtime, get_client, principal_registry):
        cached.cache_clear()
